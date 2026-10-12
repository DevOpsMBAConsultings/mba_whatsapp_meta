# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import api, fields, models, _
from odoo.exceptions import UserError


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

    def action_retry(self):
        """Permite reintentar el envío de un mensaje fallido."""
        for rec in self.filtered(lambda m: m.state == "error" and m.template_id):
            record = False
            if rec.model and rec.res_id:
                record = self.env[rec.model].browse(rec.res_id).exists()
            rec.template_id.send_to_phone(phone=rec.mobile_number, record=record)
            rec.unlink()
