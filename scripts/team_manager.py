#!/usr/bin/env python3
import json
import os
import re

TEAM_FILE = "/home/captain/team_members.json"

def load_members():
    if os.path.exists(TEAM_FILE):
        try:
            with open(TEAM_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_members(members):
    try:
        with open(TEAM_FILE, "w", encoding="utf-8") as f:
            json.dump(members, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Error saving team members] {e}")

def match_engineer(text):
    if not text:
        return "Captain (Phongthep Phimthong)", "@Captain", "U7065613de16484ec6166f78c33eb1bb6"
    
    clean = str(text).lower()
    members = load_members()
    for m in members:
        if m["line_name"].lower() in clean:
            return m["line_name"], m["display_tag"], m.get("user_id")
        for alias in m.get("aliases", []):
            if alias.lower() in clean:
                return m["line_name"], m["display_tag"], m.get("user_id")
                
    return "Captain (Phongthep Phimthong)", "@Captain", "U7065613de16484ec6166f78c33eb1bb6"

if __name__ == "__main__":
    for test in ["เบียร์", "ต้น", "chalerm", "กบ", "ต่อ", "unknown"]:
        name, tag, uid = match_engineer(test)
        print(f"Test: {test} -> Name: {name}, Tag: {tag}, UID: {uid}")
