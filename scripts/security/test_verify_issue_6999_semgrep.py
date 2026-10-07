"""Synthetic report checks for the scoped Semgrep regression fixture."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from verify_issue_6999_semgrep import check_report, ASSIGNED, EVAL, SSRF


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.fixture = Path('/tmp/unsafe-server.js')
        results = []
        for rule, path, line in ASSIGNED:
            results.append(self.result(rule, path, line))
        results.extend((self.result(SSRF, str(self.fixture), 2),
                        self.result(EVAL, str(self.fixture), 3)))
        self.report = {'runs': [{'results': results}]}

    @staticmethod
    def result(rule, path, line):
        return {'ruleId': 'gitlab.' + rule, 'locations': [{'physicalLocation': {
            'artifactLocation': {'uri': path}, 'region': {'startLine': line}}}]}

    def test_exact_candidate_and_both_unsafe_controls(self):
        self.assertEqual(len(check_report(self.report, self.fixture)), 11)

    def test_uncovered_server_fetch_or_timer_rejected(self):
        for index in (-1, -2):
            results = list(self.report['runs'][0]['results'])
            results.pop(index)
            with self.assertRaisesRegex(ValueError, 'missing'):
                check_report({'runs': [{'results': results}]}, self.fixture)

    def test_extra_finding_or_scanner_error_rejected(self):
        self.report['runs'][0]['results'].append(self.result(SSRF, 'other.js', 20))
        with self.assertRaisesRegex(ValueError, 'Unassigned'):
            check_report(self.report, self.fixture)
        self.report['runs'][0]['results'].pop()
        self.report['runs'][0]['invocations'] = [{'toolExecutionNotifications': [{'level': 'error'}]}]
        with self.assertRaisesRegex(ValueError, 'scan error'):
            check_report(self.report, self.fixture)


if __name__ == '__main__':
    unittest.main()
