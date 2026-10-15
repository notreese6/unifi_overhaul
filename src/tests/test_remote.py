"""
The ssh paths end to end, against fake ssh / ping / iw that run unifi-overhaul.sh locally.
"""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from unifi_overhaul import client

from tests.helpers import LAUNCHER, fake_path


def script():
    """
    The on-device script, as the client pipes it.

    Args:
        None

    Returns:
        bytes: the contents of scripts/unifi-overhaul.sh.

    Raises:
        OSError: the script can't be read.
    """

    with open(client.DEFAULT_SCRIPT, "rb") as handle:
        return handle.read()


def target(ssh_target):
    """
    A command-line style target.

    Args:
        ssh_target (str): "user@host".

    Returns:
        client.Target: the target.

    Raises:
        None
    """

    return client.Target.from_arg(ssh_target)


@mock.patch.dict(os.environ, fake_path())
class TestRunRemote(unittest.TestCase):
    """
    run_remote and the two script modes it drives.
    """

    def test_probe_reports_hostname_and_radios(self):
        run = client.run_remote(target("root@127.0.0.2"), script(), "__probe")
        self.assertEqual(run.error, "")
        self.assertRegex(run.stdout, r"(?m)^H \S+$")
        self.assertRegex(run.stdout, r"(?m)^W \d+$")

    def test_diag_runs_every_section_to_the_end(self):
        run      = client.run_remote(target("root@127.0.0.2"), script(), "__diag", timeout=120)
        sections = [line for line in run.stdout.splitlines() if line.startswith("##########")]

        self.assertEqual(run.error, "")
        self.assertEqual(len(sections), 7)
        # The last block only prints if the run got all the way through
        self.assertIn("===== diag_controller", run.stdout)
        self.assertIn("===== iw dev vwiresta8 survey dump", run.stdout)

    def test_unreachable_keeps_ssh_reason(self):
        run = client.run_remote(target("u@198.51.100.9"), script(), "__probe")
        self.assertIn("No route to host", run.error)

    def test_timeout_keeps_partial_output(self):
        run = client.run_remote(target("u@slow"), script(), "__diag", timeout=1)
        self.assertTrue(run.is_timed_out)
        self.assertIn("timed out", run.error)
        self.assertIn("partial output", run.stdout)


class TestDiagnosticsRun(unittest.TestCase):
    """
    A whole --diagnostics run through the launcher, with one good and one unreachable device.
    """

    def test_writes_log_and_flags_the_unreachable_device(self):
        with tempfile.TemporaryDirectory() as folder:
            log    = os.path.join(folder, "run.log")
            env    = dict(os.environ, **fake_path())
            result = subprocess.run([LAUNCHER, "root@127.0.0.2", "u@198.51.100.9", "--diagnostics", "--log", log],
                                    env=env, capture_output=True, text=True, timeout=300)
            with open(log, encoding="utf-8") as handle:
                text = handle.read()

        self.assertEqual(result.returncode, client.ExitStatus.NEEDS_ACTION)
        for title in ["devices", "this machine", "mesh map", "device root@127.0.0.2", "device u@198.51.100.9", "end"]:
            self.assertIn(title, text)
        self.assertIn("diagnostics incomplete", text)


if __name__ == "__main__":
    unittest.main()
