from odoo import models, fields, api
from datetime import timedelta

class HelpdeskTicketCategory(models.Model):
    _name = 'helpdesk.ticket.category'
    _description = 'Ticket Category'
    
    name = fields.Char('Category Name', required=True)
    active = fields.Boolean('Active', default=True)
    sequence = fields.Integer('Sequence', default=10)

class HelpdeskTicketTag(models.Model):
    _name = 'helpdesk.ticket.tag'
    _description = 'Ticket Tag'
    
    name = fields.Char('Tag Name', required=True)
    color = fields.Integer('Color Index', default=0)
    active = fields.Boolean('Active', default=True)

class HelpdeskSlaPolicy(models.Model):
    _name = 'helpdesk.sla.policy'
    _description = 'SLA Policy'
    
    name = fields.Char('Policy Name', required=True)
    ticket_type = fields.Selection([
        ('question', 'Question'), 
        ('incident', 'Incident'), 
        ('request', 'Service Request')
    ], string='Apply on Type')
    priority = fields.Selection([
        ('0', 'Low'), 
        ('1', 'Medium'), 
        ('2', 'High'), 
        ('3', 'Urgent')
    ], string='Apply on Priority')
    target_hours = fields.Float('Target (Hours)', required=True, default=24.0)
    active = fields.Boolean('Active', default=True)

