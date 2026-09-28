import os
import json
import re
import ssl
import logging
import threading
import urllib.request
import urllib.error
from datetime import datetime, time
from markupsafe import Markup
from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)

AI_CACHE_FILE = "/var/lib/odoo/ai_ticket_summaries.json"
_AI_MEM_CACHE = {}
_ACTIVE_LLM_TASKS = set()
_CACHE_LOCK = threading.Lock()
_CACHE_MTIME = 0

def _load_ai_cache():
    global _AI_MEM_CACHE, _CACHE_MTIME
    with _CACHE_LOCK:
        for p in [AI_CACHE_FILE, "/tmp/ai_ticket_summaries.json"]:
            if os.path.exists(p):
                try:
                    mtime = os.path.getmtime(p)
                    if _AI_MEM_CACHE and mtime == _CACHE_MTIME:
                        return _AI_MEM_CACHE
                    with open(p, "r", encoding="utf-8") as f:
                        _AI_MEM_CACHE = json.load(f)
                        _CACHE_MTIME = mtime
                        return _AI_MEM_CACHE
                except Exception:
                    pass
        if _AI_MEM_CACHE is not None:
            return _AI_MEM_CACHE
        _AI_MEM_CACHE = {}
        return _AI_MEM_CACHE

def _save_ai_cache(cache_data):
    global _AI_MEM_CACHE
    with _CACHE_LOCK:
        _AI_MEM_CACHE = cache_data
        for p in [AI_CACHE_FILE, "/tmp/ai_ticket_summaries.json"]:
            try:
                d = os.path.dirname(p)
                if d and not os.path.exists(d):
                    os.makedirs(d, exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)
                break
            except Exception:
                continue

def _clean_text_snippet(html_str):
    if not html_str:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_str, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    
    # Strip standard automated boilerplate footers from Avaya notifications
    text = re.sub(r'Management Escalation Activity Process.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'On-Line Support Login and access improved knowledge search.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'Opt-Out: You are receiving this email because.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'Note: This is an automated notification\. Please do not reply.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def _extract_fresh_reply_text(html_body):
    if not html_body:
        return ""
    text = _clean_text_snippet(html_body)
    quote_splitters = [
        r'[-_]{3,}\s*Original Message\s*[-_]{3,}',
        r'On\s+[A-Za-z]{3},\s+[A-Za-z]{3}\s+\d+.*wrote:',
        r'On\s+[A-Za-z]+,\s+[A-Za-z]+\s+\d+.*wrote:',
        r'From:\s+.*Sent:\s+.*To:',
        r'________________________________',
        r'Reply above this line',
        r'Management Escalation Activity Process',
    ]
    fresh_text = text
    for pattern in quote_splitters:
        m = re.split(pattern, fresh_text, flags=re.IGNORECASE)
        if len(m) > 1 and len(m[0].strip()) > 15:
            fresh_text = m[0].strip()
            break
    return fresh_text

GROQ_API_KEY = "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"
GEMINI_API_KEY = "AIzaSyANKzGWQHMWSnsfS0iaNadvobovVO6377U"

def _format_conversation_history(ticket, messages, max_msgs=6):
    content_msgs = [m for m in messages if m.get("body") and len(_clean_text_snippet(m.get("body", "")).strip()) > 3]
    recent = content_msgs[:max_msgs]
    recent = list(reversed(recent))
    history_lines = []
    for idx, m in enumerate(recent, start=1):
        dt = str(m.get("date") or "")[:16]
        raw_b = str(m.get("body") or "")
        author = str(m.get("author") or m.get("email_from") or "Unknown")
        if "📤" in raw_b or "เราตอบกลับ" in raw_b or "phongthep" in author.lower():
            sender = "วิศวกร JADS (Phongthep)"
        elif "📩" in raw_b or "avaya" in author.lower() or "@avaya.com" in raw_b.lower():
            sender = f"Avaya Support ({author})"
        else:
            sender = author
        clean_text = _extract_fresh_reply_text(raw_b)
        if clean_text:
            snippet = clean_text[:450].replace("\n", " ")
            history_lines.append(f"{idx}. [{dt}] {sender}: {snippet}")
    return "\n\n".join(history_lines)

def _call_cloud_ai_llm(ticket_subject, sender_name, latest_text, customer_name="", history_text=""):
    """
    Calls Groq (openai/gpt-oss-120b) or Gemini (gemini-2.5-flash) to extract precise
    stage, action owner, next action, and status summary in Thai & English with full context.
    """
    system_prompt = (
        "You are an elite Telecom Support AI for JADS Comm (Avaya Partner in Thailand).\n"
        "The Partner Engineer is 'วิศวกร JADS (Phongthep)'.\n"
        "The Customer/Client is indicated in the ticket details (e.g. Thanachart, MEA, etc.).\n"
        "Analyze the full conversation history to understand what was already attempted and what is the current blocker.\n"
        "CRITICAL RULE: Never repeat an old action item that has already been executed in the conversation history (e.g. if a config parameter was already changed, do not suggest changing it again).\n"
        "Determine the EXACT current state, who currently has the action item, and the specific next step.\n"
        "Possible Action Owners:\n"
        "- 'ลูกค้า/ผู้ใช้งาน (<Customer Name>)' if waiting for information, testing, confirmation, or client device logs from the customer/user.\n"
        "- 'วิศวกร JADS (Phongthep)' if JADS engineer needs to investigate, configure, test, join meeting, or upload data to TAC.\n"
        "- 'วิศวกร Avaya (<Name/TAC>)' if waiting for Avaya TAC to analyze logs, respond, or provide next steps.\n"
        "Return strict JSON:\n"
        "{\n"
        '  "stage_badge_th": "Short Thai status badge (max 25 chars, e.g. รอ Logs จากผู้ใช้, รอ TAC วิเคราะห์, รอแก้ไขระบบ, นัดหมาย Session)",\n'
        '  "stage_badge_en": "Short English status badge (max 25 chars, e.g. Waiting for Client Logs, TAC Analyzing, Meeting Scheduled)",\n'
        '  "stage_title_th": "Thai title describing current status and progress in detail",\n'
        '  "stage_title_en": "English title describing current status and progress in detail",\n'
        '  "status_summary_th": "Concise Thai explanation of what has been done and what the current status is",\n'
        '  "status_summary_en": "Concise English explanation of what has been done and what the current status is",\n'
        '  "next_action_th": "Clear Thai sentence specifying the exact next action to take",\n'
        '  "next_action_en": "Clear English sentence specifying the exact next action to take",\n'
        '  "action_owner_th": "Name of who must act in Thai (e.g. ลูกค้า/ผู้ใช้งาน (Thanachart), วิศวกร JADS (Phongthep), วิศวกร Avaya (Akshat Mehta))",\n'
        '  "action_owner_en": "Name of who must act in English (e.g. Customer/User (Thanachart), JADS Engineering (Phongthep), Avaya Engineer (Akshat Mehta))",\n'
        '  "action_priority": 1 if JADS or Customer must act else 2\n'
        "}"
    )
    if history_text:
        user_prompt = (
            f"Ticket: {ticket_subject}\n"
            f"Customer: {customer_name or 'General Customer'}\n\n"
            f"Conversation History (chronological order):\n{history_text}\n\n"
            f"Latest Message Sender: {sender_name}\n"
            f"Latest Message Body:\n{latest_text[:600]}"
        )
    else:
        user_prompt = f"Ticket: {ticket_subject}\nCustomer: {customer_name}\nLatest Message Sender: {sender_name}\nMessage Body:\n{latest_text[:2500]}"
    
    # 1. Try Groq (Fastest: ~0.8s)
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            }
        )
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return parsed
    except Exception as e:
        _logger.warning("Groq AI failed, attempting Gemini: %s", e)
        
    # 2. Try Gemini 2.5 Flash
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{
                "parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2
            }
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(content)
            return parsed
    except Exception as e:
        _logger.warning("Gemini AI failed: %s", e)
        
    return None

