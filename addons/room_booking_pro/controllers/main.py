import os
import json
from datetime import datetime, timedelta
from odoo import http, fields
from odoo.http import request

class RoomBookingController(http.Controller):
    @http.route('/room_booking/display', type='http', auth='public', website=False)
    def room_booking_display(self, **kwargs):
        addon_path = os.path.dirname(os.path.dirname(__file__))
        html_path = os.path.join(addon_path, 'static', 'src', 'display.html')
        if os.path.exists(html_path):
            with open(html_path, 'r', encoding='utf-8') as f:
                html_content = f.read()
            return request.make_response(html_content, headers=[('Content-Type', 'text/html')])
        return "Display HTML file not found."

    @http.route('/room_booking/api/bookings', type='http', auth='public', methods=['GET'], csrf=False)
    def room_booking_api_bookings(self, room=None, **kwargs):
        if not room:
            room = request.httprequest.args.get('room')
        
        if not room:
            return request.make_response(
                json.dumps({'error': 'Missing room parameter'}),
                headers=[('Content-Type', 'application/json')]
            )

        now_utc = fields.Datetime.now()
        time_limit_start = now_utc - timedelta(hours=2)
        time_limit_end = now_utc + timedelta(hours=24)

        bookings = request.env['room.booking.pro'].sudo().search([
            ('room_name', '=', room),
            ('state', '!=', 'cancelled'),
            ('end_datetime', '>=', time_limit_start),
            ('start_datetime', '<=', time_limit_end)
        ], order='start_datetime asc')

        result = {
            'room_name': room,
            'current_time': now_utc.isoformat() + 'Z',
            'current_meeting': None,
            'upcoming_meetings': []
        }

        for b in bookings:
            start_iso = b.start_datetime.isoformat() + 'Z'
            end_iso = b.end_datetime.isoformat() + 'Z'
            meeting_info = {
                'id': b.id,
                'name': b.name,
                'user_name': b.user_id.name or 'Unknown',
                'start': start_iso,
                'end': end_iso,
                'state': b.state
            }
            if b.start_datetime <= now_utc <= b.end_datetime:
                result['current_meeting'] = meeting_info
            elif b.start_datetime > now_utc:
                result['upcoming_meetings'].append(meeting_info)

        return request.make_response(
            json.dumps(result),
            headers=[('Content-Type', 'application/json')]
        )