class HelpdeskTicketPro(models.Model):
    _name = 'helpdesk.ticket.pro'
    _description = 'Professional Helpdesk Ticket'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']

    access_url = fields.Char('Portal Access URL', compute='_compute_access_url')

    def _compute_access_url(self):
        for ticket in self:
            ticket.access_url = '/my/ticket/%s' % ticket.id


    def _default_user_id(self):
        phongthep = self.env['res.users'].sudo().search([
            '|', ('login', '=', 'phongthep@jadscomm.com'),
            ('email', '=', 'phongthep@jadscomm.com')
        ], limit=1)
        if phongthep:
            return phongthep.id
        if self.env.user.id != 1:
            return self.env.user.id
        return False

    name = fields.Char(string='Ticket Ref.', required=True, copy=False, readonly=True, default=lambda self: 'New')
    subject = fields.Char(string='Subject', required=True, tracking=True)
    description = fields.Html(string='Description')
    
    partner_id = fields.Many2one('res.partner', string='Customer', tracking=True)
    user_id = fields.Many2one('res.users', string='Assigned To', tracking=True, default=_default_user_id)
    
    category_id = fields.Many2one('helpdesk.ticket.category', string='Category', tracking=True)
    tag_ids = fields.Many2many('helpdesk.ticket.tag', string='Tags')
    
    ticket_type = fields.Selection([
        ('question', 'Question'), 
        ('incident', 'Incident'), 
        ('request', 'Service Request')
    ], string='Type', default='incident', tracking=True)
    
    priority = fields.Selection([
        ('0', 'Low'), 
        ('1', 'Medium'), 
        ('2', 'High'), 
        ('3', 'Urgent')
    ], string='Priority', default='0', tracking=True)
    
    state = fields.Selection([
        ('new', 'New'),
        ('in_progress', 'In Progress'),
        ('waiting', 'Waiting for Customer'),
        ('solved', 'Solved'),
        ('closed', 'Closed')
    ], string='Stage', default='new', tracking=True, group_expand='_read_group_state')
    
    deadline = fields.Datetime(string='SLA Deadline', tracking=True)

    # Source / Channel (Support both Customer & Avaya cases)
    ticket_source = fields.Selection([
        ('customer', 'Customer Direct / Portal'),
        ('phone_call', 'Voice PBX Recording'),
        ('avaya_support', 'Avaya TAC Support'),
        ('email', 'Email Notification'),
        ('internal', 'Internal / Manual')
    ], string='Source Channel', default='customer', tracking=True)

    # Avaya Support Integration Fields (Optional / Conditional)
    is_avaya_support = fields.Boolean(string='Avaya Support Case', default=False, tracking=True)
    avaya_sr_number = fields.Char(string='Avaya SR #', tracking=True, index=True)
    sold_to_id = fields.Char(string='Sold To #', tracking=True, index=True)
    avaya_product = fields.Char(string='Avaya Product / Solution', tracking=True)
    avaya_asset_id = fields.Char(string='Asset ID', tracking=True)
    avaya_severity = fields.Selection([
        ('p1', 'P1 - Critical (System Down)'),
        ('p2', 'P2 - Major (Severe Impact)'),
        ('p3', 'P3 - Minor (Degraded)'),
        ('p4', 'P4 - Informational')
    ], string='Avaya Severity', tracking=True)
    avaya_status = fields.Char(string='Avaya Status', tracking=True, default='Assigned')
    avaya_portal_url = fields.Char(string='Avaya Portal URL', compute='_compute_avaya_portal_url')

    @api.depends('avaya_sr_number')
    def _compute_avaya_portal_url(self):
        for record in self:
            if record.avaya_sr_number:
                clean_sr = record.avaya_sr_number.strip()
                record.avaya_portal_url = f"https://support.avaya.com/service-request/{clean_sr}"
            else:
                record.avaya_portal_url = False

    @api.onchange('avaya_sr_number')
    def _onchange_avaya_sr_number(self):
        for record in self:
            if record.avaya_sr_number:
                record.is_avaya_support = True
                record.ticket_source = 'avaya_support'

    @api.onchange('avaya_severity')
    def _onchange_avaya_severity(self):
        sev_map = {'p1': '3', 'p2': '2', 'p3': '1', 'p4': '0'}
        for record in self:
            if record.avaya_severity in sev_map:
                record.priority = sev_map[record.avaya_severity]

    def action_open_avaya_portal(self):
        self.ensure_one()
        url = self.avaya_portal_url or "https://support.avaya.com/"
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }

    @api.onchange('priority', 'ticket_type')
    def _onchange_sla_calculation(self):
        for record in self:
            domain = []
            if record.priority:
                domain.append(('priority', '=', record.priority))
            if record.ticket_type:
                domain.append(('ticket_type', '=', record.ticket_type))
            
            if not domain:
                continue
                
            policy = self.env['helpdesk.sla.policy'].search(domain, limit=1)
            if policy:
                start_time = record.create_date if record.create_date else fields.Datetime.now()
                record.deadline = start_time + timedelta(hours=policy.target_hours)

    @api.model
    def _read_group_state(self, stages, domain, order):
        return ['new', 'in_progress', 'waiting', 'solved']

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('avaya_sr_number'):
                vals['is_avaya_support'] = True
                if not vals.get('ticket_source'):
                    vals['ticket_source'] = 'avaya_support'
            if vals.get('is_avaya_support') and vals.get('avaya_severity') and not vals.get('priority'):
                sev_map = {'p1': '3', 'p2': '2', 'p3': '1', 'p4': '0'}
                vals['priority'] = sev_map.get(vals['avaya_severity'], '0')
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('helpdesk.ticket.pro') or 'New'
        records = super().create(vals_list)
        for record in records:
            if not record.access_token:
                record._portal_ensure_token()
            if not record.deadline:
                record._onchange_sla_calculation()
            if record.user_id:
                record._send_assignment_email()
        return records

    def write(self, vals):
        state_changed = 'state' in vals
        old_states = {}
        if state_changed:
            for record in self:
                old_states[record.id] = record.state
                
        res = super(HelpdeskTicketPro, self).write(vals)
        
        if 'user_id' in vals:
            for record in self:
                if record.user_id:
                    record._send_assignment_email()
                    
        if state_changed:
            for record in self:
                old_state = old_states.get(record.id)
                new_state = record.state
                if old_state != new_state:
                    record._send_stage_change_email(old_state, new_state)
                    
        return res

    def _send_stage_change_email(self, old_state, new_state):
        self.ensure_one()
        
        template_xmlid = None
        if new_state == 'in_progress':
            template_xmlid = 'helpdesk_ticket_pro.helpdesk_ticket_pro_stage_in_progress_template'
        elif new_state == 'waiting':
            template_xmlid = 'helpdesk_ticket_pro.helpdesk_ticket_pro_stage_waiting_template'
        elif new_state == 'solved':
            template_xmlid = 'helpdesk_ticket_pro.helpdesk_ticket_pro_stage_solved_template'
        elif new_state == 'closed':
            template_xmlid = 'helpdesk_ticket_pro.helpdesk_ticket_pro_stage_closed_template'
            
        if not template_xmlid:
            return
            
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            return
            
        latest_comment = ''
        if new_state == 'waiting':
            last_message = self.env['mail.message'].search([
                ('model', '=', 'helpdesk.ticket.pro'),
                ('res_id', '=', self.id),
                ('message_type', '=', 'comment')
            ], limit=1)
            if last_message:
                latest_comment = last_message.body
            else:
                latest_comment = self.description or 'โปรดตรวจสอบรายละเอียดในระบบ'
                
        # Send to customer
        if self.partner_id and self.partner_id.email:
            try:
                template.with_context(latest_comment=latest_comment).send_mail(
                    self.id, 
                    force_send=True,
                    email_values={'email_to': self.partner_id.email}
                )
            except Exception as e:
                pass
            
        # For closed state, also notify the assigned agent
        if new_state == 'closed' and self.user_id and self.user_id.email:
            try:
                template.send_mail(
                    self.id,
                    force_send=True,
                    email_values={'email_to': self.user_id.email}
                )
            except Exception as e:
                pass

    def _send_assignment_email(self):
        self.ensure_one()
        if not self.user_id or not self.user_id.email:
            return
        if 'example.com' in self.user_id.email or self.user_id.id == 1 or self.user_id.login == '__system__':
            return
            
        template = self.env.ref('helpdesk_ticket_pro.helpdesk_ticket_pro_assignment_template', raise_if_not_found=False)
        if template:
            try:
                template.send_mail(
                    self.id, 
                    force_send=True,
                    email_values={'email_to': self.user_id.email}
                )
            except Exception as e:
                pass



