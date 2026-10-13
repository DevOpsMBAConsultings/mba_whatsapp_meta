# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import json
import logging
import re
import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhatsappTemplate(models.Model):
    _name = "whatsapp.template"
    _description = "Plantilla de WhatsApp (Meta Cloud API)"
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

    phone_field = fields.Char(string="Destinatario por Defecto", default="phone", help="Campo de teléfono en el modelo.")
    user_scope = fields.Selection(
        [
            ("all", "Accessible to all Users"),
            ("specific", "Specific Users"),
        ],
        string="Usuarios",
        default="all",
    )
    variable_type = fields.Selection(
        [
            ("number", "number"),
            ("text", "text"),
        ],
        string="Tipo de Variable",
        default="number",
    )

    lang_code = fields.Char(string="Idioma", default="es_PA", required=True)
    category = fields.Selection(
        [
            ("UTILITY", "Utility"),
            ("MARKETING", "Marketing"),
            ("AUTHENTICATION", "Authentication"),
        ],
        string="Categoría",
        default="UTILITY",
        required=True,
    )
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="Estado",
        default="draft",
        tracking=True,
    )

    header_type = fields.Selection(
        [
            ("none", "None"),
            ("text", "Text"),
            ("image", "Image"),
            ("document", "Document"),
            ("video", "Video"),
        ],
        string="Tipo de Encabezado",
        default="none",
    )
    header_text = fields.Char(string="Texto del Encabezado")
    body = fields.Text(string="Cuerpo del Mensaje", help="Ej: Hola {{1}}, su cuota {{2}} por ${{3}} vence el {{4}}.")
    footer_text = fields.Char(string="Pie de Página")
    variable_ids = fields.One2many("whatsapp.template.variable", "template_id", string="Mapeo de Variables")
    button_ids = fields.One2many("whatsapp.template.button", "template_id", string="Botones")

    message_count = fields.Integer(string="Mensajes", compute="_compute_message_count")

    def _compute_message_count(self):
        for rec in self:
            rec.message_count = self.env["whatsapp.message"].search_count([("template_id", "=", rec.id)])

    def action_view_messages(self):
        self.ensure_one()
        return {
            "name": f"Mensajes de {self.name}",
            "type": "ir.actions.act_window",
            "res_model": "whatsapp.message",
            "view_mode": "list,form",
            "domain": [("template_id", "=", self.id)],
        }

    def action_preview(self):
        """Abre la previsualización del mensaje simulando WhatsApp."""
        self.ensure_one()
        return {
            "name": f"Preview - {self.name}",
            "type": "ir.actions.act_window",
            "res_model": "whatsapp.preview",
            "view_mode": "form",
            "target": "new",
            "context": {"default_template_id": self.id},
        }

    def action_submit_for_approval(self):
        """Envía la plantilla a Meta Graph API para aprobación oficial."""
        self.ensure_one()
        account = self.wa_account_id
        if not account or not account.token:
            raise UserError(_("Debes configurar un Access Token válido en la cuenta de WhatsApp."))

        components = []
        if self.header_type == "text" and self.header_text:
            components.append({
                "type": "HEADER",
                "format": "TEXT",
                "text": self.header_text,
            })

        body_component = {
            "type": "BODY",
            "text": self.body or "",
        }
        if self.variable_ids:
            samples = [v.sample_value or f"Ejemplo {v.sequence}" for v in self.variable_ids]
            body_component["example"] = {
                "body_text": [samples]
            }
        components.append(body_component)

        if self.footer_text:
            components.append({
                "type": "FOOTER",
                "text": self.footer_text,
            })

        if self.button_ids:
            buttons = []
            for b in self.button_ids:
                if b.button_type == "quick_reply":
                    buttons.append({"type": "QUICK_REPLY", "text": b.name})
                elif b.button_type == "url":
                    buttons.append({"type": "URL", "text": b.name, "url": b.url_or_phone or "https://mbaconsultings.com"})
                elif b.button_type == "phone_number":
                    buttons.append({"type": "PHONE_NUMBER", "text": b.name, "phone_number": b.url_or_phone or "+50700000000"})
            if buttons:
                components.append({
                    "type": "BUTTONS",
                    "buttons": buttons,
                })

        payload = {
            "name": self.template_name,
            "category": self.category,
            "allow_category_change": True,
            "language": self.lang_code or "es_PA",
            "components": components,
        }

        url = f"https://graph.facebook.com/{account.api_version}/{account.account_uid}/message_templates"
        headers = {
            "Authorization": f"Bearer {account.token}",
            "Content-Type": "application/json",
        }

        try:
            res = requests.post(url, headers=headers, json=payload, timeout=20)
            data = res.json() if res.content else {}
        except Exception as e:
            raise UserError(_("Error de conexión al enviar plantilla a Meta: %s", e))

        if res.status_code in (200, 201):
            new_status = data.get("status", "pending").lower()
            self.write({"status": new_status})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Plantilla Enviada a Meta"),
                    "message": _("La plantilla «%s» fue enviada exitosamente a Meta. Estado: %s", self.name, new_status.upper()),
                    "type": "success",
                    "sticky": False,
                },
            }
        else:
            err = data.get("error", {}).get("message", str(data))
            raise UserError(_("Meta rechazó la creación de la plantilla: %s", err))

    @api.onchange("model_id")
    def _onchange_model_id(self):
        """Propaga el modelo a todas las variables asociadas."""
        if self.model_id and self.variable_ids:
            for var in self.variable_ids:
                var.model = self.model_id.model

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
                    "name": f"{{{{{idx}}}}}",
                    "sample_value": f"Sample {idx}",
                    "line_type": "field",
                    "model": self.model or "",
                }))
        if new_vars:
            self.variable_ids = new_vars

    def send_to_phone(self, phone, record=None, custom_params=None):
        """Envía un mensaje usando esta plantilla vía Meta WhatsApp Cloud API."""
        self.ensure_one()
        account = self.wa_account_id
        if not account or not account.active:
            raise UserError(_("La cuenta de WhatsApp configurada no está activa."))

        cleaned_phone = account.clean_phone_number(phone)
        if not cleaned_phone:
            raise UserError(_("El número de teléfono proporcionado no es válido."))

        if self.env["whatsapp.blocklist"].is_number_blocked(cleaned_phone):
            _logger.info("WhatsApp abortado: el número %s está en la lista negra.", cleaned_phone)
            return self.env["whatsapp.message"].sudo().create({
                "wa_account_id": account.id,
                "template_id": self.id,
                "mobile_number": cleaned_phone,
                "body": self.body or "",
                "state": "error",
                "error_msg": "Número en lista negra (opt-out previo).",
            })

        # Construir parámetros
        parameters = []
        rendered_body = self.body or ""
        if custom_params:
            for idx, val in enumerate(custom_params, start=1):
                val_str = str(val or "")
                parameters.append({"type": "text", "text": val_str})
                rendered_body = rendered_body.replace(f"{{{{{idx}}}}}", val_str)
        elif record and self.variable_ids:
            for var in self.variable_ids:
                val = var.get_value_for_record(record)
                parameters.append({"type": "text", "text": val})
                rendered_body = rendered_body.replace(f"{{{{{var.sequence}}}}}", val)

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
                "language": {"code": self.lang_code or "es_PA"},
            },
        }
        if components:
            payload["template"]["components"] = components

        url = f"https://graph.facebook.com/{account.api_version}/{account.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {account.token}",
            "Content-Type": "application/json",
        }

        msg_vals = {
            "wa_account_id": account.id,
            "template_id": self.id,
            "mobile_number": cleaned_phone,
            "body": rendered_body,
            "state": "outgoing",
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            status_code = response.status_code
            data = response.json() if response.content else {}
        except Exception as e:
            _logger.error("Error de conexión con Meta Graph API: %s", e)
            msg_vals.update({
                "state": "error",
                "error_msg": str(e),
            })
            return self.env["whatsapp.message"].sudo().create(msg_vals)

        if status_code in (200, 201):
            messages = data.get("messages", [])
            wamid = messages[0].get("id") if messages else False
            msg_vals.update({
                "state": "sent",
                "msg_uid": wamid,
            })
            msg = self.env["whatsapp.message"].sudo().create(msg_vals)
            _logger.info("WhatsApp Meta enviado exitosamente a %s (ID: %s)", cleaned_phone, wamid)
            if record and hasattr(record, "message_post"):
                record.message_post(
                    body=_("Mensaje de WhatsApp enviado («%s» a %s). ID: %s") % (self.name, cleaned_phone, wamid),
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


class WhatsappTemplateButton(models.Model):
    _name = "whatsapp.template.button"
    _description = "Botón de Plantilla WhatsApp"
    _order = "sequence"

    template_id = fields.Many2one("whatsapp.template", required=True, ondelete="cascade")
    sequence = fields.Integer(string="Secuencia", default=10)
    button_type = fields.Selection(
        [
            ("quick_reply", "Quick Reply"),
            ("url", "Visit Website"),
            ("phone_number", "Call Phone"),
        ],
        string="Tipo",
        default="quick_reply",
        required=True,
    )
    name = fields.Char(string="Texto del Botón", required=True)
    url_or_phone = fields.Char(string="URL / Teléfono")


class WhatsappTemplateVariable(models.Model):
    _name = "whatsapp.template.variable"
    _description = "Variable dinámica de Plantilla WhatsApp"
    _order = "sequence"

    template_id = fields.Many2one("whatsapp.template", ondelete="cascade")
    sequence = fields.Integer(string="Posición", default=1)
    name = fields.Char(string="Name", default="{{1}}", required=True)
    model = fields.Char(string="Model", compute="_compute_model", store=True, readonly=False)
    sample_value = fields.Char(string="Sample Value", default="Sample Value")
    line_type = fields.Selection(
        [
            ("field", "Field of Model"),
            ("user_name", "User Name"),
            ("user_phone", "User Phone"),
            ("free_text", "Free Text"),
            ("portal_link", "Portal Link"),
        ],
        string="Type",
        default="field",
        required=True,
    )
    field_name = fields.Char(string="Field", help="Ej: partner_id.name, amount_total, due_date")

    @api.depends("template_id.model")
    def _compute_model(self):
        for rec in self:
            rec.model = rec.template_id.model or self.env.context.get("default_model") or ""

    @api.onchange("name")
    def _onchange_name(self):
        """Si el usuario escribe '1', lo formatea a '{{1}}' y sincroniza la secuencia."""
        if self.name:
            clean = self.name.strip()
            if clean.isdigit():
                self.name = f"{{{{{clean}}}}}"
            match = re.search(r"\d+", self.name)
            if match:
                self.sequence = int(match.group(0))

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
