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
