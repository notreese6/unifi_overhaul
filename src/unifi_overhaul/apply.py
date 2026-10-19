"""
unifi_overhaul.apply: bring the live mesh in line with mesh.ini, one device at a time, parents first. Each device
is re-checked after its change, and the run stops at the first one that doesn't settle, so a failure never leaves
the devices below it half-changed.
"""

from __future__ import annotations

import enum
import sys
import time
from dataclasses import dataclass

from unifi_overhaul import client
from unifi_overhaul.topology import Role

WEAK_UPLINK_DBM    = -75   # uplinks weaker than this are flagged on the map
POLL_SECONDS       = 10
APPLY_SETTLE       = 180   # seconds for an applied device (and the ones below it) to come back
RECONNECT_SETTLE   = 300   # a reboot takes longer than a wifi restart
UNREACHABLE_GRACE  = 180   # an AP isolated by an outage can need this long to find a fixed parent
SSH_ACTION_TIMEOUT = 30


class Action(enum.Enum):
    """
    What one device needs.
    """

    NONE      = "ok"
    APPLY     = "apply"
    RECONNECT = "reboot"
    BLOCKED   = "unreachable"


@dataclass
class Step:
    """
    The verdict for one device: what it needs and why.
    """

    node:   object    # topology.Node
    role:   Role
    action: Action
    reason: str

    @property
    def line(self):
        """
        The step as one line of the plan.

        Args:
            None

        Returns:
            str: e.g. "ap-hall (relay): apply -- not offering 2.4 GHz to ap-far".

        Raises:
            None
        """

        return f"{self.node.name} ({self.role.value}): {self.action.value} -- {self.reason}"


@dataclass
class Observation:
    """
    The live mesh, probed once, with each probed device keyed by its mesh.ini name.
    """

    mesh:    client.Mesh
    devices: dict    # name -> client.Device

    def parent_name(self, name):
        """
        Which mesh.ini device this one is actually connected to.

        Args:
            name (str): the device's mesh.ini name.

        Returns:
            str: the parent's mesh.ini name, or "" when the parent isn't one of the probed devices.

        Raises:
            None
        """

        parent = self.mesh.parent(self.devices[name])
        return next((other for other, dev in self.devices.items() if dev is parent), "")


def target_for(node):
    """
    The ssh target for a mesh.ini device.

    Args:
        node (topology.Node): the device.

    Returns:
        client.Target: the target, keyed by the host as written in mesh.ini.

    Raises:
        None
    """

    return client.Target(ssh_target=node.ssh_target, ip=node.host)


def observe(topology, script, is_quiet=True):
    """
    Probe every device in mesh.ini and join the results into a mesh.

    Args:
        topology (topology.Topology): the intended mesh.
        script (bytes): the contents of unifi-overhaul.sh.
        is_quiet (bool): skip the per-device progress lines.

    Returns:
        Observation: the live state.

    Raises:
        OSError: ssh can't be executed.
    """

    devices = {node.name: client.probe(target_for(node), script, is_quiet=is_quiet) for node in topology.nodes}
    return Observation(mesh=client.Mesh(list(devices.values())), devices=devices)


def decide(topology, node, observation):
    """
    Work out what one device needs to match mesh.ini, from the live state.

    Args:
        topology (topology.Topology): the intended mesh.
        node (topology.Node): the device.
        observation (Observation): the live state.

    Returns:
        Step: NONE when it matches; APPLY when its 2.4 GHz uplink or downlink is missing; RECONNECT when it's
        on 2.4 GHz but connected to the wrong parent; BLOCKED when it can't be reached.

    Raises:
        None
    """

    dev  = observation.devices[node.name]
    role = topology.role(node)

    if not dev.is_reachable:
        return Step(node, role, Action.BLOCKED, f"can't ssh in ({dev.error})")
    if role is Role.ROOT:
        if dev.has_downlink_24:
            return Step(node, role, Action.NONE, "offering 2.4 GHz")
        return Step(node, role, Action.APPLY, "not offering 2.4 GHz")

    missing = []
    if dev.uplink_band != client.BAND_24:
        missing.append(f"uplink on {dev.uplink_band if dev.uplink_band not in ('', '?') else 'nothing'}")
    if role is Role.RELAY and not dev.has_downlink_24:
        children = ", ".join(child.name for child in topology.children(node))
        missing.append(f"not offering 2.4 GHz to {children}")
    if missing:
        return Step(node, role, Action.APPLY, "; ".join(missing))

    actual = observation.parent_name(node.name)
    if actual != node.parent:
        return Step(node, role, Action.RECONNECT,
                    f"connected to {actual or 'a device not in mesh.ini'}, mesh.ini says {node.parent}")
    return Step(node, role, Action.NONE, f"on 2.4 GHz under {node.parent} ({dev.uplink_signal} dBm)")


