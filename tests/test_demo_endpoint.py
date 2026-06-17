from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from backend import main


class DemoEndpointTests(unittest.TestCase):
    def test_demo_alert_endpoint(self) -> None:
        with TestClient(main.app) as client:
            response = client.get("/demo/alert")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("service"), "checkout")
        self.assertIn("repo_path", payload)
        self.assertIn("repro_command", payload)


if __name__ == "__main__":
    unittest.main()
