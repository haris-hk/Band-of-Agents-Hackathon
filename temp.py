import requests
import json

AGENT_KEY = "band_a_1781718378_oGUOGcE1RkdxVP9apFtoOvUiIvU-3NSJ"
ROOM_ID = "b03e5ffe-59e1-48a1-97c9-2345db411b1d"

alert_data = {
    "repo_url": "https://github.com/hamzaraza123/mock-buggy-project",
    "error": "Fix syntax error in level_1_syntax/app.py",
    "impact": "error",
    "service_short": "mock-buggy-project",
    "severity": "sev2",
    "auto_pr": "true",
}

data = {
    "message": {
        "content": f"@alert-triager {json.dumps(alert_data)}",
        "mentions": [
            {"handle": "zealox587/alert-triager"}
        ]
    }
}

response = requests.post(
    f"https://app.band.ai/api/v1/agent/chats/{ROOM_ID}/messages",
    headers={
        "X-API-Key": AGENT_KEY,
        "Content-Type": "application/json"
    },
    json=data
)

print(f"Status: {response.status_code}")
print(f"Response: {response.text}")