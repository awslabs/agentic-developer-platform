"""Candidate reviews must reject mismatched artifact/scan identity."""
import hashlib
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'images/ingestion/security/libxml2/review-candidate.py'
FIXED = '91ed51f91f06eb5a7c9fedff2ead9cb7ac6b54c9b5a10fc08ca46a61f5a699e6'


class CandidateReviewTests(unittest.TestCase):
    def review(self, *, binary=FIXED, version='2.13.9-0+adp3', runtime='21309',
               image='sha256:fixture', altered_scan=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = json.dumps({'matches': [{'artifact': {'name': 'libxml2', 'version': version},
                'vulnerability': {'id': 'CVE-2026-6653', 'severity': 'Critical'}}]}).encode()
            (root / 'grype.json').write_bytes(raw + (b' ' if altered_scan else b''))
            (root / 'receipt.json').write_text(json.dumps({'grype_sha256': hashlib.sha256(raw).hexdigest(),
                'image': 'fixture', 'docker_root_descriptor': 'sha256:fixture', 'config_digest': 'sha256:config'}))
            observed = {'version': version, 'runtime_version': runtime, 'sha256': binary}
            with patch.object(sys, 'argv', [str(SCRIPT), str(root), '--output', str(root / 'review.json')]), \
                 patch('subprocess.check_output', side_effect=[image, json.dumps(observed)]):
                runpy.run_path(str(SCRIPT), run_name='__main__')
            return json.loads((root / 'review.json').read_text())

    def test_registered_adp3_is_bound_to_exact_occurrence(self):
        report = self.review()
        self.assertEqual(report['dispositions'][0]['native_match_index'], 0)
        self.assertEqual(report['dispositions'][0]['binary_sha256'], FIXED)

    def test_same_package_version_with_other_binary_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.review(binary='0' * 64)

    def test_other_image_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.review(image='sha256:other')

    def test_modified_scan_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.review(altered_scan=True)

    def test_other_runtime_abi_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.review(runtime='21405')

    def test_unreviewed_package_build_is_rejected(self):
        with self.assertRaises(KeyError):
            self.review(version='2.13.9-0+adp4')


if __name__ == '__main__':
    unittest.main()
