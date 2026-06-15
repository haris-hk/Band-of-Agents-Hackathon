"""Regression tests for frontend lib logic (mirrors parseDiff.ts)."""
from __future__ import annotations

import unittest

from tests._parse_diff_py import parse_unified_diff


SAMPLE_PATCH = """\
--- a/services/checkout/handler.py
+++ b/services/checkout/handler.py
@@ -1,2 +1,4 @@
 def handle(x):
+    if x is None:
+        return {}
     return x
"""


class ParseDiffRegressionTests(unittest.TestCase):
    def test_parses_hunk_with_line_numbers(self) -> None:
        files = parse_unified_diff(SAMPLE_PATCH)
        self.assertEqual(len(files), 1)
        file_diff = files[0]
        self.assertEqual(file_diff.display_path, "services/checkout/handler.py")
        adds = [row for row in file_diff.rows if row.kind == "add"]
        self.assertEqual(len(adds), 2)
        self.assertEqual(adds[0].new_num, 2)
        self.assertIn("if x is None", adds[0].text)

    def test_empty_diff_returns_no_files(self) -> None:
        self.assertEqual(parse_unified_diff(""), [])
        self.assertEqual(parse_unified_diff("   "), [])


if __name__ == "__main__":
    unittest.main()