def _async_cloud_ai_synthesize(ticket_id, ticket_subject, sender_name, latest_text, base_summary, cache_key, customer_name="", history_text=""):
    global _ACTIVE_LLM_TASKS
    if ticket_id in _ACTIVE_LLM_TASKS:
        return
    _ACTIVE_LLM_TASKS.add(ticket_id)
    try:
        parsed = _call_cloud_ai_llm(ticket_subject, sender_name, latest_text, customer_name=customer_name, history_text=history_text)
        if parsed and isinstance(parsed, dict) and parsed.get("stage_badge_th") and parsed.get("next_action_th"):
            updated = dict(base_summary)
            updated["stage_badge"] = {
                "en": parsed.get("stage_badge_en") or updated.get("stage_badge", {}).get("en", "In Progress"),
                "th": parsed.get("stage_badge_th") or updated.get("stage_badge", {}).get("th", "กำลังดำเนินการ")
            }
            if parsed.get("stage_title_th"):
                updated["stage_title"] = {
                    "en": parsed.get("stage_title_en") or updated.get("stage_title", {}).get("en", ""),
                    "th": parsed.get("stage_title_th") or updated.get("stage_title", {}).get("th", "")
                }
            if parsed.get("status_summary_th"):
                updated["status_summary"] = {
                    "en": parsed.get("status_summary_en") or updated.get("status_summary", {}).get("en", ""),
                    "th": parsed.get("status_summary_th") or updated.get("status_summary", {}).get("th", "")
                }
            if parsed.get("next_action_th"):
                updated["next_action"] = {
                    "en": parsed.get("next_action_en") or updated.get("next_action", {}).get("en", ""),
                    "th": parsed.get("next_action_th") or updated.get("next_action", {}).get("th", "")
                }
            if parsed.get("action_owner_th"):
                updated["action_owner"] = {
                    "en": parsed.get("action_owner_en") or updated.get("action_owner", {}).get("en", ""),
                    "th": parsed.get("action_owner_th") or updated.get("action_owner", {}).get("th", "")
                }
            if "action_priority" in parsed:
                updated["action_priority"] = int(parsed["action_priority"])
            
            # Badge styling based on priority/owner
            owner_str = str(updated.get("action_owner", {})).lower()
            if "ลูกค้า" in owner_str or "customer" in owner_str or "user" in owner_str:
                updated["badge_color"] = "bg-purple-500/20 text-purple-300 border-purple-500/30"
                updated["stage_key"] = "awaiting_customer"
            elif updated.get("action_priority") == 1 or "jads" in owner_str or "phongthep" in owner_str:
                updated["badge_color"] = "bg-amber-400/20 text-amber-300 border-amber-400/30"
                updated["stage_key"] = "action_required"
            else:
                updated["badge_color"] = "bg-sky-500/20 text-sky-300 border-sky-500/30"
                updated["stage_key"] = "awaiting_vendor"
                
            updated["source"] = "Cloud AI (Groq / Gemini) & Hybrid Engine"
            updated["_cache_key"] = cache_key
            updated["_is_llm"] = True
            
            cache = _load_ai_cache()
            cache[str(ticket_id)] = updated
            _save_ai_cache(cache)
    except Exception as e:
        _logger.warning("Error in _async_cloud_ai_synthesize for ticket %s: %s", ticket_id, e)
    finally:
        _ACTIVE_LLM_TASKS.discard(ticket_id)

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

    @http.route(["/helpdesk/ticket/<string:identifier>", "/helpdesk/view/<string:identifier>"], type="http", auth="public", website=False)
    def helpdesk_ticket_view(self, identifier, **kwargs):
        addon_path = os.path.dirname(os.path.dirname(__file__))
        html_path = os.path.join(addon_path, "static", "src", "ticket_public_detail.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            return request.make_response(html_content, headers=[("Content-Type", "text/html"), ("Cache-Control", "no-cache")])
        return "Helpdesk Ticket Viewer HTML file not found."

    @http.route("/helpdesk/api/ticket_detail/<string:identifier>", type="http", auth="public", methods=["GET"], csrf=False)
    def helpdesk_api_ticket_detail(self, identifier, **kwargs):
        tickets_model = request.env["helpdesk.ticket.pro"].sudo()
        ident = identifier.strip() if identifier else ""

        ticket = None
        if ident.isdigit():
            ticket = tickets_model.browse(int(ident))
            if not ticket.exists():
                ticket = None
        
        if not ticket:
            ticket = tickets_model.search(["|", "|", ("name", "=", ident), ("avaya_sr_number", "=", ident), ("subject", "ilike", ident)], order="id desc", limit=1)

        if not ticket or not ticket.exists():
            return request.make_response(
                json.dumps({"success": False, "error": "ไม่พบข้อมูลตั๋วใบงานในระบบ"}),
                headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
            )

        c_name = ticket.partner_id.name or ticket.avaya_customer_name or ""
        prod_name = ticket.avaya_product or (ticket.category_id.name if ticket.category_id else "General")

        ticket_data = {
            "id": ticket.id,
            "ref": ticket.name,
            "subject": ticket.subject,
            "category": ticket.category_id.name if ticket.category_id else "General",
            "priority": ticket.priority or "0",
            "state": ticket.state or "new",
            "ticket_type": ticket.ticket_type or "incident",
            "ticket_source": ticket.ticket_source or "customer",
            "is_avaya_support": bool(ticket.is_avaya_support),
            "avaya_sr_number": ticket.avaya_sr_number or "",
            "sold_to_id": ticket.sold_to_id or "",
            "avaya_product": prod_name,
            "avaya_severity": ticket.avaya_severity or "",
            "avaya_status": ticket.avaya_status or "",
            "avaya_contact_name": ticket.avaya_contact_name or "",
            "site_engineer": getattr(ticket, "site_engineer", False) or "Phongthep Phimthong",
            "avaya_contact_phone": ticket.avaya_contact_phone or "",
            "avaya_contact_email": ticket.avaya_contact_email or "",
            "avaya_date_reported": ticket.avaya_date_reported or "",
            "avaya_customer_name": ticket.avaya_customer_name or "",
            "avaya_location": ticket.avaya_location or "",
            "avaya_owner_sbl": ticket.avaya_owner_sbl or "",
            "avaya_portal_url": ticket.avaya_portal_url or "",
            "assigned_to": getattr(ticket, "site_engineer", None) or ticket.user_id.name or "Phongthep Phimthong",
            "site_engineer": getattr(ticket, "site_engineer", None) or "Phongthep Phimthong",
            "customer": c_name or "Not Specified",
            "description": ticket.description or "",
            "create_date": ticket.create_date.isoformat() + "Z" if ticket.create_date else None,
            "write_date": ticket.write_date.isoformat() + "Z" if ticket.write_date else None,
            "deadline": ticket.deadline.isoformat() + "Z" if ticket.deadline else None,
            "portal_url": f"/helpdesk/ticket/{ticket.id}",
            "backend_url": f"https://phongthep.lol/web#id={ticket.id}&model=helpdesk.ticket.pro&view_type=form"
        }

        messages_model = request.env["mail.message"].sudo()
        msgs = messages_model.search([
            ("res_id", "=", ticket.id),
            ("model", "=", "helpdesk.ticket.pro"),
            ("message_type", "in", ["comment", "email"])
        ], order="id desc")

        messages_list = []
        for m in msgs:
            if not m.body:
                continue
            messages_list.append({
                "id": m.id,
                "date": m.date.isoformat() + "Z" if m.date else None,
                "author": m.author_id.name if m.author_id else (m.email_from or "Support"),
                "email_from": m.email_from or "",
                "subject": m.subject or "",
                "body": m.body or "",
                "subtype": m.subtype_id.name if m.subtype_id else ""
            })

        # Generate AI Incident Stage & Context Summary
        ai_summary = self._synthesize_ticket_ai_stage(ticket, messages_list)

        return request.make_response(
            json.dumps({
                "success": True,
                "ticket": ticket_data,
                "messages": messages_list,
                "ai_summary": ai_summary
            }),
            headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
        )

    def _synthesize_ticket_ai_stage(self, ticket, messages):
        """
        Hybrid AI Synthesizer:
        1. Checks persistent cache for an up-to-date LLM summary.
        2. Computes instant baseline Rule-based summary (<0.02s).
        3. Asynchronously triggers Ollama (192.168.10.30) to enrich the summary.
        4. Returns immediately so the web page never hangs.
        """
        content_msgs = [m for m in messages if m.get("body") and len(_clean_text_snippet(m.get("body", "")).strip()) > 3]
        latest_msg_id = content_msgs[0]["id"] if content_msgs else (messages[0]["id"] if messages else 0)
        cache_key = f"{ticket.id}_{latest_msg_id}_{ticket.write_date}"
        
        cache = _load_ai_cache()
        cached = cache.get(str(ticket.id))
        
        # If an up-to-date summary exists for this exact ticket state, return it immediately
        if cached and cached.get("_cache_key") == cache_key:
            return cached
            
        # Compute baseline rule-based summary instantly
        rule_summary = self._compute_rule_based_ai_stage(ticket, content_msgs)
        rule_summary["_cache_key"] = cache_key
        
        # If not cached or ticket has updated, compute Cloud AI synthesis and save
        if not cached or cached.get("_cache_key") != cache_key:
            if rule_summary.get("_lock_rule"):
                cache[str(ticket.id)] = rule_summary
                _save_ai_cache(cache)
                return rule_summary

            latest_msg = content_msgs[0] if content_msgs else {}
            fresh_content = _extract_fresh_reply_text(latest_msg.get("body", "") or ticket.description or "")
            sender = latest_msg.get("author", "") or ticket.avaya_contact_name or "Avaya Support"
            cust_name = ticket.partner_id.name or ticket.avaya_customer_name or ""
            history_text = _format_conversation_history(ticket, messages)
            
            try:
                parsed = _call_cloud_ai_llm(ticket.subject, sender, fresh_content, customer_name=cust_name, history_text=history_text)
                if parsed and isinstance(parsed, dict) and parsed.get("stage_badge_th") and parsed.get("next_action_th"):
                    rule_summary["stage_badge"] = {
                        "en": parsed.get("stage_badge_en") or rule_summary.get("stage_badge", {}).get("en", "In Progress"),
                        "th": parsed.get("stage_badge_th") or rule_summary.get("stage_badge", {}).get("th", "กำลังดำเนินการ")
                    }
                    if parsed.get("stage_title_th"):
                        rule_summary["stage_title"] = {
                            "en": parsed.get("stage_title_en") or rule_summary.get("stage_title", {}).get("en", ""),
                            "th": parsed.get("stage_title_th") or rule_summary.get("stage_title", {}).get("th", "")
                        }
                    if parsed.get("status_summary_th"):
                        rule_summary["status_summary"] = {
                            "en": parsed.get("status_summary_en") or rule_summary.get("status_summary", {}).get("en", ""),
                            "th": parsed.get("status_summary_th") or rule_summary.get("status_summary", {}).get("th", "")
                        }
                    if parsed.get("next_action_th"):
                        rule_summary["next_action"] = {
                            "en": parsed.get("next_action_en") or rule_summary.get("next_action", {}).get("en", ""),
                            "th": parsed.get("next_action_th") or rule_summary.get("next_action", {}).get("th", "")
                        }
                    if parsed.get("action_owner_th"):
                        rule_summary["action_owner"] = {
                            "en": parsed.get("action_owner_en") or rule_summary.get("action_owner", {}).get("en", ""),
                            "th": parsed.get("action_owner_th") or rule_summary.get("action_owner", {}).get("th", "")
                        }
                    if "action_priority" in parsed:
                        rule_summary["action_priority"] = int(parsed["action_priority"])
                    
                    owner_str = str(rule_summary.get("action_owner", {})).lower()
                    if "ลูกค้า" in owner_str or "customer" in owner_str or "user" in owner_str:
                        rule_summary["badge_color"] = "bg-purple-500/20 text-purple-300 border-purple-500/30"
                        rule_summary["stage_key"] = "awaiting_customer"
                    elif rule_summary.get("action_priority") == 1 or any(k in owner_str for k in ["jads", "phongthep", "kai", "ไก่", "engineer", "site"]):
                        rule_summary["badge_color"] = "bg-amber-400/20 text-amber-300 border-amber-400/30"
                        rule_summary["stage_key"] = "action_required"
                    else:
                        rule_summary["badge_color"] = "bg-sky-500/20 text-sky-300 border-sky-500/30"
                        rule_summary["stage_key"] = "awaiting_vendor"
                        
                    rule_summary["source"] = "Cloud AI (Groq / Gemini) & Hybrid Engine"
                    rule_summary["_is_llm"] = True
                else:
                    rule_summary["source"] = "Hybrid AI (Instant Rule Engine)"
            except Exception as e:
                _logger.warning("Cloud AI synthesis in _synthesize_ticket_ai_stage failed: %s", e)
                rule_summary["source"] = "Hybrid AI (Instant Rule Engine)"
                
            cache[str(ticket.id)] = rule_summary
            _save_ai_cache(cache)
            return rule_summary
            
        return cached or rule_summary

    def _compute_rule_based_ai_stage(self, ticket, messages):
        """
        High-speed deterministic rule-based AI stage synthesizer.
        """
        clean_html_text = _clean_text_snippet

        is_closed = ticket.state in ["solved", "closed"]
        if is_closed:
            return {
                "stage_key": "closed",
                "badge_color": "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
                "stage_badge": {"en": "Resolved", "th": "ปิดงานเรียบร้อย"},
                "stage_title": {"en": "Incident Resolved & Closed", "th": "แก้ไขปัญหาและปิดตั๋วงานแล้ว"},
                "status_summary": {
                    "en": "The incident has been resolved and officially marked as completed.",
                    "th": "ใบงานนี้ได้รับการแก้ไขเสร็จสิ้นและปิดงานอย่างเป็นทางการเรียบร้อยแล้ว"
                },
                "next_action": {
                    "en": "No pending action required. Archival mode active.",
                    "th": "ไม่มีขั้นตอนค้าง ดำเนินการเสร็จสมบูรณ์"
                },
                "action_owner": {"en": "Ticket Closed", "th": "ปิดงานแล้ว"},
                "latest_note": {"en": "Issue resolved.", "th": "ปัญหาได้รับการแก้ไขแล้ว"},
                "source": "Local Ollama (192.168.10.30) & AI Engine"
            }

        # Filter messages to only include those with actual meaningful content
        content_msgs = [m for m in messages if m.get("body") and len(clean_html_text(m.get("body", "")).strip()) > 3]

        if not content_msgs:
            # If no content messages yet, check if ticket has description (e.g. from initial creation email)
            if ticket.description and len(ticket.description.strip()) > 10:
                raw_body = ticket.description or ""
                body_text = clean_html_text(raw_body)
                author = ticket.avaya_contact_name or "Avaya Support"
                latest_text = body_text.lower()
                recent_text = latest_text
            elif ticket.is_avaya_support or ticket.avaya_sr_number:
                prod_name = ticket.avaya_product or "Avaya System"
                return {
                    "stage_key": "awaiting_vendor",
                    "badge_color": "bg-sky-500/20 text-sky-300 border-sky-500/30",
                    "stage_badge": {"en": "Awaiting TAC", "th": "รอ TAC ตอบกลับ"},
                    "stage_title": {
                        "en": "New Service Request Opened / Awaiting TAC Assignment",
                        "th": "เปิดเคสใหม่เรียบร้อย / รอ Avaya TAC มอบหมายวิศวกร"
                    },
                    "status_summary": {
                        "en": f"New Service Request successfully logged for {prod_name}. Awaiting initial review and technical assignment by Avaya TAC engineering.",
                        "th": f"เปิดตั๋วใบงานใหม่สำหรับระบบ {prod_name} เรียบร้อยแล้ว อยู่ระหว่างรอฝ่ายสนับสนุน Avaya TAC ตรวจสอบและมอบหมายวิศวกรผู้เชี่ยวชาญ"
                    },
                    "next_action": {
                        "en": "Avaya TAC to assign technical specialist and review incident details.",
                        "th": "รอฝ่ายสนับสนุน Avaya TAC มอบหมายวิศวกรและเริ่มตรวจสอบรายละเอียดเคส"
                    },
                    "action_owner": {"en": "Avaya TAC Support", "th": "ทีมสนับสนุน Avaya TAC"},
                    "latest_note": {
                        "en": "Service request received and registered with Avaya support.",
                        "th": "ระบบ Avaya ได้รับข้อมูลการเปิดเคสแล้ว"
                    },
                    "source": "Hybrid AI (Instant Rule Engine)"
                }
            else:
                prod_name = ticket.category_id.name or "General Request"
                return {
                    "stage_key": "new",
                    "badge_color": "bg-blue-500/20 text-blue-300 border-blue-500/30",
                    "stage_badge": {"en": "New Request", "th": "ตั๋วงานใหม่"},
                    "stage_title": {"en": "Initial Triage & Dispatch", "th": "รับเรื่องและรอจ่ายงานเบื้องต้น"},
                    "status_summary": {
                        "en": f"Incident logged for {prod_name}. Awaiting initial engineer triage.",
                        "th": f"เปิดตั๋วงานใหม่สำหรับระบบ {prod_name} กำลังรอวิศวกรเริ่มตรวจสอบและวิเคราะห์"
                    },
                    "next_action": {
                        "en": "Assigned engineer to review incident scope and engage customer.",
                        "th": "วิศวกรผู้รับผิดชอบเข้าตรวจสอบขอบเขตปัญหาและประสานงานต่อไป"
                    },
                    "action_owner": {"en": ticket.user_id.name or "Helpdesk Dispatcher", "th": ticket.user_id.name or "วิศวกรประจำตั๋ว"},
                    "latest_note": {"en": "Awaiting initial response.", "th": "รอการตอบกลับเริ่มต้น"},
                    "source": "Local Ollama (192.168.10.30) & AI Engine"
                }
        else:
            latest_msg = content_msgs[0]
            raw_body = latest_msg.get("body", "")
            author = latest_msg.get("author", "")

        fresh_text = _extract_fresh_reply_text(raw_body)
        fresh_lower = fresh_text.lower()
        full_text = clean_html_text(raw_body).lower()

        is_avaya_inbound = "📩" in raw_body or "customerstatus@avaya.com" in raw_body.lower() or "from: customerstatus@avaya.com" in raw_body.lower() or "service request alert" in full_text or "open notification" in full_text
        is_outbound = ("📤" in raw_body or "เราตอบกลับ" in raw_body or "engineer reply" in raw_body.lower() or "sent) phongthep" in raw_body.lower()) and not is_avaya_inbound

        # Dynamically detect engineer name and role
        detected_eng = ""
        m_sig = re.search(r'\*([A-Za-z\s\.\-]+)\*\s*(?:<br/?>|\n|\r)\s*(?:Customer Support Engineer|Principal Support Engineer|Technical Support|Support Engineer|Backbone Engineer)', raw_body, re.IGNORECASE)
        if m_sig:
            detected_eng = m_sig.group(1).strip()
        if not detected_eng:
            m_head = re.search(r'<strong>([A-Za-z\s\,\.\-]+)</strong>\s*&lt;([a-zA-Z0-9_.+-]+@avaya\.com)&gt;', raw_body, re.IGNORECASE)
            if m_head:
                detected_eng = m_head.group(1).strip()
                if "," in detected_eng:
                    parts = [p.strip() for p in detected_eng.split(",")]
                    if len(parts) == 2:
                        detected_eng = f"{parts[1]} {parts[0]}"
        if not detected_eng:
            known_map = [
                (["akshat", "mehta"], "Akshat Mehta"),
                (["sophie", "zhao", "chunyu"], "Sophie Zhao"),
                (["hitesh", "tahilyani"], "Hitesh Kumar Tahilyani"),
                (["sreenath"], "Sreenath A"),
                (["jay", "thomas"], "Jay Thomas"),
                (["ayyappa", "borra"], "Ayyappa Borra"),
                (["asmita", "sharma"], "Asmita Sharma"),
                (["vinaya", "kumar"], "Vinaya Kumar"),
                (["renjoy", "henry"], "Renjoy Henry"),
                (["mitch", "zhang", "jian"], "Mitch Zhang"),
            ]
            for keys, name in known_map:
                if any(k in fresh_lower for k in keys) or any(k in raw_body.lower() for k in keys):
                    detected_eng = name
                    break

        eng_disp_th = f"วิศวกร Avaya ({detected_eng})" if detected_eng else "วิศวกร Avaya TAC"
        eng_disp_en = f"Avaya Engineer ({detected_eng})" if detected_eng else "Avaya TAC Engineer"

        # Dynamically determine internal site engineer display
        eng_raw = getattr(ticket, "site_engineer", None) or (ticket.user_id.name if ticket.user_id else "") or ""
        eng_raw_lower = eng_raw.lower()
        if "kai" in eng_raw_lower:
            site_eng_th = "พี่ไก่ อำนาจ (kai_amnat)"
            site_eng_en = "Kai Amnat (Site Engineer)"
        elif "beer" in eng_raw_lower:
            site_eng_th = "วิศวกร JADS (Beer)"
            site_eng_en = "JADS Engineer (Beer)"
        elif "chalerm" in eng_raw_lower:
            site_eng_th = "วิศวกร JADS (เฉลิม)"
            site_eng_en = "JADS Engineer (Chalerm)"
        elif "jackie" in eng_raw_lower:
            site_eng_th = "วิศวกร JADS (แจ็คกี้)"
            site_eng_en = "JADS Engineer (Jackie)"
        elif "nueng" in eng_raw_lower:
            site_eng_th = "วิศวกร JADS (หนึ่ง)"
            site_eng_en = "JADS Engineer (Nueng)"
        elif "romance" in eng_raw_lower:
            site_eng_th = "วิศวกร JADS (โรม)"
            site_eng_en = "JADS Engineer (Romance)"
        elif "surawat" in eng_raw_lower:
            site_eng_th = "วิศวกร JADS (Surawat)"
            site_eng_en = "JADS Engineer (Surawat)"
        elif "toh" in eng_raw_lower:
            site_eng_th = "วิศวกร JADS (โต้ง/ต่อ)"
            site_eng_en = "JADS Engineer (Toh)"
        elif "ton" in eng_raw_lower:
            site_eng_th = "วิศวกร JADS (ต้น)"
            site_eng_en = "JADS Engineer (Ton)"
        elif any(k in eng_raw_lower for k in ["phongthep", "captain", "phongthe", "แคป"]):
            site_eng_th = "วิศวกร JADS (Phongthep / Captain_Phongthe☺️😆)"
            site_eng_en = "JADS Engineer (Phongthep / Captain_Phongthe)"
        elif eng_raw:
            site_eng_th = f"วิศวกรผู้ดูแล ({eng_raw})"
            site_eng_en = f"Site Engineer ({eng_raw})"
        else:
            site_eng_th = "วิศวกร JADS"
            site_eng_en = "JADS Engineering"

        # PRIORITY 0: Avaya TAC SR Closed but Internal Action Required / Maintenance Window Pending!
        # (e.g. Mitch Zhang diagnosed JBoss SWAP exhaustion from rrdcached -> Known issue fixed in SM 10.1.3.4+)
        all_bodies_str = " ".join([(m.get("body") or "") for m in content_msgs]).lower()
        is_tac_closed = (
            any(k in fresh_lower or k in full_text for k in [
                "sr closed", "sr is closed", "case closed", "ticket closed", "closed notification"
            ]) or
            getattr(ticket, "avaya_status", "").lower() in ["closed", "solved"] or
            any("sr closed" in (m.get("body", "") or "").lower() for m in content_msgs[:2])
        )

        if is_tac_closed and ticket.state not in ["solved", "closed"]:
            p_name = ticket.avaya_product or "System"
            cust_name = ticket.partner_id.name or ticket.avaya_customer_name or "ลูกค้า"
            
            # Check for RCA / Upgrade / Known Issue in conversation history
            has_upgrade_req = any(k in all_bodies_str for k in [
                "upgrad", "known issue", "patch", "workaround", "permanent fix", "swap exhaustion", "rrdcached"
            ])
            
            if has_upgrade_req:
                if "swap" in all_bodies_str or "rrdcached" in all_bodies_str:
                    action_th = f"{site_eng_th}: ประสานงาน {cust_name} เพื่อนัดหมาย Maintenance Window อัปเกรด SMGR/SM เป็นเวอร์ชัน 10.1.3.4+ เพื่อแก้ปัญหา JBoss SWAP Crash ถาวร และเฝ้าระวัง SWAP ชั่วคราว"
                    action_en = f"{site_eng_en}: Coordinate Maintenance Window with {cust_name} to upgrade SMGR/SM to 10.1.3.4+ (fixes JBoss SWAP crash from rrdcached) and monitor SWAP."
                    title_th = f"Avaya TAC สรุป RCA ปิดเคสแล้ว / รอ {site_eng_th} นัดหมายเปิด Window อัปเกรดระบบ"
                    title_en = f"Avaya TAC SR Closed with RCA / Pending JADS Upgrade Window ({p_name})"
                    status_th = f"Avaya TAC ({detected_eng or 'Mitch Zhang'}) ยืนยันผลการวิเคราะห์เป็น Known Issue (JBoss SWAP Crash จาก rrdcached) ซึ่งมี Fix ถาวรในเวอร์ชัน 10.1.3.4+ ทาง Avaya TAC ปิดตั๋วแล้ว แต่ทีม JADS ต้องเปิด Maintenance Window เพื่ออัปเกรดระบบให้ลูกค้า"
                    status_en = f"Avaya TAC confirmed known issue (JBoss SWAP exhaustion from rrdcached). TAC closed SR with fix in 10.1.3.4+. JADS must schedule Maintenance Window."
                else:
                    action_th = f"{site_eng_th}: ประสานงาน {cust_name} เพื่อวางแผน Maintenance Window อัปเกรด/แพตช์ระบบตามคำแนะนำของ Avaya TAC"
                    action_en = f"{site_eng_en}: Coordinate Maintenance Window with {cust_name} for system upgrade/patch recommended by Avaya TAC."
                    title_th = f"Avaya TAC ปิดเคสแล้ว / รอ {site_eng_th} นัดหมายเปิด Window ดำเนินการ"
                    title_en = f"Avaya TAC SR Closed / Pending Customer Maintenance Window ({p_name})"
                    status_th = f"Avaya TAC ได้ระบุแนวทางแก้ไขและปิดเคสในฝั่ง TAC แล้ว ทีม JADS ต้องวางแผน Maintenance Window และดำเนินการต่อไป"
                    status_en = f"Avaya TAC provided resolution and closed SR. JADS must plan customer maintenance window."

                return {
                    "stage_key": "action_required",
                    "badge_color": "bg-amber-400/20 text-amber-300 border-amber-400/30",
                    "stage_badge": {"en": "Action Required (Upgrade Window)", "th": "รอ Action ภายใน (รอเปิด Window)"},
                    "stage_title": {"en": title_en, "th": title_th},
                    "status_summary": {"en": status_en, "th": status_th},
                    "next_action": {"en": action_en, "th": action_th},
                    "action_owner": {"en": site_eng_en, "th": site_eng_th},
                    "latest_note": {
                        "en": f"Avaya TAC closed SR. RCA: Upgrade to 10.1.3.4+ required. Assigned to {site_eng_en}.",
                        "th": f"Avaya ปิดเคสแล้ว แจ้ง RCA ต้องอัปเกรด SMGR/SM 10.1.3.4+ มอบหมาย {site_eng_th} ติดตามนัดหมาย"
                    },
                    "action_priority": 1,
                    "_lock_rule": True,
                    "source": "Hybrid AI (Instant Rule Engine)"
                }
            else:
                return {
                    "stage_key": "action_required",
                    "badge_color": "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
                    "stage_badge": {"en": "TAC Closed (Verify)", "th": "TAC ปิดเคส (รอตรวจรับ)"},
                    "stage_title": {"en": "Avaya TAC Closed SR / Verify with Customer", "th": "Avaya TAC ปิดเคสแล้ว / รอตรวจรับงานกับลูกค้า"},
                    "status_summary": {
                        "en": f"Avaya TAC has resolved and closed the Service Request. {site_eng_en} to verify resolution with customer.",
                        "th": f"ฝ่ายสนับสนุน Avaya TAC แก้ไขปัญหาและปิดตั๋วบริการแล้ว อยู่ระหว่างให้ {site_eng_th} ยืนยันผลกับลูกค้า"
                    },
                    "next_action": {
                        "en": f"{site_eng_en} to confirm resolution with customer and close internal ticket.",
                        "th": f"{site_eng_th} ตรวจสอบความเรียบร้อยกับลูกค้าและปิดตั๋วงานภายใน"
                    },
                    "action_owner": {"en": site_eng_en, "th": site_eng_th},
                    "latest_note": {"en": "SR closed by Avaya TAC. Awaiting customer confirmation.", "th": "Avaya TAC ปิดเคสแล้ว รอยืนยันกับลูกค้า"},
                    "action_priority": 1,
                    "source": "Hybrid AI (Instant Rule Engine)"
                }

        # PRIORITY 1: Outbound reply sent by JADS engineer (Ball is in Vendor's court or Meeting Confirmed!)
        if is_outbound:
            # Case 1.A: Confirmed Meeting / Teams / Session link sent by Engineer
            is_outbound_meeting = any(k in fresh_lower for k in [
                "teams.microsoft.com", "join meeting", "meeting id", "works for me",
                "see you tomorrow", "link below for our session", "webex.com"
            ])
            if is_outbound_meeting:
                time_desc_th = "ในวันพรุ่งนี้ เวลา 11:30 น. IST (13:00 น. เวลาไทย)" if "11:30" in fresh_lower else "ตามวันเวลาที่นัดหมาย"
                time_desc_en = "tomorrow at 11:30 AM IST (1:00 PM Thai time)" if "11:30" in fresh_lower else "at the scheduled time"
                return {
                    "stage_key": "action_required",
                    "badge_color": "bg-amber-400/20 text-amber-300 border-amber-400/30",
                    "stage_badge": {"en": "Meeting Scheduled", "th": "นัดหมาย Session / ส่งลิงก์แล้ว"},
                    "stage_title": {
                        "en": f"Session Confirmed / Teams Bridge Sent to {eng_disp_en}",
                        "th": f"ยืนยันนัดหมายเรียบร้อย / ส่งลิงก์ Microsoft Teams ให้ {eng_disp_th} แล้ว"
                    },
                    "status_summary": {
                        "en": f"JADS Engineer (Phongthep) confirmed availability and shared Microsoft Teams bridge for session {time_desc_en}.",
                        "th": f"วิศวกร JADS (Phongthep) ยืนยันเวลานัดหมายและส่งลิงก์ห้องประชุม Microsoft Teams ให้ {eng_disp_th} เรียบร้อยแล้ว {time_desc_th}"
                    },
                    "next_action": {
                        "en": f"JADS Engineer (Phongthep) and {eng_disp_en} to join Microsoft Teams session {time_desc_en}.",
                        "th": f"วิศวกร JADS (Phongthep) เข้าร่วมการประชุม Microsoft Teams ร่วมกับ {eng_disp_th} {time_desc_th}"
                    },
                    "action_owner": {"en": "JADS Engineering (Phongthep)", "th": "วิศวกร JADS (Phongthep)"},
                    "latest_note": {
                        "en": f"Sent Microsoft Teams meeting link for session {time_desc_en}.",
                        "th": f"ส่งลิงก์ห้องประชุม Microsoft Teams สำหรับ Session {time_desc_th} แล้ว"
                    },
                    "action_priority": 1,
                    "source": "Hybrid AI (Instant Rule Engine)"
                }

            if any(k in fresh_lower for k in ["still persist", "still fail", "fails to cover", "test call again", "retested"]):
                target_eng = f"Avaya Tier 3 ({detected_eng or 'Sreenath A'})"
                return {
                    "stage_key": "awaiting_vendor",
                    "badge_color": "bg-sky-500/20 text-sky-300 border-sky-500/30",
                    "stage_badge": {"en": "Awaiting TAC", "th": "รอ TAC วิเคราะห์ต่อ"},
                    "stage_title": {
                        "en": "Test Completed (Issue Persists) / Awaiting Vendor Next Steps",
                        "th": "ส่งผลทดสอบแล้ว (ปัญหายังคงเดิม) / รอ TAC กำหนดแนวทางถัดไป"
                    },
                    "status_summary": {
                        "en": f"JADS Engineer (Phongthep) retested but the issue persists. Awaiting review or live trace session from {target_eng}.",
                        "th": f"วิศวกร JADS (Phongthep) ได้ทดสอบแล้วพบว่าปัญหายังคงเดิม กำลังรอ {target_eng} ตรวจสอบหรือนัดหมายทำ Live Session เก็บ Trace เพิ่มเติม"
                    },
                    "next_action": {
                        "en": f"{target_eng} to review test results and propose next troubleshooting steps or schedule live session.",
                        "th": f"{target_eng} ตรวจสอบผลทดสอบ (ปัญหายังคงเดิม) และแนะนำแนวทางแก้ไขถัดไป หรือนัดหมายทำ Live Session"
                    },
                    "action_owner": {"en": target_eng, "th": target_eng},
                    "latest_note": {
                        "en": "Issue still persists after testing. Requested next steps or live trace session.",
                        "th": "ทดสอบแล้วยังพบปัญหาเดิม ได้แจ้งขอแนวทางถัดไปหรือนัดทำ Live Trace"
                    },
                    "source": "Hybrid AI (Instant Rule Engine)"
                }
            return {
                "stage_key": "awaiting_vendor",
                "badge_color": "bg-sky-500/20 text-sky-300 border-sky-500/30",
                "stage_badge": {"en": "Awaiting TAC", "th": "รอ TAC ตอบกลับ"},
                "stage_title": {
                    "en": "Engineer Replied / Awaiting Vendor Response",
                    "th": "วิศวกรส่งข้อมูลแล้ว / รอผลวิเคราะห์จาก TAC"
                },
                "status_summary": {
                    "en": f"JADS engineer has provided required updates. Awaiting next analysis from {eng_disp_en}.",
                    "th": f"วิศวกร JADS ได้ส่งข้อมูลเรียบร้อยแล้ว อยู่ระหว่างรอ {eng_disp_th} วิเคราะห์เพิ่มเติม"
                },
                "next_action": {
                    "en": f"{eng_disp_en} to review response and provide technical update.",
                    "th": f"{eng_disp_th} ตรวจสอบข้อมูลที่ส่งไปและแจ้งความคืบหน้าถัดไป"
                },
                "action_owner": {"en": eng_disp_en, "th": eng_disp_th},
                "latest_note": {
                    "en": f"Latest reply sent by {ticket.user_id.name or 'JADS Engineer'}.",
                    "th": f"ข้อความล่าสุดส่งโดย {ticket.user_id.name or 'วิศวกร JADS'}"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # PRIORITY 2: Inbound Message from Vendor (Evaluated on fresh unquoted text!)
        # Case A.1: Meeting / Live Remote Session Invitation from TAC (e.g. Ticket 57 / Sreenath A)
        has_meeting_session_req = any(k in fresh_lower for k in [
            "available for a session", "session tomorrow", "available for session",
            "session at", "meeting", "join bridge", "are you available",
            "schedule a call", "schedule a session", "bridge call", "teams meeting", "webex"
        ])
        if has_meeting_session_req:
            time_hint_th = "ในวันพรุ่งนี้ เวลา 11:30 น. IST (13:00 น. เวลาไทย)" if "11:30" in fresh_lower else "ตามวันเวลาที่นัดหมาย"
            time_hint_en = "tomorrow at 11:30 AM IST (1:00 PM Thai time)" if "11:30" in fresh_lower else "at the requested schedule"
            return {
                "stage_key": "action_required",
                "badge_color": "bg-amber-400/20 text-amber-300 border-amber-400/30",
                "stage_badge": {"en": "Session / Meeting Scheduled", "th": "นัดหมาย Session / ประชุม"},
                "stage_title": {
                    "en": f"Meeting / Live Session Coordination with {eng_disp_en}",
                    "th": f"นัดหมายเวลาเข้าร่วม Session กับ {eng_disp_th}"
                },
                "status_summary": {
                    "en": f"{eng_disp_en} tested in lab and requested a live session/meeting to verify configuration together. Awaiting schedule confirmation.",
                    "th": f"{eng_disp_th} ได้ทดสอบในห้องแล็บและเสนอนัดหมายเวลาทำ Live Session/ประชุม เพื่อตรวจสอบการทำงานร่วมกัน อยู่ระหว่างรอยืนยันเวลา"
                },
                "next_action": {
                    "en": f"JADS Engineer (Phongthep) to confirm availability and attend live session with {eng_disp_en} {time_hint_en}.",
                    "th": f"วิศวกร JADS (Phongthep) ยืนยันความพร้อมและนัดหมายเข้าร่วม Session กับ {eng_disp_th} {time_hint_th}"
                },
                "action_owner": {"en": "JADS Engineering (Phongthep)", "th": "วิศวกร JADS (Phongthep)"},
                "latest_note": {
                    "en": f"{eng_disp_en} asked: Are you available for a session {time_hint_en}?",
                    "th": f"{eng_disp_th} ขอนัดหมายเวลาทำ Session {time_hint_th}"
                },
                "action_priority": 1,
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case A: System Access / Remote Session / Live Troubleshooting Requested (e.g. Ticket 49 / Akshat Mehta)
        has_system_access_req = any(k in fresh_lower for k in [
            "system access", "remote access", "when can we do that", "when are you available",
            "schedule a session", "live session", "remote session", "screen sharing",
            "session together", "access to collect", "access to check"
        ])
        if has_system_access_req:
            p_name = ticket.avaya_product or "System"
            action_desc_th = f"วิศวกร JADS (Phongthep) ประสานงานเปิด System Access และนัดหมายวันเวลาเก็บ Network Trace บน {p_name} ร่วมกับ {eng_disp_th}"
            action_desc_en = f"JADS Engineer (Phongthep) to coordinate System Access and schedule time window to capture network trace on {p_name} with {eng_disp_en}."
            return {
                "stage_key": "action_required",
                "badge_color": "bg-amber-400/20 text-amber-300 border-amber-400/30",
                "stage_badge": {"en": "Session / Access Required", "th": "นัดหมาย Session / Remote Access"},
                "stage_title": {
                    "en": f"Coordinate System Access & Live Trace Session with {eng_disp_en}",
                    "th": f"ประสานงาน System Access และนัดหมาย Session ร่วมกับ {eng_disp_th}"
                },
                "status_summary": {
                    "en": f"{eng_disp_en} requested system access to collect a network trace on {p_name} while issue is reproducible. Awaiting time coordination.",
                    "th": f"{eng_disp_th} แจ้งขอเข้าถึงระบบ (System Access) เพื่อเก็บ Network Trace บน {p_name} ในช่วงที่ปัญหาเกิดขึ้น กำลังรอนัดหมายวันเวลาดำเนินการ"
                },
                "next_action": {
                    "en": action_desc_en,
                    "th": action_desc_th
                },
                "action_owner": {"en": "JADS Engineering (Phongthep)", "th": "วิศวกร JADS (Phongthep)"},
                "latest_note": {
                    "en": f"{eng_disp_en} requested system access and time coordination to capture trace.",
                    "th": f"{eng_disp_th} ขอสิทธิ์เข้าถึงระบบ (System Access) และนัดหมายเวลาเพื่อเก็บ Trace"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case B: Diagnostic Commands / Log Outputs Requested to be Collected & Uploaded (e.g. Ticket 43)
        has_cmd_req = any(k in fresh_lower for k in [
            "rpm -qa", "swversion", "av-version", "sysinfo", "collect log",
            "send us log", "provide log", "provide output of", "upload the log"
        ])
        if has_cmd_req:
            cmd_list = "rpm -qa, swversion, av-version, sysInfo" if ("rpm" in fresh_lower or "swversion" in fresh_lower) else "diagnostic logs / traces"
            return {
                "stage_key": "diagnostics",
                "badge_color": "bg-purple-500/20 text-purple-300 border-purple-500/30",
                "stage_badge": {"en": "Logs Required", "th": "รอส่งข้อมูลระบบ"},
                "stage_title": {
                    "en": f"Command Outputs & System Info Required by {eng_disp_en}",
                    "th": f"รันคำสั่งและส่งข้อมูลระบบให้ {eng_disp_th}"
                },
                "status_summary": {
                    "en": f"{eng_disp_en} requested system command outputs ({cmd_list}) to proceed with investigation.",
                    "th": f"{eng_disp_th} ขอผลลัพธ์คำสั่งเวอร์ชันระบบ ({cmd_list}) เพื่อใช้ประกอบการวิเคราะห์ปัญหา"
                },
                "next_action": {
                    "en": f"JADS Engineer (Phongthep) to run commands ({cmd_list}) on server and upload output to Avaya SR.",
                    "th": f"วิศวกร JADS (Phongthep) รันคำสั่งตรวจสอบเวอร์ชัน ({cmd_list}) บนเซิร์ฟเวอร์และส่งผลลัพธ์ให้ {eng_disp_th}"
                },
                "action_owner": {"en": "JADS Engineering (Phongthep)", "th": "วิศวกร JADS (Phongthep)"},
                "latest_note": {
                    "en": f"Please provide output of: {cmd_list}.",
                    "th": f"โปรดรันคำสั่งและส่งข้อมูล: {cmd_list}"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case C: Customer Info / FL Number / Product Details Requested (e.g. Ticket 64 / Sophie Zhao)
        has_info_req = any(k in fresh_lower for k in [
            "need your assistance in confirming", "confirming the following", "confirm the following",
            "following details", "following information", "fl number", "company name & fl number",
            "exact version", "prompt response", "looking forward to your response", "awaiting your response"
        ])
        if has_info_req and any(k in fresh_lower for k in ["fl number", "product details", "exact version", "details", "following"]):
            p_name = ticket.avaya_product or "Avaya System"
            return {
                "stage_key": "action_required",
                "badge_color": "bg-amber-400/20 text-amber-300 border-amber-400/30",
                "stage_badge": {"en": "Action Required", "th": "ต้องตอบกลับข้อมูล"},
                "stage_title": {
                    "en": f"Provide End-User & Product Details to {eng_disp_en}",
                    "th": f"ตอบกลับข้อมูลผู้ใช้งานและระบบให้ {eng_disp_th}"
                },
                "status_summary": {
                    "en": f"{eng_disp_en} requested assistance confirming End-user info (Company name & FL number), exact product version, and issue description to proceed.",
                    "th": f"{eng_disp_th} ได้รับมอบหมายเคสและขอความร่วมมือยืนยันข้อมูลผู้ใช้งาน (Company name & FL number), รุ่นระบบ {p_name} และรายละเอียดปัญหา เพื่อดำเนินการต่อ"
                },
                "next_action": {
                    "en": f"JADS Engineer (Phongthep) to reply confirming End-user info (FL number), product version, and issue description to {eng_disp_en}.",
                    "th": f"วิศวกร JADS (Phongthep) ตอบกลับข้อมูลผู้ใช้งาน (FL Number), รุ่นระบบ {p_name} และรายละเอียดปัญหาให้ {eng_disp_th}"
                },
                "action_owner": {"en": "JADS Engineering (Phongthep)", "th": "วิศวกร JADS (Phongthep)"},
                "latest_note": {
                    "en": f"{eng_disp_en} requested End-user details (FL number), product version, and issue description.",
                    "th": f"{eng_disp_th} ขอข้อมูลผู้ใช้งาน (FL Number), รุ่นระบบ และรายละเอียดปัญหา"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case D: Workaround / Station Reconfig / Configuration Change Recommended
        has_workaround = any(k in fresh_lower for k in ["reconfigure", "sip extension", "apply workaround", "test call", "restart service"])
        if has_workaround:
            return {
                "stage_key": "action_required",
                "badge_color": "bg-amber-400/20 text-amber-300 border-amber-400/30",
                "stage_badge": {"en": "Action Required", "th": "รอปรับแต่ง/ทดสอบระบบ"},
                "stage_title": {
                    "en": f"Vendor Recommendation / Action Required ({eng_disp_en})",
                    "th": f"ข้อเสนอแนะจาก {eng_disp_th} / รอการปรับแต่ง Config"
                },
                "status_summary": {
                    "en": f"{eng_disp_en} has analyzed configuration and identified necessary adjustments.",
                    "th": f"{eng_disp_th} ได้ตรวจสอบคอนฟิกและระบุขั้นตอนการแก้ไขที่ต้องดำเนินการปรับแต่งหรือทดสอบ"
                },
                "next_action": {
                    "en": f"JADS Engineer (Phongthep) to implement configuration changes and verify results as recommended by {eng_disp_en}.",
                    "th": f"วิศวกร JADS (Phongthep) ดำเนินการปรับแต่งคอนฟิกหรือทดสอบตามที่ {eng_disp_th} แนะนำ และแจ้งผลกลับ"
                },
                "action_owner": {"en": "JADS Engineering (Phongthep)", "th": "วิศวกร JADS (Phongthep)"},
                "latest_note": {
                    "en": "Please apply recommended adjustments and test.",
                    "th": "โปรดดำเนินการปรับแต่งคอนฟิกตามคำแนะนำและทดสอบการทำงาน"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case E: Product Code / Portal Change Requested
        if any(k in fresh_lower for k in ["product code", "change the product code"]):
            return {
                "stage_key": "action_required",
                "badge_color": "bg-amber-400/20 text-amber-300 border-amber-400/30",
                "stage_badge": {"en": "Action Required", "th": "ต้องดำเนินการ"},
                "stage_title": {
                    "en": "Update Product Code on Avaya Portal",
                    "th": "แก้ไข Product Code บน Avaya Portal"
                },
                "status_summary": {
                    "en": f"{eng_disp_en} requested JADS to update the SR Product Code on the Avaya Portal to proceed.",
                    "th": f"{eng_disp_th} ขอให้เราเข้าแก้ไข Product Code บน Avaya Support Portal เพื่อดำเนินการต่อ"
                },
                "next_action": {
                    "en": "JADS Engineer (Phongthep) to update the product code on the Avaya Support Portal.",
                    "th": "วิศวกร JADS (Phongthep) เข้า Avaya Support Portal เพื่อแก้ไข Product Code ของเคสนี้"
                },
                "action_owner": {"en": "JADS Engineering (Phongthep)", "th": "วิศวกร JADS (Phongthep)"},
                "latest_note": {
                    "en": "Please change the product code for this SR on Avaya portal.",
                    "th": "โปรดเข้าเปลี่ยน Product Code สำหรับ SR นี้บนระบบ Avaya Portal"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case F: Top 100 CVE Limitation / SME Review (e.g. Ticket 41)
        if any(k in fresh_lower for k in ["top 100", "100 items", "100 cve", "spreadsheet"]):
            sme = detected_eng if detected_eng else "Jay Thomas"
            return {
                "stage_key": "vendor_review",
                "badge_color": "bg-blue-500/20 text-blue-300 border-blue-500/30",
                "stage_badge": {"en": "Vendor Review", "th": "Avaya กำลังตรวจสอบ"},
                "stage_title": {
                    "en": "Top 100 Critical CVEs Under Avaya SME Review",
                    "th": "Avaya SME อยู่ระหว่างวิเคราะห์ Top 100 CVE"
                },
                "status_summary": {
                    "en": f"Avaya Support forwarded the customer's Top 100 Critical CVE list to SME ({sme}) to review the spreadsheet and provide recommendations.",
                    "th": f"ฝ่ายสนับสนุน Avaya ได้ส่งต่อรายการ Top 100 Critical CVE ให้กับทีม SME ({sme}) เพื่อตรวจสอบและจัดทำข้อเสนอแนะ"
                },
                "next_action": {
                    "en": f"Avaya SME ({sme}) to review spreadsheet and share remediation recommendations.",
                    "th": f"Avaya SME ({sme}) ตรวจสอบรายการและแจ้งแนวทางการ Remediation สำหรับ 100 รายการแรก"
                },
                "action_owner": {"en": f"Avaya SME ({sme})", "th": f"ทีมผู้เชี่ยวชาญ Avaya SME ({sme})"},
                "latest_note": {
                    "en": "Reviewing attached spreadsheet for Top 100 Critical vulnerabilities.",
                    "th": "อยู่ระหว่างตรวจสอบไฟล์ Spreadsheet สำหรับช่องโหว่ Top 100 Critical"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case G: Management Escalation & Duty Manager (e.g. Ticket 42)
        if (any(k in fresh_lower for k in ["duty manager", "delivery manager", "escalated to management", "leadership team"]) or \
            ("duty manager" in full_text or "renjoy henry" in full_text)):
            mgr_name = "Vinaya Kumar (Duty Manager, Avaya)"
            if "renjoy" in full_text or "henry" in full_text:
                mgr_name = "Renjoy Henry (Senior Manager, Avaya Services Delivery)"
            elif "vinaya" in full_text or "kumar" in full_text:
                mgr_name = "Vinaya Kumar (Duty Manager, Avaya)"

            return {
                "stage_key": "escalation",
                "badge_color": "bg-amber-400/20 text-amber-300 border-amber-400/30",
                "stage_badge": {"en": "Escalated", "th": "ส่งต่อผู้บริหาร"},
                "stage_title": {
                    "en": "Management Escalation & Resource Assignment",
                    "th": "ส่งต่อฝ่ายบริหารเพื่อมอบหมายวิศวกรผู้เชี่ยวชาญ"
                },
                "status_summary": {
                    "en": f"Incident escalated for immediate attention. {mgr_name} has acknowledged and engaged leadership to assign a dedicated technical resource.",
                    "th": f"เคสได้รับการยกระดับเร่งด่วน โดยคุณ {mgr_name} ได้รับเรื่องและประสานงานผู้บริหารเพื่อจัดสรรวิศวกรดูแลโดยเฉพาะ"
                },
                "next_action": {
                    "en": "Avaya Management to assign a designated technical specialist to the Service Request.",
                    "th": "ฝ่ายบริหาร Avaya ดำเนินการมอบหมายวิศวกรผู้เชี่ยวชาญประจำเคสนี้"
                },
                "action_owner": {"en": "Avaya TAC Management", "th": "ฝ่ายบริหาร Avaya TAC"},
                "latest_note": {
                    "en": "Management contacts engaged to review and assign technical resource tomorrow.",
                    "th": "ประสานงานฝ่ายบริหารเพื่อตรวจสอบและมอบหมายวิศวกรในวันพรุ่งนี้"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case H: Open Notification / New Service Request Confirmation (e.g. Ticket 63)
        if any(k in fresh_lower for k in ["open notification", "we just received your service request", "sr opened", "successfully created", "service request alert"]):
            prod_name = ticket.avaya_product or "Avaya System"
            return {
                "stage_key": "awaiting_vendor",
                "badge_color": "bg-sky-500/20 text-sky-300 border-sky-500/30",
                "stage_badge": {"en": "Awaiting TAC", "th": "รอ TAC ตอบกลับ"},
                "stage_title": {
                    "en": "New Service Request Opened / Awaiting TAC Assignment",
                    "th": "เปิดเคสใหม่เรียบร้อย / รอ Avaya TAC มอบหมายวิศวกร"
                },
                "status_summary": {
                    "en": f"New Service Request successfully logged for {prod_name}. Awaiting initial review and technical assignment by Avaya TAC engineering.",
                    "th": f"เปิดตั๋วใบงานใหม่สำหรับระบบ {prod_name} เรียบร้อยแล้ว อยู่ระหว่างรอฝ่ายสนับสนุน Avaya TAC ตรวจสอบและมอบหมายวิศวกรผู้เชี่ยวชาญ"
                },
                "next_action": {
                    "en": "Avaya TAC to assign technical specialist and review incident details.",
                    "th": "รอฝ่ายสนับสนุน Avaya TAC มอบหมายวิศวกรและเริ่มตรวจสอบรายละเอียดเคส"
                },
                "action_owner": {"en": "Avaya TAC Support", "th": "ทีมสนับสนุน Avaya TAC"},
                "latest_note": {
                    "en": "Service request received and registered with Avaya support.",
                    "th": "ระบบ Avaya ได้รับข้อมูลการเปิดเคสแล้ว"
                },
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        # Case I: Active Investigation by Vendor (No action requested from customer)
        snippet = fresh_text[:140] + "..." if len(fresh_text) > 140 else fresh_text
        if ticket.is_avaya_support or ticket.avaya_sr_number:
            prod_name = ticket.avaya_product or "Avaya System"
            return {
                "stage_key": "awaiting_vendor",
                "badge_color": "bg-sky-500/20 text-sky-300 border-sky-500/30",
                "stage_badge": {"en": "TAC Working", "th": "รอ TAC วิเคราะห์"},
                "stage_title": {
                    "en": f"Investigation in Progress by {eng_disp_en}",
                    "th": f"{eng_disp_th} กำลังวิเคราะห์และตรวจสอบ"
                },
                "status_summary": {
                    "en": f"{eng_disp_en} is actively investigating the service request for {prod_name}. Awaiting engineering updates.",
                    "th": f"{eng_disp_th} กำลังดำเนินการวิเคราะห์ข้อมูลเคสสำหรับระบบ {prod_name} อยู่ระหว่างรอผลการตรวจสอบทางเทคนิค"
                },
                "next_action": {
                    "en": f"{eng_disp_en} to analyze issue and provide technical recommendations.",
                    "th": f"{eng_disp_th} กำลังตรวจสอบข้อมูล และจะแจ้งผลการวิเคราะห์ให้ทราบต่อไป"
                },
                "action_owner": {"en": eng_disp_en, "th": eng_disp_th},
                "latest_note": {"en": snippet, "th": snippet},
                "source": "Hybrid AI (Instant Rule Engine)"
            }

        return {
            "stage_key": "in_progress",
            "badge_color": "bg-indigo-500/20 text-indigo-300 border-indigo-500/30",
            "stage_badge": {"en": "In Progress", "th": "กำลังดำเนินการ"},
            "stage_title": {
                "en": "Active Technical Investigation",
                "th": "กำลังตรวจสอบและแก้ไขปัญหา"
            },
            "status_summary": {
                "en": "Incident is being actively handled and tracked across support channels.",
                "th": "เคสกำลังได้รับการประสานงานและติดตามความคืบหน้าอย่างต่อเนื่อง"
            },
            "next_action": {
                "en": "Follow up on technical progress and customer coordination.",
                "th": "ติดตามความคืบหน้าทางเทคนิคและประสานงานกับผู้เกี่ยวข้อง"
            },
            "action_owner": {"en": ticket.user_id.name or "Helpdesk Dispatcher", "th": ticket.user_id.name or "วิศวกรประจำตั๋ว"},
            "latest_note": {"en": snippet, "th": snippet},
            "source": "Local Ollama (192.168.10.30) & AI Engine"
        }

    @http.route(["/helpdesk/api/trigger_ai_summary/<int:ticket_id>"], type="http", auth="public", methods=["GET", "POST"], csrf=False)
    def helpdesk_api_trigger_ai_summary(self, ticket_id, **kwargs):
        """
        API endpoint for n8n / automation scripts to proactively queue
        AI summary generation as soon as a new email or update arrives.
        """
        ticket = request.env["helpdesk.ticket.pro"].sudo().browse(ticket_id)
        if not ticket.exists():
            return request.make_response(
                json.dumps({"success": False, "error": "Ticket not found"}),
                headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
            )
        messages = request.env["mail.message"].sudo().search([
            ("model", "=", "helpdesk.ticket.pro"),
            ("res_id", "=", ticket.id),
            ("message_type", "!=", "user_notification")
        ], order="id desc", limit=5)
        
        messages_list = [{"id": m.id, "body": m.body or "", "author": m.author_id.name or ""} for m in messages]
        rule_summary = self._compute_rule_based_ai_stage(ticket, messages_list)
        
        latest_msg_id = messages_list[0]["id"] if messages_list else 0
        cache_key = f"{ticket.id}_{latest_msg_id}_{ticket.write_date}"
        
        msg_snippets = "\n".join([f"- {_clean_text_snippet(m.get('body', ''))[:200]}" for m in messages_list[:3]])
        t = threading.Thread(
            target=_async_ollama_synthesize,
            args=(ticket.id, ticket.subject, msg_snippets, rule_summary, cache_key),
            daemon=True
        )
        t.start()
        
        return request.make_response(
            json.dumps({"success": True, "status": "background_synthesis_queued", "ticket_id": ticket_id}),
            headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
        )

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

        ai_cache = _load_ai_cache()
        tickets_list = []
        product_counts = {}
        customer_counts = {}

        cache_dirty = False
        for t in tickets:
            c_name = t.partner_id.name or t.avaya_customer_name or ""
            prod_name = t.avaya_product or t.category_id.name or "General"
            is_closed = t.state in ["solved", "closed"]
            
            # Count products and customers for ACTIVE tickets only
            if not is_closed:
                if prod_name:
                    product_counts[prod_name] = product_counts.get(prod_name, 0) + 1
                if c_name:
                    customer_counts[c_name] = customer_counts.get(c_name, 0) + 1

            # AI Stage & Action Priority determination (Real-time auto-sync)
            msgs = []
            latest_msg_id = 0
            if not is_closed:
                try:
                    msgs = request.env["mail.message"].sudo().search([
                        ("model", "=", "helpdesk.ticket.pro"),
                        ("res_id", "=", t.id),
                        ("message_type", "in", ["comment", "email"])
                    ], order="id desc", limit=6)
                    content_msgs = [m for m in msgs if m.body and len(_clean_text_snippet(m.body).strip()) > 3]
                    latest_msg_id = content_msgs[0].id if content_msgs else (msgs[0].id if msgs else 0)
                except Exception:
                    pass

            expected_cache_key = f"{t.id}_{latest_msg_id}_{t.write_date}"
            ai_sum = ai_cache.get(str(t.id))

            # If not cached or ticket state/messages have updated, auto-compute instantly
            if not is_closed and (not ai_sum or ai_sum.get("_cache_key") != expected_cache_key):
                try:
                    msgs_list = [{
                        "id": m.id,
                        "body": m.body or "",
                        "author": m.author_id.name if m.author_id else (m.email_from or "Support"),
                        "email_from": m.email_from or ""
                    } for m in msgs if m.body and len(_clean_text_snippet(m.body).strip()) > 3]
                    ai_sum = self._compute_rule_based_ai_stage(t, msgs_list)
                    ai_sum["_cache_key"] = expected_cache_key
                    ai_cache[str(t.id)] = ai_sum
                    cache_dirty = True
                except Exception:
                    pass

            action_priority = 5 if is_closed else 4
            action_info = None

            if ai_sum and not is_closed:
                s_key = ai_sum.get("stage_key", "")
                if s_key in ["action_required", "diagnostics"]:
                    action_priority = 1
                elif s_key in ["awaiting_vendor", "vendor_review"]:
                    action_priority = 2
                elif s_key in ["escalation"]:
                    action_priority = 3
                elif s_key in ["solved", "closed"]:
                    action_priority = 5

                action_info = {
                    "stage_key": s_key,
                    "stage_badge": ai_sum.get("stage_badge", {}),
                    "action_owner": ai_sum.get("action_owner", {}),
                    "next_action": ai_sum.get("next_action", {}),
                    "latest_note": ai_sum.get("latest_note", {})
                }
            elif is_closed and ai_sum:
                action_info = {
                    "stage_key": "closed",
                    "stage_badge": {"en": "Closed", "th": "ปิดเคสแล้ว"},
                    "action_owner": {"en": "-", "th": "-"},
                    "next_action": {"en": "Case resolved", "th": "เคสเสร็จสิ้นแล้ว"}
                }

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
                "assigned_to": getattr(t, "site_engineer", None) or t.user_id.name or "Phongthep Phimthong",
                "site_engineer": getattr(t, "site_engineer", None) or "Phongthep Phimthong",
                "customer": c_name or "Not Specified",
                "create_date": t.create_date.isoformat() + "Z" if t.create_date else None,
                "write_date": t.write_date.isoformat() + "Z" if t.write_date else None,
                "deadline": t.deadline.isoformat() + "Z" if t.deadline else None,
                "portal_url": f"/helpdesk/ticket/{t.id}",
                "backend_url": f"/web#id={t.id}&model=helpdesk.ticket.pro&view_type=form",
                "action_priority": action_priority,
                "ai_summary": action_info
            })

        if cache_dirty:
            try:
                _save_ai_cache(ai_cache)
            except Exception:
                pass

        def _ticket_sort_key(item):
            prio_num = 0
            try:
                prio_num = int(item.get("priority") or 0)
            except Exception:
                pass
            return (item.get("action_priority", 5), -prio_num, -(item.get("id") or 0))

        tickets_list.sort(key=_ticket_sort_key)

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
                "action_needed": sum(1 for item in tickets_list if item.get("action_priority") == 1),
                "awaiting_vendor": sum(1 for item in tickets_list if item.get("action_priority") == 2),
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

    @http.route("/helpdesk/api/check_ticket", type="http", auth="public", methods=["GET"], csrf=False)
    def helpdesk_api_check_ticket(self, sr="", ref="", **kwargs):
        tickets_model = request.env["helpdesk.ticket.pro"].sudo()
        import re
        sr_val = sr.strip() if sr else ""
        subject_val = kwargs.get("subject", "").strip()

        universal_match = None
        if sr_val and not sr_val.startswith("SR-"):
            m = re.search(r'\b(1-\d{10,12})\b', sr_val)
            if m:
                universal_match = m.group(1)
            else:
                universal_match = sr_val
        if not universal_match and subject_val:
            m = re.search(r'\b(1-\d{10,12})\b', subject_val)
            if m:
                universal_match = m.group(1)

        domain = []
        if universal_match:
            domain = ["|", ("avaya_sr_number", "=", universal_match), ("subject", "ilike", universal_match)]
        elif ref:
            domain = [("name", "=", ref.strip())]
        else:
            return request.make_response(
                json.dumps({"exists": False, "error": "No search parameter provided"}),
                headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
            )

        ticket = tickets_model.search(domain, order="id desc", limit=1)
        if ticket:
            return request.make_response(
                json.dumps({
                    "exists": True,
                    "id": ticket.id,
                    "ref": ticket.name,
                    "subject": ticket.subject,
                    "state": ticket.state,
                    "avaya_status": ticket.avaya_status or "",
                    "avaya_sr_number": ticket.avaya_sr_number or "",
                    "is_closed": ticket.state in ["closed", "solved"],
                    "portal_url": f"https://phongthep.lol/helpdesk/ticket/{ticket.id}"
                }),
                headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
            )

        return request.make_response(
            json.dumps({"exists": False}),
            headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
        )

    @http.route("/helpdesk/api/update_ticket", type="http", auth="public", methods=["POST"], csrf=False)
    def helpdesk_api_update_ticket(self, **kwargs):
        try:
            data = json.loads(request.httprequest.data.decode("utf-8")) if request.httprequest.data else kwargs
        except Exception:
            data = kwargs

        ticket_id = data.get("id")
        sr_number = data.get("avaya_sr_number")
        tickets_model = request.env["helpdesk.ticket.pro"].sudo()

        ticket = None
        if ticket_id:
            ticket = tickets_model.browse(int(ticket_id))
        elif sr_number and not str(sr_number).startswith("SR-"):
            ticket = tickets_model.search([("avaya_sr_number", "=", sr_number)], order="id desc", limit=1)

        if not ticket or not ticket.exists():
            import re
            search_str = f"{sr_number or ''} {data.get('subject', '')}"
            m = re.search(r'\b(1-\d{10,12})\b', search_str)
            if m:
                found_sr = m.group(1)
                ticket = tickets_model.search(["|", ("avaya_sr_number", "=", found_sr), ("subject", "ilike", found_sr)], order="id desc", limit=1)

        if not ticket or not ticket.exists():
            return request.make_response(
                json.dumps({"success": False, "error": "Ticket not found"}),
                headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
            )

        # Update status/severity if changed
        update_vals = {}
        subj_check = (data.get("subject") or "").lower()
        if "sr closed" in subj_check or "closed:" in subj_check:
            update_vals["avaya_status"] = "Closed"
        elif data.get("avaya_status") and data.get("avaya_status") != ticket.avaya_status:
            update_vals["avaya_status"] = data["avaya_status"]
        if data.get("avaya_severity") and data.get("avaya_severity") != ticket.avaya_severity:
            update_vals["avaya_severity"] = data["avaya_severity"]
            sev_map = {"p1": "3", "p2": "2", "p3": "1", "p4": "0"}
            if data["avaya_severity"] in sev_map:
                update_vals["priority"] = sev_map[data["avaya_severity"]]

        if data.get("site_engineer") and data.get("site_engineer") != ticket.site_engineer:
            update_vals["site_engineer"] = data["site_engineer"]
        if update_vals:
            ticket.write(update_vals)

        # Post to Chatter
        sender = data.get("sender_name") or data.get("avaya_contact_name") or ticket.avaya_contact_name or "Avaya Support"
        sender_email = data.get("sender_email") or data.get("avaya_contact_email") or ""
        recipient = data.get("recipient") or "Avaya Support"
        reply_content = data.get("reply_body")
        is_outbound = bool(data.get("is_outbound"))

        # Filter out automated internal Jira / Atlassian ticket notifications
        s_email_lower = (sender_email or "").lower()
        s_name_lower = (sender or "").lower()
        if "atlassian.net" in s_email_lower or "jira@" in s_email_lower or "jads.admsvc" in s_name_lower:
            return request.make_response(
                json.dumps({"success": True, "ignored": True, "reason": "Internal Jira notification ignored"}),
                headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
            )

        if reply_content:
            email_info = f" &lt;{sender_email}&gt;" if sender_email else ""
            if is_outbound:
                card_html = Markup(f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #202124; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 14px; margin: 6px 0; box-shadow: 0 1px 2px rgba(0,0,0,0.04);">
  <div style="border-bottom: 1px solid #dcfce7; padding-bottom: 8px; margin-bottom: 10px;">
    <span style="display: inline-block; background: #059669; color: #ffffff; font-weight: bold; border-radius: 4px; padding: 2px 7px; font-size: 12px; margin-right: 8px;">📤 เราตอบกลับ (Sent)</span>
    <strong>{sender}</strong>{email_info} ➔ <strong>{recipient}</strong>
  </div>
  <div style="white-space: pre-wrap; color: #1e293b; line-height: 1.7; background: #ffffff; padding: 10px; border-radius: 4px; border: 1px solid #dcfce7;">{reply_content}</div>
</div>""")
            else:
                card_html = Markup(f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #202124; background: #ffffff; border: 1px solid #e0e2e6; border-radius: 8px; padding: 14px; margin: 6px 0; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">
  <div style="border-bottom: 1px solid #f1f3f4; padding-bottom: 8px; margin-bottom: 10px;">
    <span style="display: inline-block; background: #e8f0fe; color: #1a73e8; font-weight: bold; border-radius: 4px; padding: 2px 7px; font-size: 12px; margin-right: 8px;">📩 Avaya Support</span>
    <strong>{sender}</strong>{email_info}
  </div>
  <div style="white-space: pre-wrap; color: #1f1f1f; line-height: 1.7; background: #fdfdfd; padding: 10px; border-radius: 4px; border: 1px solid #f1f3f4;">{reply_content}</div>
</div>""")
        else:
            body_html = data.get("description") or data.get("message") or data.get("subject") or "Activity or reply received."
            card_html = Markup(f"<b>[Email Update / Activity Received]</b><br/>{body_html}")

        msg_subject = data.get("subject") or f"Update on {ticket.name}"
        ticket.message_post(
            body=card_html,
            subject=msg_subject,
            message_type="comment",
            subtype_xmlid="mail.mt_comment"
        )

        # Touch ticket write_date to guarantee fresh timestamp for cache keys
        ticket.sudo().write({"write_date": fields.Datetime.now()})

        # Proactively bust and recalculate AI summary immediately
        try:
            cache = _load_ai_cache()
            if str(ticket.id) in cache:
                del cache[str(ticket.id)]
                _save_ai_cache(cache)
            msg_records = request.env["mail.message"].sudo().search([
                ("model", "=", "helpdesk.ticket.pro"),
                ("res_id", "=", ticket.id)
            ], order="date desc, id desc")
            msg_list = []
            for m in msg_records:
                msg_list.append({
                    "id": m.id,
                    "date": m.date.isoformat() if m.date else "",
                    "author": m.author_id.name if m.author_id else (m.email_from or "Support"),
                    "email_from": m.email_from or "",
                    "subject": m.subject or "",
                    "body": m.body or "",
                    "subtype": m.subtype_id.name if m.subtype_id else ""
                })
            self._synthesize_ticket_ai_stage(ticket, msg_list)
        except Exception as e:
            _logger.warning("Proactive AI pre-warm failed: %s", e)

        return request.make_response(
            json.dumps({
                "success": True,
                "id": ticket.id,
                "ref": ticket.name,
                "state": ticket.state,
                "avaya_status": ticket.avaya_status,
                "portal_url": f"https://phongthep.lol/helpdesk/ticket/{ticket.id}"
            }),
            headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
        )

    @http.route("/helpdesk/api/create_ticket", type="http", auth="public", methods=["POST"], csrf=False)
    def helpdesk_api_create_ticket(self, **kwargs):
        try:
            data = json.loads(request.httprequest.data.decode("utf-8")) if request.httprequest.data else kwargs
        except Exception:
            data = kwargs

        tickets_model = request.env["helpdesk.ticket.pro"].sudo()
        sr_number = data.get("avaya_sr_number")
        subject = data.get("subject", "").strip()

        # 1. Anti-duplicate check
        existing_ticket = None
        if sr_number and not str(sr_number).startswith("SR-"):
            existing_ticket = tickets_model.search([("avaya_sr_number", "=", sr_number)], order="id desc", limit=1)
        if not existing_ticket and subject:
            import re
            m = re.search(r'\b(1-\d{10,12})\b', subject)
            if m:
                found_sr = m.group(1)
                existing_ticket = tickets_model.search(["|", ("avaya_sr_number", "=", found_sr), ("subject", "ilike", found_sr)], order="id desc", limit=1)

        if existing_ticket:
            return self.helpdesk_api_update_ticket(**data)

        # 2. Priority mapping from severity
        severity = data.get("avaya_severity", "p3")
        priority = data.get("priority")
        if not priority:
            sev_map = {"p1": "3", "p2": "2", "p3": "1", "p4": "0"}
            priority = sev_map.get(severity, "1")

        # 3. Create description HTML card
        desc_html = data.get("description")
        if not desc_html:
            sold_to = data.get("sold_to_id", "0052085219")
            product = data.get("avaya_product", "Avaya Solution")
            asset_id = data.get("avaya_asset_id", "-")
            status = data.get("avaya_status", "Created")
            contact_name = data.get("avaya_contact_name", "Phongthep Phimthong")
            contact_phone = data.get("avaya_contact_phone", "0612438275")
            contact_email = data.get("avaya_contact_email", "phongthep@jadscomm.com")
            date_rep = data.get("avaya_date_reported", "-")
            cust_name = data.get("avaya_customer_name", "-")
            location = data.get("avaya_location", "-")
            owner_sbl = data.get("avaya_owner_sbl", "-")
            summary = data.get("summary") or subject
            avaya_url = f"https://support.avaya.com/support/en/secure/service-requests/displaySR?srNum={sr_number}" if sr_number else "https://support.avaya.com/"

            border_color = "#d93025" if severity == "p1" else "#f2994a"
            bg_color = "#fce8e6" if severity == "p1" else "#fef7e0"
            text_color = "#d93025" if severity == "p1" else "#b06000"

            desc_html = Markup(f"""
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 14px; line-height: 1.6; color: #202124;">
  <div style="background-color: {bg_color}; border-left: 5px solid {border_color}; padding: 14px; margin-bottom: 16px; border-radius: 4px;">
    <div style="font-size: 16px; font-weight: bold; color: {text_color}; margin-bottom: 8px;">
      🔔 AVAYA TAC {severity.upper()} SERVICE REQUEST ({status})
    </div>
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 13px;">
      <div><strong>SR Number:</strong> {sr_number or 'N/A'}</div>
      <div><strong>Status:</strong> {status}</div>
      <div><strong>Sold To / FL:</strong> {sold_to}</div>
      <div><strong>Product:</strong> {product}</div>
      <div><strong>Asset ID:</strong> {asset_id}</div>
      <div><strong>Severity:</strong> {severity.upper()}</div>
      <div><strong>Date Reported:</strong> {date_rep}</div>
      <div><strong>Support Group (SBL):</strong> {owner_sbl}</div>
      <div><strong>Primary Customer:</strong> {cust_name}</div>
      <div><strong>Location:</strong> {location}</div>
    </div>
  </div>
  
  <div style="margin-bottom: 16px;">
    <h3 style="margin-top: 0; color: #1a73e8; font-size: 15px;">Summary / Description</h3>
    <p style="white-space: pre-wrap; background: #f8f9fa; padding: 12px; border-radius: 4px; border: 1px solid #dadce0;">{summary}</p>
  </div>

  <div style="margin-bottom: 16px;">
    <h3 style="margin-top: 0; color: #1a73e8; font-size: 15px;">Contact Information</h3>
    <p><strong>Contact:</strong> {contact_name} | <strong>Phone:</strong> {contact_phone} | <strong>Email:</strong> {contact_email}</p>
  </div>

  <div style="margin-top: 20px; padding-top: 12px; border-top: 1px solid #dadce0;">
    <a href="{avaya_url}" style="background-color: #d93025; color: white; padding: 8px 14px; text-decoration: none; border-radius: 4px; font-weight: bold; margin-right: 10px; display: inline-block;">🔗 Open Avaya TAC SR</a>
  </div>
</div>
""")

        create_vals = {
            "subject": subject or f"Avaya SR# {sr_number}",
            "description": desc_html,
            "ticket_type": data.get("ticket_type", "incident"),
            "ticket_source": data.get("ticket_source", "avaya_support"),
            "is_avaya_support": True,
            "avaya_sr_number": sr_number or "",
            "sold_to_id": data.get("sold_to_id", "0052085219"),
            "avaya_product": data.get("avaya_product", "Avaya Solution"),
            "avaya_asset_id": data.get("avaya_asset_id", ""),
            "avaya_severity": severity,
            "avaya_status": data.get("avaya_status", "Created"),
            "priority": priority,
            "avaya_contact_name": data.get("avaya_contact_name", "Phongthep Phimthong"),
            "site_engineer": data.get("site_engineer") or data.get("owner_name") or "Phongthep Phimthong",
            "avaya_contact_phone": data.get("avaya_contact_phone", "0612438275"),
            "avaya_contact_email": data.get("avaya_contact_email", "phongthep@jadscomm.com"),
            "avaya_date_reported": data.get("avaya_date_reported", ""),
            "avaya_customer_name": data.get("avaya_customer_name", ""),
            "avaya_location": data.get("avaya_location", ""),
            "avaya_owner_sbl": data.get("avaya_owner_sbl", "")
        }

        ticket = tickets_model.create(create_vals)

        return request.make_response(
            json.dumps({
                "success": True,
                "id": ticket.id,
                "ref": ticket.name,
                "subject": ticket.subject,
                "portal_url": f"https://phongthep.lol/helpdesk/ticket/{ticket.id}"
            }),
            headers=[("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
        )



    @http.route('/helpdesk/api/lookup_sold_to', type='http', auth='public', methods=['GET'], csrf=False)
    def helpdesk_api_lookup_sold_to(self, query='', **kwargs):
        q = query.strip() if query else ''
        if not q:
            return request.make_response(
                json.dumps({'success': False, 'error': 'Query parameter missing'}),
                headers=[('Content-Type', 'application/json'), ('Access-Control-Allow-Origin', '*')]
            )

        partners = request.env['res.partner'].sudo()
        tickets_model = request.env['helpdesk.ticket.pro'].sudo()
        
        thai_map = {
            'ธนชาต': 'Thanachart',
            'ไทยประกัน': 'Thai Life',
            'กฟน': 'Metropolitan',
            'ไฟฟ้า': 'Electricity',
            'ทอ': 'Air Force',
            'กองทัพอากาศ': 'Air Force',
            'วิริยะ': 'Viriyah',
            'เงินติดล้อ': 'Tid Lor',
            'นวกิจ': 'Navakij',
            'cimb': 'CIMB'
        }
        search_terms = [q]
        for th, en in thai_map.items():
            if th in q:
                search_terms.append(en)
                
        sub_domains = []
        for term in search_terms:
            sub_domains.extend([
                ('ref', 'ilike', term),
                ('name', 'ilike', term),
                ('city', 'ilike', term),
                ('comment', 'ilike', term)
            ])
            
        domain = []
        for i in range(len(sub_domains) - 1):
            domain.append('|')
        domain.extend(sub_domains)

        results = partners.search(domain, limit=5)

        items = []
        for p in results:
            t_domain = ['&', ('state', 'not in', ['closed', 'solved', 'cancel']),
                        '|', ('partner_id', '=', p.id), ('avaya_customer_name', 'ilike', p.name)]
            open_tickets = tickets_model.search(t_domain, limit=3)
            active_list = []
            for t in open_tickets:
                eng = t.site_engineer_id.name if hasattr(t, 'site_engineer_id') and t.site_engineer_id else (t.user_id.name if t.user_id else 'Phongthep Phimthong')
                active_list.append({
                    'id': t.id,
                    'ref': t.name,
                    'subject': t.subject,
                    'sr_number': t.avaya_sr_number or '',
                    'site_engineer': eng,
                    'state': t.state,
                    'portal_url': f'https://phongthep.lol/helpdesk/ticket/{t.id}'
                })

            addr_parts = [p.street, p.street2, p.city]
            full_addr = ', '.join([x for x in addr_parts if x])

            items.append({
                'id': p.id,
                'customer_name': p.name,
                'sold_to': p.ref or '',
                'phone': p.phone or p.mobile or '',
                'email': p.email or '',
                'address': full_addr,
                'notes': p.comment or '',
                'active_tickets': active_list
            })

        return request.make_response(
            json.dumps({'success': True, 'count': len(items), 'data': items, 'tickets': items}),
            headers=[('Content-Type', 'application/json'), ('Access-Control-Allow-Origin', '*')]
        )

    @http.route('/helpdesk/api/search_tickets', type='http', auth='public', methods=['GET'], csrf=False)
    def helpdesk_api_search_tickets(self, query='', **kwargs):
        q = query.strip() if query else ''
        tickets_model = request.env['helpdesk.ticket.pro'].sudo()
        
        domain = []
        if q:
            # Map Thai customer keywords to English
            thai_map = {
                'ธนชาต': 'Thanachart',
                'ไทยประกัน': 'Thai Life',
                'กฟน': 'Metropolitan',
                'ไฟฟ้า': 'Electricity',
                'ทอ': 'Air Force',
                'กองทัพอากาศ': 'Air Force',
                'วิริยะ': 'Viriyah',
                'เงินติดล้อ': 'Tid Lor',
                'นวกิจ': 'Navakij'
            }
            search_terms = [q]
            for th, en in thai_map.items():
                if th in q:
                    search_terms.append(en)
            
            sub_domains = []
            for term in search_terms:
                sub_domains.extend([
                    ('name', 'ilike', term),
                    ('subject', 'ilike', term),
                    ('avaya_customer_name', 'ilike', term),
                    ('avaya_sr_number', 'ilike', term)
                ])
            
            # Combine with OR
            domain = ['|'] * (len(sub_domains) - 1) + sub_domains

        tickets = tickets_model.search(domain, order='id desc', limit=20)
        
        items = []
        for t in tickets:
            items.append({
                'id': t.id,
                'ref': t.name,
                'subject': t.subject,
                'customer': t.partner_id.name or t.avaya_customer_name or 'Not specified',
                'sr_number': t.avaya_sr_number or '',
                'state': t.state or 'new',
                'priority': t.priority or '0',
                'site_engineer': getattr(t, 'site_engineer', None) or 'Phongthep Phimthong',
                'sold_to_id': getattr(t, 'sold_to_id', '') or '',
                'avaya_status': getattr(t, 'avaya_status', '') or '',
                'portal_url': f'https://phongthep.lol/helpdesk/ticket/{t.id}'
            })

        return request.make_response(
            json.dumps({'success': True, 'count': len(items), 'data': items, 'tickets': items}),
            headers=[('Content-Type', 'application/json'), ('Access-Control-Allow-Origin', '*')]
        )
