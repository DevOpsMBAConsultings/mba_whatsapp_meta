# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "MBA - WhatsApp Meta Cloud API",
    "version": "19.0.1.0.0",
    "category": "Discuss",
    "summary": "Integración oficial con Meta WhatsApp Cloud API para Odoo Community (Cuentas, Plantillas y Mensajes)",
    "author": "MBA Consultings, Brooks Gonzalez",
    "website": "https://mbaconsultings.com",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "wizard/whatsapp_composer_views.xml",
        "wizard/whatsapp_preview_views.xml",
        "views/whatsapp_message_views.xml",
        "views/whatsapp_template_views.xml",
        "views/whatsapp_blocklist_views.xml",
        "views/whatsapp_account_views.xml",
        "views/res_partner_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "mba_whatsapp_meta/static/src/scss/phone_icons.scss",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
