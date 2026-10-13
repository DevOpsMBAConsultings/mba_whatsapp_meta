# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import json
import logging
import re
import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WhatsappAccount(models.Model):
    _name = "whatsapp.account"
    _description = "Cuenta de WhatsApp Business (Meta Cloud API)"
    _inherit = ["mail.thread"]
    _order = "sequence, id desc"

    name = fields.Char(string="Nombre de la Cuenta", required=True, tracking=True)
    sequence = fields.Integer(string="Secuencia", default=10)
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one(
        "res.company", string="Compañía", default=lambda self: self.env.company, required=True
    )
    allowed_company_ids = fields.Many2many(
        "res.company",
        "whatsapp_account_allowed_company_rel",
        "account_id",
        "company_id",
        string="Compañías Permitidas",
        default=lambda self: [self.env.company.id],
    )
    notify_user_ids = fields.Many2many(
        "res.users",
        "whatsapp_account_notify_user_rel",
        "account_id",
        "user_id",
        string="Usuarios Asignados",
    )

    # 1. Enviar Mensajes (Meta Graph API)
    app_id = fields.Char(string="App ID", tracking=True)
    app_secret = fields.Char(string="App Secret")
    phone_number_id = fields.Char(string="Phone Number ID", required=True, tracking=True)
    account_uid = fields.Char(string="WhatsApp Business Account ID", required=True, tracking=True)
    token = fields.Char(string="Access Token Permanente", required=True)
    phone_uid = fields.Char(string="Número de Teléfono", tracking=True)
    api_version = fields.Char(string="Versión Graph API", default="v21.0", required=True)

    # 2. Recibir Mensajes (Webhooks)
    callback_url = fields.Char(string="Callback URL", compute="_compute_callback_url")
    webhook_verify_token = fields.Char(
        string="Webhook Verify Token", default=lambda self: self._default_webhook_token()
    )

    # Contadores
    template_count = fields.Integer(string="Plantillas", compute="_compute_counts")
    message_count = fields.Integer(string="Mensajes", compute="_compute_counts")

    def _default_webhook_token(self):
        return self.env["ir.attachment"]._compute_checksum(str(fields.Datetime.now()).encode("utf-8"))[:16]

    def _compute_callback_url(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for rec in self:
            rec.callback_url = f"{base_url.rstrip('/')}/whatsapp/webhook/{rec.id}" if base_url else False

    def _compute_counts(self):
        for rec in self:
            rec.template_count = self.env["whatsapp.template"].search_count([("wa_account_id", "=", rec.id)])
            rec.message_count = self.env["whatsapp.message"].search_count([("wa_account_id", "=", rec.id)])

    @api.model
    def clean_phone_number(self, phone):
        """Sanitiza y normaliza un número a formato internacional E.164.

        Si tiene 8 dígitos (formato estándar de Panamá), antepone '507'.
        """
        if not phone:
            return False
        digits = re.sub(r"\D", "", str(phone))
        if len(digits) == 8:
            return f"507{digits}"
        if digits.startswith("00"):
            digits = digits[2:]
        return digits

    @api.model
    def find_partner_by_phone(self, phone):
        """Busca un res.partner por número de teléfono de forma compatible entre versiones de Odoo."""
        if not phone:
            return self.env["res.partner"].browse()
        clean = self.clean_phone_number(phone)
        short_num = clean[-8:] if len(clean) >= 8 else clean
        Partner = self.env["res.partner"].sudo()
        if "phone_mobile_search" in Partner._fields:
            domain = [("phone_mobile_search", "ilike", short_num)]
        elif "mobile" in Partner._fields:
            domain = ["|", ("mobile", "ilike", short_num), ("phone", "ilike", short_num)]
        else:
            domain = [("phone", "ilike", short_num)]
        return Partner.search(domain, limit=1)

    def action_test_connection(self):
        """Prueba credenciales contra Meta Graph API."""
        self.ensure_one()
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, timeout=12)
            data = res.json()
            if res.status_code == 200:
                display_phone = data.get("display_phone_number", self.phone_uid or "")
                name_status = data.get("name_status", "AVAILABLE")
                quality = data.get("quality_rating", "GREEN")
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Conexión Exitosa con Meta"),
                        "message": _(
                            "Credenciales válidas. Número: %s | Calidad: %s | Estado: %s"
                        ) % (display_phone, quality, name_status),
                        "type": "success",
                        "sticky": False,
                    },
                }
            err_msg = data.get("error", {}).get("message", res.text)
            raise UserError(_("Error de Meta API (%s): %s") % (res.status_code, err_msg))
        except requests.exceptions.RequestException as e:
            raise UserError(_("Error de conexión con Meta: %s") % str(e))

    def action_sync_templates(self):
        """Sincroniza plantillas desde Meta WhatsApp Business Account."""
        self.ensure_one()
        url = f"https://graph.facebook.com/{self.api_version}/{self.account_uid}/message_templates"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, timeout=15)
            data = res.json()
            if res.status_code != 200:
                err_msg = data.get("error", {}).get("message", res.text)
                raise UserError(_("Error al sincronizar con Meta (%s): %s") % (res.status_code, err_msg))
            templates = data.get("data", [])
            Template = self.env["whatsapp.template"].sudo()
            synced = 0
            for item in templates:
                meta_name = item.get("name")
                meta_status = item.get("status", "DRAFT").lower()
                meta_lang = item.get("language", "es")
                meta_cat = item.get("category", "UTILITY")
                body_text = ""
                for comp in item.get("components", []):
                    if comp.get("type") == "BODY":
                        body_text = comp.get("text", "")

                existing = Template.search([
                    ("wa_account_id", "=", self.id),
                    ("template_name", "=", meta_name),
                ], limit=1)

                vals = {
                    "wa_account_id": self.id,
                    "name": item.get("name", "").replace("_", " ").title(),
                    "template_name": meta_name,
                    "lang_code": meta_lang,
                    "category": meta_cat,
                    "status": "approved" if meta_status == "approved" else "pending",
                    "body": body_text,
                }
                if existing:
                    existing.write(vals)
                else:
                    Template.create(vals)
                synced += 1

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Plantillas Sincronizadas"),
                    "message": _("Se han sincronizado %s plantillas desde Meta.") % synced,
                    "type": "success",
                    "sticky": False,
                },
            }
        except requests.exceptions.RequestException as e:
            raise UserError(_("Error de conexión al sincronizar plantillas: %s") % str(e))

    def send_meta_raw(self, payload):
        """Método de bajo nivel para despachar un JSON hacia Meta Graph API."""
        self.ensure_one()
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        res = requests.post(url, json=payload, headers=headers, timeout=15)
        try:
            data = res.json()
        except Exception:
            data = {"raw": res.text}
        return res.status_code, data

    def action_view_templates(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Plantillas - %s") % self.name,
            "res_model": "whatsapp.template",
            "view_mode": "list,form",
            "domain": [("wa_account_id", "=", self.id)],
            "context": {"default_wa_account_id": self.id},
        }

    def action_view_messages(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Mensajes - %s") % self.name,
            "res_model": "whatsapp.message",
            "view_mode": "list,form",
            "domain": [("wa_account_id", "=", self.id)],
            "context": {"default_wa_account_id": self.id},
        }

    def send_text_to_phone(self, phone, text):
        """Envía un mensaje de texto libre a través de Meta Graph API (ventana de 24h)."""
        self.ensure_one()
        clean_phone = self.clean_phone_number(phone)
        if not clean_phone:
            return {"error": {"message": "Número de teléfono no válido"}}

        # Validar si está en lista negra
        if self.env["whatsapp.blocklist"].sudo().search([("mobile_number", "=", clean_phone)], limit=1):
            _logger.info("WhatsApp abortado: el número %s está en la lista negra.", clean_phone)
            return {"error": {"message": "El número está en la lista negra (opt-out)"}}

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_phone,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }

        msg_vals = {
            "wa_account_id": self.id,
            "mobile_number": clean_phone,
            "body": text,
            "msg_type": "outbound",
            "state": "outgoing",
        }

        try:
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            data = res.json() if res.content else {}
            status_code = res.status_code
        except Exception as e:
            _logger.error("Error al conectar con Meta Graph API: %s", e)
            msg_vals.update({"state": "error", "error_msg": str(e)})
            self.env["whatsapp.message"].sudo().create(msg_vals)
            return {"error": {"message": str(e)}}

        if status_code in (200, 201):
            messages = data.get("messages", [])
            wamid = messages[0].get("id") if messages else False
            msg_vals.update({"state": "sent", "msg_uid": wamid})
            msg = self.env["whatsapp.message"].sudo().create(msg_vals)
            _logger.info("WhatsApp texto libre enviado con éxito a %s (ID: %s)", clean_phone, wamid)
            return {"success": True, "wamid": wamid, "message_id": msg.id}
        else:
            err_data = data.get("error", {})
            err_msg = err_data.get("message", str(data))
            msg_vals.update({"state": "error", "error_msg": f"HTTP {status_code}: {err_msg}"})
            self.env["whatsapp.message"].sudo().create(msg_vals)
            _logger.warning("Fallo al enviar texto libre a %s: %s", clean_phone, err_msg)
            return {"error": err_data or {"message": err_msg}}

