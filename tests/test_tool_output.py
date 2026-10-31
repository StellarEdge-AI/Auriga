"""Reply order, opaque output excerpts and strict read pagination."""

import json
import unittest

from auriga.core.tool_output import ToolOutput, wire_response, bounded_text, resolve_detail, select_fields, validate_page


class ToolOutputTests(unittest.TestCase):
    def test_summary_is_not_wrapped_and_images_keep_order(self):
        image = dict(type="inputImage", imageUrl="data:image/png;base64,fixture")
        output = wire_response(ToolOutput(dict(status="unknown"), dict(full_ref="artifact:full"), [image]).response())
        self.assertTrue(output["success"])
        self.assertEqual(json.loads(output["contentItems"][0]["text"]), dict(status="unknown"))
        self.assertEqual(json.loads(output["contentItems"][1]["text"]), dict(full_ref="artifact:full"))
        self.assertEqual(output["contentItems"][2], image)
        self.assertEqual(len(ToolOutput(None).content_items()), 1)

    def test_excerpt_keeps_start_and_terminal_error_with_exact_ranges(self):
        raw = b"BEGIN\n" + b"x" * 20000 + b"\nterminal failure: partial action"
        excerpt = bounded_text(raw, max_bytes=128)
        self.assertTrue(excerpt["text"].startswith("BEGIN\n"))
        self.assertTrue(excerpt["text"].endswith("terminal failure: partial action"))
        self.assertEqual(excerpt["byte_ranges"], [[0, 64], [len(raw)-64, len(raw)]])
        self.assertEqual(excerpt["omitted_bytes"], len(raw)-128)
        self.assertEqual(excerpt["byte_size"], len(raw))
        self.assertEqual(bounded_text("您好")["text"], "您好")
        self.assertEqual(bounded_text("")["byte_ranges"], [[0, 0]])
        full = bounded_text(raw, max_bytes=None)
        self.assertEqual(full["text"].encode(), raw)
        self.assertEqual(full["byte_ranges"], [[0, len(raw)]])
        self.assertFalse(full["truncated"])

    def test_read_controls_reject_boolean_negative_or_fractional_offsets(self):
        self.assertEqual(validate_page(), (0, 25))
        for start, limit in ((True, 2), (-1, 1), (1.0, 2), (0, False), (0, 0), (0, 101)):
            with self.subTest(start=start, limit=limit), self.assertRaises(ValueError):
                validate_page(start, limit)
        self.assertEqual(resolve_detail(program=True), "full")
        self.assertEqual(resolve_detail("summary", program=True), "summary")
        with self.assertRaises(ValueError):
            resolve_detail("unknown")

    def test_selection_copies_only_present_values_without_rounding(self):
        original = dict(a=[.123456789], b=None)
        result = select_fields(original, ("a", "b", "c"))
        self.assertEqual(result, original)
        result["a"].append(1)
        self.assertEqual(original["a"], [.123456789])


if __name__ == "__main__":
    unittest.main()
