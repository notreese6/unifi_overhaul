"""
Up-front input validation for --diagnostics / --log.
"""

import os
import tempfile
import unittest

from unifi_overhaul import client


def parse(*argv):
    """
    Parse a command line the way parse_arguments does, including the default log name.

    Args:
        *argv (str): the arguments after the program name.

    Returns:
        argparse.Namespace: the parsed arguments.

    Raises:
        SystemExit: argparse rejected the arguments.
    """

    args = client.build_parser().parse_args(list(argv))
    if args.diagnostics and not args.log:
        args.log = os.path.join(client.LOG_DIR, "unifi-diagnostics-test.log")
    return args


class TestDiagnosticsProblems(unittest.TestCase):
    """
    diagnostics_problems accepts a usable log path and names each kind of bad one.
    """

    def test_log_without_diagnostics(self):
        self.assertIn("only applies", client.diagnostics_problems(parse("--log", "x.log"))[0])

    def test_list_with_diagnostics(self):
        self.assertIn("--list", client.diagnostics_problems(parse("--diagnostics", "--list"))[0])

    def test_existing_log_is_never_overwritten(self):
        with tempfile.NamedTemporaryFile(suffix=".log") as existing:
            problems = client.diagnostics_problems(parse("--diagnostics", "--log", existing.name))
        self.assertIn("already exists", problems[0])

    def test_missing_directory(self):
        problems = client.diagnostics_problems(parse("--diagnostics", "--log", "/nonexistent-dir/x.log"))
        self.assertIn("does not exist", problems[0])

    def test_default_log_lands_in_logs_dir(self):
        self.assertEqual(client.diagnostics_problems(parse("--diagnostics")), [])
        self.assertTrue(os.path.isdir(client.LOG_DIR))

    def test_plain_run_needs_nothing(self):
        self.assertEqual(client.diagnostics_problems(parse()), [])


if __name__ == "__main__":
    unittest.main()
