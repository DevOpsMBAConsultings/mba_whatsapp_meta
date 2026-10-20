# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhatsappMessage(models.Model):
    _name = "whatsapp.message"
    _description = "Historial de Mensajes de WhatsApp"
    _order = "create_date desc, id desc"

    wa_account_id = fields.Many2one("whatsapp.account", string="Cuenta de WhatsApp", required=True, index=True)
    template_id = fields.Many2one("whatsapp.template", string="Plantilla", ondelete="set null")
    company_id = fields.Many2one(
        "res.company", string="Compañía", related="wa_account_id.company_id", store=True, index=True
    )

    mobile_number = fields.Char(string="Destinatario", required=True, index=True)
    partner_id = fields.Many2one("res.partner", string="Contacto", index=True)
    body = fields.Text(string="Mensaje", required=True)
    msg_type = fields.Selection(
        [("outbound", "Saliente"), ("inbound", "Entrante")],
        string="Tipo",
        default="outbound",
        required=True,
    )
    state = fields.Selection(
        [
            ("draft", "Borrador"),
            ("sent", "Enviado"),
            ("delivered", "Entregado"),
            ("read", "Leído"),
            ("error", "Error"),
        ],
        string="Estado",
        default="draft",
        required=True,
        index=True,
    )
    msg_uid = fields.Char(string="ID Meta (wamid)", index=True)
    error_msg = fields.Text(string="Detalle del Error")

    model = fields.Char(string="Modelo Origen", index=True)
    res_id = fields.Integer(string="ID del Registro", index=True)
    currency_id = fields.Many2one("res.currency", string="Moneda", related="company_id.currency_id")
    cost_usd = fields.Float(string="Costo (USD)", digits=(6, 4), default=0.0)

    # Ciclo de vida y auditoría de archivos multimedia (anti-falla silenciosa)
    media_id = fields.Char(string="ID Multimedia Meta", index=True)
    media_type = fields.Selection(
        [
            ("none", "Ninguno"),
            ("image", "Imagen"),
            ("document", "Documento"),
            ("audio", "Audio"),
            ("video", "Video"),
        ],
        string="Tipo de Archivo",
        default="none",
    )
    media_status = fields.Selection(
        [
            ("none", "Sin Archivo"),
            ("pending", "Descarga Pendiente"),
            ("downloaded", "Descargado"),
            ("failed", "Descarga Fallida"),
        ],
        string="Estado de Descarga",
        default="none",
        index=True,
    )
    media_error = fields.Text(string="Error de Descarga")
    attachment_id = fields.Many2one("ir.attachment", string="Archivo Adjunto", ondelete="set null")
    attachment_data = fields.Binary(related="attachment_id.datas", string="Vista Previa", readonly=True)
    attachment_name = fields.Char(related="attachment_id.name", string="Nombre del Archivo", readonly=True)

    def action_open_chat(self):
        """Abre el canal de Conversaciones asociado a este número."""
        self.ensure_one()
        clean_num = self.wa_account_id.clean_phone_number(self.mobile_number)
        channel = self.env["discuss.channel"].sudo().search([("whatsapp_number", "=", clean_num)], limit=1)
        if not channel:
            raise UserError(_("No existe un canal de Conversaciones para este contacto."))
        return {
            "type": "ir.actions.act_window",
            "name": channel.name or "Conversaciones",
            "res_model": "discuss.channel",
            "res_id": channel.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_retry(self):
        """Permite reintentar el envío de un mensaje fallido."""
        for rec in self.filtered(lambda m: m.state == "error" and m.template_id):
            record = False
            if rec.model and rec.res_id:
                record = self.env[rec.model].browse(rec.res_id).exists()
            rec.template_id.send_to_phone(phone=rec.mobile_number, record=record)
            rec.unlink()

    def action_retry_media_download(self):
        """Reintenta descargar un archivo multimedia fallido desde Meta."""
        self.ensure_one()
        if not self.media_id or not self.wa_account_id:
            raise UserError(_("Este mensaje no tiene un ID de multimedia válido para descargar."))
        raw_bytes, mime_type = self.wa_account_id.download_media(self.media_id)
        if not raw_bytes:
            raise UserError(_("No se pudo descargar el archivo desde Meta. Verifique la conexión o vigencia del ID."))

        ext = "jpg" if "jpeg" in (mime_type or "") or "jpg" in (mime_type or "") else (
            "png" if "png" in (mime_type or "") else (
                "pdf" if "pdf" in (mime_type or "") else "bin"
            )
        )
        fname = f"whatsapp_{self.media_type}_{self.msg_uid[-6:] if self.msg_uid else self.id}.{ext}"

        Channel = self.env["discuss.channel"].sudo()
        clean_num = self.wa_account_id.clean_phone_number(self.mobile_number)
        channel = Channel.search([("whatsapp_number", "=", clean_num)], limit=1)
        partner = self.wa_account_id.find_partner_by_phone(clean_num)
        admin_user = self.env.ref("base.user_admin").sudo()

        if not channel:
            if not partner:
                partner = self.env["res.partner"].sudo().create({
                    "name": f"WhatsApp +{clean_num}",
                    "phone": f"+{clean_num}",
                })
            channel = Channel.with_user(admin_user).sudo()._get_or_create_chat(partners_to=[partner.id], pin=True).sudo()
            channel.write({
                "whatsapp_number": clean_num,
                "whatsapp_partner_id": partner.id,
                "whatsapp_account_id": self.wa_account_id.id,
            })

        post_vals = {
            "body": self.body or "📷 Archivo recuperado de WhatsApp",
            "author_id": partner.id if partner else admin_user.partner_id.id,
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_comment",
            "attachments": [(fname, raw_bytes)],
        }
        posted_msg = channel.with_user(admin_user).with_context(whatsapp_no_dispatch=True).message_post(**post_vals)
        att = posted_msg.attachment_ids[:1]
        self.write({
            "partner_id": partner.id if partner else False,
            "media_status": "downloaded",
            "media_error": False,
            "attachment_id": att.id if att else False,
        })

        if att:
            try:
                self._post_media_download_hook(att, fname, raw_bytes, clean_num)
            except Exception as e_hook:
                _logger.warning("Error en hook _post_media_download_hook: %s", e_hook)

        return True

    def _post_media_download_hook(self, attachment, filename, raw_bytes, clean_num):
        """Hook extensible para vincular comprobantes o archivos descargados a otros modelos."""
        pass

    def _post_webhook_attachment_hook(self, posted_discuss_msg, text_body):
        """Hook extensible para procesar adjuntos recibidos por webhook (IA, expediente, etc.)."""
        pass

    @api.model
    def cron_retry_failed_media_downloads(self):
        """Cron para reintentar descargas multimedia pendientes o fallidas de los últimos 7 días."""
        seven_days_ago = fields.Datetime.now() - timedelta(days=7)
        failed_msgs = self.search([
            ("media_status", "in", ("pending", "failed")),
            ("media_id", "!=", False),
            ("create_date", ">=", seven_days_ago),
        ], limit=50)
        for msg in failed_msgs:
            try:
                msg.action_retry_media_download()
            except Exception as e:
                msg.write({
                    "media_status": "failed",
                    "media_error": str(e),
                })
                _logger.warning("Fallo al reintentar descarga de media para mensaje %s: %s", msg.id, e)

