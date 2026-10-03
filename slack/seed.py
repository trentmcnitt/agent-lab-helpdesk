"""Post seeded requests into Slack through the running server, so each one
goes out to Slack and comes back in through the Socket Mode listener.

    OPERATOR_TOKEN=... uv run slack/seed.py req-003 req-011 req-019

Seeding needs the operator token the server was started with (it prints it at
startup; set OPERATOR_TOKEN when starting the server to choose it yourself).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('HELPDESK_PORT', '8731')}"
TOKEN = os.environ.get("OPERATOR_TOKEN") or sys.exit("Set OPERATOR_TOKEN to the token the server printed at startup.")

for request_id in sys.argv[1:] or ["req-003", "req-011", "req-019"]:
    req = urllib.request.Request(f"{BASE}/api/requests/seed/{request_id}", method="POST",
                                 headers={"X-Operator-Token": TOKEN})
    with urllib.request.urlopen(req) as resp:
        print(request_id, json.loads(resp.read()))
