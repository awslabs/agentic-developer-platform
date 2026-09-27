import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "awscli_verify", Path(__file__).with_name("verify.py")
)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class SignatureStatusTests(unittest.TestCase):
    def setUp(self):
        self.good = (
            "[GNUPG:] GOODSIG A6310ACC4672475C AWS CLI Team\n[GNUPG:] VALIDSIG "
            + verify.FINGERPRINT
            + " 2026-09-25 1790360275 0 4 0 1 10 00 "
            + verify.FINGERPRINT
            + "\n"
        )

    def test_expected_signature(self):
        verify.check_status(self.good)

    def test_gpg_zero_exit_expired_signature_is_rejected(self):
        for error in (
            "KEYEXPIRED 1783435745",
            "EXPKEYSIG A6310ACC4672475C AWS CLI Team",
            "REVKEYSIG A6310ACC4672475C AWS CLI Team",
            "EXPSIG A6310ACC4672475C AWS CLI Team",
            "BADSIG A6310ACC4672475C",
            "ERRSIG A6310ACC4672475C",
            "FAILURE verify 1",
            "ERROR verify 1",
        ):
            with self.subTest(error=error), self.assertRaises(ValueError):
                verify.check_status(self.good + "[GNUPG:] " + error + "\n")

    def test_missing_duplicate_or_other_signer_is_rejected(self):
        for status in (
            "",
            self.good + self.good,
            self.good.replace(verify.FINGERPRINT, "0" * 40),
            self.good.splitlines()[1],
        ):
            with self.subTest(status=status), self.assertRaises(ValueError):
                verify.check_status(status)


if __name__ == "__main__":
    unittest.main()
