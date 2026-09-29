import unittest

from job_name import normalize_job_name


class JobNameTests(unittest.TestCase):
    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(normalize_job_name(" \tNightly Backup\n"), "Nightly Backup")

    def test_internal_spacing_and_case_are_preserved(self):
        self.assertEqual(normalize_job_name("Nightly  Backup"), "Nightly  Backup")

    def test_whitespace_only_becomes_empty(self):
        self.assertEqual(normalize_job_name(" \t\n"), "")


if __name__ == "__main__":
    unittest.main()
