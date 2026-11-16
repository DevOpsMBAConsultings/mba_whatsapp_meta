# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import json
import logging
import re
import requests

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

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
    wa_template_uid = fields.Char(string="Meta Template ID", copy=False, index=True)
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
            ("paused", "Paused"),
            ("disabled", "Disabled"),
        ],
        string="Estado",
        default="draft",
        tracking=True,
    )
    quality_score = fields.Selection(
        [
            ("green", "Green"),
            ("yellow", "Yellow"),
            ("red", "Red"),
            ("unknown", "Unknown"),
        ],
        string="Calidad Meta",
        default="unknown",
        readonly=True,
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
    header_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Archivo de Encabezado",
        help="Imagen, documento o video estático para el encabezado de la plantilla.",
    )
    report_id = fields.Many2one(
        "ir.actions.report",
        string="Reporte PDF Dinámico",
        domain="[('model', '=', model), ('report_type', '=', 'qweb-pdf')]",
        help="Reporte QWeb PDF generado dinámicamente al enviar encabezados de tipo documento.",
    )
    body = fields.Text(string="Cuerpo del Mensaje", help="Ej: Hola {{1}}, su cuota {{2}} por ${{3}} vence el {{4}}.")
    footer_text = fields.Char(string="Pie de Página")
    variable_ids = fields.One2many("whatsapp.template.variable", "template_id", string="Mapeo de Variables")
    button_ids = fields.One2many("whatsapp.template.button", "template_id", string="Botones")

    message_count = fields.Integer(string="Mensajes", compute="_compute_message_count")

    _unique_template_lang_account = models.Constraint(
        "UNIQUE(template_name, lang_code, wa_account_id)",
        "Ya existe una plantilla con el mismo nombre técnico, idioma y cuenta de WhatsApp.",
    )

    def _auto_init(self):
        cr = self.env.cr
        cr.execute("SELECT to_regclass('whatsapp_template')")
        if cr.fetchone()[0]:
            # Deduplicar plantillas previas antes de aplicar la restricción única
            cr.execute("""
                SELECT template_name, lang_code, wa_account_id,
                       array_agg(id ORDER BY (status = 'approved') DESC, write_date DESC, id DESC) AS ids
                FROM whatsapp_template
                GROUP BY template_name, lang_code, wa_account_id
                HAVING COUNT(*) > 1
            """)
            for _name, _lang, _acc, ids in cr.fetchall():
                keep_id, dup_ids = ids[0], ids[1:]
                cr.execute(
                    "UPDATE whatsapp_message SET template_id = %s WHERE template_id = ANY(%s)",
                    (keep_id, dup_ids),
                )
                cr.execute("DELETE FROM whatsapp_template WHERE id = ANY(%s)", (dup_ids,))
        return super()._auto_init()

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
        token = account.sudo().token if account else False
        if not account or not token:
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
            "Authorization": f"Bearer {token}",
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

    def action_add_variable(self):
        """Inserta automáticamente la siguiente variable {{n}} en el cuerpo y crea su registro."""
        self.ensure_one()
        matches = [int(x) for x in re.findall(r"\{\{(\d+)\}\}", self.body or "")]
        next_seq = max(matches, default=0) + 1
        var_marker = f"{{{{{next_seq}}}}}"

        current_body = (self.body or "").strip()
        new_body = f"{current_body} {var_marker}" if current_body else var_marker

        existing_seqs = {v.sequence for v in self.variable_ids}
        vals = {"body": new_body}
        if next_seq not in existing_seqs:
            vals["variable_ids"] = [(0, 0, {
                "sequence": next_seq,
                "name": var_marker,
                "sample_value": f"Sample {next_seq}",
                "line_type": "field",
                "model": self.model or "",
            })]
        self.write(vals)
        return True

    def send_to_phone(self, phone, record=None, custom_params=None):
        """Envía un mensaje usando esta plantilla vía Meta WhatsApp Cloud API."""
        self.ensure_one()
        account = self.wa_account_id
        if not account or not account.active:
            raise UserError(_("La cuenta de WhatsApp configurada no está activa."))

        cleaned_phone = account.clean_phone_number(phone)
        if not cleaned_phone:
            raise UserError(_("El número de teléfono proporcionado no es válido."))

        if self.env["whatsapp.blocklist"].sudo().is_number_blocked(cleaned_phone):
            _logger.info("WhatsApp abortado: el número %s está en la lista negra.", cleaned_phone)
            return self.env["whatsapp.message"].sudo().create({
                "wa_account_id": account.id,
                "template_id": self.id,
                "mobile_number": cleaned_phone,
                "body": self.body or "",
                "state": "error",
                "error_msg": "Número en lista negra (opt-out previo).",
            })

        # Construir parámetros por componente
        components = []

        # 1. HEADER component (texto dinámico, reporte PDF o adjunto estático)
        header_params = []
        if self.header_type == "text":
            header_vars = self.variable_ids.filtered(lambda v: v.component_type == "header").sorted("sequence")
            for var in header_vars:
                if record or var.line_type != "field":
                    header_params.append({"type": "text", "text": var.get_value_for_record(record)})
        elif self.header_type == "document" and self.report_id and record:
            pdf_bytes, _fmt = self.env["ir.actions.report"].sudo()._render_qweb_pdf(
                self.report_id.report_name, [record.id]
            )
            pdf_name = f"{(record.display_name or self.template_name).replace('/', '_')}.pdf"
            media_id = account.upload_media(pdf_bytes, pdf_name, "application/pdf")
            if media_id:
                header_params.append({"type": "document", "document": {"id": media_id, "filename": pdf_name}})
        elif self.header_type in ("image", "document", "video") and self.header_attachment_id:
            att = self.header_attachment_id.sudo()
            media_id = account.upload_media(att.raw, att.name, att.mimetype or "application/octet-stream")
            if media_id:
                media_payload = {"id": media_id}
                if self.header_type == "document" and att.name:
                    media_payload["filename"] = att.name
                header_params.append({"type": self.header_type, self.header_type: media_payload})

        if header_params:
            components.append({"type": "header", "parameters": header_params})

        # 2. BODY component
        body_parameters = []
        rendered_body = self.body or ""
        if custom_params:
            for idx, val in enumerate(custom_params, start=1):
                val_str = str(val or "")
                body_parameters.append({"type": "text", "text": val_str})
                rendered_body = rendered_body.replace(f"{{{{{idx}}}}}", val_str)
        elif self.variable_ids:
            body_vars = self.variable_ids.filtered(lambda v: v.component_type == "body").sorted("sequence")
            seen_seq = set()
            for var in body_vars:
                if var.sequence in seen_seq:
                    continue
                seen_seq.add(var.sequence)
                val = var.get_value_for_record(record) if (record or var.line_type != "field") else ""
                body_parameters.append({"type": "text", "text": val})
                rendered_body = rendered_body.replace(f"{{{{{var.sequence}}}}}", val)

        if body_parameters:
            components.append({
                "type": "body",
                "parameters": body_parameters,
            })

        # 3. BUTTON components (URL dinámica con {{1}})
        btn_vars = self.variable_ids.filtered(lambda v: v.component_type == "button").sorted("sequence")
        url_buttons = self.button_ids.filtered(
            lambda b: b.button_type == "url" and "{{" in (b.url_or_phone or "")
        ).sorted("sequence")
        for b_idx, btn in enumerate(url_buttons):
            if b_idx < len(btn_vars):
                var = btn_vars[b_idx]
                btn_val = var.get_value_for_record(record) if (record or var.line_type != "field") else ""
                components.append({
                    "type": "button",
                    "sub_type": "url",
                    "index": str(b_idx),
                    "parameters": [{"type": "text", "text": btn_val}],
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

        # Costo oficial de Meta según categoría de plantilla
        cost_usd = 0.0
        cat_upper = (self.category or "").upper()
        if cat_upper == "UTILITY":
            cost_usd = account.rate_utility_usd
        elif cat_upper == "MARKETING":
            cost_usd = account.rate_marketing_usd

        msg_vals = {
            "wa_account_id": account.id,
            "template_id": self.id,
            "mobile_number": cleaned_phone,
            "body": rendered_body,
            "state": "outgoing",
            "cost_usd": cost_usd,
        }

        try:
            status_code, data = account.send_meta_raw(payload)
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

            # Publicar en el módulo de Conversaciones (discuss.channel)
            partner = False
            if record:
                if record._name == "res.partner":
                    partner = record
                elif hasattr(record, "partner_id") and record.partner_id:
                    partner = record.partner_id
                elif hasattr(record, "application_id") and getattr(record.application_id, "partner_id", False):
                    partner = record.application_id.partner_id

            # Si el teléfono del registro no coincide con el número al que se envió, buscar por teléfono
            partner_phone = getattr(partner, "phone", "") or (getattr(partner, "mobile", "") if partner and "mobile" in partner._fields else "")
            if not partner or (partner and account.clean_phone_number(partner_phone) != cleaned_phone):
                partner = account.find_partner_by_phone(cleaned_phone)

            # Si no existe ningún contacto con este teléfono, crear el contacto base para no perder la conversación
            if not partner:
                p_vals = {
                    "name": f"WhatsApp +{cleaned_phone}",
                    "company_id": account.company_id.id,
                }
                if "phone" in self.env["res.partner"]._fields:
                    p_vals["phone"] = f"+{cleaned_phone}"
                elif "mobile" in self.env["res.partner"]._fields:
                    p_vals["mobile"] = f"+{cleaned_phone}"
                partner = self.env["res.partner"].sudo().create(p_vals)

            if partner:
                try:
                    Channel = self.env["discuss.channel"].sudo()
                    agent_user = (
                        account.notify_user_ids[:1]
                        or self.env.ref("base.user_admin", raise_if_not_found=False)
                        or self.env.user
                    )
                    channel = Channel.search(
                        [("whatsapp_number", "=", cleaned_phone), ("whatsapp_account_id", "=", account.id)],
                        limit=1,
                    )
                    if not channel:
                        channel = Channel.with_user(agent_user).sudo()._get_or_create_chat(
                            partners_to=[partner.id], pin=True
                        )
                    channel.write({
                        "whatsapp_number": cleaned_phone,
                        "whatsapp_partner_id": partner.id,
                        "whatsapp_account_id": account.id,
                    })
                    channel.with_user(agent_user).sudo().with_context(whatsapp_no_dispatch=True).message_post(
                        body=(rendered_body or "").replace("\n", "<br/>"),
                        message_type="comment",
                        subtype_xmlid="mail.mt_comment",
                    )
                except Exception as ex:
                    _logger.warning("No se pudo publicar WhatsApp en Conversaciones: %s", ex)

            return msg
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
            msg = Msg.create(msg_vals)
            _logger.error("Error al enviar WhatsApp Meta a %s: %s", cleaned_phone, err_msg)
            if record and hasattr(record, "message_post"):
                record.message_post(
                    body=_("Fallo al enviar WhatsApp («%s») a %s: %s") % (self.name, cleaned_phone, err_msg),
                    message_type="notification",
                )
            return msg

    def _post_sync_template_hook(self, meta_name, matches):
        """Hook extensible para módulos de negocio o integraciones específicas de cliente."""
        pass


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


    def _post_sync_template_hook(self, meta_name, matches):
        """Hook extensible para módulos de negocio o integraciones específicas de cliente."""
        pass


class WhatsappTemplateVariable(models.Model):
    _name = "whatsapp.template.variable"
    _description = "Variable dinámica de Plantilla WhatsApp"
    _order = "sequence"

    template_id = fields.Many2one("whatsapp.template", ondelete="cascade")
    component_type = fields.Selection(
        [
            ("body", "Body"),
            ("header", "Header"),
            ("button", "Button"),
        ],
        string="Componente",
        default="body",
        required=True,
    )
    sequence = fields.Integer(string="Posición", default=1)
    name = fields.Char(string="Name", default="{{1}}", required=True)
    model = fields.Char(string="Model", compute="_compute_model", store=True, readonly=False)
    sample_value = fields.Char(string="Sample Value", default="Sample Value")
    free_text = fields.Char(string="Free Text")
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

    @api.constrains("field_name", "model")
    def _check_field_name_path(self):
        for rec in self:
            if not rec.field_name:
                continue
            parts = rec.field_name.split(".")
            if any(not p or p.startswith("_") for p in parts):
                raise ValidationError(_("Ruta de campo inválida en variable de plantilla: %s") % rec.field_name)
            if rec.model and rec.model in self.env:
                model_obj = self.env[rec.model]
                for part in parts:
                    field = model_obj._fields.get(part)
                    if not field:
                        raise ValidationError(
                            _("El campo '%s' no existe en el modelo '%s'.") % (part, model_obj._name)
                        )
                    if field.comodel_name and field.comodel_name in self.env:
                        model_obj = self.env[field.comodel_name]

    def get_value_for_record(self, record):
        """Extrae el valor de la variable según line_type y validación de permisos."""
        self.ensure_one()
        if self.line_type == "free_text":
            return self.free_text or self.sample_value or ""
        if self.line_type == "user_name":
            return self.env.user.name or ""
        if self.line_type == "user_phone":
            return self.env.user.partner_id.phone or ""
        if self.line_type == "portal_link":
            if record and hasattr(record, "get_portal_url"):
                return (record.get_base_url() or "") + (record.get_portal_url() or "")
            return ""
        if not self.field_name or not record:
            return ""
        val = record.sudo(False)
        for part in self.field_name.split("."):
            if not part or part.startswith("_") or part not in val._fields:
                raise AccessError(_("Ruta de campo no permitida: %s") % self.field_name)
            val.check_access("read")
            field = val._fields[part]
            if field.groups and not self.env.su and not self.env.user.has_groups(field.groups):
                raise AccessError(_("No tiene permisos para leer el campo '%s'.") % part)
            val = val[part]
            if not val:
                break
        if isinstance(val, models.BaseModel):
            return val.display_name or ""
        return str(val) if val is not None else ""
