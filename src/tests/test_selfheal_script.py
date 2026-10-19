"""
The gateway self-heal modes of unifi-overhaul.sh (__install, __heal, __remove, and the probe's S line), run against
temporary directories, a fake systemctl and a fake syswrapper, so nothing on the test machine is touched.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from unifi_overhaul import client

from tests.helpers import fake_path
from tests.test_script import FIXTURE, downlinks_24

KEY  = "ssh-rsa AAAAexamplekey unifi"
UNIT = "unifi-overhaul-heal"


class TestSelfHealScript(unittest.TestCase):
    """
    Install writes exactly its files, heal re-applies the root and restores keys, remove deletes only what install made.
    """

    def setUp(self):
        self.folder    = tempfile.mkdtemp()
        self.heal_dir  = os.path.join(self.folder, "data", "unifi-overhaul")
        self.unit_dir  = os.path.join(self.folder, "units")
        self.auth_keys = os.path.join(self.folder, "root", ".ssh", "authorized_keys")
        self.cfg       = os.path.join(self.folder, "system.cfg")
        self.calls     = os.path.join(self.folder, "systemctl.log")
        self.applies   = os.path.join(self.folder, "syswrapper.log")

        # What the client's install step does before running __install: copy the script into the data dir
        os.makedirs(self.heal_dir)
        shutil.copy(client.DEFAULT_SCRIPT, os.path.join(self.heal_dir, "unifi-overhaul.sh"))
        os.makedirs(self.unit_dir)
        os.makedirs(os.path.dirname(self.auth_keys))
        with open(self.auth_keys, "w", encoding="utf-8") as handle:
            handle.write(KEY + "\n")
        shutil.copy(FIXTURE, self.cfg)

        syswrapper = os.path.join(self.folder, "syswrapper.sh")
        with open(syswrapper, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\necho "$@" >> {self.applies}\n')
        os.chmod(syswrapper, stat.S_IRWXU)

        self.env = dict(os.environ, **fake_path(), UNIFI_OVERHAUL_DATA=self.heal_dir, UNIFI_OVERHAUL_UNITS=self.unit_dir,
                        UNIFI_OVERHAUL_AUTH_KEYS=self.auth_keys, UNIFI_OVERHAUL_CFG=self.cfg,
                        UNIFI_OVERHAUL_SYSWRAPPER=syswrapper, FAKE_SYSTEMCTL_LOG=self.calls)

    def tearDown(self):
        shutil.rmtree(self.folder)

    def run_script(self, *args, expect=0):
        """
        Run unifi-overhaul.sh with every path pointed into the temporary directory.

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

    def read(self, path):
        """
        A file's contents, or "" when it doesn't exist.

        Args:
            path (str): the file.

        Returns:
            str: its text.

        Raises:
            None
        """

        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_install_writes_units_settings_and_key_snapshot(self):
        output = self.run_script("__install", "--channel", "6")

        self.assertIn(f"{UNIT}.timer: active", output)
        self.assertIn(f"ExecStart=/bin/sh {self.heal_dir}/unifi-overhaul.sh __heal", self.read(os.path.join(self.unit_dir, f"{UNIT}.service")))
        self.assertIn("OnUnitActiveSec=5min", self.read(os.path.join(self.unit_dir, f"{UNIT}.timer")))
        self.assertEqual(self.read(os.path.join(self.heal_dir, "settings")), "CHANNEL=6\n")
        self.assertEqual(self.read(os.path.join(self.heal_dir, "authorized_keys")), KEY + "\n")
        self.assertIn(f"enable --now {UNIT}.timer", self.read(self.calls))

    def test_install_refuses_without_the_script_copy(self):
        os.remove(os.path.join(self.heal_dir, "unifi-overhaul.sh"))
        self.assertIn("missing", self.run_script("__install", expect=1))
        self.assertEqual(os.listdir(self.unit_dir), [])

    def test_heal_applies_the_root_once(self):
        self.run_script("__install", "--channel", "6")

        self.run_script("__heal")
        self.assertEqual(downlinks_24(self.cfg), 1)
        self.assertIn("radio.1.channel=6", self.read(self.cfg))

        self.assertIn("already applied", self.run_script("__heal"))
        self.assertEqual(len(self.read(self.applies).splitlines()), 1)

    def test_heal_restores_a_wiped_key(self):
        self.run_script("__install")
        with open(self.auth_keys, "w", encoding="utf-8") as handle:
            handle.write("")

        self.assertIn("restored an ssh key", self.run_script("__heal"))
        self.assertEqual(self.read(self.auth_keys), KEY + "\n")

    def test_heal_waits_for_the_config(self):
        self.run_script("__install")
        os.remove(self.cfg)

        self.assertIn("not there yet", self.run_script("__heal"))
        self.assertEqual(self.read(self.applies), "")

    def test_probe_reports_the_timer(self):
        self.assertIn("S absent", self.run_script("__probe"))
        self.run_script("__install")
        self.assertIn("S active", self.run_script("__probe"))

    def test_remove_deletes_only_what_install_made(self):
        self.run_script("__install")
        self.run_script("__remove")

        self.assertEqual(os.listdir(self.unit_dir), [])
        self.assertFalse(os.path.exists(self.heal_dir))
        self.assertIn(f"disable --now {UNIT}.timer", self.read(self.calls))

    def test_remove_leaves_files_it_did_not_create(self):
        self.run_script("__install")
        stranger = os.path.join(self.heal_dir, "notes.txt")
        with open(stranger, "w", encoding="utf-8") as handle:
            handle.write("keep me")

        self.assertIn("left", self.run_script("__remove"))
        self.assertEqual(os.listdir(self.heal_dir), ["notes.txt"])


if __name__ == "__main__":
    unittest.main()
