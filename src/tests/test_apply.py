"""
--apply's planner and executor against a fake mesh whose state changes when it's acted on.
"""

import unittest

from unifi_overhaul import apply as mesh_apply
from unifi_overhaul import client
from unifi_overhaul.topology import Node, Topology

ROOT_24 = "02:00:00:00:01:24"
ROOT_5  = "02:00:00:00:01:05"
HALL_24 = "02:00:00:00:02:24"
HALL_5  = "02:00:00:00:02:05"


def chain():
    """
    The intended mesh: gateway -> ap-hall (relay) -> ap-far (leaf).

    Args:
        None

    Returns:
        Topology: the topology.

    Raises:
        None
    """

    nodes = [Node("gateway", "192.0.2.1", "root"), Node("ap-hall", "192.0.2.20", parent="gateway"),
             Node("ap-far", "192.0.2.21", parent="ap-hall")]
    return Topology(path="mesh.ini", channel=1, nodes=nodes)


class FakeMesh:
    """
    Live device state as __probe output per device, which perform() edits the way a real apply or reboot would.
    """

    def __init__(self, probes, fixes=None):
        """
        Start from a given state.

        Args:
            probes (dict[str, str | None]): __probe output per device name; None means unreachable.
            fixes (dict[str, dict[str, str | None]] | None): per device, the probes to switch to once it's acted
                on; a device missing here doesn't change when acted on.

        Returns:
            None

        Raises:
            None
        """

        self.probes  = dict(probes)
        self.fixes   = fixes or {}
        self.actions = []

    def observe(self):
        """
        Probe every device.

        Args:
            None

        Returns:
            mesh_apply.Observation: the current state.

        Raises:
            None
        """

        devices = {}
        for name, output in self.probes.items():
            target = client.Target(ssh_target=name, ip=name)
            if output is None:
                devices[name] = client.Device(target=target, error="No route to host")
            else:
                devices[name] = client.parse_probe(target, output)
        return mesh_apply.Observation(mesh=client.Mesh(list(devices.values())), devices=devices)

    def perform(self, step):
        """
        Record the action and apply its effect.

        Args:
            step (mesh_apply.Step): the step being carried out.

        Returns:
            str: "" (the action always starts).

        Raises:
            None
        """

        self.actions.append((step.node.name, step.action))
        self.probes.update(self.fixes.get(step.node.name, {}))
        return ""


class FakeClock:
    """
    Time that only moves when the runner sleeps.
    """

    def __init__(self):
        """
        Start at zero.

        Args:
            None

        Returns:
            None

        Raises:
            None
        """

        self.now = 0.0

    def sleep(self, seconds):
        """
        Advance time.

        Args:
            seconds (float): how long to "sleep".

        Returns:
            None

        Raises:
            None
        """

        self.now += seconds


GATEWAY_LIVE     = f"H gateway\nW 3\nU WIRED - -\nA {ROOT_5} 5GHz\nA {ROOT_24} 2.4GHz"
GATEWAY_STOCK    = f"H gateway\nW 3\nU WIRED - -\nA {ROOT_5} 5GHz"
HALL_RELAY       = f"H ap-hall\nW 3\nU {ROOT_24} 2.4GHz -60\nA {HALL_5} 5GHz\nA {HALL_24} 2.4GHz"
HALL_AS_LEAF     = f"H ap-hall\nW 3\nU {ROOT_24} 2.4GHz -60\nA {HALL_5} 5GHz"
FAR_UNDER_HALL   = "H ap-far\nW 3\nU {} 2.4GHz -53".format(HALL_24)
FAR_UNDER_ROOT   = "H ap-far\nW 3\nU {} 2.4GHz -78".format(ROOT_24)


def runner(fake, answer=True):
    """
    A Runner wired to a FakeMesh, a fake clock, and a fixed answer to the confirmation.

    Args:
        fake (FakeMesh): the mesh.
        answer (bool): what the confirmation prompt returns.

    Returns:
        mesh_apply.Runner: the runner (its `asked` list records each question).

    Raises:
        None
    """

    clock = FakeClock()
    asked = []

    def confirm(question):
        asked.append(question)
        return answer

    built       = mesh_apply.Runner(observe=fake.observe, perform=fake.perform, confirm=confirm, sleep=clock.sleep,
                                    clock=lambda: clock.now)
    built.asked = asked
    return built


