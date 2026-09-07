from odoo import models, fields, api

class HelpdeskTicket(models.Model):
    _name = 'helpdesk.ticket'
    _description = 'Helpdesk Ticket'
    # ดึงระบบของ Odoo มาใช้เพื่อทำกล่องคอมเมนต์ (Chatter) และประวัติการแก้ไข
    _inherit = ['mail.thread', 'mail.activity.mixin'] 

    name = fields.Char(string='หัวข้อ / อาการเสีย', required=True, tracking=True)
    description = fields.Text(string='รายละเอียดเพิ่มเติม')
    customer_id = fields.Many2one('res.partner', string='ผู้แจ้ง', tracking=True)
    
    # Custom Fields สำหรับสาย Network Engineer
    device_type = fields.Selection([
        ('switch', 'Switch / Core Switch'),
        ('router', 'Router / Firewall'),
        ('ap', 'Access Point / WiFi'),
        ('server', 'Server / PC'),
        ('other', 'อื่นๆ')
    ], string='ประเภทอุปกรณ์', tracking=True)
    
    device_ip = fields.Char(string='IP Address', tracking=True)

    priority = fields.Selection([
        ('0', 'Low (ทั่วไป)'),
        ('1', 'Medium (กระทบการทำงาน)'),
        ('2', 'High (กระทบวงกว้าง)'),
        ('3', 'Critical (ระบบล่ม)')
    ], string='ความสำคัญ', default='0', tracking=True)

    state = fields.Selection([
        ('new', 'New (เคสใหม่)'),
        ('progress', 'In Progress (กำลังตรวจสอบ)'),
        ('pending', 'Pending (รอ Vendor/อะไหล่)'),
        ('solved', 'Solved (ปิดงาน)'),
        ('cancel', 'Cancelled (ยกเลิก)')
    ], string='สถานะ', default='new', tracking=True)
