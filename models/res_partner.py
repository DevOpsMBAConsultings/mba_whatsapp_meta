# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import models


class ResPartner(models.Model):
    _inherit = "res.partner"

    def action_open_whatsapp_composer(self):
        """Abre el asistente de WhatsApp para este contacto."""
        self.ensure_one()
        return {
            "name": f"Enviar WhatsApp a {self.name}",
            "type": "ir.actions.act_window",
            "res_model": "whatsapp.composer",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_partner_id": self.id,
                "default_phone": self.phone or "",
            },
        }
