# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import re
from odoo import api, fields, models


class WhatsappBlocklist(models.Model):
    _name = "whatsapp.blocklist"
    _description = "Lista Negra de WhatsApp (Opt-out)"
    _order = "create_date desc"

    mobile_number = fields.Char(string="Número Bloqueado", required=True, index=True)
    reason = fields.Char(string="Motivo de Bloqueo")
    active = fields.Boolean(default=True)

    _mobile_number_uniq = models.Constraint(
        "unique(mobile_number)",
        "Este número ya está registrado en la lista negra.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "mobile_number" in vals:
                vals["mobile_number"] = re.sub(r"\D", "", str(vals["mobile_number"]))
        return super().create(vals_list)

    @api.model
    def is_number_blocked(self, phone):
        """Verifica si un número está bloqueado en la lista negra."""
        if not phone:
            return False
        clean = re.sub(r"\D", "", str(phone))
        # Buscar coincidencia exacta o coincidencia de los últimos 8 dígitos
        domain = ["|", ("mobile_number", "=", clean), ("mobile_number", "=", clean[-8:])]
        return bool(self.search_count(domain))
