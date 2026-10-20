"""
unifi_overhaul.selfheal: install or remove the gateway self-heal timer, which re-applies the root's 2.4 GHz backhaul
after every reboot and controller provision. The work on the gateway is unifi-overhaul.sh's __install / __remove.
"""

from __future__ import annotations

import sys

from unifi_overhaul import apply as mesh_apply
from unifi_overhaul import client

SSH_TIMEOUT = 60


def gateway(topology):
    """
    The mesh.ini device the self-heal goes on: the root.

    Args:
        topology (topology.Topology): the intended mesh.

    Returns:
        topology.Node: the root device.

    Raises:
        None
    """

    return topology.ordered()[0]


def report(run, what):
    """
    Show what the gateway printed and turn the ssh result into an exit status.

    Args:
        run (client.RemoteRun): the ssh run.
        what (str): what was being done, for the failure message.

    Returns:
        client.ExitStatus: ALL_LIVE when the gateway reported success, otherwise NEEDS_ACTION.

    Raises:
        None
    """

    if run.stdout.strip():
        print(run.stdout.rstrip())
    if run.returncode == 0:
        return client.ExitStatus.ALL_LIVE

    reason = run.stderr.strip().splitlines()
    print(f"[-] couldn't {what}: {reason[-1] if reason else f'ssh exited {run.returncode}'}", file=sys.stderr)
    return client.ExitStatus.NEEDS_ACTION


def install(topology, script, confirm, is_confirmed=False):
    """
    Copy unifi-overhaul.sh to the gateway's persistent storage and start the self-heal timer there.

    Args:
        topology (topology.Topology): the intended mesh (the root and the channel).
        script (bytes): the contents of unifi-overhaul.sh.
        confirm (callable): str -> bool, asks before changing the gateway.
        is_confirmed (bool): skip the question (--yes).

    Returns:
        client.ExitStatus: ALL_LIVE when installed, NEEDS_ACTION when it failed, NOT_RUN when declined.

    Raises:
        OSError: ssh can't be executed.
    """

    root = gateway(topology)
    print(f"[*] self-heal on {root.name}: re-applies 2.4 GHz (channel {topology.channel}) 90s after every boot and "
          f"every 5 minutes, and restores root's ssh keys. Files: {client.REMOTE_HEAL_DIR}/ and two systemd units.")
    if not is_confirmed and not confirm(f"Install it on {root.name}?"):
        print("[*] nothing changed")
        return client.ExitStatus.NOT_RUN

    script_path = f"{client.REMOTE_HEAL_DIR}/unifi-overhaul.sh"
    remote      = (f"mkdir -p {client.REMOTE_HEAL_DIR} && cat > {script_path} && "
                   f"sh {script_path} __install --channel {topology.channel}")
    run         = client.run_ssh(mesh_apply.target_for(root), remote, script, timeout=SSH_TIMEOUT)
    return report(run, f"install self-heal on {root.name}")


def remove(topology, script, confirm, is_confirmed=False):
    """
    Stop the gateway's self-heal timer and delete exactly the files the install created.

    Args:
        topology (topology.Topology): the intended mesh (the root).
        script (bytes): the contents of unifi-overhaul.sh, piped so removal works even if the gateway's copy is gone.
        confirm (callable): str -> bool, asks before changing the gateway.
        is_confirmed (bool): skip the question (--yes).

    Returns:
        client.ExitStatus: ALL_LIVE when removed, NEEDS_ACTION when it failed, NOT_RUN when declined.

    Raises:
        OSError: ssh can't be executed.
    """

    root = gateway(topology)
    if not is_confirmed and not confirm(f"Remove self-heal from {root.name}?"):
        print("[*] nothing changed")
        return client.ExitStatus.NOT_RUN

    run = client.run_remote(mesh_apply.target_for(root), script, "__remove", timeout=SSH_TIMEOUT)
    return report(run, f"remove self-heal from {root.name}")
