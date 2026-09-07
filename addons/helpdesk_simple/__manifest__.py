{
    'name': 'Custom Helpdesk Simple',
    'version': '17.0.1.0',
    'category': 'Services/Helpdesk',
    'summary': 'ระบบจัดการแจ้งซ่อม IT Support สำหรับ Network Engineer',
    'depends': ['base', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/ticket_views.xml',
    ],
    'installable': True,
    'application': True,
}
