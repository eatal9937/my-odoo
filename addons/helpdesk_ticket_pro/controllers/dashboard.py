import os
import json
from datetime import datetime, time
from odoo import http, fields
from odoo.http import request

class HelpdeskDashboardController(http.Controller):
    @http.route('/helpdesk/dashboard', type='http', auth='public', website=False)
    def helpdesk_dashboard(self, **kwargs):
        addon_path = os.path.dirname(os.path.dirname(__file__))
        html_path = os.path.join(addon_path, 'static', 'src', 'dashboard.html')
        if os.path.exists(html_path):
            with open(html_path, 'r', encoding='utf-8') as f:
                html_content = f.read()
            return request.make_response(html_content, headers=[('Content-Type', 'text/html')])
        return "Helpdesk Dashboard HTML file not found."

    @http.route('/helpdesk/api/dashboard_data', type='http', auth='public', methods=['GET'], csrf=False)
    def helpdesk_api_dashboard_data(self, **kwargs):
        # Fetch active tickets
        tickets_model = request.env['helpdesk.ticket.pro'].sudo()
        
        # Calculate start of today (UTC)
        today_start = datetime.combine(datetime.today(), time.min)
        
        # KPI Counters
        count_new = tickets_model.search_count([('state', '=', 'new')])
        count_in_progress = tickets_model.search_count([('state', '=', 'in_progress')])
        count_waiting = tickets_model.search_count([('state', '=', 'waiting')])
        count_solved_today = tickets_model.search_count([
            ('state', 'in', ['solved', 'closed']),
            ('write_date', '>=', today_start)
        ])
        count_urgent = tickets_model.search_count([
            ('priority', '=', '3'),
            ('state', 'not in', ['solved', 'closed'])
        ])

        # Recent / Active tickets queue
        # Sort by priority desc (Urgent first), then create_date desc
        active_tickets = tickets_model.search([
            ('state', 'in', ['new', 'in_progress', 'waiting'])
        ], order='priority desc, create_date desc', limit=15)

        tickets_list = []
        for t in active_tickets:
            tickets_list.append({
                'id': t.id,
                'ref': t.name,
                'subject': t.subject,
                'category': t.category_id.name or 'General',
                'priority': t.priority,
                'state': t.state,
                'assigned_to': t.user_id.name or 'Unassigned',
                'customer': t.partner_id.name or 'Portal / Public',
                'create_date': t.create_date.isoformat() + 'Z' if t.create_date else None,
                'deadline': t.deadline.isoformat() + 'Z' if t.deadline else None
            })

        # Agent Workload Calculation
        agents = request.env['res.users'].sudo().search([])
        agent_workload = []
        for agent in agents:
            ticket_count = tickets_model.search_count([
                ('user_id', '=', agent.id),
                ('state', 'in', ['new', 'in_progress', 'waiting'])
            ])
            if ticket_count > 0:
                agent_workload.append({
                    'agent_name': agent.name,
                    'active_tickets': ticket_count
                })
        
        # Sort agents by ticket count desc
        agent_workload = sorted(agent_workload, key=lambda x: x['active_tickets'], reverse=True)

        result = {
            'current_time': fields.Datetime.now().isoformat() + 'Z',
            'kpis': {
                'new': count_new,
                'in_progress': count_in_progress,
                'waiting': count_waiting,
                'solved_today': count_solved_today,
                'urgent': count_urgent
            },
            'tickets': tickets_list,
            'agent_workload': agent_workload
        }

        return request.make_response(
            json.dumps(result),
            headers=[('Content-Type', 'application/json')]
        )
