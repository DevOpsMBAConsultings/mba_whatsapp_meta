# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from datetime import timedelta
import logging
from odoo import api, fields, models, tools, _

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    whatsapp_number = fields.Char(string="Número WhatsApp", index=True)
    whatsapp_partner_id = fields.Many2one("res.partner", string="Contacto WhatsApp", index=True)
    whatsapp_account_id = fields.Many2one("whatsapp.account", string="Cuenta WhatsApp")
    whatsapp_last_inbound_dt = fields.Datetime(string="Último Mensaje Entrante WhatsApp")
    whatsapp_window_valid_until = fields.Datetime(
        string="Ventana 24h Válida Hasta", compute="_compute_whatsapp_window"
    )
    whatsapp_window_active = fields.Boolean(
        string="Ventana 24h Activa", compute="_compute_whatsapp_window"
    )

    @api.depends("whatsapp_last_inbound_dt")
    def _compute_whatsapp_window(self):
        now = fields.Datetime.now()
        for rec in self:
            if rec.whatsapp_last_inbound_dt:
                valid_until = rec.whatsapp_last_inbound_dt + timedelta(hours=24)
                rec.whatsapp_window_valid_until = valid_until
                rec.whatsapp_window_active = valid_until > now
            else:
                rec.whatsapp_window_valid_until = False
                rec.whatsapp_window_active = False

    def _infer_wa_media_type(self, mimetype):
        mt = (mimetype or "").lower()
        if mt.startswith("image/"):
            return "image"
        if mt.startswith("audio/"):
            return "audio"
        if mt.startswith("video/"):
            return "video"
        return "document"

    def message_post(self, *, message_type="notification", **kwargs):
        """Intercepte el envío de mensajes desde la app Conversaciones hacia WhatsApp."""
        message = super().message_post(message_type=message_type, **kwargs)

        # Si el mensaje fue enviado por un usuario de Odoo (no por webhook) en un canal vinculado a WhatsApp
        if (
            not self.env.context.get("whatsapp_no_dispatch")
            and self.whatsapp_number
            and self.whatsapp_account_id
            and message_type == "comment"
            and message.author_id != self.whatsapp_partner_id
        ):
            clean_body = tools.html2plaintext(message.body or "").strip()
            has_content = (clean_body and not clean_body.startswith("⚠️")) or bool(message.attachment_ids)
            if not has_content:
                return message

            # Si tenemos registro de último entrante y la ventana de 24h expiró, avisar localmente
            if self.whatsapp_last_inbound_dt and not self.whatsapp_window_active:
                self.with_context(whatsapp_no_dispatch=True).message_post(
                    body=_("⚠️ La ventana de 24 horas de Meta expiró. Envía una plantilla aprobada."),
                    message_type="notification",
                )
                return message

            errors = []
            if clean_body and not clean_body.startswith("⚠️"):
                res = self.whatsapp_account_id.send_text_to_phone(self.whatsapp_number, clean_body)
                if "error" in res:
                    errors.append(res["error"])

            for att in message.attachment_ids.sudo():
                wa_type = self._infer_wa_media_type(att.mimetype)
                media_id = self.whatsapp_account_id.upload_media(att.raw, att.name, att.mimetype)
                if not media_id:
                    errors.append({"message": _("No se pudo subir el adjunto '%s' a Meta.") % att.name})
                    continue
                res = self.whatsapp_account_id.send_media_to_phone(
                    self.whatsapp_number,
                    media_id,
                    media_type=wa_type,
                    filename=att.name,
                )
                if "error" in res:
                    errors.append(res["error"])

            if errors:
                for err in errors:
                    if str(err.get("code") or "") == "131047":
                        self.sudo().write({"whatsapp_last_inbound_dt": fields.Datetime.now() - timedelta(hours=25)})
                        break
                err_msg = errors[0].get("message", "Error desconocido de Meta")
                self.with_context(whatsapp_no_dispatch=True).message_post(
                    body=_("⚠️ No se pudo enviar por WhatsApp: %s") % err_msg,
                    message_type="notification",
                )
        return message