class TestDecide(unittest.TestCase):
    """
    The verdict for each device in the states seen on the real mesh.
    """

    def verdicts(self, probes):
        fake = FakeMesh(probes)
        return {step.node.name: step.action for step in mesh_apply.plan(chain(), fake.observe())}

    def test_all_matching(self):
        verdicts = self.verdicts({"gateway": GATEWAY_LIVE, "ap-hall": HALL_RELAY, "ap-far": FAR_UNDER_HALL})
        self.assertEqual(set(verdicts.values()), {mesh_apply.Action.NONE})

    def test_relay_demoted_to_leaf_and_leaf_on_the_wrong_parent(self):
        verdicts = self.verdicts({"gateway": GATEWAY_LIVE, "ap-hall": HALL_AS_LEAF, "ap-far": FAR_UNDER_ROOT})
        self.assertEqual(verdicts, {"gateway": mesh_apply.Action.NONE, "ap-hall": mesh_apply.Action.APPLY,
                                    "ap-far": mesh_apply.Action.RECONNECT})

    def test_reverted_root_and_unreachable_ap(self):
        verdicts = self.verdicts({"gateway": GATEWAY_STOCK, "ap-hall": None, "ap-far": None})
        self.assertEqual(verdicts["gateway"], mesh_apply.Action.APPLY)
        self.assertEqual(verdicts["ap-hall"], mesh_apply.Action.BLOCKED)

    def test_warnings_flag_wrong_parent_and_weak_uplink(self):
        fake  = FakeMesh({"gateway": GATEWAY_LIVE, "ap-hall": HALL_AS_LEAF, "ap-far": FAR_UNDER_ROOT})
        lines = mesh_apply.warnings(chain(), fake.observe())
        self.assertTrue(any("ap-far is connected to gateway, mesh.ini says ap-hall" in line for line in lines))
        self.assertTrue(any("ap-far's uplink is weak (-78 dBm" in line for line in lines))


class TestRun(unittest.TestCase):
    """
    Whole --apply runs: order, stopping, confirming, and waiting out an outage.
    """

    def test_fixes_relay_then_moves_leaf(self):
        fake   = FakeMesh({"gateway": GATEWAY_LIVE, "ap-hall": HALL_AS_LEAF, "ap-far": FAR_UNDER_ROOT},
                          fixes={"ap-hall": {"ap-hall": HALL_RELAY}, "ap-far": {"ap-far": FAR_UNDER_HALL}})
        status = mesh_apply.run(chain(), runner(fake))

        self.assertEqual(status, client.ExitStatus.ALL_LIVE)
        self.assertEqual(fake.actions, [("ap-hall", mesh_apply.Action.APPLY), ("ap-far", mesh_apply.Action.RECONNECT)])

    def test_stops_at_a_device_that_wont_settle_and_leaves_the_rest(self):
        fake   = FakeMesh({"gateway": GATEWAY_LIVE, "ap-hall": HALL_AS_LEAF, "ap-far": FAR_UNDER_ROOT})
        status = mesh_apply.run(chain(), runner(fake))

        self.assertEqual(status, client.ExitStatus.NEEDS_ACTION)
        self.assertEqual(fake.actions, [("ap-hall", mesh_apply.Action.APPLY)])

    def test_power_cut_waits_for_aps_once_the_gateway_is_fixed(self):
        fake = FakeMesh({"gateway": GATEWAY_STOCK, "ap-hall": None, "ap-far": None},
                        fixes={"gateway": {"gateway": GATEWAY_LIVE, "ap-hall": HALL_RELAY, "ap-far": FAR_UNDER_HALL}})
        status = mesh_apply.run(chain(), runner(fake))

        self.assertEqual(status, client.ExitStatus.ALL_LIVE)
        self.assertEqual(fake.actions, [("gateway", mesh_apply.Action.APPLY)])

    def test_unreachable_ap_stops_after_the_grace_period(self):
        fake   = FakeMesh({"gateway": GATEWAY_LIVE, "ap-hall": None, "ap-far": None})
        status = mesh_apply.run(chain(), runner(fake))

        self.assertEqual(status, client.ExitStatus.NEEDS_ACTION)
        self.assertEqual(fake.actions, [])

    def test_nothing_to_do_asks_nothing(self):
        fake  = FakeMesh({"gateway": GATEWAY_LIVE, "ap-hall": HALL_RELAY, "ap-far": FAR_UNDER_HALL})
        built = runner(fake)

        self.assertEqual(mesh_apply.run(chain(), built), client.ExitStatus.ALL_LIVE)
        self.assertEqual(built.asked, [])

    def test_declining_changes_nothing(self):
        fake   = FakeMesh({"gateway": GATEWAY_STOCK, "ap-hall": HALL_AS_LEAF, "ap-far": FAR_UNDER_ROOT})
        status = mesh_apply.run(chain(), runner(fake, answer=False))

        self.assertEqual(status, client.ExitStatus.NOT_RUN)
        self.assertEqual(fake.actions, [])


if __name__ == "__main__":
    unittest.main()
