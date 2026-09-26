#!/usr/bin/env python3
"""
Odoo 17 Helpdesk AI Assistant Bot for Captain
- Queries Odoo database directly from Docker container (my-odoo-db-1)
- Uses Groq (gpt-oss-120b / qwen3.8-27b) and Gemini fallback for AI intelligence
- Supports interactive Q&A in Telegram (@OdooHelpdesk99bot)
- Sends automated daily summaries at 08:30 and 18:00
"""

import os
import sys
import json
import time
import datetime
import subprocess
import urllib.request
import urllib.parse

BOT_TOKEN = "8761131136:AAHyWSwn1McAArISZiFLcJauntnR7vm5Fo8"
ALLOWED_USER_ID = 8948351323  # Captain Phongthep

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6LX97bgqEGNVk0AyodLxHtq7EE4uDBGlscak3lPdC7G9A")

def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # If Markdown parsing fails, fallback to plain text
        payload.pop('parse_mode', None)
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except Exception as ex:
            print(f"Failed to send telegram message: {ex}")
            return None
    except Exception as e:
        print(f"Error sending telegram message: {e}")
        return None

def get_odoo_tickets(limit=30, search_term=None):
    where_clause = ""
    if search_term:
        clean_term = search_term.replace("'", "''")
        where_clause = f"WHERE name ILIKE '%{clean_term}%' OR subject ILIKE '%{clean_term}%' OR avaya_customer_name ILIKE '%{clean_term}%' OR avaya_sr_number ILIKE '%{clean_term}%'"
    
    query = f"""
    SELECT json_agg(t) FROM (
        SELECT id, name, subject, avaya_customer_name, state, priority, 
               avaya_sr_number, avaya_product, avaya_severity, avaya_contact_name,
               to_char(create_date, 'YYYY-MM-DD HH24:MI') as created
        FROM helpdesk_ticket_pro 
        {where_clause}
        ORDER BY id DESC LIMIT {limit}
    ) t;
    """
    cmd = ['docker', 'exec', 'my-odoo-db-1', 'psql', '-U', 'odoo', '-d', 'phongthep', '-t', '-A', '-c', query]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = res.stdout.strip()
        if out and out != "":
            return json.loads(out)
    except Exception as e:
        print(f"Error querying Odoo: {e}")
    return []

def ask_groq(prompt, system_instruction):
    models = ['openai/gpt-oss-120b', 'qwen/qwen3.8-27b', 'openai/gpt-oss-20b']
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        'Authorization': f"Bearer {GROQ_API_KEY}",
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    
    for model in models:
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_instruction},
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.3,
            'max_tokens': 1200
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode())
                return res['choices'][0]['message']['content']
        except Exception as e:
            print(f"Groq model {model} failed: {e}")
            continue
    return None

