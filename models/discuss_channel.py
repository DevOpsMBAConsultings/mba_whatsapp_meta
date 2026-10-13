# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
from odoo import api, fields, models, tools, _

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    whatsapp_number = fields.Char(string="Número WhatsApp", index=True)
    whatsapp_partner_id = fields.Many2one("res.partner", string="Contacto WhatsApp", index=True)
    whatsapp_account_id = fields.Many2one("whatsapp.account", string="Cuenta WhatsApp")

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
            # Ignorar mensajes del sistema o notificaciones internas
            if clean_body and not clean_body.startswith("⚠️"):
                res = self.whatsapp_account_id.send_text_to_phone(self.whatsapp_number, clean_body)
                if "error" in res:
                    err_msg = res["error"].get("message", "Error desconocido de Meta")
                    # Notificar en el canal si expiró la ventana de 24 horas u ocurrió un error
                    self.with_context(whatsapp_no_dispatch=True).message_post(
                        body=_("⚠️ No se pudo enviar por WhatsApp: %s") % err_msg,
                        message_type="notification",
                    )
        return message
