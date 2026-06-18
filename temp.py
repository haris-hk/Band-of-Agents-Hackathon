import requests
import json

AGENT_KEY = "band_a_1781718378_oGUOGcE1RkdxVP9apFtoOvUiIvU-3NSJ"
ROOM_ID = "a16e6f9b-0e0b-49a6-8d0b-f3ea2e7fa5ad"

import os

alert_data = {
    "repo_url": "https://github.com/hamzaraza123/mock-buggy-project",
    "error": "Fix syntax error in level_1_syntax/app.py",
    "impact": "error",
    "service_short": "mock-buggy-project",
    "severity": "sev2",
    "auto_pr": "true",
    "github_token": "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"   # ← your token here
}

data = {
    "message": {
        "content": f"@alert-triager {json.dumps(alert_data)}",
        "mentions": [
            {"handle": "zealox587/alert-triager"}  # Mention Alert Triager
        ]
    }
}

response = requests.post(
    f"https://app.band.ai/api/v1/agent/chats/{ROOM_ID}/messages",
    headers={
        "X-API-Key": AGENT_KEY,  # Using Reproducer's key
        "Content-Type": "application/json"
    },
    json=data
)

print(f"Status: {response.status_code}")