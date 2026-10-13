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
        # 1. Verificación del Webhook por Meta (GET)
        if request.httprequest.method == "GET":
            hub_mode = kwargs.get("hub.mode") or kwargs.get("hub_mode")
            hub_token = kwargs.get("hub.verify_token") or kwargs.get("hub_verify_token")
            hub_challenge = kwargs.get("hub.challenge") or kwargs.get("hub_challenge")

            if hub_mode == "subscribe":
                Account = request.env["whatsapp.account"].sudo()
                account = Account.browse(account_id) if account_id else Account.search([], limit=1)
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

                    if msg_type == "text":
                        text_body = incoming_msg.get("text", {}).get("body", "").strip()
                    elif msg_type == "image":
                        text_body = incoming_msg.get("image", {}).get("caption", "").strip() or "[Imagen recibida por WhatsApp]"
                    elif msg_type == "document":
                        text_body = incoming_msg.get("document", {}).get("caption", "").strip() or "[Documento recibido por WhatsApp]"
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

                    # Registrar en whatsapp.message si no existe ya
                    MsgModel = request.env["whatsapp.message"].sudo()
                    if not MsgModel.search([("msg_uid", "=", msg_uid)], limit=1):
                        MsgModel.create({
                            "wa_account_id": account.id,
                            "mobile_number": clean_num,
                            "body": text_body,
                            "msg_type": "inbound",
                            "state": "delivered",
                            "msg_uid": msg_uid,
                        })

                    # Publicar en Conversaciones (discuss.channel)
                    try:
                        partner = account.find_partner_by_phone(clean_num)

                        Channel = request.env["discuss.channel"].sudo()
                        if not partner:
                            # Si no existe partner, buscar si ya hay un canal con este número o crearlo
                            channel = Channel.search([("whatsapp_number", "=", clean_num)], limit=1)
                            if not channel:
                                partner = request.env["res.partner"].sudo().create({
                                    "name": f"WhatsApp +{clean_num}",
                                    "phone": f"+{clean_num}",
                                })
                                channel = Channel._get_or_create_chat(partners_to=[partner.id], pin=True)
                        else:
                            channel = Channel._get_or_create_chat(partners_to=[partner.id], pin=True)

                        channel.write({
                            "whatsapp_number": clean_num,
                            "whatsapp_partner_id": partner.id,
                            "whatsapp_account_id": account.id,
                        })
                        channel.with_context(whatsapp_no_dispatch=True).message_post(
                            body=text_body,
                            author_id=partner.id,
                            message_type="comment",
                            subtype_xmlid="mail.mt_comment",
                        )
                        _logger.info("Mensaje entrante de WhatsApp publicado en Discuss (Canal %s, Partner %s)", channel.id, partner.name)
                    except Exception as ex:
                        _logger.error("Error al publicar mensaje entrante en Discuss: %s", ex)

        return request.make_response(
            json.dumps({"status": "success"}), headers=[("Content-Type", "application/json")]
        )
