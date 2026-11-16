# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import hashlib
import hmac
import json
import logging
from odoo import fields, http
from odoo.http import request
from odoo.tools import consteq

_logger = logging.getLogger(__name__)


class WhatsAppWebhookController(http.Controller):

    def _resolve_get_account(self, Account, account_id, hub_token):
        if not hub_token:
            return Account.browse()
        if account_id:
            account = Account.browse(account_id)
            if account.exists() and account.webhook_verify_token and consteq(
                account.webhook_verify_token, str(hub_token)
            ):
                return account
            return Account.browse()
        for candidate in Account.search([("webhook_verify_token", "!=", False)]):
            if consteq(candidate.webhook_verify_token, str(hub_token)):
                return candidate
        return Account.browse()

    def _resolve_post_account(self, Account, account_id, payload):
        if account_id:
            account = Account.browse(account_id)
            return account if account.exists() else Account.browse()

        entries = payload.get("entry", []) if isinstance(payload, dict) else []
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                if phone_number_id:
                    account = Account.search([("phone_number_id", "=", str(phone_number_id))], limit=1)
                    if account:
                        return account
        for entry in entries:
            entry_id = entry.get("id")
            if entry_id:
                account = Account.search([("account_uid", "=", str(entry_id))], limit=1)
                if account:
                    return account
        return Account.browse()

    def _verify_signature(self, account, raw_body, signature_header):
        if not account or not account.exists() or not account.app_secret:
            return False
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(
            account.app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return consteq(signature_header[7:], expected)

    def _process_template_webhook(self, account, field_name, value):
        Template = request.env["whatsapp.template"].sudo()
        tpl_id = str(value.get("message_template_id") or "")
        tpl_name = value.get("message_template_name")
        tpl_lang = value.get("message_template_language")

        tpl = Template.browse()
        if tpl_id:
            tpl = Template.search([("wa_account_id", "=", account.id), ("wa_template_uid", "=", tpl_id)], limit=1)
        if not tpl and tpl_name:
            domain = [("wa_account_id", "=", account.id), ("template_name", "=", tpl_name)]
            if tpl_lang:
                domain.append(("lang_code", "=", tpl_lang))
            tpl = Template.search(domain, limit=1)
        if not tpl:
            return

        if field_name == "message_template_status_update":
            event = (value.get("event") or "").lower()
            status_map = {
                "approved": "approved",
                "pending": "pending",
                "rejected": "rejected",
                "paused": "paused",
                "disabled": "disabled",
            }
            if event in status_map:
                tpl.write({"status": status_map[event]})
        elif field_name == "message_template_quality_update":
            score = (value.get("new_quality_score") or "").lower()
            if score in ("green", "yellow", "red", "unknown"):
                tpl.write({"quality_score": score})
        elif field_name == "template_category_update":
            new_cat = (value.get("new_category") or "").upper()
            if new_cat in ("UTILITY", "MARKETING", "AUTHENTICATION"):
                tpl.write({"category": new_cat})

    @http.route(
        ["/whatsapp/webhook/<int:account_id>", "/whatsapp/webhook"],
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        save_session=False,
    )
    def whatsapp_webhook(self, account_id=None, **kwargs):
        Account = request.env["whatsapp.account"].sudo()

        # 1. Verificación del Webhook por Meta (GET)
        if request.httprequest.method == "GET":
            hub_mode = kwargs.get("hub.mode") or kwargs.get("hub_mode")
            hub_token = kwargs.get("hub.verify_token") or kwargs.get("hub_verify_token")
            hub_challenge = kwargs.get("hub.challenge") or kwargs.get("hub_challenge")

            if hub_mode == "subscribe":
                account = self._resolve_get_account(Account, account_id, hub_token)
                if account:
                    _logger.info("WhatsApp Webhook verificado con éxito para cuenta %s", account.name)
                    return request.make_response(str(hub_challenge), headers=[("Content-Type", "text/plain")])

            _logger.warning("Fallo en verificación de Webhook WhatsApp: token inválido")
            return request.make_response("Forbidden", status=403)

        # 2. Recepción de eventos y estados de entrega (POST)
        raw_body = request.httprequest.get_data() or b""
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except Exception:
            _logger.warning("Payload JSON inválido en webhook de WhatsApp")
            return request.make_response("Bad Request", status=400)

        account = self._resolve_post_account(Account, account_id, payload)
        sig_header = request.httprequest.headers.get("X-Hub-Signature-256", "")
        if not self._verify_signature(account, raw_body, sig_header):
            _logger.warning("Firma X-Hub-Signature-256 inválida o cuenta no resuelta en webhook de WhatsApp")
            return request.make_response("Forbidden", status=403)

        entries = payload.get("entry", [])
        status_rank = {"outgoing": 0, "sent": 1, "delivered": 2, "read": 3}
        for entry in entries:
            for change in entry.get("changes", []):
                field_name = change.get("field")
                value = change.get("value", {})
                if field_name in (
                    "message_template_status_update",
                    "message_template_quality_update",
                    "template_category_update",
                ):
                    self._process_template_webhook(account, field_name, value)
                    continue

                # Actualizar estado de entrega (sent, delivered, read, failed) con orden monotónico
                for status in value.get("statuses", []):
                    wamid = status.get("id")
                    st = status.get("status")
                    if wamid and st:
                        msg = request.env["whatsapp.message"].sudo().search([("msg_uid", "=", wamid)], limit=1)
                        if not msg:
                            continue
                        if st == "failed":
                            errors = status.get("errors") or []
                            err_txt = errors[0].get("message") if errors else "Meta delivery failed"
                            msg.write({"state": "error", "error_msg": err_txt})
                        elif st in status_rank:
                            cur_rank = status_rank.get(msg.state, -1)
                            if status_rank[st] > cur_rank:
                                msg.write({"state": st})

                # Mensajes entrantes (procesamiento de respuestas y opt-out)
                for incoming_msg in value.get("messages", []):
                    from_num = incoming_msg.get("from")
                    msg_type = incoming_msg.get("type", "text")
                    msg_uid = incoming_msg.get("id")
                    if not from_num:
                        continue

                    clean_num = account.clean_phone_number(from_num)

                    media_dict = incoming_msg.get(msg_type, {}) if isinstance(incoming_msg.get(msg_type), dict) else {}
                    media_id = media_dict.get("id")

                    if msg_type == "text":
                        text_body = incoming_msg.get("text", {}).get("body", "").strip()
                    elif msg_type == "image":
                        caption = media_dict.get("caption", "").strip()
                        text_body = caption or "📷 Imagen recibida por WhatsApp"
                    elif msg_type == "document":
                        caption = media_dict.get("caption", "").strip()
                        filename = media_dict.get("filename", "documento.pdf")
                        text_body = caption or f"📄 Documento: {filename}"
                    elif msg_type == "audio":
                        text_body = "🎵 Mensaje de voz recibido por WhatsApp"
                    elif msg_type == "video":
                        caption = media_dict.get("caption", "").strip()
                        text_body = caption or "🎥 Video recibido por WhatsApp"
                    else:
                        text_body = f"[{msg_type.capitalize()} recibido por WhatsApp]"

                    # Detección de opt-out / STOP
                    if text_body.upper() in ("STOP", "BAJA", "ALTO", "CANCELAR"):
                        Blocklist = request.env["whatsapp.blocklist"].sudo()
                        if not Blocklist.search([("mobile_number", "=", clean_num)], limit=1):
                            Blocklist.create({
                                "mobile_number": clean_num,
                                "reason": f"Opt-out recibido por webhook ({text_body})",
                            })
                            _logger.info("Número %s añadido a lista negra por petición expresa", clean_num)

                    # 1. Registrar en whatsapp.message PRIMERO con media_id y estado pending si tiene archivo
                    MsgModel = request.env["whatsapp.message"].sudo()
                    wa_msg = MsgModel.search([("msg_uid", "=", msg_uid)], limit=1)
                    partner_pre = account.find_partner_by_phone(clean_num)
                    if not wa_msg:
                        wa_msg = MsgModel.create({
                            "wa_account_id": account.id,
                            "mobile_number": clean_num,
                            "partner_id": partner_pre.id if partner_pre else False,
                            "body": text_body,
                            "msg_type": "inbound",
                            "state": "delivered",
                            "msg_uid": msg_uid,
                            "media_id": media_id or False,
                            "media_type": msg_type if media_id else "none",
                            "media_status": "pending" if media_id else "none",
                        })
                    elif media_id and wa_msg.media_status != "downloaded":
                        wa_msg.write({
                            "partner_id": partner_pre.id if partner_pre else wa_msg.partner_id.id,
                            "media_id": media_id,
                            "media_type": msg_type,
                            "media_status": "pending",
                        })

                    # 2. Descargar archivo multimedia si aplica
                    post_attachments = []
                    if media_id and wa_msg.media_status != "downloaded":
                        raw_bytes, mime_type = account.download_media(media_id)
                        if raw_bytes:
                            fname = media_dict.get("filename")
                            if not fname:
                                ext = "jpg" if "jpeg" in (mime_type or "") or "jpg" in (mime_type or "") else (
                                    "png" if "png" in (mime_type or "") else (
                                        "pdf" if "pdf" in (mime_type or "") else "bin"
                                    )
                                )
                                fname = f"whatsapp_{msg_type}_{msg_uid[-6:]}.{ext}"
                            post_attachments.append((fname, raw_bytes))
                        else:
                            wa_msg.write({
                                "media_status": "failed",
                                "media_error": "No se pudieron obtener los bytes desde Meta Cloud API",
                            })

                    # 3. Publicar en Conversaciones (discuss.channel)
                    try:
                        Channel = request.env["discuss.channel"].sudo()
                        channel = Channel.search(
                            [("whatsapp_number", "=", clean_num), ("whatsapp_account_id", "=", account.id)],
                            limit=1,
                        )
                        if not channel:
                            channel = Channel.search(
                                [("whatsapp_number", "=", clean_num), ("whatsapp_account_id", "=", False)],
                                limit=1,
                            )
                        partner = False
                        if channel and channel.whatsapp_partner_id:
                            partner = channel.whatsapp_partner_id
                        else:
                            partner = account.find_partner_by_phone(clean_num)

                        if not partner:
                            partner = request.env["res.partner"].sudo().create({
                                "name": f"WhatsApp +{clean_num}",
                                "phone": f"+{clean_num}",
                                "company_id": account.company_id.id,
                            })

                        agent_user = (
                            account.notify_user_ids[:1]
                            or request.env.ref("base.user_admin").sudo()
                        ).sudo()
                        if not channel:
                            # Chat directo estrictamente 1-a-1 entre el agente asignado y el contacto de WhatsApp
                            channel = Channel.with_user(agent_user).sudo()._get_or_create_chat(
                                partners_to=[partner.id], pin=True
                            ).sudo()

                        channel = channel.sudo()
                        channel.write({
                            "whatsapp_number": clean_num,
                            "whatsapp_partner_id": partner.id,
                            "whatsapp_account_id": account.id,
                            "whatsapp_last_inbound_dt": fields.Datetime.now(),
                        })
                        if partner and not wa_msg.partner_id:
                            wa_msg.write({"partner_id": partner.id})

                        post_vals = {
                            "body": text_body,
                            "author_id": partner.id,
                            "message_type": "comment",
                            "subtype_xmlid": "mail.mt_comment",
                        }
                        if post_attachments:
                            post_vals["attachments"] = post_attachments
                        posted_msg = channel.with_user(agent_user).sudo().with_context(whatsapp_no_dispatch=True).message_post(**post_vals)

                        if post_attachments and posted_msg.attachment_ids:
                            wa_msg.write({
                                "media_status": "downloaded",
                                "attachment_id": posted_msg.attachment_ids[0].id,
                                "media_error": False,
                            })
                            try:
                                wa_msg._post_webhook_attachment_hook(posted_msg, text_body)
                            except Exception as ex_hook:
                                _logger.warning("Error ejecutando _post_webhook_attachment_hook: %s", ex_hook)

                        _logger.info("Mensaje entrante de WhatsApp publicado en Discuss (Canal %s, Partner %s, Adjuntos: %s)", channel.id, partner.name, len(posted_msg.attachment_ids))
                    except Exception as ex:
                        if wa_msg and media_id and wa_msg.media_status != "downloaded":
                            wa_msg.write({"media_status": "failed", "media_error": str(ex)})
                        _logger.error("Error al publicar mensaje entrante en Discuss: %s", ex)

        return request.make_response(
            json.dumps({"status": "success"}), headers=[("Content-Type", "application/json")]
        )
