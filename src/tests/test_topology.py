"""
mesh.ini loading: roles and order come from the tree, and every kind of invalid file is named.
"""

import os
import tempfile
import textwrap
import unittest

from unifi_overhaul.topology import Role, Topology, TopologyError

VALID = """
[mesh]
channel = 11

[gateway]
host = 192.0.2.1
user = root

[ap-hall]
host   = 192.0.2.20
user   = device-ssh-user
parent = gateway

[ap-far]
host   = ap-far
parent = ap-hall
"""


def load(text):
    """
    Load mesh.ini content from a temporary file.

    Args:
        text (str): the file content.

    Returns:
        Topology: the loaded topology.

    Raises:
        TopologyError: the content is invalid.
    """

    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "mesh.ini")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(text))
        return Topology.load(path)


def problems(text):
    """
    The problems an invalid mesh.ini is rejected with.

    Args:
        text (str): the file content.

    Returns:
        list[str]: the problems, or [] if it loaded.

    Raises:
        None
    """

    try:
        load(text)
    except TopologyError as error:
        return error.problems
    return []


class TestTopology(unittest.TestCase):
    """
    A valid mesh.ini, and each way one can be wrong.
    """

    def test_roles_and_order_come_from_the_tree(self):
        topology = load(VALID)
        self.assertEqual([(n.name, topology.role(n)) for n in topology.ordered()],
                         [("gateway", Role.ROOT), ("ap-hall", Role.RELAY), ("ap-far", Role.LEAF)])
        self.assertEqual(topology.channel, 11)

    def test_ssh_target_defers_to_ssh_config_without_a_user(self):
        topology = load(VALID)
        self.assertEqual(topology.node("ap-hall").ssh_target, "device-ssh-user@192.0.2.20")
        self.assertEqual(topology.node("ap-far").ssh_target, "ap-far")

    def test_inline_comments_are_ignored(self):
        topology = load("[gw]\nhost = 192.0.2.1   # the gateway\nuser = root ; admin\n")
        self.assertEqual(topology.node("gw").ssh_target, "root@192.0.2.1")

    def test_missing_file(self):
        with self.assertRaises(TopologyError) as caught:
            Topology.load("/nonexistent-dir/mesh.ini")
        self.assertIn("not found", caught.exception.problems[0])

    def test_every_problem_is_reported(self):
        found = problems("""
            [mesh]
            channel = 36
            [a]
            parent = nowhere
            [b]
            host = 192.0.2.9
        """)
        self.assertEqual(len(found), 3)
        self.assertTrue(any("channel" in p for p in found))
        self.assertTrue(any("[a] has no host" in p for p in found))
        self.assertTrue(any("'nowhere'" in p for p in found))

    def test_two_roots(self):
        self.assertIn("exactly one", problems("[a]\nhost = x\n[b]\nhost = y\n")[0])

    def test_loop(self):
        found = problems("[root]\nhost = r\n[a]\nhost = x\nparent = b\n[b]\nhost = y\nparent = a\n")
        self.assertIn("loop", found[0])

    def test_no_devices(self):
        self.assertIn("no devices", problems("[mesh]\nchannel = 1\n")[0])


if __name__ == "__main__":
    unittest.main()
