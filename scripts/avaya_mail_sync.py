from team_manager import match_engineer
#!/usr/bin/env python3
import os
import sys
import time
import json
import re
import imaplib
import email
from email.header import decode_header
import urllib.request
import urllib.parse

IMAP_HOST = "mail.jadscomm.com"
IMAP_PORT = 993
IMAP_USER = "phongthep@jadscomm.com"
IMAP_PASS = "Phongthep@8275"

ODOO_BASE_URL = "https://phongthep.lol"
TELEGRAM_BOT_TOKEN = "8889798857:AAF1ocHXbDogH5VnbZLaESIhQgSb9vEkzms"
TELEGRAM_CHAT_ID = "8948351323"
N8N_WEBHOOK_URL = "http://127.0.0.1:5678/webhook/avaya-support"

STATE_FILE = "/home/captain/avaya_synced_ids.json"
CHECK_INTERVAL_SEC = 30
TARGET_FOLDERS = ["INBOX", "Sent", "Avaya Emails", "Avaya Product Security teams"]

def load_synced_ids():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_synced_ids(synced_ids):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(synced_ids), f, indent=2)
    except Exception as e:
        print(f"[Error saving state] {e}", file=sys.stderr)

def decode_mime(s):
    if not s:
        return ""
    try:
        dh = decode_header(s)
        res = []
        for text, enc in dh:
            if isinstance(text, bytes):
                enc_name = enc
                if enc_name and "874" in str(enc_name).lower():
                    enc_name = "cp874"
                try:
                    res.append(text.decode(enc_name or "utf-8", errors="replace"))
                except Exception:
                    res.append(text.decode("latin1", errors="replace"))
            else:
                res.append(str(text))
        return "".join(res)
    except Exception:
        return str(s)

def clean_reply_body(text):
    if not text:
        return ""
    clean = text.replace("\r\n", "\n").replace("\r", "\n")
    markers = [
        r"\n--\s*\n",
        r"\n_{5,}",
        r"\n-{5,}",
        r"\nFrom:.*?\nSent:",
        r"\nOn .*? wrote:",
        r"\n>+"
    ]
    for pattern in markers:
        parts = re.split(pattern, clean, flags=re.IGNORECASE | re.DOTALL)
        if len(parts) > 1 and len(parts[0].strip()) > 10:
            clean = parts[0].strip()
            break
    return clean.strip()



def check_requires_team_action(subj, body):
    text = (str(subj) + " " + str(body)).lower()
    keywords = [
        "please provide", "please collect", "collect logs", "collect traces", "trace",
        "wireshark", "pcap", "mst", "action required", "action needed", "awaiting customer",
        "awaiting partner", "reproduce", "test call", "onsite", "patch", "upgrade",
        "firmware", "reboot", "restart", "hardware", "replace", "rma", "check configuration",
        "packet capture", "need logs", "send logs"
    ]
    return any(k in text for k in keywords)

def send_line(text):
    try:
        sys.path.insert(0, '/home/captain')
        from send_line_alert import send_line_push
        import html
        clean = re.sub(r'<[^>]+>', '', text)
        clean = html.unescape(clean).strip()
        send_line_push(clean)
    except Exception as e:
        print(f'[LINE push error] {e}', file=sys.stderr)

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        print(f"[Telegram error] {e}", file=sys.stderr)

def check_odoo_ticket(sr, subject):
    try:
        params = urllib.parse.urlencode({"sr": sr or "", "subject": subject or ""})
        url = f"{ODOO_BASE_URL}/helpdesk/api/check_ticket?{params}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[Odoo check error] {e}", file=sys.stderr)
        return {"exists": False}

def create_odoo_ticket(data):
    try:
        url = f"{ODOO_BASE_URL}/helpdesk/api/create_ticket"
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[Odoo create error] {e}", file=sys.stderr)
        return {"success": False}

def update_odoo_ticket(data):
    try:
        url = f"{ODOO_BASE_URL}/helpdesk/api/update_ticket"
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[Odoo update error] {e}", file=sys.stderr)
        return {"success": False}

