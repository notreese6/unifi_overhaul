"""
unifi-overhaul.sh apply/dryrun/rollback against a fixture config and a fake syswrapper: re-applying changes nothing.
"""

import filecmp
import os
import shutil
import subprocess
import tempfile
import unittest

from unifi_overhaul import client

from tests.helpers import FAKEBIN, fake_path

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "stock-leaf.cfg")


def downlinks_24(path):
    """
    Count the vwire backhaul APs on the 2.4 GHz radio in a config.

    Args:
        path (str): the system.cfg to read.

    Returns:
        int: how many wireless.N entries are an enabled vwire downlink on wifi0.

    Raises:
        OSError: the file can't be read.
    """

    entries = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            key, _, value = line.strip().partition("=")
            parts = key.split(".")
            if len(parts) == 3 and parts[0] == "wireless":
                entries.setdefault(parts[1], {})[parts[2]] = value

    return sum(1 for entry in entries.values()
               if entry.get("parent") == "wifi0" and entry.get("usage") == "downlink" and entry.get("vwire") == "enabled")


class TestApplyIsIdempotent(unittest.TestCase):
    """
    apply only adds what's missing, keeps the first backup, and rollback still reaches the original.
    """

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.cfg    = os.path.join(self.folder, "system.cfg")
        self.calls  = os.path.join(self.folder, "syswrapper.calls")
        shutil.copy(FIXTURE, self.cfg)

        fake_syswrapper = os.path.join(self.folder, "syswrapper.sh")
        with open(fake_syswrapper, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\necho "$@" >> {self.calls}\n')
        os.chmod(fake_syswrapper, 0o755)

        self.env = dict(os.environ, **fake_path(), UNIFI_OVERHAUL_CFG=self.cfg, UNIFI_OVERHAUL_SYSWRAPPER=fake_syswrapper)

    def tearDown(self):
        shutil.rmtree(self.folder)

    def run_script(self, *args):
        """
        Run unifi-overhaul.sh with the fixture config and fake syswrapper.

        Args:
            *args (str): the mode and options.

        Returns:
            str: everything it printed.

        Raises:
            AssertionError: it exited non-zero.
        """

        result = subprocess.run(["sh", client.DEFAULT_SCRIPT, *args], env=self.env, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def syswrapper_calls(self):
        """
        How many times the fake syswrapper was run.

        Args:
            None

        Returns:
            int: the number of apply-config calls.

        Raises:
            None
        """

        if not os.path.exists(self.calls):
            return 0
        with open(self.calls, encoding="utf-8") as handle:
            return len(handle.readlines())

    def test_relay_apply_adds_downlink_and_moves_uplink(self):
        self.run_script("apply", "--role", "relay", "--channel", "1")

        self.assertEqual(downlinks_24(self.cfg), 1)
        with open(self.cfg, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("wireless.9.parent=wifi0", text)
        self.assertIn("radio.1.channel=1", text)
        self.assertEqual(self.syswrapper_calls(), 1)

    def test_second_apply_changes_nothing(self):
        self.run_script("apply", "--role", "relay", "--channel", "1")
        after_first = open(self.cfg, encoding="utf-8").read()

        output = self.run_script("apply", "--role", "relay", "--channel", "1")

        self.assertIn("already applied", output)
        self.assertEqual(open(self.cfg, encoding="utf-8").read(), after_first)
        self.assertEqual(self.syswrapper_calls(), 1)

    def test_root_applied_twice_has_one_downlink(self):
        self.run_script("apply", "--role", "root")
        self.run_script("apply", "--role", "root")
        self.assertEqual(downlinks_24(self.cfg), 1)

    def test_leaf_then_relay_only_adds_the_downlink(self):
        self.run_script("apply", "--role", "leaf")
        self.assertEqual(downlinks_24(self.cfg), 0)

        self.run_script("apply", "--role", "relay")
        self.assertEqual(downlinks_24(self.cfg), 1)

    def test_dryrun_after_apply_reports_nothing_to_do(self):
        self.run_script("apply", "--role", "relay")
        self.assertIn("apply would change nothing", self.run_script("dryrun", "--role", "relay"))

    def test_backup_and_rollback_keep_the_original(self):
        self.run_script("apply", "--role", "leaf")
        self.run_script("apply", "--role", "relay")
        self.assertTrue(filecmp.cmp(self.cfg + ".unifi-overhaul.bak", FIXTURE, shallow=False))

        self.run_script("rollback")
        self.assertTrue(filecmp.cmp(self.cfg, FIXTURE, shallow=False))


if __name__ == "__main__":
    unittest.main()
