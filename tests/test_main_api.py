from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main


async def no_op_run_incident(_alert):
    return None


class MainApiTests(unittest.TestCase):
    def test_submit_incident_accepts_alert(self) -> None:
        with patch.dict(os.environ, {"INCIDENT_API_KEY": ""}, clear=False):
            with patch("backend.main.run_incident", no_op_run_incident):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/incidents",
                        json={"alert": {"service": "checkout", "error": "boom"}},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "accepted"})

    def test_websocket_ping_accepts_pong(self) -> None:
        async def fast_sleep(_seconds):
            raise asyncio.CancelledError

        with patch.dict(os.environ, {"INCIDENT_API_KEY": ""}, clear=False):
            with (
                patch("backend.main.run_incident", no_op_run_incident),
                patch("backend.main.asyncio.sleep", fast_sleep),
            ):
                with TestClient(main.app) as client:
                    with client.websocket_connect("/ws/incidents") as websocket:
                        websocket.send_json({"type": "pong"})


if __name__ == "__main__":
    unittest.main()