def forward_to_n8n(msg_payload):
    try:
        payload = json.dumps(msg_payload).encode("utf-8")
        req = urllib.request.Request(N8N_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except Exception as e:
        print(f"[n8n webhook error] {e}", file=sys.stderr)
        return False

def parse_avaya_email_fields(subj, body, html_body="", from_hdr="", to_hdr=""):
    table_map = {}
    
    # 1. HTML Table Parsing (Modern Avaya Open Notification & Siebel Alerts)
    if html_body:
        row_matches = re.findall(r'<tr[^>]*>\s*<td[^>]*>([\s\S]*?)</td>\s*<td[^>]*>([\s\S]*?)</td>\s*</tr>', html_body, re.I)
        for k_raw, v_raw in row_matches:
            k = re.sub(r'<[^>]+>', '', k_raw).replace('&nbsp;', ' ').replace(':', '').strip().lower()
            v = re.sub(r'<[^>]+>', ' ', v_raw).replace('&nbsp;', ' ').replace('&#xD;', '').strip()
            v = re.sub(r'\s+', ' ', v).strip()
            if k:
                table_map[k] = v

    # 2. Text body with preserved line breaks
    clean_body = body or ""
    if not clean_body and html_body:
        h = re.sub(r'<(style|script)[^>]*>[\s\S]*?</\1>', ' ', html_body, flags=re.I)
        h = re.sub(r'<br\s*/?>', '\n', h, flags=re.I)
        h = re.sub(r'</p>|</div>|</tr>|</td>', '\n', h, flags=re.I)
        clean_body = re.sub(r'<[^>]+>', ' ', h)
        clean_body = re.sub(r'[ \t]+', ' ', clean_body)
        clean_body = re.sub(r'\n\s*\n+', '\n\n', clean_body).strip()
        
    full_text = f"{subj}\n{clean_body}"
    
    # Field boundary tokens to prevent regex runaway
    boundaries = r'(?:\bSR\s*(?:Number|#)?|\bBusiness\s*Severity|\bSeverity|\bSold\s*To\s*Name|\bSold\s*To\s*Address|\bSold\s*To\b|\bFL\b|\bContact\s*Name|\bContact\b|\bPhone\s*Number|\bContact\s*Phone|\bPhone\b|\bContact\s*Email|\bEmail\b|\bDate\s*Reported|\bOpened\b|\bCreated\b|\bStatus\b|\bProduct\b|\bAsset\s*ID|\bElement\s*Nick\s*Name|\bHost\s*Name|\bDescription\b|\bComments\b|\bOwner\s*-\\s*SBL|\*\s*This\s*notification|\bAlert\s*ID|\bOn-Line\s*Support|\bManagement\s*Escalation|\bOpt-Out|\r?\n|$)'
    
    def extract_bounded(pattern, text):
        if not text:
            return ""
        m = re.search(pattern + r'\s*[:\-]?\s*([^\r\n]+?)(?=\s*' + boundaries + ')', text, re.I)
        if m:
            val = m.group(1).strip()
            return re.sub(r'[;,\s]+$', '', val)
        return ""

    # SR Number
    sr_num = table_map.get('sr number') or table_map.get('service request number') or table_map.get('sr#') or table_map.get('case')
    if not sr_num or not re.match(r'^[0-9\-]+$', sr_num.strip()):
        m = re.search(r'\b(1-\d{10,12})\b', full_text)
        sr_num = m.group(1) if m else ""

    # Sold To
    sold_to = table_map.get('sold to') or table_map.get('fl') or table_map.get('functional location')
    if not sold_to:
        sold_to = extract_bounded(r'Sold\s*To(?![\s*]+(?:Name|Address))', clean_body)
    if not sold_to:
        m = re.search(r'\b(005\d{7})\b', full_text)
        sold_to = m.group(1) if m else "0050280387"

    # Customer Name
    customer_name = table_map.get('sold to name') or table_map.get('primary customer')
    if not customer_name:
        customer_name = extract_bounded(r'(?:Sold\s*To\s*Name|Primary\s*Customer)', clean_body)
    customer_name = re.sub(r'\[.*$', '', customer_name).strip()

    # Location / Address
    location = table_map.get('sold to address') or table_map.get('location')
    if not location:
        location = extract_bounded(r'(?:Sold\s*To\s*Address|Location)', clean_body)

    # Contact Name
    contact_name = table_map.get('contact name') or table_map.get('contact')
    if not contact_name:
        contact_name = extract_bounded(r'(?:Contact\s*Name|\bContact\b\s*[:\-])', clean_body)
    if not contact_name:
        contact_name = from_hdr.split("<")[0].replace('"', '').strip() if from_hdr else "Phongthep Phimthong"
    contact_name = re.sub(r'\[.*$', '', contact_name).strip()

    # Contact Phone
    contact_phone = table_map.get('phone number') or table_map.get('contact phone') or table_map.get('phone')
    if contact_phone and not re.search(r'\d{7,}', contact_phone):
        contact_phone = ""
    if not contact_phone:
        m = re.search(r'(?:Phone\s*Number|Contact\s*Phone|\bPhone\b)[\s:]*([0-9+\-\s()]{8,15})', clean_body, re.I)
        if m:
            contact_phone = m.group(1).strip()
    if not contact_phone:
        contact_phone = "0612438275"

    # Contact Email
    contact_email = table_map.get('contact email') or table_map.get('email')
    if not contact_email or "@" not in contact_email:
        m = re.search(r'[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}', clean_body)
        contact_email = m.group(0) if m else "phongthep@jadscomm.com"

    # Date Reported
    date_reported = table_map.get('date reported') or table_map.get('opened') or table_map.get('created')
    if not date_reported:
        date_reported = extract_bounded(r'(?:Date\s*Reported|Opened|Created)', clean_body)

    # Description & Comments
    raw_desc = table_map.get('description') or extract_bounded(r'(?:Description|SR\s*Description)', clean_body)
    raw_comments = table_map.get('comments') or extract_bounded(r'Comments', clean_body)
    desc = raw_desc or raw_comments or ""
    desc = re.sub(r'^URGENT\s+P[1-4]:\s*', '', desc, flags=re.I).strip()

    # Product
    product = table_map.get('product') or table_map.get('op skill')
    if not product:
        product = extract_bounded(r'(?:Product|Op\s*Skill)', clean_body)
    
    # Smart Product Deduction if empty or generic
    if not product or product in ['SEID', 'Avaya Solution', 'Collaboration Environment']:
        combined_text = f"{subj} {desc} {clean_body}"
        if re.search(r'push notification|push', combined_text, re.I):
            product = 'Avaya Aura® Session Manager (SM) / Workplace Push Notification'
        elif re.search(r'Communication Manager|\bCM\b', combined_text, re.I):
            product = 'Avaya Aura® Communication Manager (CM)'
        elif re.search(r'Session Manager|\bSM\b', combined_text, re.I):
            product = 'Avaya Aura® Session Manager (SM)'
        elif re.search(r'AADS|Device Services', combined_text, re.I):
            product = 'Avaya Aura® Device Services (AADS)'
        elif re.search(r'Breeze|Collaboration Environment', combined_text, re.I):
            product = 'Avaya Breeze Platform'
        elif re.search(r'SBC|ASBCE|Border Controller', combined_text, re.I):
            product = 'Avaya Session Border Controller (ASBCE)'
        elif re.search(r'AAMS|Media Server', combined_text, re.I):
            product = 'Avaya Aura® Media Server (AAMS)'
        elif re.search(r'IP Office|\bIPO\b', combined_text, re.I):
            product = 'Avaya IP Office (IPO)'
        else:
            product = 'Avaya Solution'

    # Severity & Priority
    raw_sev = table_map.get('business severity') or table_map.get('severity') or extract_bounded(r'(?:Business\s*Severity|Severity)', clean_body)
    if re.search(r'Outage|OTG|P1|CRITICAL|URGENT', raw_sev or full_text, re.I):
        severity = 'p1'
        priority = '3'
    elif re.search(r'Major|Business Impact|P2', raw_sev or full_text, re.I):
        severity = 'p2'
        priority = '2'
    elif re.search(r'Minor|P3', raw_sev or full_text, re.I):
        severity = 'p3'
        priority = '1'
    else:
        severity = 'p4'
        priority = '0'

    # Subject & Summary
    summary = ""
    if desc and desc != "Break/Fix" and len(desc) > 5:
        first_line = desc.split('\n')[0].strip()
        if len(first_line) > 85:
            sentences = re.split(r'\.\s+', first_line)
            if sentences and len(sentences[0]) > 10:
                summary = sentences[0].strip()
            else:
                summary = first_line[:85].strip()
        else:
            summary = first_line
    elif subj:
        summary = re.sub(r'^(?:Fwd:\s*|New Activity:\s*|BUSINESS IMPACT SR (?:Opened|Updated):\s*|Your request has been successfully created\.\.\.\s*)', '', subj, flags=re.I).strip()
    
    if not summary or len(summary) < 5:
        summary = f"{product} - Service Request"

    if summary and summary[0].islower():
        summary = summary[0].upper() + summary[1:]

    # Customer short tag
    cust_short = ""
    if customer_name:
        c_low = customer_name.lower()
        if "thanachart" in c_low:
            cust_short = "Thanachart"
        elif "electricity" in c_low or "mea" in c_low:
            cust_short = "MEA"
        else:
            cust_short = customer_name.split()[0]
            
    if cust_short and cust_short.lower() not in summary.lower():
        clean_subj = f"[Avaya SR# {sr_num}] {summary} ({cust_short})" if sr_num else f"{summary} ({cust_short})"
    else:
        clean_subj = f"[Avaya SR# {sr_num}] {summary}" if sr_num else summary

    avaya_url = f"https://support.avaya.com/support/en/secure/service-requests/displaySR?srNum={sr_num}" if sr_num else "https://support.avaya.com/"
    ops_url = f"https://report.avaya.com/siebelreports/casedetails.aspx?case_id={sr_num}" if sr_num else "https://report.avaya.com/"

    border_color = "#d93025" if severity == "p1" else "#f9ab00"
    bg_color = "#fce8e6" if severity == "p1" else "#fef7e0"
    text_color = "#d93025" if severity == "p1" else "#b06000"

    html_desc = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 14px; line-height: 1.6; color: #202124;">
  <div style="background-color: {bg_color}; border-left: 5px solid {border_color}; padding: 14px; margin-bottom: 16px; border-radius: 4px;">
    <div style="font-size: 16px; font-weight: bold; color: {text_color}; margin-bottom: 8px;">
      🔔 AVAYA TAC {severity.upper()} SERVICE REQUEST (Created)
    </div>
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 13px;">
      <div><strong>SR Number:</strong> {sr_num or 'N/A'}</div>
      <div><strong>Status:</strong> Created</div>
      <div><strong>Sold To / FL:</strong> {sold_to}</div>
      <div><strong>Product:</strong> {product}</div>
      <div><strong>Severity:</strong> {severity.upper()}</div>
      <div><strong>Date Reported:</strong> {date_reported or '-'}</div>
      <div><strong>Primary Customer:</strong> {customer_name or '-'}</div>
      <div><strong>Location:</strong> {location or '-'}</div>
    </div>
  </div>
  
  <div style="margin-bottom: 16px;">
    <h3 style="margin-top: 0; color: #1a73e8; font-size: 15px;">Summary / Description</h3>
    <p style="white-space: pre-wrap; background: #f8f9fa; padding: 12px; border-radius: 4px; border: 1px solid #dadce0;">{desc or summary}</p>
  </div>

  <div style="margin-bottom: 16px;">
    <h3 style="margin-top: 0; color: #1a73e8; font-size: 15px;">Contact Information</h3>
    <p><strong>Contact:</strong> {contact_name} | <strong>Phone:</strong> {contact_phone} | <strong>Email:</strong> {contact_email}</p>
  </div>

  <div style="margin-top: 20px; padding-top: 12px; border-top: 1px solid #dadce0;">
    <a href="{avaya_url}" target="_blank" style="background-color: #d93025; color: white; padding: 8px 14px; text-decoration: none; border-radius: 4px; font-weight: bold; margin-right: 10px; display: inline-block;">🔗 Open Avaya TAC SR</a>
    <a href="{ops_url}" target="_blank" style="background-color: #1a73e8; color: white; padding: 8px 14px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block;">📊 View Siebel Case Report</a>
  </div>
</div>"""

    return {
        "subject": clean_subj,
        "summary": summary,
        "description": html_desc,
        "avaya_sr_number": sr_num,
        "sold_to_id": sold_to,
        "avaya_product": product,
        "avaya_severity": severity,
        "priority": priority,
        "avaya_status": "Created",
        "avaya_contact_name": contact_name,
        "avaya_contact_phone": contact_phone,
        "avaya_contact_email": contact_email,
        "avaya_date_reported": date_reported,
        "avaya_customer_name": customer_name,
        "avaya_location": location,
        "avaya_portal_url": avaya_url
    }

def sync_mailbox(synced_ids):
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)
    except Exception as e:
        print(f"[IMAP Connect Error] {e}", file=sys.stderr)
        return synced_ids

    new_synced = set(synced_ids)

    for folder in TARGET_FOLDERS:
        try:
            res, _ = mail.select(f'"{folder}"')
            if res != "OK":
                continue
            
            typ, data = mail.search(None, "ALL")
            if not data or not data[0]:
                continue
            
            msg_ids = data[0].split()
            # Check the last 30 emails in folder
            for mid in msg_ids[-30:]:
                typ, msg_data = mail.fetch(mid, "(BODY.PEEK[])")
                if not msg_data or not msg_data[0]:
                    continue
                
                msg = email.message_from_bytes(msg_data[0][1])
                msg_id_header = msg.get("Message-ID", "").strip() or f"{folder}-{mid.decode()}"
                
                if msg_id_header in new_synced:
                    continue

                subj = decode_mime(msg.get("Subject", ""))
                from_hdr = decode_mime(msg.get("From", ""))
                to_hdr = decode_mime(msg.get("To", ""))
                
                from_lower = from_hdr.lower()
                subj_lower = subj.lower()

                # Hard block internal notifications, Jira, Atlassian, and system daemons
                if any(k in from_lower for k in ["atlassian.net", "jira", "jadssupport", "notifications@jadscomm.com", "postmaster", "mailer-daemon"]):
                    new_synced.add(msg_id_header)
                    continue

                if any(k in subj_lower for k in ["[helpdesk]", "[jira]", "ใบงาน:"]):
                    new_synced.add(msg_id_header)
                    continue

                full_hdr = f"{subj} {from_hdr} {to_hdr}".lower()
                sr_match = re.search(r'\b(1-\d{10,12})\b', subj)
                is_from_avaya = ("avaya.com" in from_lower) or ("avaya.com" in to_hdr.lower())
                
                # Must be directly from/to avaya.com OR contain an official Avaya SR number
                is_avaya = is_from_avaya or (sr_match is not None)
                if not is_avaya:
                    new_synced.add(msg_id_header)
                    continue

                body = ""
                html_body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        if ctype == "text/plain" and not body:
                            p = part.get_payload(decode=True)
                            if p:
                                body = p.decode("utf-8", errors="replace")
                        elif ctype == "text/html" and not html_body:
                            p = part.get_payload(decode=True)
                            if p:
                                html_body = p.decode("utf-8", errors="replace")
                else:
                    p = msg.get_payload(decode=True)
                    if p:
                        if msg.get_content_type() == "text/html":
                            html_body = p.decode("utf-8", errors="replace")
                        else:
                            body = p.decode("utf-8", errors="replace")

                if not body and html_body:
                    h_clean = re.sub(r"<(style|script)[^>]*>[\s\S]*?</\1>", " ", html_body, flags=re.IGNORECASE)
                    h_clean = re.sub(r'<br\s*/?>', '\n', h_clean, flags=re.IGNORECASE)
                    h_clean = re.sub(r'</p>|</div>|</tr>|</td>', '\n', h_clean, flags=re.IGNORECASE)
                    body = re.sub(r"<[^>]+>", " ", h_clean)
                    body = re.sub(r"[ \t]+", " ", body)
                    body = re.sub(r"\n\s*\n+", "\n\n", body).strip()

                if not sr_match:
                    sr_match = re.search(r'\b(1-\d{10,12})\b', body)
                sr_num = sr_match.group(1) if sr_match else ""

                is_outbound = (folder == "Sent") or ("jadscomm.com" in from_hdr.lower())
                clean_body = clean_reply_body(body)

                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Detected Avaya email in {folder}: {subj[:50]} (SR: {sr_num})")

                # Check if ticket exists in Odoo
                ticket_info = check_odoo_ticket(sr_num, subj)
                if ticket_info.get("exists"):
                    tkt_id = ticket_info.get("id")
                    tkt_ref = ticket_info.get("ref")
                    update_payload = {
                        "id": tkt_id,
                        "avaya_sr_number": sr_num,
                        "is_outbound": is_outbound,
                        "sender_name": from_hdr.split("<")[0].replace('"', '').strip() or from_hdr,
                        "sender_email": re.search(r'[\w\.-]+@[\w\.-]+', from_hdr).group(0) if re.search(r'[\w\.-]+@[\w\.-]+', from_hdr) else "",
                        "recipient": to_hdr.split("<")[0].replace('"', '').strip() or to_hdr,
                        "subject": subj,
                        "reply_body": clean_body or body[:500]
                    }
                    res = update_odoo_ticket(update_payload)
                    if res.get("success"):
                        print(f"-> Successfully updated Odoo ticket #{tkt_ref} (ID: {tkt_id})")
                        direction = "📤 เราตอบกลับ Avaya (Sent)" if is_outbound else "📩 Avaya ตอบกลับ (Inbox)"
                        sender_label = "ผู้ส่ง (เรา):" if is_outbound else "ผู้ส่ง (Avaya):"
                        tg_msg = f"""💬 <b>[{direction}]</b>

🏷 <b>เลขที่ตั๋ว Odoo:</b> #{tkt_ref} (ID: {tkt_id})
🔥 <b>Avaya SR#:</b> {sr_num or '-'}
📌 <b>หัวข้อ:</b> {subj}
👤 <b>{sender_label}</b> {from_hdr}

✉️ <b>ข้อความ:</b>
<i>{(clean_body or body)[:350]}</i>

🌐 <a href="{ODOO_BASE_URL}/helpdesk/ticket/{tkt_id}">ดูรายละเอียดเคส (ไม่ต้องล็อกอิน)</a> """
                        send_telegram(tg_msg)
                        if not is_outbound and check_requires_team_action(subj, clean_body or body):
                            eng_name, eng_tag, eng_uid = match_engineer(existing.get("site_engineer") or subj)
                            team_card = f"""🚨 [งานสำหรับทีมวิศวกร - Avaya TAC ร้องขอข้อมูล/Action]
━━━━━━━━━━━━━━━━━━━━
👤 ผู้รับผิดชอบ: {eng_tag}
🏷 เลขที่ตั๋ว: #{tkt_ref}
🔥 Avaya SR#: {sr_num or "-"}
📌 หัวข้อ: {subj}
⚠️ รายละเอียดงาน:
{(clean_body or body)[:220]}...

🌐 ดูรายละเอียดงาน (ไม่ต้องล็อกอิน):
{ODOO_BASE_URL}/helpdesk/ticket/{tkt_id}"""
                            send_line(team_card)
                else:
                    # Ticket does NOT exist yet -> Create new ticket directly
                    if not is_outbound and ("avaya.com" in from_hdr.lower() or sr_num):
                        parsed = parse_avaya_email_fields(subj, body, html_body, from_hdr, to_hdr)
                        res = create_odoo_ticket(parsed)
                        if res.get("success"):
                            tkt_ref = res.get("ref")
                            tkt_id = res.get("id")
                            print(f"-> Successfully created Odoo ticket #{tkt_ref} (ID: {tkt_id}) for SR: {parsed['avaya_sr_number']}")
                            tg_msg = f"""🔥 <b>[เปิดตั๋วใหม่จาก Avaya TAC]</b>

🏷 <b>เลขที่ตั๋ว Odoo:</b> #{tkt_ref} (ID: {tkt_id})
🔥 <b>Avaya SR#:</b> {parsed['avaya_sr_number']}
🏢 <b>ลูกค้า:</b> {parsed['avaya_customer_name'] or '-'}
📦 <b>ระบบ / Product:</b> {parsed['avaya_product']}
⚠️ <b>ความเร่งด่วน:</b> {parsed['avaya_severity'].upper()}
📌 <b>หัวข้อ:</b> {parsed['subject']}
👤 <b>ผู้แจ้ง / ติดต่อ:</b> {parsed['avaya_contact_name']}

🌐 <a href="{ODOO_BASE_URL}/helpdesk/ticket/{tkt_id}">ดูรายละเอียดเคส (ไม่ต้องล็อกอิน)</a> """
                            send_telegram(tg_msg)
                        send_line(tg_msg)
                        
                        # Also forward to n8n as backup
                        forward_to_n8n({
                            "from": from_hdr,
                            "to": to_hdr,
                            "subject": subj,
                            "text": body,
                            "html": html_body
                        })
                
                new_synced.add(msg_id_header)
                save_synced_ids(new_synced)

        except Exception as e:
            print(f"[Folder error {folder}] {e}", file=sys.stderr)

    try:
        mail.logout()
    except Exception:
        pass

    return new_synced

def main():
    print(f"Starting Avaya Mailbox Sync Service (checking every {CHECK_INTERVAL_SEC}s)...")
    synced_ids = load_synced_ids()
    print(f"Loaded {len(synced_ids)} previously synced message IDs.")

    while True:
        try:
            synced_ids = sync_mailbox(synced_ids)
        except Exception as e:
            print(f"[Main loop error] {e}", file=sys.stderr)
        time.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    main()
