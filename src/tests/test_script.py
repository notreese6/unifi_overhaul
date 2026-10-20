"""
unifi-overhaul.sh apply/dryrun/rollback against a fixture config and a fake radio: re-applying changes nothing,
and a failed apply-config leaves nothing half-done.
"""

import filecmp
import os
import shutil
import subprocess
import tempfile
import unittest

from unifi_overhaul import client

from tests.helpers import FakeRadio, fake_path

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


def read(path):
    """
    A file's text.

    Args:
        path (str): the file.

    Returns:
        str: its contents.

    Raises:
        OSError: the file can't be read.
    """

    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestApplyIsIdempotent(unittest.TestCase):
    """
    apply only adds what's missing, keeps the first backup, and rollback still reaches the original.
    """

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.cfg    = os.path.join(self.folder, "system.cfg")
        self.radio  = FakeRadio(self.folder)
        shutil.copy(FIXTURE, self.cfg)

        self.env = dict(os.environ, **fake_path(), **self.radio.env(), UNIFI_OVERHAUL_CFG=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.folder)

    def run_script(self, *args, expect=0):
        """
        Run unifi-overhaul.sh with the fixture config and the fake radio.

        Args:
            *args (str): the mode and options.
            expect (int): the exit status the run must have.

        Returns:
            str: everything it printed.

        Raises:
            AssertionError: it exited with a different status.
        """

        result = subprocess.run(["sh", client.DEFAULT_SCRIPT, *args], env=self.env, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result.stdout

    def test_relay_apply_adds_downlink_and_moves_uplink(self):
        self.run_script("apply", "--role", "relay", "--channel", "1")

        self.assertEqual(downlinks_24(self.cfg), 1)
        self.assertIn("wireless.9.parent=wifi0", read(self.cfg))
        self.assertIn("radio.1.channel=1", read(self.cfg))
        self.assertEqual(self.radio.call_count(), 1)

    def test_second_apply_changes_nothing(self):
        self.run_script("apply", "--role", "relay", "--channel", "1")
        after_first = read(self.cfg)

        output = self.run_script("apply", "--role", "relay", "--channel", "1")

        self.assertIn("already applied", output)
        self.assertEqual(read(self.cfg), after_first)
        self.assertEqual(self.radio.call_count(), 1)

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

    def test_failed_syswrapper_puts_the_config_back(self):
        open(self.radio.fail, "w").close()

        output = self.run_script("apply", "--role", "root", expect=1)

        self.assertIn("config was put back", output)
        self.assertTrue(filecmp.cmp(self.cfg, FIXTURE, shallow=False))

    def test_retry_after_a_failure_applies(self):
        open(self.radio.fail, "w").close()
        self.run_script("apply", "--role", "root", expect=1)
        os.remove(self.radio.fail)

        self.run_script("apply", "--role", "root")
        self.assertEqual(downlinks_24(self.cfg), 1)
        self.assertTrue(os.path.exists(self.radio.live))

    def test_waits_for_a_busy_radio_manager(self):
        self.radio.set_busy(2)

        output = self.run_script("apply", "--role", "root")

        self.assertIn("radio manager busy", output)
        self.assertEqual(self.radio.call_count(), 1)
        self.assertEqual(downlinks_24(self.cfg), 1)

    def test_gives_up_on_a_radio_manager_that_stays_busy(self):
        self.radio.set_busy(100)

        output = self.run_script("apply", "--role", "root", expect=1)

        self.assertIn("still busy", output)
        self.assertEqual(self.radio.call_count(), 0)
        self.assertTrue(filecmp.cmp(self.cfg, FIXTURE, shallow=False))

    def test_change_in_the_file_but_not_live_is_pushed_again(self):
        self.run_script("apply", "--role", "root")
        after_first = read(self.cfg)
        os.remove(self.radio.live)

        self.assertIn("isn't live -- apply would push it again", self.run_script("dryrun", "--role", "root"))
        self.assertIn("pushing it again", self.run_script("apply", "--role", "root"))
        self.assertEqual(read(self.cfg), after_first)
        self.assertEqual(self.radio.call_count(), 2)

    def test_parent_pins_the_preferred_uplink(self):
        self.run_script("apply", "--role", "leaf", "--parent", "02:00:00:00:02:00")
        self.assertIn("mesh.serial1=02:00:00:00:02:00", read(self.cfg))

        self.assertIn("already applied", self.run_script("apply", "--role", "leaf", "--parent", "02:00:00:00:02:00"))
        self.assertEqual(self.radio.call_count(), 1)

    def test_root_ignores_parent(self):
        self.run_script("apply", "--role", "root", "--parent", "02:00:00:00:02:00")
        self.assertIn("mesh.serial1=02:00:00:00:01:00", read(self.cfg))

    def test_backup_and_rollback_keep_the_original(self):
        self.run_script("apply", "--role", "leaf")
        self.run_script("apply", "--role", "relay")
        self.assertTrue(filecmp.cmp(self.cfg + ".unifi-overhaul.bak", FIXTURE, shallow=False))

        self.run_script("rollback")
        self.assertTrue(filecmp.cmp(self.cfg, FIXTURE, shallow=False))


if __name__ == "__main__":
    unittest.main()
