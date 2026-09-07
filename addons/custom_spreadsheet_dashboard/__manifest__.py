# -*- coding: utf-8 -*-
{
    'name': 'Helpdesk Spreadsheet Dashboard (Separate)',
    'version': '1.0',
    'category': 'Services/Helpdesk',
    'summary': 'Separate Spreadsheet Dashboard for Helpdesk stats without affecting the main dashboard.',
    'description': """
        This module creates a dedicated Spreadsheet Dashboard group and a custom spreadsheet 
        for tracking Helpdesk Tickets. It is completely isolated from the standard dashboard layout.
    """,
    'depends': ['spreadsheet_dashboard', 'helpdesk_ticket_pro'],
    'data': [
        'data/spreadsheet_dashboard_data.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
