# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class WhatsappComposer(models.TransientModel):
    _name = "whatsapp.composer"
    _description = "Asistente de Envío de WhatsApp (Meta Cloud API)"

    res_model = fields.Char(string="Modelo de Origen", default="res.partner")
    res_id = fields.Integer(string="ID del Registro")
    partner_id = fields.Many2one("res.partner", string="Contacto")
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
        domain="[('wa_account_id', '=', wa_account_id), ('status', '=', 'approved'), '|', ('model_id.model', '=', res_model), ('model_id', '=', False)]",
    )
    body_preview = fields.Text(string="Vista previa del mensaje", compute="_compute_body_preview")

    @api.depends("template_id", "partner_id", "res_model", "res_id")
    def _compute_body_preview(self):
        for rec in self:
            if rec.template_id:
                record = (
                    self.env[rec.res_model].browse(rec.res_id)
                    if rec.res_model and rec.res_id and rec.res_model in self.env
                    else rec.partner_id
                )
                text = rec.template_id.body or ""
                if record and rec.template_id.variable_ids:
                    for var in rec.template_id.variable_ids:
                        val = var.get_value_for_record(record)
                        text = text.replace(f"{{{{{var.sequence}}}}}", val)
                elif rec.partner_id:
                    text = text.replace("{{1}}", rec.partner_id.name or "")
                rec.body_preview = text
            else:
                rec.body_preview = False

    def _post_send_actions(self, msg, record):
        """Hook extensible por otros módulos al completarse el envío."""
        pass

    def action_send_meta(self):
        """Envía la plantilla seleccionada a través de Meta Cloud API."""
        self.ensure_one()
        if not self.template_id:
            raise UserError(_("Debes seleccionar una plantilla aprobada de Meta para enviar por la API oficial."))

        record = (
            self.env[self.res_model].browse(self.res_id)
            if self.res_model and self.res_id and self.res_model in self.env
            else self.partner_id
        )

        msg = self.template_id.send_to_phone(self.phone, record=record)

        if msg.state != "error":
            self._post_send_actions(msg, record)

            if self.partner_id:
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