def plan(topology, observation):
    """
    Every device's verdict, in apply order.

    Args:
        topology (topology.Topology): the intended mesh.
        observation (Observation): the live state.

    Returns:
        list[Step]: one per device, parents first.

    Raises:
        None
    """

    return [decide(topology, node, observation) for node in topology.ordered()]


def warnings(topology, observation):
    """
    What's wrong with the live mesh compared with mesh.ini, for the plain map.

    Args:
        topology (topology.Topology): the intended mesh.
        observation (Observation): the live state.

    Returns:
        list[str]: one line per wrong parent or weak uplink; empty when there's nothing to flag.

    Raises:
        None
    """

    lines = []

    for node in topology.ordered():
        dev = observation.devices[node.name]
        if not node.parent or not dev.is_reachable or dev.is_root:
            continue
        actual = observation.parent_name(node.name)
        if actual != node.parent:
            lines.append(f"[-] {node.name} is connected to {actual or 'a device not in mesh.ini'}, "
                         f"mesh.ini says {node.parent} -- run --apply")
        signal = dev.uplink_signal.lstrip("-")
        if signal.isdigit() and -int(signal) < WEAK_UPLINK_DBM:
            lines.append(f"[-] {node.name}'s uplink is weak ({dev.uplink_signal} dBm, below {WEAK_UPLINK_DBM})")
    return lines


def perform(step, topology, script):
    """
    Carry out one step on its device: start a detached apply, or reboot it.

    Args:
        step (Step): an APPLY or RECONNECT step.
        topology (topology.Topology): the intended mesh (for the channel).
        script (bytes): the contents of unifi-overhaul.sh.

    Returns:
        str: "" when the action was started, otherwise why it couldn't be.

    Raises:
        OSError: ssh can't be executed.
    """

    target = target_for(step.node)

    if step.action is Action.RECONNECT:
        # The connection drops as the device goes down, so ssh's own exit status says nothing here
        client.run_ssh(target, "reboot", b"", timeout=SSH_ACTION_TIMEOUT)
        return ""

    # XXX - TODO: a mesh.ini channel that differs from the live chain isn't detected, so only the devices that
    # need an apply for other reasons would move and the chain would split across two channels. Changing the
    # channel of an applied mesh needs every device moved together, with the STAs following, and isn't built.
    options = f"--role {step.role.value} --channel {topology.channel}"
    run     = client.run_ssh(target, client.apply_remote_command(options), script, timeout=SSH_ACTION_TIMEOUT)
    if run.returncode != 0:
        reason = run.stderr.strip().splitlines()
        return reason[-1] if reason else f"ssh exited {run.returncode}"
    return ""


@dataclass
class Runner:
    """
    How the apply talks to the world. The defaults use ssh, the clock and the terminal; tests swap them out.
    """

    observe:    object                 # () -> Observation
    perform:    object                 # (Step) -> str
    confirm:    object                 # (str) -> bool
    sleep:      object = time.sleep
    clock:      object = time.monotonic
    poll:       float  = POLL_SECONDS

    def wait_for(self, topology, node, is_done, timeout):
        """
        Re-check one device until a condition holds or the time runs out.

        Args:
            topology (topology.Topology): the intended mesh.
            node (topology.Node): the device to watch.
            is_done (callable): Step -> bool, the condition to wait for.
            timeout (float): seconds to keep checking.

        Returns:
            Step: the last verdict, whether or not the condition was met.

        Raises:
            OSError: ssh can't be executed.
        """

        deadline = self.clock() + timeout
        step     = decide(topology, node, self.observe())

        while not is_done(step) and self.clock() < deadline:
            self.sleep(self.poll)
            step = decide(topology, node, self.observe())
        return step


