# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class WhatsappComposer(models.TransientModel):
    _name = "whatsapp.composer"
    _description = "Asistente de Envío de WhatsApp (Meta Cloud API)"

    partner_id = fields.Many2one("res.partner", string="Contacto", required=True)
    phone = fields.Char(string="Teléfono", required=True)
    wa_account_id = fields.Many2one(
        "whatsapp.account",
        string="Cuenta de Envío",
        required=True,
        default=lambda self: self.env["whatsapp.account"].search([("active", "=", True)], limit=1),
    )
    template_id = fields.Many2one(
        "whatsapp.template",
        string="Plantilla de WhatsApp",
        domain="[('wa_account_id', '=', wa_account_id), ('status', '=', 'approved')]",
    )
    body_preview = fields.Text(string="Vista previa del mensaje", compute="_compute_body_preview")

    @api.depends("template_id", "partner_id")
    def _compute_body_preview(self):
        for rec in self:
            if rec.template_id:
                text = rec.template_id.body or ""
                text = text.replace("{{1}}", rec.partner_id.name or "")
                rec.body_preview = text
            else:
                rec.body_preview = False

    def action_send_meta(self):
        """Envía la plantilla seleccionada a través de Meta Cloud API."""
        self.ensure_one()
        if not self.template_id:
            raise UserError(_("Debes seleccionar una plantilla aprobada de Meta para enviar por la API oficial."))

        custom_params = [self.partner_id.name]
        msg = self.template_id.send_to_phone(self.phone, custom_params=custom_params)

        if msg.state != "error":
            self.partner_id.message_post(
                body=_(
                    "Mensaje de WhatsApp enviado vía Meta Cloud API (Plantilla: %s).<br/>ID Meta: %s",
                    self.template_id.name,
                    msg.msg_uid or "",
                )
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("WhatsApp Enviado"),
                    "message": _("Mensaje enviado con éxito vía Meta API a %s", self.phone),
                    "type": "success",
                    "sticky": False,
                },
            }
        else:
            raise UserError(_("Error al enviar por Meta: %s", msg.error_msg))
