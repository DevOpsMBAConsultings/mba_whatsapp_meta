# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import json
import logging
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WhatsAppWebhookController(http.Controller):

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
        account = Account.browse(account_id) if account_id else Account.search([], limit=1)
        if not account or not account.exists():
            account = Account.search([], limit=1)

        # 1. Verificación del Webhook por Meta (GET)
        if request.httprequest.method == "GET":
            hub_mode = kwargs.get("hub.mode") or kwargs.get("hub_mode")
            hub_token = kwargs.get("hub.verify_token") or kwargs.get("hub_verify_token")
            hub_challenge = kwargs.get("hub.challenge") or kwargs.get("hub_challenge")

            if hub_mode == "subscribe":
                if account and account.webhook_verify_token == hub_token:
                    _logger.info("WhatsApp Webhook verificado con éxito para cuenta %s", account.name)
                    return request.make_response(str(hub_challenge), headers=[("Content-Type", "text/plain")])

            _logger.warning("Fallo en verificación de Webhook WhatsApp: token inválido")
            return request.make_response("Forbidden", status=403)

        # 2. Recepción de eventos y estados de entrega (POST)
        try:
            payload = json.loads(request.httprequest.data.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        entries = payload.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Actualizar estado de entrega (sent, delivered, read, failed)
                for status in value.get("statuses", []):
                    wamid = status.get("id")
                    st = status.get("status")
                    if wamid and st:
                        msg = request.env["whatsapp.message"].sudo().search([("msg_uid", "=", wamid)], limit=1)
                        if msg:
                            msg.write({"state": st})

                # Mensajes entrantes (procesamiento de respuestas y opt-out)
                for incoming_msg in value.get("messages", []):
                    from_num = incoming_msg.get("from")
                    msg_type = incoming_msg.get("type", "text")
                    msg_uid = incoming_msg.get("id")
                    if not from_num:
                        continue

                    clean_num = request.env["whatsapp.account"].clean_phone_number(from_num)

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
                        channel = Channel.search([("whatsapp_number", "=", clean_num)], limit=1)
                        partner = False
                        if channel and channel.whatsapp_partner_id:
                            partner = channel.whatsapp_partner_id
                        else:
                            partner = account.find_partner_by_phone(clean_num)

                        if not partner:
                            partner = request.env["res.partner"].sudo().create({
                                "name": f"WhatsApp +{clean_num}",
                                "phone": f"+{clean_num}",
                            })

                        admin_user = request.env.ref("base.user_admin").sudo()
                        if not channel:
                            # Chat directo estrictamente 1-a-1 entre el agente y el contacto de WhatsApp
                            channel = Channel.with_user(admin_user).sudo()._get_or_create_chat(
                                partners_to=[partner.id], pin=True
                            ).sudo()

                        channel = channel.sudo()
                        channel.write({
                            "whatsapp_number": clean_num,
                            "whatsapp_partner_id": partner.id,
                            "whatsapp_account_id": account.id,
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
                        posted_msg = channel.with_user(admin_user).sudo().with_context(whatsapp_no_dispatch=True).message_post(**post_vals)

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