def print_plan(steps):
    """
    Show the verdict for every device.

    Args:
        steps (list[Step]): the plan.

    Returns:
        None

    Raises:
        None
    """

    print("[*] plan (parents first):")
    for step in steps:
        print(f"      {step.line}")


def stop(step, what):
    """
    Explain why the run stopped at this device and what to do next.

    Args:
        step (Step): the device's last verdict.
        what (str): what was being waited for.

    Returns:
        client.ExitStatus: NEEDS_ACTION.

    Raises:
        None
    """

    print(f"[-] stopped at {step.node.name}: {what} -- {step.reason}", file=sys.stderr)
    if step.action is Action.BLOCKED:
        print("[*] if it was re-provisioned it may be on stock 5 GHz and out of range: see the README's Recovery",
              file=sys.stderr)
    print("[*] nothing below it was touched; fix it, then run --apply again", file=sys.stderr)
    return client.ExitStatus.NEEDS_ACTION


def settle(runner, topology, node, step):
    """
    Bring one device in line: wait out an unreachable spell, act if needed, then wait for it to match.

    Args:
        runner (Runner): how to observe, act and wait.
        topology (topology.Topology): the intended mesh.
        node (topology.Node): the device.
        step (Step): its verdict before acting.

    Returns:
        client.ExitStatus | None: None when the device now matches mesh.ini, otherwise the status to exit with.

    Raises:
        OSError: ssh can't be executed.
    """

    if step.action is Action.BLOCKED:
        print(f"[*] {node.name}: unreachable, waiting up to {UNREACHABLE_GRACE}s for it to reconnect ...")
        step = runner.wait_for(topology, node, lambda s: s.action is not Action.BLOCKED, UNREACHABLE_GRACE)
        if step.action is Action.BLOCKED:
            return stop(step, "still unreachable")

    if step.action is Action.NONE:
        print(f"[+] {step.line}")
        return None

    print(f"[*] {step.line}")
    failure = runner.perform(step)
    if failure:
        return stop(step, f"couldn't start {step.action.value}: {failure}")

    timeout = RECONNECT_SETTLE if step.action is Action.RECONNECT else APPLY_SETTLE
    print(f"[*] {node.name}: waiting up to {timeout}s for it to settle ...")
    step = runner.wait_for(topology, node, lambda s: s.action is Action.NONE, timeout)
    if step.action is not Action.NONE:
        return stop(step, f"didn't settle within {timeout}s")

    print(f"[+] {step.line}")
    return None


def run(topology, runner, is_confirmed=False):
    """
    Show the plan, confirm it, then fix each device in order, stopping at the first that won't settle.

    Args:
        topology (topology.Topology): the intended mesh.
        runner (Runner): how to observe, act, wait and ask.
        is_confirmed (bool): skip the confirmation prompt (--yes).

    Returns:
        client.ExitStatus: ALL_LIVE when the mesh matches mesh.ini, NEEDS_ACTION when the run stopped,
        NOT_RUN when the plan was declined.

    Raises:
        OSError: ssh can't be executed.
    """

    steps = plan(topology, runner.observe())
    print_plan(steps)

    if all(step.action is Action.NONE for step in steps):
        print("[+] the mesh already matches mesh.ini")
        return client.ExitStatus.ALL_LIVE
    if not is_confirmed and not runner.confirm("Make these changes?"):
        print("[*] nothing changed")
        return client.ExitStatus.NOT_RUN

    for node in topology.ordered():
        # Re-decided now rather than taken from the plan: fixing a parent changes what its children need
        step   = decide(topology, node, runner.observe())
        status = settle(runner, topology, node, step)
        if status is not None:
            return status

    print("[+] the mesh matches mesh.ini")
    return client.ExitStatus.ALL_LIVE


def ask(question):
    """
    Ask a yes/no question on the terminal.

    Args:
        question (str): the question, without the [y/N] suffix.

    Returns:
        bool: True only for an explicit yes.

    Raises:
        None
    """

    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ["y", "yes"]


def default_runner(topology, script):
    """
    A Runner that probes and acts over ssh and asks on the terminal.

    Args:
        topology (topology.Topology): the intended mesh.
        script (bytes): the contents of unifi-overhaul.sh.

    Returns:
        Runner: the runner.

    Raises:
        None
    """

    return Runner(observe=lambda: observe(topology, script),
                  perform=lambda step: perform(step, topology, script),
                  confirm=ask)
