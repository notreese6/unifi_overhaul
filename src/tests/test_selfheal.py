"""
--install-self-heal / --remove-self-heal on the client side. ssh is mocked throughout: the install command creates
directories under /data, which must never run on the test machine.
"""

import os
import re
import unittest
from unittest import mock

from unifi_overhaul import cli, client, selfheal
from unifi_overhaul.topology import Node, Topology


def topology():
    """
    A two-device mesh on channel 6.

    Args:
        None

    Returns:
        Topology: gateway -> ap-hall.

    Raises:
        None
    """

    nodes = [Node("gateway", "192.0.2.1", "root"), Node("ap-hall", "192.0.2.20", parent="gateway")]
    return Topology(path="mesh.ini", channel=6, nodes=nodes)


def ok_run(stdout="[+] done"):
    """
    A successful ssh run.

    Args:
        stdout (str): what the gateway printed.

    Returns:
        client.RemoteRun: the run.

    Raises:
        None
    """

    return client.RemoteRun(target=None, returncode=0, stdout=stdout, stderr="", seconds=0.1)


class TestSelfHeal(unittest.TestCase):
    """
    The install and remove commands, their confirmation, and agreement with the on-device script.
    """

    def test_remote_dir_matches_the_script_default(self):
        with open(client.DEFAULT_SCRIPT, encoding="utf-8") as handle:
            default = re.search(r"HEAL_DIR=\$\{UNIFI_OVERHAUL_DATA:-([^}]+)\}", handle.read()).group(1)
        self.assertEqual(client.REMOTE_HEAL_DIR, default)

    def test_install_copies_the_script_then_installs_on_the_root(self):
        with mock.patch.object(client, "run_ssh", return_value=ok_run()) as run_ssh:
            status = selfheal.install(topology(), b"#!/bin/sh\n", confirm=lambda question: True)

        target, remote, stdin = run_ssh.call_args.args[:3]
        self.assertEqual(status, client.ExitStatus.ALL_LIVE)
        self.assertEqual(target.ssh_target, "root@192.0.2.1")
        self.assertIn(f"cat > {client.REMOTE_HEAL_DIR}/unifi-overhaul.sh", remote)
        self.assertTrue(remote.endswith("__install --channel 6"))
        self.assertEqual(stdin, b"#!/bin/sh\n")

    def test_declined_install_touches_nothing(self):
        with mock.patch.object(client, "run_ssh") as run_ssh:
            status = selfheal.install(topology(), b"", confirm=lambda question: False)

        run_ssh.assert_not_called()
        self.assertEqual(status, client.ExitStatus.NOT_RUN)

    def test_failed_install_needs_action(self):
        failed = client.RemoteRun(target=None, returncode=1, stdout="[-] no systemd", stderr="", seconds=0.1)
        with mock.patch.object(client, "run_ssh", return_value=failed):
            status = selfheal.install(topology(), b"", confirm=lambda question: True, is_confirmed=True)
        self.assertEqual(status, client.ExitStatus.NEEDS_ACTION)

    def test_remove_pipes_the_script_to_the_root(self):
        with mock.patch.object(client, "run_remote", return_value=ok_run("[+] self-heal removed")) as run_remote:
            status = selfheal.remove(topology(), b"script", confirm=lambda question: True)

        target, script, mode = run_remote.call_args.args[:3]
        self.assertEqual((target.ssh_target, script, mode), ("root@192.0.2.1", b"script", "__remove"))
        self.assertEqual(status, client.ExitStatus.ALL_LIVE)


class TestChangeProblems(unittest.TestCase):
    """
    The command-line checks shared by --apply and the self-heal options.
    """

    def problems(self, *argv):
        args = cli.build_parser().parse_args(list(argv))
        with mock.patch("sys.stdin.isatty", return_value=True):
            return cli.change_problems(args)

    def test_one_change_at_a_time(self):
        self.assertIn("pick one", self.problems("--apply", "--install-self-heal")[0])

    def test_self_heal_takes_its_device_from_mesh_ini(self):
        self.assertIn("mesh.ini", self.problems("--remove-self-heal", "root@192.0.2.1")[0])

    def test_yes_needs_a_change(self):
        self.assertIn("--yes only applies", self.problems("--yes")[0])

    def test_self_heal_alone_is_fine(self):
        self.assertEqual(self.problems("--install-self-heal"), [])

    def test_no_terminal_needs_yes(self):
        args = cli.build_parser().parse_args(["--install-self-heal"])
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertIn("pass --yes", cli.change_problems(args)[0])


if __name__ == "__main__":
    unittest.main()
