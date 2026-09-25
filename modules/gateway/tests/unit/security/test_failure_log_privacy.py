"""Exercise the production exception-handler bodies without importing the app."""

import ast
import logging
import unittest
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[3]
HANDLERS = {
    "src/admin/middleware.py": "audit log write failed",
    "src/auth/middleware.py": "token_context pre-population failed",
    "src/internal/credential_routes.py": "credential denial audit write failed",
    "src/ratelimit/backends/in_memory.py": "rate-limit cleanup cycle failed",
}


class FailureLogPrivacy(unittest.TestCase):
    def test_failure_is_observable_without_exception_payload(self):
        for filename, message in HANDLERS.items():
            with self.subTest(file=filename):
                tree = ast.parse((GATEWAY / filename).read_text())
                handler = next(
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ExceptHandler)
                    and any(isinstance(child, ast.Constant) and child.value == message for child in ast.walk(node))
                )
                # Execute the real handler with a failure that carries a secret,
                # as database/HTTP exceptions can. No source replacement occurs.
                probe = ast.Module(
                    body=[
                        ast.Try(
                            body=[
                                ast.Raise(
                                    exc=ast.Call(
                                        func=ast.Name(id="ValueError", ctx=ast.Load()),
                                        args=[ast.Constant(value="private-request-token")],
                                        keywords=[],
                                    )
                                )
                            ],
                            handlers=[handler],
                            orelse=[],
                            finalbody=[],
                        )
                    ],
                    type_ignores=[],
                )
                ast.fix_missing_locations(probe)
                logger = logging.getLogger("security-failure-privacy")
                with self.assertLogs(logger, level="WARNING") as captured:
                    exec(compile(probe, filename, "exec"), {"logger": logger})
                self.assertEqual(captured.records[0].getMessage(), message)
                self.assertIsNone(captured.records[0].exc_info)
                self.assertNotIn("private-request-token", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
