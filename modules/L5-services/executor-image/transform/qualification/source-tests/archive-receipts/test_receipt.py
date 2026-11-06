import unittest
from receipt import make_receipt

class Receipts(unittest.TestCase):
    def record(self, **changes):
        return {"record_id": "synthetic-17", "created_at": "2026-01-01T00:00:00Z", "body": "example", **changes}
    def test_deterministic_and_changed_content(self):
        self.assertEqual(make_receipt(self.record()), make_receipt(self.record()))
        self.assertNotEqual(make_receipt(self.record())["sha256"], make_receipt(self.record(body="changed"))["sha256"])
    def test_timezone_normalization(self):
        self.assertEqual(make_receipt(self.record()), make_receipt(self.record(created_at="2026-01-01T01:00:00+01:00")))
    def test_path_traversal_and_unbounded_body(self):
        for changes in ({"record_id": "../escape"}, {"body": "x" * 4097}, {"created_at": "2026-01-01T00:00:00"}):
            with self.assertRaises(ValueError):
                make_receipt(self.record(**changes))

if __name__ == "__main__":
    unittest.main()
