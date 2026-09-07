import os
import json
from datetime import datetime, time
from odoo import http, fields
from odoo.http import request

class HelpdeskDashboardController(http.Controller):
    @http.route("/helpdesk/dashboard", type="http", auth="public", website=False)
    def helpdesk_dashboard(self, **kwargs):
        addon_path = os.path.dirname(os.path.dirname(__file__))
        html_path = os.path.join(addon_path, "static", "src", "dashboard.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            return request.make_response(html_content, headers=[("Content-Type", "text/html"), ("Cache-Control", "no-cache")])
        return "Helpdesk Dashboard HTML file not found."

    @http.route("/helpdesk/api/dashboard_data", type="http", auth="public", methods=["GET"], csrf=False)
    def helpdesk_api_dashboard_data(self, **kwargs):
        tickets_model = request.env["helpdesk.ticket.pro"].sudo()
        
        today_start = datetime.combine(datetime.today(), time.min)
        
        # 1. KPI Counters - strictly separating ACTIVE (unclosed) from CLOSED
        count_new = tickets_model.search_count([("state", "=", "new")])
        count_in_progress = tickets_model.search_count([("state", "=", "in_progress")])
        count_waiting = tickets_model.search_count([("state", "=", "waiting")])
        count_active = count_new + count_in_progress + count_waiting
        
        # P1 Outage (strictly active and priority 3 or severity p1)
        count_p1 = tickets_model.search_count([
            "&",
            ("state", "not in", ["solved", "closed"]),
            "|", ("priority", "=", "3"), ("avaya_severity", "=", "p1")
        ])
        
        # P2 Major (strictly active and priority 2 or severity p2)
        count_p2 = tickets_model.search_count([
            "&",
            ("state", "not in", ["solved", "closed"]),
            "|", ("priority", "=", "2"), ("avaya_severity", "=", "p2")
        ])

        # Active Avaya TAC tickets
        count_avaya = tickets_model.search_count([
            ("is_avaya_support", "=", True),
            ("state", "not in", ["solved", "closed"])
        ])
        
        # Active Customer Direct tickets
        count_customer = tickets_model.search_count([
            ("is_avaya_support", "=", False),
            ("state", "not in", ["solved", "closed"])
        ])

        # Closed Tickets
        count_closed_total = tickets_model.search_count([
            ("state", "in", ["solved", "closed"])
        ])
        count_solved_today = tickets_model.search_count([
            ("state", "in", ["solved", "closed"]),
            ("write_date", ">=", today_start)
        ])
        count_total = tickets_model.search_count([])

        # 2. Fetch tickets: active first, then recent closed tickets
        tickets = tickets_model.search([], order="priority desc, write_date desc", limit=150)

        tickets_list = []
        product_counts = {}
        customer_counts = {}

        for t in tickets:
            c_name = t.partner_id.name or t.avaya_customer_name or ""
            prod_name = t.avaya_product or t.category_id.name or "General"
            
            # Count products and customers for ACTIVE tickets only
            if t.state not in ["solved", "closed"]:
                if prod_name:
                    product_counts[prod_name] = product_counts.get(prod_name, 0) + 1
                if c_name:
                    customer_counts[c_name] = customer_counts.get(c_name, 0) + 1

            tickets_list.append({
                "id": t.id,
                "ref": t.name,
                "subject": t.subject,
                "category": t.category_id.name or "General",
                "priority": t.priority or "0",
                "state": t.state or "new",
                "ticket_source": t.ticket_source or "customer",
                "is_avaya_support": bool(t.is_avaya_support),
                "avaya_sr_number": t.avaya_sr_number or "",
                "sold_to_id": t.sold_to_id or "",
                "avaya_product": t.avaya_product or "",
                "avaya_severity": t.avaya_severity or "",
                "avaya_status": t.avaya_status or "",
                "avaya_contact_name": t.avaya_contact_name or "",
                "avaya_contact_phone": t.avaya_contact_phone or "",
                "avaya_contact_email": t.avaya_contact_email or "",
                "avaya_portal_url": t.avaya_portal_url or "",
                "assigned_to": t.user_id.name or "Unassigned",
                "customer": c_name or "Not Specified",
                "create_date": t.create_date.isoformat() + "Z" if t.create_date else None,
                "write_date": t.write_date.isoformat() + "Z" if t.write_date else None,
                "deadline": t.deadline.isoformat() + "Z" if t.deadline else None,
                "portal_url": f"/web#id={t.id}&model=helpdesk.ticket.pro&view_type=form"
            })

        # Agent Workload (Active tickets only)
        agents = request.env["res.users"].sudo().search([("share", "=", False)])
        agent_workload = []
        for agent in agents:
            ticket_count = tickets_model.search_count([
                ("user_id", "=", agent.id),
                ("state", "in", ["new", "in_progress", "waiting"])
            ])
            if ticket_count > 0:
                agent_workload.append({
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "active_tickets": ticket_count
                })
        agent_workload = sorted(agent_workload, key=lambda x: x["active_tickets"], reverse=True)

        result = {
            "current_time": fields.Datetime.now().isoformat() + "Z",
            "kpis": {
                "total_active": count_active,
                "p1_critical": count_p1,
                "p2_major": count_p2,
                "new": count_new,
                "in_progress": count_in_progress,
                "waiting": count_waiting,
                "avaya_active": count_avaya,
                "customer_active": count_customer,
                "closed_total": count_closed_total,
                "solved_today": count_solved_today,
                "total_all": count_total,
                "urgent": count_p1
            },
            "products": sorted([{"name": k, "count": v} for k, v in product_counts.items()], key=lambda x: x["count"], reverse=True)[:10],
            "customers": sorted([{"name": k, "count": v} for k, v in customer_counts.items()], key=lambda x: x["count"], reverse=True)[:10],
            "tickets": tickets_list,
            "agent_workload": agent_workload
        }

        return request.make_response(
            json.dumps(result),
            headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
        )
