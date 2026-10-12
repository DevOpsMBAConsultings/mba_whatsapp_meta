# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhatsappTemplate(models.Model):
    _name = "whatsapp.template"
    _description = "Plantilla de WhatsApp (Meta)"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(string="Nombre", required=True, tracking=True)
    template_name = fields.Char(
        string="Nombre Técnico en Meta",
        required=True,
        tracking=True,
        help="Nombre exacto de la plantilla en Meta (en minúsculas con guiones bajos).",
    )
    wa_account_id = fields.Many2one(
        "whatsapp.account", string="Cuenta de WhatsApp", required=True, ondelete="cascade", tracking=True
    )
    model_id = fields.Many2one("ir.model", string="Aplica a", help="Modelo de Odoo sobre el que se utiliza.")
    model = fields.Char(related="model_id.model", string="Nombre Técnico del Modelo", store=True)

    lang_code = fields.Char(string="Idioma", default="es", required=True)
    category = fields.Selection(
        [
            ("UTILITY", "Utilidad (Transaccional / Recordatorios)"),
            ("MARKETING", "Marketing (Comercial / Promociones)"),
            ("AUTHENTICATION", "Autenticación (OTP / Seguridad)"),
        ],
        string="Categoría",
        default="UTILITY",
        required=True,
    )
    status = fields.Selection(
        [
            ("draft", "Borrador"),
            ("pending", "En Revisión por Meta"),
            ("approved", "Aprobada"),
            ("rejected", "Rechazada"),
        ],
        string="Estado en Meta",
        default="draft",
        tracking=True,
    )

    header_type = fields.Selection(
        [
            ("none", "Ninguno"),
            ("text", "Texto"),
            ("image", "Imagen"),
            ("document", "Documento PDF"),
        ],
        string="Encabezado",
        default="none",
    )
    header_text = fields.Char(string="Texto del Encabezado")
    body = fields.Text(string="Cuerpo del Mensaje", help="Ej: Hola {{1}}, su cuota {{2}} por ${{3}} vence el {{4}}.")
    footer_text = fields.Char(string="Pie de Página")
    variable_ids = fields.One2many("whatsapp.template.variable", "template_id", string="Mapeo de Variables")

    @api.onchange("body")
    def _onchange_body(self):
        """Detecta automáticamente los placeholders {{1}}, {{2}}... en el cuerpo."""
        if not self.body:
            return
        matches = sorted(list(set(re.findall(r"\{\{(\d+)\}\}", self.body))), key=int)
        existing_indices = {v.sequence for v in self.variable_ids}
        new_vars = []
        for idx_str in matches:
            idx = int(idx_str)
            if idx not in existing_indices:
                new_vars.append((0, 0, {
                    "sequence": idx,
                    "name": f"Variable {{{{{idx}}}}}",
                }))
        if new_vars:
            self.variable_ids = new_vars

    def send_to_phone(self, phone, record=None, custom_params=None):
        """Envía un mensaje usando esta plantilla vía Meta WhatsApp Cloud API.

        :param phone: Número de teléfono destinatario
        :param record: Registro de Odoo origen (opcional)
        :param custom_params: Lista de strings con los valores en orden [param1, param2, ...]
        :return: Registro de whatsapp.message creado
        """
        self.ensure_one()
        account = self.wa_account_id
        if not account or not account.active:
            raise UserError(_("La cuenta de WhatsApp configurada no está activa."))

        cleaned_phone = account.clean_phone_number(phone)
        if not cleaned_phone:
            raise UserError(_("El número de teléfono proporcionado no es válido."))

        # Verificación de lista negra
        is_blocked = self.env["whatsapp.blocklist"].is_number_blocked(cleaned_phone)
        if is_blocked:
            _logger.info("WhatsApp abortado: el número %s está en la lista negra.", cleaned_phone)
            msg = self.env["whatsapp.message"].sudo().create({
                "wa_account_id": account.id,
                "template_id": self.id,
                "mobile_number": cleaned_phone,
                "body": _("[OMITIDO] El número está en la lista negra."),
                "state": "error",
                "error_msg": _("Destinatario en lista negra."),
                "model": record._name if record else False,
                "res_id": record.id if record else False,
            })
            return msg

        # Resolver parámetros de la plantilla
        parameters = []
        if custom_params and isinstance(custom_params, (list, tuple)):
            for val in custom_params:
                parameters.append({"type": "text", "text": str(val or "")})
        elif self.variable_ids and record:
            for var in self.variable_ids.sorted(key=lambda v: v.sequence):
                val = var.get_value_for_record(record)
                parameters.append({"type": "text", "text": str(val or "")})

        # Construir componentes del payload de Meta
        components = []
        if parameters:
            components.append({
                "type": "body",
                "parameters": parameters,
            })

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": cleaned_phone,
            "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.lang_code or "es"},
            },
        }
        if components:
            payload["template"]["components"] = components

        status_code, data = account.send_meta_raw(payload)

        # Determinar resultado y registrar mensaje
        rendered_body = self.body or self.name
        for i, param in enumerate(parameters, start=1):
            rendered_body = rendered_body.replace(f"{{{{{i}}}}}", param["text"])

        msg_vals = {
            "wa_account_id": account.id,
            "template_id": self.id,
            "mobile_number": cleaned_phone,
            "body": rendered_body,
            "model": record._name if record else False,
            "res_id": record.id if record else False,
            "company_id": account.company_id.id,
        }

        if status_code in (200, 201) and "messages" in data:
            wamid = data["messages"][0]["id"]
            msg_vals.update({
                "state": "sent",
                "msg_uid": wamid,
                "error_msg": False,
            })
            msg = self.env["whatsapp.message"].sudo().create(msg_vals)
            if record and hasattr(record, "message_post"):
                record.message_post(
                    body=_("Mensaje de WhatsApp («%s») enviado exitosamente a %s (ID Meta: %s).") % (
                        self.name, cleaned_phone, wamid
                    ),
                    message_type="notification",
                )
            return msg
        else:
            err_msg = data.get("error", {}).get("message", str(data))
            msg_vals.update({
                "state": "error",
                "error_msg": f"HTTP {status_code}: {err_msg}",
            })
            msg = self.env["whatsapp.message"].sudo().create(msg_vals)
            _logger.error("Error al enviar WhatsApp Meta a %s: %s", cleaned_phone, err_msg)
            if record and hasattr(record, "message_post"):
                record.message_post(
                    body=_("Fallo al enviar WhatsApp («%s») a %s: %s") % (self.name, cleaned_phone, err_msg),
                    message_type="notification",
                )
            return msg


class WhatsappTemplateVariable(models.Model):
    _name = "whatsapp.template.variable"
    _description = "Variable dinámica de Plantilla WhatsApp"
    _order = "sequence"

    template_id = fields.Many2one("whatsapp.template", required=True, ondelete="cascade")
    sequence = fields.Integer(string="Posición {{n}}", default=1, required=True)
    name = fields.Char(string="Descripción", required=True)
    field_name = fields.Char(string="Campo en el Modelo", help="Ej: partner_id.name, amount_total, due_date")

    def get_value_for_record(self, record):
        """Extrae el valor del registro siguiendo la ruta de campos punto a punto."""
        self.ensure_one()
        if not self.field_name or not record:
            return ""
        try:
            val = record
            for part in self.field_name.split("."):
                val = getattr(val, part, None)
                if val is None:
                    break
            if isinstance(val, models.BaseModel):
                return val.display_name or ""
            return str(val) if val is not None else ""
        except Exception as e:
            _logger.warning("No se pudo evaluar variable %s en %s: %s", self.field_name, record, e)
            return ""
