#!/usr/bin/env python3
import os
import sys
import json
import urllib.request

LINE_TOKEN = 'WNdOFqC4kto2088XfYnjyUtdFCsclKz3WO2mU0X4LlS0Z8ue0rULhH01s+HVk403o0Zxqjo64kdgRmz9CXw+6KV7gR2j4eFq37dVqdXzPdilT5SBB214alUlV3fOXacQul6h/Ml7egbQBtuhdf5fvAdB04t89/1O/w1cDnyilFU='
GROUP_FILE = '/home/captain/line_group_id.txt'

def get_target():
    if os.path.exists(GROUP_FILE):
        try:
            with open(GROUP_FILE, 'r', encoding='utf-8') as f:
                target = f.read().strip()
                if target:
                    return target
        except Exception:
            pass
    return None

def send_line_push(text, target=None):
    if not target:
        target = get_target()
    if not target:
        print('[LINE Push] No target group or user ID found yet.', file=sys.stderr)
        return False

    url = 'https://api.line.me/v2/bot/message/push'
    headers = {
        'Authorization': f'Bearer {LINE_TOKEN}',
        'Content-Type': 'application/json'
    }
    payload = {
        'to': target,
        'messages': [
            {
                'type': 'text',
                'text': text
            }
        ]
    }
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except Exception as e:
        print(f'[LINE Push error] {e}', file=sys.stderr)
        return False

if __name__ == '__main__':
    if len(sys.argv) > 1:
        msg = ' '.join(sys.argv[1:])
        send_line_push(msg)
    else:
        print('Usage: send_line_alert.py <message>')
