"""
Probe parsing, the per-device LIVE/REVERTED/PARTIAL judgement, and how the tree is joined.
"""

import unittest

from unifi_overhaul import client

ROOT_24  = "02:00:00:00:01:24"
ROOT_5   = "02:00:00:00:01:05"
RELAY_24 = "02:00:00:00:02:24"
RELAY_5  = "02:00:00:00:02:05"


def device(ip, probe_output):
    """
    Parse canned __probe output for a device at `ip`.

    Args:
        ip (str): the device's address.
        probe_output (str): H / W / U / A lines as unifi-overhaul.sh __probe prints them.

    Returns:
        client.Device: the parsed device.

    Raises:
        None
    """

    target = client.Target(ssh_target=f"u@{ip}", ip=ip)
    return client.parse_probe(target, probe_output)


def gateway(has_24=True):
    """
    The wired root, optionally offering the 2.4 GHz backhaul.

    Args:
        has_24 (bool): whether its 2.4 GHz vwire AP is up.

    Returns:
        client.Device: the root.

    Raises:
        None
    """

    lines = ["H Express-7", "W 3", "U WIRED - -", f"A {ROOT_5} 5GHz"]
    if has_24:
        lines.append(f"A {ROOT_24} 2.4GHz")
    return device("192.0.2.1", "\n".join(lines))


class TestParseProbe(unittest.TestCase):
    """
    parse_probe on the probe's line format.
    """

    def test_fields(self):
        dev = device("192.0.2.195", f"H U7-Pro-Wall\nW 3\nU {ROOT_24.upper()} 2.4GHz -60\nA {RELAY_24} 2.4GHz\n")
        self.assertEqual(dev.host, "U7-Pro-Wall")
        self.assertEqual(dev.uplink, ROOT_24)
        self.assertEqual((dev.uplink_band, dev.uplink_signal), ("2.4GHz", "-60"))
        self.assertEqual(dev.aps, [(RELAY_24, "2.4GHz")])
        self.assertEqual(dev.wifi_radios, 3)

    def test_switch_has_no_wifi(self):
        self.assertFalse(device("192.0.2.2", "H switch\nW 0\nU WIRED - -").has_wifi)


class TestMeshState(unittest.TestCase):
    """
    Mesh.state and Mesh.tree_order for the layouts seen on the real mesh.
    """

    def test_chain_all_live(self):
        relay = device("192.0.2.195", f"H wall\nW 3\nU {ROOT_24} 2.4GHz -60\nA {RELAY_24} 2.4GHz")
        leaf  = device("192.0.2.72", f"H pro\nW 3\nU {RELAY_24} 2.4GHz -58")
        mesh  = client.Mesh([gateway(), relay, leaf])

        self.assertEqual([mesh.state(d) for d in mesh.devices], [client.State.LIVE] * 3)
        self.assertEqual([(d.host, depth) for d, depth in mesh.tree_order()],
                         [("Express-7", 0), ("wall", 1), ("pro", 2)])

    def test_leaf_that_skips_the_relay_hangs_off_the_root(self):
        relay = device("192.0.2.195", f"H wall\nW 3\nU {ROOT_24} 2.4GHz -60")
        leaf  = device("192.0.2.72", f"H pro\nW 3\nU {ROOT_24} 2.4GHz -78")
        mesh  = client.Mesh([gateway(), relay, leaf])

        self.assertEqual([(d.host, depth) for d, depth in mesh.tree_order()],
                         [("Express-7", 0), ("wall", 1), ("pro", 1)])

    def test_reverted_root(self):
        mesh = client.Mesh([gateway(has_24=False)])
        self.assertEqual(mesh.state(mesh.devices[0]), client.State.REVERTED)

    def test_relay_with_only_its_uplink_on_24_is_partial(self):
        relay = device("192.0.2.195", f"H wall\nW 3\nU {ROOT_24} 2.4GHz -60\nA {RELAY_5} 5GHz")
        leaf  = device("192.0.2.72", f"H pro\nW 3\nU {RELAY_5} 5GHz -73")
        mesh  = client.Mesh([gateway(), relay, leaf])

        self.assertEqual(mesh.state(relay), client.State.PARTIAL)
        self.assertEqual(mesh.state(leaf), client.State.REVERTED)

    def test_unreachable(self):
        lost = client.Device(target=client.Target(ssh_target="u@192.0.2.72", ip="192.0.2.72"), error="timeout")
        mesh = client.Mesh([gateway(), lost])
        self.assertEqual(mesh.state(lost), client.State.UNREACHABLE)


if __name__ == "__main__":
    unittest.main()
