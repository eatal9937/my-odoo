#!/usr/bin/env python3
import urllib.request
import json
import datetime
import sys
from send_line_alert import send_line_push

def generate_morning_briefing():
    url = "https://phongthep.lol/helpdesk/api/search_tickets?query="
    req = urllib.request.Request(url, headers={"User-Agent": "curl/7.88.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"[Error fetching tickets] {e}", file=sys.stderr)
        return None

    tickets = data.get("data", [])
    
    # Filter active tickets
    active_states = ["new", "in_progress", "waiting_customer", "waiting_partner"]
    active_tickets = [t for t in tickets if t.get("state") in active_states]
    closed_tickets = [t for t in tickets if t.get("state") in ["closed", "solved"]]

    # Thai days and months
    now = datetime.datetime.now()
    thai_days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
    thai_months = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    
    day_name = thai_days[now.weekday()]
    month_name = thai_months[now.month - 1]
    thai_year = now.year + 543
    date_str = f"วัน{day_name}ที่ {now.day} {month_name} {thai_year}"

    state_trans = {
        "new": "🔵 รอดำเนินการ",
        "in_progress": "🟡 กำลังดำเนินการ",
        "waiting_customer": "🟣 รอลูกค้า",
        "waiting_partner": "🟠 รอ Avaya TAC"
    }

    # Group by engineer
    by_eng = {}
    for t in active_tickets:
        eng = t.get("site_engineer") or "Phongthep Phimthong"
        by_eng.setdefault(eng, []).append(t)

    report = f"☀️ [อรุณสวัสดิ์ทีมงาน! สรุปภารกิจประจำวัน]\n"
    report += f"📅 {date_str}\n"
    report += f"━━━━━━━━━━━━━━━━━━━━\n"
    report += f"📊 สถานะภาพรวม: 🟡 กำลังทำ: {len([t for t in active_tickets if t.get('state') == 'in_progress'])} | 🔵 ใหม่: {len([t for t in active_tickets if t.get('state') == 'new'])} | 🟢 ปิดแล้ว: {len(closed_tickets)}\n"
    report += f"━━━━━━━━━━━━━━━━━━━━\n"

    if not active_tickets:
        report += "🎉 ยอดเยี่ยมมาก! ขณะนี้ไม่มีเคสค้างในระบบครับ\n"
    else:
        report += "📋 เคสที่ต้องดูแลแยกตามวิศวกร:\n\n"
        for eng, t_list in by_eng.items():
            report += f"👤 @{eng} ({len(t_list)} เคส):\n"
            for t in t_list[:3]:
                st = state_trans.get(t.get("state"), t.get("state"))
                sr_text = f" | Avaya: {t.get('sr_number')}" if t.get('sr_number') else ""
                report += f"  • 🎫 {t.get('ref')} - {t.get('customer')}\n"
                report += f"    {st}{sr_text}\n"
                report += f"    🌐 {t.get('portal_url')}\n"
            if len(t_list) > 3:
                report += f"    ...และอีก {len(t_list) - 3} เคส\n"
            report += "\n"

    report += "━━━━━━━━━━━━━━━━━━━━\n"
    report += "💡 ขอให้ทุกท่านปฏิบัติงานด้วยความปลอดภัยครับ!\n"
    report += "(พิมพ์ 'สรุปเช้า' เพื่อเรียกดูข้อมูลอัปเดตได้ตลอดเวลา)"
    return report

if __name__ == "__main__":
    briefing = generate_morning_briefing()
    if briefing:
        print("=== GENERATED BRIEFING ===")
        print(briefing)
        if "--send" in sys.argv:
            send_line_push(briefing)
            print("[Sent to LINE Group successfully]")
