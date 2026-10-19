"""
--discover asking the gateway's controller for its adopted devices, with fake ssh and a fake mongo shell.
"""

import argparse
import os
import unittest
from unittest import mock

from unifi_overhaul import cli, client

from tests.helpers import fake_path


def args(**overrides):
    """
    Arguments as discovery reads them.

    Args:
        **overrides: values to change from the defaults.

    Returns:
        argparse.Namespace: the arguments.

    Raises:
        None
    """

    values = dict(broadcast="255.255.255.255", timeout=0.1, gateway_user="root", ap_user="apuser",
                  script=client.DEFAULT_SCRIPT)
    values.update(overrides)
    return argparse.Namespace(**values)


class TestParseDeviceList(unittest.TestCase):
    """
    parse_device_list on the __devices line format.
    """

    def test_reads_devices_and_skips_the_unusable(self):
        output = ("D\t192.0.2.20\t02:00:00:00:02:00\tuap\tUAPX\tap hall\n"
                  "D\tundefined\t02:00:00:00:04:00\tuap\tUAPX\toffline\n"
                  "D\t192.0.2.21\t02:00:00:00:03:00\tuap\tUAPX\tundefined\n"
                  "E no mongo client\n")
        found  = client.parse_device_list(output)

        self.assertEqual([(d.ip, d.hostname, d.platform) for d in found],
                         [("192.0.2.20", "ap hall", "UAPX"), ("192.0.2.21", "", "UAPX")])


@mock.patch.dict(os.environ, fake_path())
class TestDiscoveredTargets(unittest.TestCase):
    """
    The broadcast finds only the gateway; the controller supplies the APs.
    """

    def test_controller_adds_the_aps_the_broadcast_missed(self):
        gateway = client.DiscoveredDevice(ip="127.0.0.2", platform="UDR", hostname="gateway")
        with mock.patch.object(client, "discover", return_value=[gateway]):
            targets = cli.discovered_targets(args(), explicit_ips=set())

        self.assertEqual([t.ssh_target for t in targets],
                         ["root@127.0.0.2", "root@192.0.2.1", "apuser@192.0.2.20", "apuser@192.0.2.21"])

    def test_listed_ips_are_not_added_twice(self):
        gateway = client.DiscoveredDevice(ip="127.0.0.2", platform="UDR")
        with mock.patch.object(client, "discover", return_value=[gateway]):
            targets = cli.discovered_targets(args(), explicit_ips={"192.0.2.20"})

        self.assertNotIn("apuser@192.0.2.20", [t.ssh_target for t in targets])

    def test_unreachable_gateway_keeps_the_broadcast_results(self):
        gateway = client.DiscoveredDevice(ip="198.51.100.9", platform="UDR")
        with mock.patch.object(client, "discover", return_value=[gateway]):
            targets = cli.discovered_targets(args(), explicit_ips=set())

        self.assertEqual([t.ssh_target for t in targets], ["root@198.51.100.9"])

    def test_no_gateway_no_controller_query(self):
        access_point = client.DiscoveredDevice(ip="127.0.0.2", platform="UAPX")
        with mock.patch.object(client, "discover", return_value=[access_point]), \
             mock.patch.object(client, "run_remote") as run_remote:
            targets = cli.discovered_targets(args(), explicit_ips=set())

        run_remote.assert_not_called()
        self.assertEqual([t.ssh_target for t in targets], ["apuser@127.0.0.2"])


if __name__ == "__main__":
    unittest.main()
