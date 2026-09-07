# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager

class HelpdeskPortal(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super(HelpdeskPortal, self)._prepare_home_portal_values(counters)
        if 'ticket_count' in counters:
            partner = request.env.user.partner_id
            ticket_count = request.env['helpdesk.ticket.pro'].search_count([
                ('partner_id', '=', partner.id)
            ])
            values['ticket_count'] = ticket_count
        return values

    @http.route(['/my/tickets', '/my/tickets/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_tickets(self, page=1, date_begin=None, date_end=None, sortby=None, **kw):
        values = self._prepare_portal_layout_values()
        partner = request.env.user.partner_id
        TicketObj = request.env['helpdesk.ticket.pro']
        
        domain = [('partner_id', '=', partner.id)]
        
        ticket_count = TicketObj.search_count(domain)
        pager = portal_pager(
            url="/my/tickets",
            total=ticket_count,
            page=page,
            step=10
        )
        
        tickets = TicketObj.search(domain, limit=10, offset=pager['offset'], order="create_date desc")
        values.update({
            'tickets': tickets,
            'page_name': 'ticket',
            'pager': pager,
            'default_url': '/my/tickets',
        })
        return request.render("helpdesk_ticket_pro.portal_my_tickets", values)

    @http.route(['/my/ticket/<int:ticket_id>'], type='http', auth="user", website=True)
    def portal_my_ticket_detail(self, ticket_id, **kw):
        ticket = request.env['helpdesk.ticket.pro'].browse(ticket_id)
        if not ticket.exists() or ticket.partner_id != request.env.user.partner_id:
            return request.redirect('/my/home')
            
        values = {
            'ticket': ticket,
            'page_name': 'ticket_detail',
        }
        return request.render("helpdesk_ticket_pro.portal_my_ticket_detail", values)

    @http.route(['/my/ticket/new'], type='http', auth="user", website=True)
    def portal_new_ticket(self, **kw):
        categories = request.env['helpdesk.ticket.category'].search([])
        values = {
            'categories': categories,
            'page_name': 'new_ticket',
        }
        return request.render("helpdesk_ticket_pro.portal_create_ticket", values)

    @http.route(['/my/ticket/create'], type='http', auth="user", methods=['POST'], website=True, csrf=True)
    def portal_create_ticket_submit(self, **kw):
        subject = kw.get('subject')
        if not subject:
            return request.redirect('/my/ticket/new?error=missing_subject')
            
        ticket_vals = {
            'subject': subject,
            'description': kw.get('description'),
            'ticket_type': kw.get('ticket_type', 'question'),
            'priority': kw.get('priority', '1'),
            'category_id': int(kw.get('category_id')) if kw.get('category_id') else False,
            'partner_id': request.env.user.partner_id.id,
        }
        
        ticket = request.env['helpdesk.ticket.pro'].sudo().create(ticket_vals)
        return request.redirect('/my/ticket/%s' % ticket.id)

