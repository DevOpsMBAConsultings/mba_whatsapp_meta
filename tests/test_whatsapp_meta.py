# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged("post_install", "-at_install", "mba_whatsapp")
class TestWhatsappMeta(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["whatsapp.account"].create({
            "name": "Test WhatsApp Account",
            "phone_number_id": "1234567890",
            "account_uid": "9876543210",
            "token": "TEST_META_TOKEN_123",
            "phone_uid": "+507 6000-1122",
        })
        cls.template = cls.env["whatsapp.template"].create({
            "name": "Recordatorio de Pago",
            "template_name": "recordatorio_cuota",
            "wa_account_id": cls.account.id,
            "lang_code": "es",
            "category": "UTILITY",
            "status": "approved",
            "body": "Hola {{1}}, le recordamos que su cuota #{{2}} por ${{3}} vence el {{4}}.",
        })

    def test_phone_sanitization(self):
        """Verifica que números de 8 dígitos de Panamá reciban el código 507 automáticamente."""
        self.assertEqual(self.account.clean_phone_number("6123-4567"), "50761234567")
        self.assertEqual(self.account.clean_phone_number("+507 6123-4567"), "50761234567")
        self.assertEqual(self.account.clean_phone_number("50761234567"), "50761234567")

    def test_template_variables_auto_detection(self):
        """Al escribir un texto con {{1}}, {{2}}, etc. se detectan las variables automáticamente."""
        self.template._onchange_body()
        self.assertEqual(len(self.template.variable_ids), 4)
        indices = [v.sequence for v in self.template.variable_ids]
        self.assertEqual(indices, [1, 2, 3, 4])

    @patch("requests.post")
    def test_send_template_success(self, mock_post):
        """Simula una respuesta exitosa de Meta Graph API y verifica el guardado del wamid."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "messaging_product": "whatsapp",
            "contacts": [{"input": "50761234567", "wa_id": "50761234567"}],
            "messages": [{"id": "wamid.HBgLNT..."}],
        }
        mock_post.return_value = mock_response

        msg = self.template.send_to_phone(
            phone="61234567",
            custom_params=["Pedro Atrasado", "3", "125.00", "15/10/2026"],
        )
        self.assertEqual(msg.state, "sent")
        self.assertEqual(msg.msg_uid, "wamid.HBgLNT...")
        self.assertEqual(msg.mobile_number, "50761234567")
        self.assertIn("Pedro Atrasado", msg.body)
        self.assertIn("$125.00", msg.body)

    def test_blocklist_prevents_sending(self):
        """Verifica que números en lista negra aborten el envío inmediatamente."""
        self.env["whatsapp.blocklist"].create({
            "mobile_number": "50769998877",
            "reason": "Solicitó no recibir más mensajes",
        })
        msg = self.template.send_to_phone(
            phone="69998877",
            custom_params=["Juan", "1", "100.00", "20/10/2026"],
        )
        self.assertEqual(msg.state, "error")
        self.assertIn("lista negra", msg.error_msg)

    @patch("requests.get")
    def test_account_connection_test(self, mock_get):
        """Verifica la validación de credenciales contra Meta."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "display_phone_number": "+507 6000-1122",
            "quality_rating": "GREEN",
            "name_status": "APPROVED",
        }
        mock_get.return_value = mock_response

        action = self.account.action_test_connection()
        self.assertEqual(action.get("type"), "ir.actions.client")
        self.assertEqual(action.get("tag"), "display_notification")

    def test_webhook_verification_token(self):
        """Verifica que la cuenta genere un webhook verify token válido y único."""
        self.assertTrue(bool(self.account.webhook_verify_token))
        self.assertIn("/whatsapp/webhook/", self.account.callback_url)

    def test_composer_and_partner_action(self):
        """Verifica que el contacto abra el wizard y genere la vista previa."""
        partner = self.env["res.partner"].create({"name": "Cliente Prueba", "phone": "61234567"})
        action = partner.action_open_whatsapp_composer()
        self.assertEqual(action.get("res_model"), "whatsapp.composer")
        self.assertEqual(action.get("target"), "new")

        composer = self.env["whatsapp.composer"].create({
            "partner_id": partner.id,
            "phone": partner.phone,
            "wa_account_id": self.account.id,
            "template_id": self.template.id,
        })
        self.assertIn("Cliente Prueba", composer.body_preview)

    def test_template_buttons_and_preview(self):
        """Verifica la creación de botones y la previsualización del mensaje."""
        btn = self.env["whatsapp.template.button"].create({
            "template_id": self.template.id,
            "button_type": "url",
            "name": "Ver Mi Cuenta",
            "url_or_phone": "https://motorgroupte.com/portal",
        })
        self.assertEqual(btn.button_type, "url")
        self.template._onchange_body()
        preview = self.env["whatsapp.preview"].create({
            "template_id": self.template.id,
        })
        self.assertTrue(bool(preview.rendered_body))

    @patch("requests.post")
    def test_submit_for_approval_success(self, mock_post):
        """Simula envío de plantilla a Meta para aprobación."""
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {"id": "123456789", "status": "PENDING"}
        mock_post.return_value = mock_res

        action = self.template.action_submit_for_approval()
        self.assertEqual(self.template.status, "pending")
        self.assertEqual(action.get("type"), "ir.actions.client")

    def test_variable_name_formatting_and_model(self):
        """Verifica que escribir '1' se convierta a '{{1}}' y que se propague el modelo."""
        var = self.env["whatsapp.template.variable"].new({
            "name": "1",
            "template_id": self.template.id,
        })
        var._onchange_name()
        self.assertEqual(var.name, "{{1}}")
        self.assertEqual(var.sequence, 1)

    def test_action_add_variable(self):
        """Verifica que al hacer clic en '+ variable' se inserte el siguiente {{n}} correlativo."""
        new_template = self.env["whatsapp.template"].create({
            "name": "Template Test Var",
            "template_name": "template_test_var",
            "wa_account_id": self.account.id,
            "body": "Hola",
        })
        new_template.action_add_variable()
        self.assertIn("{{1}}", new_template.body)
        self.assertEqual(len(new_template.variable_ids), 1)

        new_template.action_add_variable()
        self.assertIn("{{2}}", new_template.body)
        self.assertEqual(len(new_template.variable_ids), 2)

    @patch("requests.post")
    def test_send_template_syncs_to_discuss_channel(self, mock_post):
        """Verifica que enviar una plantilla cree el chat en Conversaciones y publique el mensaje."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "messaging_product": "whatsapp",
            "messages": [{"id": "wamid.TEST_DISCUSS_123"}],
        }
        mock_post.return_value = mock_response

        partner = self.env["res.partner"].create({
            "name": "Cliente Conversacion",
            "phone": "68889900",
        })

        self.template.send_to_phone(
            phone="68889900",
            custom_params=["Cliente Conversacion", "1", "50.00", "01/11/2026"],
            record=partner,
        )

        channel = self.env["discuss.channel"].search([("whatsapp_number", "=", "50768889900")], limit=1)
        self.assertTrue(bool(channel))
        self.assertEqual(channel.whatsapp_partner_id, partner)
        self.assertEqual(channel.whatsapp_account_id, self.account)
        last_msg = channel.message_ids.sorted("id", reverse=True)[0]
        self.assertIn("Cliente Conversacion", last_msg.body)

    @patch("requests.post")
    def test_discuss_channel_outgoing_text(self, mock_post):
        """Verifica que al escribir en el chat de Conversaciones se despache el texto libre a Meta."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "messaging_product": "whatsapp",
            "messages": [{"id": "wamid.REPLY_META_456"}],
        }
        mock_post.return_value = mock_response

        partner = self.env["res.partner"].create({
            "name": "Cliente Chat",
            "phone": "67778899",
        })
        channel = self.env["discuss.channel"]._get_or_create_chat(partners_to=[partner.id], pin=True)
        channel.write({
            "whatsapp_number": "50767778899",
            "whatsapp_partner_id": partner.id,
            "whatsapp_account_id": self.account.id,
        })

        # Usuario interno escribe en el canal
        msg = channel.message_post(body="Hola cliente, recibimos su pago.", message_type="comment")
        self.assertTrue(mock_post.called)
        # Se creó el registro en whatsapp.message
        wa_msg = self.env["whatsapp.message"].search([("msg_uid", "=", "wamid.REPLY_META_456")], limit=1)
        self.assertTrue(bool(wa_msg))
        self.assertEqual(wa_msg.mobile_number, "50767778899")
        self.assertEqual(wa_msg.body, "Hola cliente, recibimos su pago.")

    @patch("requests.get")
    def test_download_media_success(self, mock_get):
        """Simula la descarga de una imagen multimedia de Meta Graph API."""
        # Paso 1: mock de consulta de metadata
        mock_res1 = MagicMock()
        mock_res1.status_code = 200
        mock_res1.json.return_value = {"url": "https://cdn.whatsapp.com/test.jpg", "mime_type": "image/jpeg"}
        # Paso 2: mock de descarga de bytes CDN
        mock_res2 = MagicMock()
        mock_res2.status_code = 200
        mock_res2.content = b"FAKE_IMAGE_BYTES"
        mock_get.side_effect = [mock_res1, mock_res2]

        raw_bytes, mime = self.account.download_media("MEDIA_ID_123")
        self.assertEqual(raw_bytes, b"FAKE_IMAGE_BYTES")
        self.assertEqual(mime, "image/jpeg")

    @patch("requests.get")
    def test_retry_media_download_success(self, mock_get):
        """Verifica que el botón de reintento descargue la imagen y cree el canal 1-a-1 sin UserError."""
        mock_res1 = MagicMock()
        mock_res1.status_code = 200
        mock_res1.json.return_value = {"url": "https://cdn.whatsapp.com/test.jpg", "mime_type": "image/jpeg"}
        mock_res2 = MagicMock()
        mock_res2.status_code = 200
        mock_res2.content = b"RECOVERED_IMAGE_BYTES"
        mock_get.side_effect = [mock_res1, mock_res2]

        partner = self.env["res.partner"].create({
            "name": "Cliente Reintento",
            "phone": "+50767082995",
        })
        msg = self.env["whatsapp.message"].create({
            "wa_account_id": self.account.id,
            "mobile_number": "50767082995",
            "partner_id": partner.id,
            "body": "📷 Foto de comprobante",
            "msg_type": "inbound",
            "state": "delivered",
            "msg_uid": "wamid.TEST_RETRY_123",
            "media_id": "MEDIA_ID_RETRY",
            "media_type": "image",
            "media_status": "failed",
            "media_error": "Connection timeout",
        })

        res = msg.action_retry_media_download()
        self.assertTrue(res)
        self.assertEqual(msg.media_status, "downloaded")
        self.assertFalse(msg.media_error)
        self.assertTrue(bool(msg.attachment_id))

        # Verificar que el canal creado sea estrictamente de 2 personas
        channel = self.env["discuss.channel"].search([("whatsapp_number", "=", "50767082995")], limit=1)
        self.assertTrue(bool(channel))
        self.assertEqual(len(channel.channel_member_ids), 2)

    @patch("requests.get")
    def test_cron_retry_failed_media_downloads(self, mock_get):
        """Verifica que el cron procese los mensajes fallidos de los últimos días."""
        mock_res1 = MagicMock()
        mock_res1.status_code = 200
        mock_res1.json.return_value = {"url": "https://cdn.whatsapp.com/test.jpg", "mime_type": "image/jpeg"}
        mock_res2 = MagicMock()
        mock_res2.status_code = 200
        mock_res2.content = b"CRON_RECOVERED_BYTES"
        mock_get.side_effect = [mock_res1, mock_res2]

        msg = self.env["whatsapp.message"].create({
            "wa_account_id": self.account.id,
            "mobile_number": "50767082996",
            "body": "📷 Comprobante cron",
            "msg_type": "inbound",
            "state": "delivered",
            "msg_uid": "wamid.TEST_CRON_456",
            "media_id": "MEDIA_ID_CRON",
            "media_type": "image",
            "media_status": "pending",
        })

        self.env["whatsapp.message"].cron_retry_failed_media_downloads()
        msg.invalidate_recordset()
        self.assertEqual(msg.media_status, "downloaded")
        self.assertTrue(bool(msg.attachment_id))



