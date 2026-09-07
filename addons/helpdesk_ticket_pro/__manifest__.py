{
    'name': 'Odoo Helpdesk Pro (Ticketing)',
    'version': '17.0.1.0',
    'category': 'Services/Helpdesk',
    'summary': 'ระบบจัดการ Ticket มาตรฐานระดับ Enterprise สำหรับงาน Support',
    'depends': ['base', 'mail', 'board'],
    'data': [
        'security/helpdesk_ticket_pro_groups.xml',
        'security/helpdesk_security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'data/mail_template_data.xml',
        'views/ticket_pro_views.xml',
        'views/helpdesk_dashboard_views.xml', 
        'views/portal_templates.xml',
    ],
    'installable': True,
    'application': True,
}