class MailActivity(models.Model):
    _inherit = 'mail.activity'

    @api.model_create_multi
    def create(self, vals_list):
        activities = super(MailActivity, self).create(vals_list)
        for activity in activities:
            if activity.res_model == 'helpdesk.ticket.pro':
                activity._send_helpdesk_activity_email()
        return activities

    def _send_helpdesk_activity_email(self):
        self.ensure_one()
        ticket = self.env['helpdesk.ticket.pro'].browse(self.res_id)
        if not ticket.exists():
            return

        recipients = []
        if self.user_id and self.user_id.email and 'example.com' not in self.user_id.email and self.user_id.id != 1:
            recipients.append((self.user_id, 'activity_assignee'))
        if ticket.user_id and ticket.user_id.email and 'example.com' not in ticket.user_id.email and ticket.user_id.id != 1 and ticket.user_id != self.user_id:
            recipients.append((ticket.user_id, 'ticket_assignee'))

        activity_type_label = self.activity_type_id.name if self.activity_type_id else 'Activity'
        summary_str = self.summary or 'ไม่ได้กำหนดหัวข้อกิจกรรม'
        deadline_str = self.date_deadline.strftime('%Y-%m-%d') if self.date_deadline else 'ไม่ได้กำหนด'
        
        # Strip HTML tags or just take text for summary/note
        note_str = self.note or '-'

        for user, role in recipients:
            subject = f"[Helpdesk] กิจกรรมใหม่: {summary_str} บน Ticket {ticket.name}"
            
            role_text = "คุณได้รับมอบหมายให้ทำกิจกรรมนี้" if role == 'activity_assignee' else f"มีกิจกรรมใหม่ถูกสร้างขึ้นบน Ticket ที่คุณรับผิดชอบ ({ticket.name})"
            
            body_html = f"""
            <div style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.5; color: #333333;">
                <p>สวัสดีครับ/ค่ะ <strong>{user.name}</strong>,</p>
                <p>{role_text}:</p>
                <table style="width: 100%; border-collapse: collapse; margin-top: 15px; margin-bottom: 15px;">
                    <tr style="background-color: #f2f2f2;">
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: left; width: 150px;">เลขที่ใบงาน (Ticket Ref)</th>
                        <td style="padding: 8px; border: 1px solid #ddd;">{ticket.name}</td>
                    </tr>
                    <tr>
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">หัวข้อใบงาน (Subject)</th>
                        <td style="padding: 8px; border: 1px solid #ddd;">{ticket.subject}</td>
                    </tr>
                    <tr style="background-color: #f2f2f2;">
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">ประเภทกิจกรรม (Activity Type)</th>
                        <td style="padding: 8px; border: 1px solid #ddd;">{activity_type_label}</td>
                    </tr>
                    <tr>
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">สรุปกิจกรรม (Summary)</th>
                        <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">{summary_str}</td>
                    </tr>
                    <tr style="background-color: #f2f2f2;">
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">รายละเอียด (Note)</th>
                        <td style="padding: 8px; border: 1px solid #ddd;">{note_str}</td>
                    </tr>
                    <tr>
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">กำหนดเสร็จ (Deadline)</th>
                        <td style="padding: 8px; border: 1px solid #ddd; color: #d9534f; font-weight: bold;">{deadline_str}</td>
                    </tr>
                </table>
                <p>กรุณาตรวจสอบรายละเอียดและดำเนินการในระบบ Odoo ครับ</p>
                <hr style="border: none; border-top: 1px solid #eeeeee; margin-top: 20px;" />
                <p style="font-size: 11px; color: #999999;">นี่คืออีเมลแจ้งเตือนอัตโนมัติจากระบบ Helpdesk (Pro) กรุณาอย่าตอบกลับอีเมลนี้โดยตรง</p>
            </div>
            """
            
            mail_server = self.env['ir.mail_server'].search([], limit=1)
            email_from = mail_server.smtp_user if mail_server else None

            mail_values = {
                'subject': subject,
                'body_html': body_html,
                'email_to': user.email,
            }
            if email_from:
                mail_values['email_from'] = email_from
            
            try:
                mail = self.env['mail.mail'].create(mail_values)
                mail.send()
            except Exception as e:
                pass


