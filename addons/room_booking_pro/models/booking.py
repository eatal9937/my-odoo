from odoo import models, fields, api
from odoo.exceptions import ValidationError

class RoomBooking(models.Model):
    _name = 'room.booking.pro'
    _description = 'ระบบจองห้องประชุม'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'name'

    name = fields.Char(string='หัวข้อการประชุม', required=True)
    room_name = fields.Selection([
        ('room_a', 'ห้องประชุม A (เล็ก 4-6 คน)'),
        ('room_b', 'ห้องประชุม B (ใหญ่ 10-15 คน)'),
        ('board', 'ห้องผู้บริหาร (Boardroom)')
    ], string='ห้องที่จอง', required=True)
    
    user_id = fields.Many2one('res.users', string='ผู้จอง', default=lambda self: self.env.user)
    start_datetime = fields.Datetime(string='เริ่มเวลา', required=True)
    end_datetime = fields.Datetime(string='สิ้นสุดเวลา', required=True)
    
    need_projector = fields.Boolean(string='ต้องการโปรเจคเตอร์')
    need_snack = fields.Boolean(string='รับเบรค/กาแฟ')
    note = fields.Text(string='รายละเอียดเพิ่มเติม')
    
    state = fields.Selection([
        ('draft', 'รอยืนยัน'),
        ('confirmed', 'อนุมัติแล้ว'),
        ('cancelled', 'ไม่อนุมัติ/ยกเลิก')
    ], string='สถานะ', default='draft')

    color = fields.Integer(string='Color Index', compute='_compute_color', store=True)

    @api.constrains('room_name', 'start_datetime', 'end_datetime', 'state')
    def _check_overlapping_bookings(self):
        for record in self:
            if record.state == 'cancelled':
                continue
            if not record.start_datetime or not record.end_datetime:
                continue
            if record.start_datetime >= record.end_datetime:
                raise ValidationError("Start time must be before end time.")

            # Search for overlapping bookings for the same room
            overlap = self.env['room.booking.pro'].search([
                ('id', '!=', record.id),
                ('room_name', '=', record.room_name),
                ('state', '!=', 'cancelled'),
                ('start_datetime', '<', record.end_datetime),
                ('end_datetime', '>', record.start_datetime)
            ], limit=1)

            if overlap:
                # Format to user's timezone (usually UTC+7 / Thai time)
                # Convert start/end to display format
                start_str = fields.Datetime.context_timestamp(record, overlap.start_datetime).strftime('%d/%m/%Y %H:%M')
                end_str = fields.Datetime.context_timestamp(record, overlap.end_datetime).strftime('%d/%m/%Y %H:%M')
                raise ValidationError(
                    f"ไม่สามารถจองห้องซ้ำได้! ห้องนี้ถูกจองแล้วสำหรับหัวข้อ "
                    f"'{overlap.name}' โดย {overlap.user_id.name or 'ผู้ใช้อื่น'} "
                    f"ในช่วงเวลา {start_str} - {end_str} น."
                )

    def action_confirm(self):
        for record in self:
            record.state = 'confirmed'
            record.color = 10
            # Remove any pending admin activities
            record.activity_ids.unlink()
            # Post message to follower/booker
            if record.user_id:
                record.message_post(
                    body=f'✅ การจองห้อง <b>{record.room_name}</b> ของคุณได้รับการอนุมัติเรียบร้อยแล้วครับ!',
                    partner_ids=[record.user_id.partner_id.id]
                )

    def action_cancel(self):
        for rec in self:
            rec.state = 'cancelled'

    def action_draft(self):
        for rec in self:
            rec.state = 'draft'

    @api.depends('state')
    def _compute_color(self):
        for record in self:
            if record.state == 'confirmed':
                record.color = 10  # Green
            elif record.state == 'cancelled':
                record.color = 1   # Red
            else:
                record.color = 4   # Light Blue

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        admin_group = self.env.ref('room_booking_pro.group_room_admin', raise_if_not_found=False)
        for record in records:
            if admin_group:
                for admin in admin_group.users:
                    record.activity_schedule(
                        'mail.mail_activity_data_todo',
                        user_id=admin.id,
                        summary='มีรายการจองห้องรออนุมัติ!',
                        note=f'กรุณาตรวจสอบการจองห้อง: {record.room_name}'
                    )
        return records
