# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import api, fields, models


class WhatsappPreview(models.TransientModel):
    _name = "whatsapp.preview"
    _description = "Vista Previa de Plantilla WhatsApp"

    template_id = fields.Many2one("whatsapp.template", string="Plantilla", required=True)
    header_text = fields.Char(related="template_id.header_text", readonly=True)
    rendered_body = fields.Text(string="Mensaje Renderizado", compute="_compute_rendered_body")
    footer_text = fields.Char(related="template_id.footer_text", readonly=True)

    @api.depends("template_id", "template_id.body", "template_id.variable_ids.sample_value")
    def _compute_rendered_body(self):
        for rec in self:
            text = rec.template_id.body or ""
            if rec.template_id.variable_ids:
                for var in rec.template_id.variable_ids:
                    sample = var.sample_value or f"[Variable {var.sequence}]"
                    text = text.replace(f"{{{{{var.sequence}}}}}", sample)
            rec.rendered_body = text