class IrMailServer(models.Model):
    _inherit = 'ir.mail_server'

    @api.model
    def send_email(self, message, mail_server_id=None, smtp_server=None, smtp_port=None,
                   smtp_user=None, smtp_password=None, smtp_encryption=None,
                   smtp_debug=False, smtp_session=None):
        if not smtp_user:
            if mail_server_id:
                server = self.browse(mail_server_id)
            else:
                server = self.search([], limit=1)
            smtp_user = server.smtp_user if server else None
            
        if smtp_user and message.get('From'):
            original_from = message['From']
            from odoo.tools import email_split, formataddr
            emails = email_split(original_from)
            if emails:
                original_email = emails[0]
                if original_email != smtp_user:
                    name = original_from.split('<')[0].strip(' "') if '<' in original_from else ''
                    message.replace_header('From', formataddr((name, smtp_user)))
                    if 'Reply-To' in message:
                        message.replace_header('Reply-To', original_from)
                    else:
                        message['Reply-To'] = original_from
                        
        return super(IrMailServer, self).send_email(
            message, mail_server_id=mail_server_id, smtp_server=smtp_server, smtp_port=smtp_port,
            smtp_user=smtp_user, smtp_password=smtp_password, smtp_encryption=smtp_encryption,
            smtp_debug=smtp_debug, smtp_session=smtp_session
        )


