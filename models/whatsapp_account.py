# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import json
import logging
import re
import requests
import time

try:
    import phonenumbers
except ImportError:
    phonenumbers = None

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
    app_secret = fields.Char(string="App Secret", groups="mba_whatsapp_meta.group_whatsapp_admin")
    phone_number_id = fields.Char(string="Phone Number ID", required=True, tracking=True)
    account_uid = fields.Char(string="WhatsApp Business Account ID", required=True, tracking=True)
    token = fields.Char(string="Access Token Permanente", required=True, groups="mba_whatsapp_meta.group_whatsapp_admin")
    phone_uid = fields.Char(string="Número de Teléfono", tracking=True)
    api_version = fields.Char(string="Versión Graph API", default="v21.0", required=True)

    # 2. Recibir Mensajes (Webhooks)
    callback_url = fields.Char(string="Callback URL", compute="_compute_callback_url")
    webhook_verify_token = fields.Char(
        string="Webhook Verify Token",
        default=lambda self: self._default_webhook_token(),
        groups="mba_whatsapp_meta.group_whatsapp_admin",
    )

    # Contadores
    template_count = fields.Integer(string="Plantillas", compute="_compute_counts")
    message_count = fields.Integer(string="Mensajes", compute="_compute_counts")

    # 3. Tarifas y Control de Costos (USD)
    currency_id = fields.Many2one("res.currency", string="Moneda", default=lambda self: self.env.ref("base.USD"))
    rate_utility_usd = fields.Float(
        string="Tarifa Utilidad (USD)", default=0.0116, digits=(6, 4),
        help="Costo por conversación de utilidad según tarifario oficial de Meta (Panamá: ~$0.0116 USD)",
    )
    rate_marketing_usd = fields.Float(string="Tarifa Marketing (USD)", default=0.0385, digits=(6, 4))
    rate_service_usd = fields.Float(
        string="Tarifa Servicio (USD)", default=0.0, digits=(6, 4),
        help="Gratis dentro de la ventana de 24 horas iniciada por el cliente",
    )

    total_utility_month = fields.Integer(string="Mensajes Utilidad (Mes)", compute="_compute_monthly_usage")
    total_free_month = fields.Integer(string="Mensajes Gratuitos (Mes)", compute="_compute_monthly_usage")
    total_received_month = fields.Integer(string="Mensajes Recibidos (Mes)", compute="_compute_monthly_usage")
    total_spent_month_usd = fields.Monetary(
        string="Gasto Estimado (Mes)", compute="_compute_monthly_usage", currency_field="currency_id"
    )

    meta_official_sent = fields.Integer(string="Oficial Meta: Enviados (7 días)", readonly=True)
    meta_official_delivered = fields.Integer(string="Oficial Meta: Entregados (7 días)", readonly=True)
    meta_last_sync_date = fields.Datetime(string="Última Sincronización Meta", readonly=True)

    def _compute_monthly_usage(self):
        first_day_month = fields.Datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        Msg = self.env["whatsapp.message"]
        groups = Msg._read_group(
            [("wa_account_id", "in", self.ids), ("create_date", ">=", first_day_month)],
            groupby=["wa_account_id", "msg_type", "template_id"],
            aggregates=["__count", "cost_usd:sum"],
        )
        stats = {rec.id: {"utility": 0, "free": 0, "received": 0, "spent": 0.0} for rec in self}
        for account, msg_type, template, count, cost_sum in groups:
            bucket = stats.get(account.id)
            if not bucket:
                continue
            bucket["spent"] += cost_sum or 0.0
            if msg_type == "inbound":
                bucket["received"] += count
            elif msg_type == "outbound":
                if template and (template.category or "").upper() == "UTILITY":
                    bucket["utility"] += count
                else:
                    bucket["free"] += count
        for rec in self:
            bucket = stats[rec.id]
            rec.total_utility_month = bucket["utility"]
            rec.total_free_month = bucket["free"]
            rec.total_received_month = bucket["received"]
            rec.total_spent_month_usd = bucket["spent"]

    def action_sync_meta_analytics(self):
        """Consulta el endpoint oficial de analytics de Meta Graph API."""
        self.ensure_one()
        token = self.sudo().token
        if not token or not self.account_uid:
            raise UserError(_("Faltan credenciales de Meta (Token o WABA ID)."))

        now = int(time.time())
        start = now - (7 * 86400)
        url = f"https://graph.facebook.com/{self.api_version}/{self.account_uid}?fields=analytics.start({start}).end({now}).granularity(DAY)"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            res = requests.get(url, headers=headers, timeout=15)
            data = res.json()
        except Exception as e:
            raise UserError(_("Error al conectar con Meta Graph API: %s") % str(e))

        if res.status_code != 200:
            err = data.get("error", {}).get("message", res.text)
            raise UserError(_("Error de Meta API (%s): %s") % (res.status_code, err))

        analytics = data.get("analytics", {})
        data_points = analytics.get("data_points", [])
        total_sent = sum(dp.get("sent", 0) for dp in data_points)
        total_delivered = sum(dp.get("delivered", 0) for dp in data_points)

        self.write({
            "meta_official_sent": total_sent,
            "meta_official_delivered": total_delivered,
            "meta_last_sync_date": fields.Datetime.now(),
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Estadísticas de Meta Sincronizadas"),
                "message": _("Últimos 7 días: %s enviados, %s entregados según Meta.") % (
                    total_sent, total_delivered
                ),
                "type": "success",
                "sticky": False,
            },
        }

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
    def clean_phone_number(self, phone, country=None):
        """Sanitiza y normaliza un número a formato E.164 (solo dígitos, sin '+').

        Usa phonenumbers cuando está disponible con el país indicado o de la compañía;
        mantiene fallback de 8 dígitos a Panamá ('507').
        """
        if not phone:
            return False
        raw = str(phone).strip()
        if phonenumbers:
            region = None
            if country and getattr(country, "code", False):
                region = country.code
            elif self and self.company_id.country_id.code:
                region = self.company_id.country_id.code
            elif self.env.company.country_id.code:
                region = self.env.company.country_id.code
            try:
                parsed = phonenumbers.parse(raw, None if raw.startswith("+") else region)
                if phonenumbers.is_possible_number(parsed):
                    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164).lstrip("+")
            except Exception:
                pass
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 8:
            return f"507{digits}"
        if digits.startswith("00"):
            digits = digits[2:]
        return digits

    @api.model
    def find_partner_by_phone(self, phone):
        """Busca un res.partner por número de teléfono (E.164 exacto primero, luego sufijo)."""
        if not phone:
            return self.env["res.partner"].browse()
        clean = self.clean_phone_number(phone)
        if not clean:
            return self.env["res.partner"].browse()
        Partner = self.env["res.partner"].sudo()
        for candidate in (f"+{clean}", clean):
            if "phone" in Partner._fields:
                match = Partner.search([("phone", "=", candidate)], limit=1)
                if match:
                    return match
        short_num = clean[-8:] if len(clean) >= 8 else clean
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
        headers = {"Authorization": f"Bearer {self.sudo().token}"}
        try:
            res = requests.get(url, headers=headers, timeout=12)
            data = res.json()
            if res.status_code == 200:
                # Asegurar suscripción de la WABA a la App para webhooks entrantes
                if self.account_uid:
                    try:
                        requests.post(
                            f"https://graph.facebook.com/{self.api_version}/{self.account_uid}/subscribed_apps",
                            headers=headers,
                            timeout=8,
                        )
                    except Exception as e:
                        _logger.warning("No se pudo autoverificar subscribed_apps en Meta: %s", e)

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
        """Sincroniza plantillas desde Meta WhatsApp Business Account con paginación."""
        self.ensure_one()
        url = f"https://graph.facebook.com/{self.api_version}/{self.account_uid}/message_templates"
        headers = {"Authorization": f"Bearer {self.sudo().token}"}
        status_map = {
            "approved": "approved",
            "pending": "pending",
            "rejected": "rejected",
            "paused": "paused",
            "disabled": "disabled",
        }
        quality_map = {"green": "green", "yellow": "yellow", "red": "red"}
        Template = self.env["whatsapp.template"].sudo()
        synced = 0
        next_url = url
        pages = 0
        max_pages = 50
        try:
            while next_url and pages < max_pages:
                pages += 1
                res = requests.get(next_url, headers=headers, timeout=15)
                data = res.json()
                if res.status_code != 200:
                    err_msg = data.get("error", {}).get("message", res.text)
                    raise UserError(_("Error al sincronizar con Meta (%s): %s") % (res.status_code, err_msg))

                for item in data.get("data", []):
                    meta_uid = str(item.get("id") or "")
                    meta_name = item.get("name")
                    meta_status = (item.get("status") or "PENDING").lower()
                    meta_lang = item.get("language", "es")
                    meta_cat = item.get("category", "UTILITY")
                    raw_quality = item.get("quality_score")
                    if isinstance(raw_quality, dict):
                        raw_quality = raw_quality.get("score")
                    quality_val = quality_map.get(str(raw_quality or "").lower(), "unknown")

                    header_type = "none"
                    header_text = ""
                    body_text = ""
                    footer_text = ""
                    button_cmds = [(5, 0, 0)]
                    for comp in item.get("components", []):
                        ctype = comp.get("type")
                        if ctype == "HEADER":
                            fmt = comp.get("format", "TEXT").lower()
                            if fmt == "text":
                                header_type = "text"
                                header_text = comp.get("text", "")
                            elif fmt in ("image", "document", "video"):
                                header_type = fmt
                        elif ctype == "BODY":
                            body_text = comp.get("text", "")
                        elif ctype == "FOOTER":
                            footer_text = comp.get("text", "")
                        elif ctype == "BUTTONS":
                            for b_idx, btn in enumerate(comp.get("buttons", []), start=1):
                                btype = (btn.get("type") or "").upper()
                                if btype == "URL":
                                    mapped_btype = "url"
                                    val = btn.get("url", "")
                                elif btype == "PHONE_NUMBER":
                                    mapped_btype = "phone_number"
                                    val = btn.get("phone_number", "")
                                else:
                                    mapped_btype = "quick_reply"
                                    val = ""
                                button_cmds.append((0, 0, {
                                    "sequence": b_idx * 10,
                                    "button_type": mapped_btype,
                                    "name": btn.get("text", "Button"),
                                    "url_or_phone": val,
                                }))

                    existing = Template.browse()
                    if meta_uid:
                        existing = Template.search([
                            ("wa_account_id", "=", self.id),
                            ("wa_template_uid", "=", meta_uid),
                        ], limit=1)
                    if not existing:
                        existing = Template.search([
                            ("wa_account_id", "=", self.id),
                            ("template_name", "=", meta_name),
                            ("lang_code", "=", meta_lang),
                        ], limit=1)

                    vals = {
                        "wa_account_id": self.id,
                        "wa_template_uid": meta_uid or False,
                        "name": (meta_name or "").replace("_", " ").title(),
                        "template_name": meta_name,
                        "lang_code": meta_lang,
                        "category": meta_cat if meta_cat in ("UTILITY", "MARKETING", "AUTHENTICATION") else "UTILITY",
                        "status": status_map.get(meta_status, "pending"),
                        "quality_score": quality_val,
                        "header_type": header_type,
                        "header_text": header_text,
                        "body": body_text,
                        "footer_text": footer_text,
                    }
                    if len(button_cmds) > 1:
                        vals["button_ids"] = button_cmds
                    if existing:
                        existing.write(vals)
                        rec = existing
                    else:
                        rec = Template.create(vals)

                    matches = sorted(list(set(re.findall(r"\{\{(\d+)\}\}", body_text))), key=int)
                    existing_vars = {(v.component_type, int(v.sequence)) for v in rec.variable_ids}
                    for idx in matches:
                        if ("body", int(idx)) not in existing_vars:
                            self.env["whatsapp.template.variable"].create({
                                "template_id": rec.id,
                                "component_type": "body",
                                "sequence": int(idx),
                                "name": f"{{{{{idx}}}}}",
                                "sample_value": f"Sample {idx}",
                                "line_type": "field",
                            })

                    rec._post_sync_template_hook(meta_name, matches)
                    synced += 1

                next_url = data.get("paging", {}).get("next")

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
            "Authorization": f"Bearer {self.sudo().token}",
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

    def upload_media(self, raw_bytes, filename, mime_type="application/octet-stream"):
        """Sube un archivo multimedia a Meta Graph API y retorna su media_id (o False si falla)."""
        self.ensure_one()
        token = self.sudo().token
        if not token or not self.phone_number_id or not raw_bytes:
            return False
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/media"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            res = requests.post(
                url,
                headers=headers,
                data={"messaging_product": "whatsapp"},
                files={"file": (filename or "archivo", raw_bytes, mime_type or "application/octet-stream")},
                timeout=20,
            )
            data = res.json() if res.content else {}
            if res.status_code in (200, 201) and data.get("id"):
                return data["id"]
            _logger.warning("Fallo al subir media a Meta (%s): %s", res.status_code, data)
            return False
        except Exception as e:
            _logger.error("Excepción al subir media a Meta: %s", e)
            return False

    def send_media_to_phone(self, phone, media_id, media_type="document", filename=None, caption=None):
        """Envía un archivo multimedia ya subido (media_id) a un teléfono dentro de la ventana de 24h."""
        self.ensure_one()
        clean_phone = self.clean_phone_number(phone)
        if not clean_phone:
            return {"error": {"message": "Número de teléfono no válido"}}
        if self.env["whatsapp.blocklist"].sudo().is_number_blocked(clean_phone):
            return {"error": {"message": "El número está en la lista negra (opt-out)"}}

        wa_type = media_type if media_type in ("image", "document", "audio", "video") else "document"
        media_obj = {"id": media_id}
        if wa_type == "document" and filename:
            media_obj["filename"] = filename
        if caption and wa_type in ("image", "document", "video"):
            media_obj["caption"] = caption

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_phone,
            "type": wa_type,
            wa_type: media_obj,
        }
        msg_vals = {
            "wa_account_id": self.id,
            "mobile_number": clean_phone,
            "body": caption or f"[{wa_type.upper()}: {filename or media_id}]",
            "msg_type": "outbound",
            "state": "outgoing",
            "media_id": media_id,
            "media_type": wa_type,
            "cost_usd": 0.0,
        }
        try:
            status_code, data = self.send_meta_raw(payload)
        except Exception as e:
            msg_vals.update({"state": "error", "error_msg": str(e)})
            self.env["whatsapp.message"].sudo().create(msg_vals)
            return {"error": {"message": str(e)}}

        if status_code in (200, 201):
            messages = data.get("messages", [])
            wamid = messages[0].get("id") if messages else False
            msg_vals.update({"state": "sent", "msg_uid": wamid})
            msg = self.env["whatsapp.message"].sudo().create(msg_vals)
            return {"success": True, "wamid": wamid, "message_id": msg.id}
        err_data = data.get("error", {})
        err_msg = err_data.get("message", str(data))
        err_code = str(err_data.get("code") or "")
        Msg = self.env["whatsapp.message"].sudo()
        msg_vals.update({
            "state": Msg._classify_meta_error_state(err_data),
            "error_code": err_code or False,
            "error_msg": f"HTTP {status_code}: {err_msg}",
        })
        Msg.create(msg_vals)
        return {"error": err_data or {"message": err_msg}}

    def send_text_to_phone(self, phone, text):
        """Envía un mensaje de texto libre a través de Meta Graph API (ventana de 24h)."""
        self.ensure_one()
        clean_phone = self.clean_phone_number(phone)
        if not clean_phone:
            return {"error": {"message": "Número de teléfono no válido"}}

        # Validar si está en lista negra
        if self.env["whatsapp.blocklist"].sudo().is_number_blocked(clean_phone):
            _logger.info("WhatsApp abortado: el número %s está en la lista negra.", clean_phone)
            return {"error": {"message": "El número está en la lista negra (opt-out)"}}

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
            "cost_usd": 0.0,
        }

        try:
            status_code, data = self.send_meta_raw(payload)
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
            err_code = str(err_data.get("code") or "")
            Msg = self.env["whatsapp.message"].sudo()
            msg_vals.update({
                "state": Msg._classify_meta_error_state(err_data),
                "error_code": err_code or False,
                "error_msg": f"HTTP {status_code}: {err_msg}",
            })
            Msg.create(msg_vals)
            _logger.warning("Fallo al enviar texto libre a %s: %s", clean_phone, err_msg)
            return {"error": err_data or {"message": err_msg}}

    def download_media(self, media_id):
        """Descarga un archivo multimedia de Meta WhatsApp Cloud API a partir de su media_id.

        Retorna (raw_bytes, mime_type) o (None, None) en caso de fallo.
        """
        self.ensure_one()
        token = self.sudo().token
        if not token or not media_id:
            return None, None
        try:
            api_version = self.api_version or "v21.0"
            meta_url = f"https://graph.facebook.com/{api_version}/{media_id}"
            headers = {"Authorization": f"Bearer {token}"}
            res = requests.get(meta_url, headers=headers, timeout=15)
            if res.status_code != 200:
                _logger.error("Error al consultar media_id %s en Meta: %s", media_id, res.text)
                return None, None

            meta_data = res.json()
            download_url = meta_data.get("url")
            mime_type = meta_data.get("mime_type") or "application/octet-stream"
            if not download_url:
                return None, None

            cdn_headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": "curl/7.68.0",
            }
            file_res = requests.get(download_url, headers=cdn_headers, timeout=30)
            if file_res.status_code == 200:
                return file_res.content, mime_type
            return None, None
        except Exception as e:
            _logger.error("Excepción al descargar media %s de WhatsApp: %s", media_id, e)
            return None, None

