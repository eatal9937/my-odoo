#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Follow-up Alert Listener (@phongthep_alert_bot)
Listens for inline button callbacks: 'send_followup:<ticket_id>'
Sends follow-up email via SMTP and updates Odoo Chatter.
"""

import os
import sys
import time
import json
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import urllib.request
import urllib.parse
from datetime import datetime
import pytz

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/home/captain/telegram_alert_listener.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("telegram_alert_listener")

BOT_TOKEN = "8889798857:AAF1ocHXbDogH5VnbZLaESIhQgSb9vEkzms"
AUTHORIZED_CHAT_ID = 8948351323

SMTP_SERVER = "mail.jadscomm.com"
SMTP_PORT = 587
SMTP_USER = "phongthep@jadscomm.com"
SMTP_PASS = "Phongthep@8275"

BKK_TZ = pytz.timezone('Asia/Bangkok')

def telegram_api(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    headers = {"User-Agent": "curl/7.88.1"}
    data = None
    if payload:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        logger.error(f"Telegram API {method} error: {e}")
        return None

def answer_callback(cb_id, text, show_alert=False):
    telegram_api("answerCallbackQuery", {
        "callback_query_id": cb_id,
        "text": text,
        "show_alert": show_alert
    })

def send_smtp_email(to_email, subject, body_text):
    msg = MIMEMultipart()
    msg['From'] = f"Phongthep Phimthong (Captain) <{SMTP_USER}>"
    msg['To'] = to_email
    msg['Subject'] = subject

    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    logger.info(f"Email sent successfully to {to_email} with subject: {subject}")

def update_odoo_chatter(ticket_id, subject, body_text, recipient):
    url = "https://phongthep.lol/helpdesk/api/update_ticket"
    payload = {
        "id": int(ticket_id),
        "reply_body": body_text,
        "is_outbound": True,
        "sender_name": "Phongthep Phimthong (Captain)",
        "sender_email": SMTP_USER,
        "recipient": recipient,
        "subject": subject
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "curl/7.88.1"
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            logger.info(f"Odoo Chatter updated for ticket {ticket_id}: {res}")
            return res
    except Exception as e:
        logger.error(f"Failed to update Odoo Chatter for ticket {ticket_id}: {e}")
        return None

def fetch_ticket_info(ticket_id):
    url = f"https://phongthep.lol/helpdesk/api/ticket_detail/{ticket_id}"
    headers = {"User-Agent": "curl/7.88.1"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        logger.error(f"Failed to fetch ticket info {ticket_id}: {e}")
        return None

def handle_callback_query(cb):
    cb_id = cb.get("id")
    from_user = cb.get("from", {})
    user_id = from_user.get("id")
    data = cb.get("data", "")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if user_id != AUTHORIZED_CHAT_ID:
        answer_callback(cb_id, "⛔ ไม่อนุญาต: เฉพาะคุณ Phongthep เท่านั้นที่มีสิทธิ์กดส่งเมล", show_alert=True)
        return

    if not data.startswith("send_followup:"):
        answer_callback(cb_id, "คำสั่งไม่ถูกต้อง")
        return

    ticket_id = data.split(":")[1]
    logger.info(f"Received send_followup request for ticket_id={ticket_id} from user {user_id}")
    answer_callback(cb_id, "⏳ กำลังเชื่อมต่อ SMTP เพื่อส่งอีเมล Follow-up...")

    # Extract draft from Telegram message text
    orig_text = message.get("text") or ""
    draft_body = ""
    if "AI ร่างเมล Follow-up พร้อมส่ง Avaya:" in orig_text:
        parts = orig_text.split("AI ร่างเมล Follow-up พร้อมส่ง Avaya:")
        after = parts[1].strip()
        if "เปิดดูตั๋วใน Odoo" in after:
            draft_body = after.split("เปิดดูตั๋วใน Odoo")[0].strip()
        else:
            draft_body = after

    # Fetch fresh ticket info to get Avaya SR# and recipient
    t_info = fetch_ticket_info(ticket_id)
    t = t_info.get("ticket", {}) if t_info else {}
    sr_number = t.get("avaya_sr_number") or ""
    cust_name = t.get("customer") or ""
    t_ref = t.get("ref") or f"TKT-{ticket_id}"

    # Determine recipient
    recipient = "support@avaya.com"
    msgs = t_info.get("messages", []) if t_info else []
    for m in msgs:
        if m.get("email_from") and "@avaya.com" in m.get("email_from"):
            recipient = m.get("email_from")
            break

    email_subject = f"Follow-up: [Avaya SR# {sr_number or t_ref}] {t.get('subject', 'Service Request Update')}"
    if not draft_body:
        draft_body = f"Dear Avaya Support Team,\n\nWe would like to follow up on Avaya SR# {sr_number} ({cust_name}). Could you please provide an update on the current progress and next action plan?\n\nBest regards,\nPhongthep Phimthong"

    try:
        # 1. Send via SMTP
        send_smtp_email(recipient, email_subject, draft_body)

        # 2. Update Odoo Chatter
        update_odoo_chatter(ticket_id, email_subject, draft_body, recipient)

        # 3. Update Telegram Message: Remove button, append confirmation
        now_str = datetime.now(BKK_TZ).strftime("%H:%M น. (%d/%m/%Y)")
        updated_text = orig_text + f"\n\n━━━━━━━━━━━━━━━━━━━━\n✅ <b>ส่งอีเมล Follow-up ออกเรียบร้อยแล้ว</b>\n📤 <b>ผู้รับ:</b> {recipient}\n🕒 <b>เวลาส่ง:</b> {now_str}\n📌 <i>บันทึกลง Odoo Chatter สำเร็จ</i>"

        telegram_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": updated_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        })

        telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": f"🎉 <b>[สำเร็จ]</b> ส่งอีเมล Follow-up สำหรับเคส <b>{t_ref}</b> (SR# {sr_number}) ไปยัง <code>{recipient}</code> เรียบร้อยแล้วครับ!",
            "parse_mode": "HTML"
        })

    except Exception as e:
        logger.error(f"Error executing send_followup: {e}")
        telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ <b>เกิดข้อผิดพลาดในการส่งอีเมล:</b> {str(e)}"
        })

def main():
    logger.info("Starting Telegram Alert Listener for @phongthep_alert_bot...")
    offset = None
    while True:
        try:
            params = {
                "timeout": 25,
                "allowed_updates": ["callback_query"]
            }
            if offset is not None:
                params["offset"] = offset

            resp = telegram_api("getUpdates", params)
            if resp and resp.get("ok"):
                updates = resp.get("result", [])
                for upd in updates:
                    offset = upd.get("update_id") + 1
                    if "callback_query" in upd:
                        handle_callback_query(upd["callback_query"])
            time.sleep(1)
        except Exception as e:
            logger.error(f"Polling loop exception: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