def ask_gemini(prompt, system_instruction):
    gemini_models = ['gemini-3-flash-preview', 'gemini-3.1-pro-preview', 'gemini-flash-latest']
    combined_prompt = f"{system_instruction}\n\n{prompt}"
    payload = {'contents': [{'parts': [{'text': combined_prompt}]}]}
    data = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    
    for model in gemini_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode())
                return res['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            print(f"Gemini model {model} failed: {e}")
            continue
    return None

def ask_ai(user_prompt, tickets_context):
    system_instruction = (
        "คุณคือ 'น้อง Odoo AI' ผู้ช่วยอัจฉริยะประจำระบบ Odoo 17 Helpdesk ของกัปตัน (Captain Phongthep) "
        "บทบาทของคุณ: สรุปตั๋วงาน วิเคราะห์ปัญหา ตอบคำถามสถานะเคสของลูกค้า (เช่น MEA, AIS, True, Avaya) อย่างแม่นยำ กระชับ สุภาพ และเป็นมืออาชีพ "
        "ข้อกำหนดในการตอบ:\n"
        "1. ตอบเป็นภาษาไทย มีอิโมจิประกอบให้อ่านง่าย สบายตา\n"
        "2. ใช้ Markdown จัดหัวข้อ และ bullet list\n"
        "3. อ้างอิงเลขตั๋ว (เช่น TKT-xxxxx), ชื่อลูกค้า, ระดับความรุนแรง (P1/P2/P3), และสถานะ (State) ให้ชัดเจนเสมอ\n"
        "4. หากคำถามถามเฉพาะเจาะจง ให้ค้นหาและสรุปเฉพาะส่วนที่เกี่ยวข้องตรงประเด็น"
    )
    
    prompt = f"[ข้อมูลตั๋วงาน Odoo ล่าสุดในระบบ]:\n{json.dumps(tickets_context, ensure_ascii=False, indent=2)}\n\n[คำถาม/คำขอของกัปตัน]:\n{user_prompt}"
    
    # 1. Try Groq (Ultra fast <500ms)
    answer = ask_groq(prompt, system_instruction)
    if answer:
        return answer
        
    # 2. Try Gemini
    answer = ask_gemini(prompt, system_instruction)
    if answer:
        return answer
        
    return "ขออภัยครับกัปตัน ระบบ AI กำลังประมวลผลหนาแน่นชั่วคราว รบกวนลองส่งคำถามใหม่อีกครั้งในสักครู่ครับ"

def generate_daily_summary():
    tickets = get_odoo_tickets(limit=35)
    prompt = (
        "ช่วยจัดทำรายงานสรุปเคสประจำวัน (Daily Helpdesk Executive Brief) สำหรับกัปตัน:\n"
        "1. ภาพรวมตัวเลข (จำนวนเคสทั้งหมด, เคสใหม่, กำลังดำเนินการ, รอลูกค้า, ปิดแล้ว)\n"
        "2. ไฮไลท์เคสสำคัญ/เคสด่วน (P1/P2 หรือเคสที่มีประเด็นต้องติดตามใกล้ชิด พร้อมเลขตั๋วและชื่อลูกค้า)\n"
        "3. ข้อเสนอแนะหรือ Action items ที่กัปตันควร follow up ในวันนี้"
    )
    return ask_ai(prompt, tickets)

def get_updates(offset=0):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {'ok': False}

def main():
    print("Starting Odoo AI Telegram Assistant with Groq & Gemini...")
    offset = 0
    last_daily_sent = None
    
    # Send online notification to Captain
    startup_msg = (
        "🟢 *Odoo AI Helpdesk Assistant (Groq 120B Enhanced) พร้อมทำงานครับ!*\n\n"
        "กัปตันสามารถสั่งการได้ทันที เช่น:\n"
        "• พิมพ์ `/summary` หรือ `สรุปเคส` เพื่อดูภาพรวมงานล่าสุด\n"
        "• พิมพ์ถามเรื่องเคส เช่น *'มีเคส MEA อะไรบ้าง?'*, *'เคส P1 ค้างกี่เคส?'*\n"
        "• ระบบจะส่ง Daily Summary อัตโนมัติเวลา 08:30 และ 18:00 ครับ 🚀"
    )
    send_telegram(ALLOWED_USER_ID, startup_msg)
    
    while True:
        try:
            # Check for scheduled daily summary (08:30 and 18:00)
            now = datetime.datetime.now()
            current_date_hour_min = (now.strftime('%Y-%m-%d'), now.hour, now.minute)
            if (now.hour == 8 and now.minute == 30) or (now.hour == 18 and now.minute == 0):
                if last_daily_sent != current_date_hour_min:
                    summary_text = generate_daily_summary()
                    send_telegram(ALLOWED_USER_ID, summary_text)
                    last_daily_sent = current_date_hour_min
            
            # Poll Telegram updates
            updates = get_updates(offset)
            if updates.get('ok') and updates.get('result'):
                for item in updates['result']:
                    offset = item['update_id'] + 1
                    msg = item.get('message', {})
                    from_user = msg.get('from', {})
                    user_id = from_user.get('id')
                    text = msg.get('text', '').strip()
                    chat_id = msg.get('chat', {}).get('id')
                    
                    if not text:
                        continue
                    
                    if user_id != ALLOWED_USER_ID:
                        send_telegram(chat_id, "ขออภัยครับ ระบบนี้สงวนสิทธิ์เฉพาะกัปตันเท่านั้นครับ 🔒")
                        continue
                    
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Question from Captain: {text}")
                    
                    # Handle /start
                    if text in ['/start', 'start']:
                        send_telegram(chat_id, "สวัสดีครับกัปตัน! ผมคือ **Odoo AI Helpdesk** พร้อมช่วยตอบคำถามและสรุปเคสงานครับ 🛠️ พิมพ์ถามได้เลยครับ!")
                        continue
                    
                    # Handle /summary or keywords
                    if text in ['/summary', '/today', 'สรุป', 'สรุปงาน', 'สรุปเคส', 'รายงานประจำวัน']:
                        send_telegram(chat_id, "⏳ กำลังประมวลผลสรุปเคสล่าสุดจาก Odoo ให้ครับ สักครู่...")
                        summary_msg = generate_daily_summary()
                        send_telegram(chat_id, summary_msg)
                        continue
                    
                    # Answer general question with tickets context
                    send_telegram(chat_id, "🔍 กำลังตรวจสอบข้อมูลใน Odoo ให้ครับ...")
                    tickets = get_odoo_tickets(limit=35)
                    ai_reply = ask_ai(text, tickets)
                    send_telegram(chat_id, ai_reply)
            
            time.sleep(0.5)
        except Exception as e:
            print(f"Main loop error: {e}")
            time.sleep(3)

if __name__ == '__main__':
    main()
