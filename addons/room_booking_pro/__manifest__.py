{
    'name': 'Odoo Room Booking Pro',
    'version': '17.0.1.0',
    'summary': 'ระบบจองห้องประชุมระดับองค์กร',
    'description': 'Custom Room Booking Module by Captain',
    'author': 'Captain',
    'category': 'Productivity',
    'depends': ['base', 'mail'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/booking_views.xml',
    ],
    'installable': True,
    'application': True,
    'icon': '/base/static/description/icon.png',
}
