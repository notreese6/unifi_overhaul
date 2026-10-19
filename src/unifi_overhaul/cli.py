"""
unifi_overhaul.cli: the command line — parse and validate arguments, pick the devices, and dispatch to the mesh
map, --diagnostics, or --apply.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import sys
import time

from unifi_overhaul import apply as mesh_apply
from unifi_overhaul import client
from unifi_overhaul.topology import Topology, TopologyError, default_mesh_path


def uses_mesh_file(args):
    """
    Whether the devices come from mesh.ini: always for --apply, and for a plain run given no devices.

    Args:
        args (argparse.Namespace): parsed arguments.

    Returns:
        bool: True when mesh.ini is the source of devices.

    Raises:
        None
    """

    return args.apply or (not args.targets and not args.discover)


def read_script(path):
    """
    Read the on-device script that gets piped to each device.

    Args:
        path (str): local path of unifi-overhaul.sh.

    Returns:
        bytes: its contents.

    Raises:
        OSError: the file can't be read.
    """

    with open(path, "rb") as handle:
        return handle.read()


def discovered_target(args, found):
    """
    A target for a discovered device, reached as the gateway user or the AP user.

    Args:
        args (argparse.Namespace): parsed arguments (users).
        found (client.DiscoveredDevice): the device.

    Returns:
        client.Target: the target.

    Raises:
        None
    """

    user = args.gateway_user if found.is_gateway else args.ap_user
    return client.Target(ssh_target=f"{user}@{found.ip}", ip=found.ip, found=found)


def controller_devices(gateway, script):
    """
    Ask a gateway's UniFi Network app for every device it has adopted, since APs don't always answer the broadcast.

    Args:
        gateway (client.Target): the gateway found by the broadcast.
        script (bytes): the contents of unifi-overhaul.sh.

    Returns:
        list[client.DiscoveredDevice]: the adopted devices; [] when the gateway can't be asked.

    Raises:
        OSError: ssh can't be executed.
    """

    print(f"[*] asking the controller on {gateway.ssh_target} for its adopted devices ...", file=sys.stderr)
    run = client.run_remote(gateway, script, "__devices", timeout=client.SSH_CONNECT_TIMEOUT * 5)
    if run.error:
        print(f"[-] couldn't ask {gateway.ssh_target} ({run.error}), so only devices that answered the broadcast "
              f"are listed", file=sys.stderr)
        return []
    return client.parse_device_list(run.stdout)


def discovered_targets(args, explicit_ips):
    """
    Find devices by broadcast, then add every device the gateway's controller has adopted.

    Args:
        args (argparse.Namespace): parsed arguments (broadcast, timeout, users, script).
        explicit_ips (set[str]): IPs already given on the command line; those are not overridden.

    Returns:
        list[client.Target]: one target per newly found device, broadcast replies first.

    Raises:
        OSError: the discovery broadcast can't be sent, unifi-overhaul.sh can't be read, or ssh can't be executed.
    """

    print(f"[*] discovering UniFi devices (UDP {client.DISCOVERY_PORT} broadcast to {args.broadcast}, "
          f"{args.timeout:g}s) ...", file=sys.stderr)
    known   = set(explicit_ips)
    targets = []

    for found in client.discover(args.broadcast, args.timeout):
        if found.ip not in known:
            known.add(found.ip)
            targets.append(discovered_target(args, found))

    gateways = [target for target in targets if target.found.is_gateway]
    script   = read_script(args.script) if gateways else b""
    for gateway in gateways:
        for found in controller_devices(gateway, script):
            if found.ip not in known:
                known.add(found.ip)
                targets.append(discovered_target(args, found))

    print(f"[*] discovery found {len(targets)} device(s) not already listed", file=sys.stderr)
    return targets


def collect_targets(args, topology):
    """
    Every device to probe: mesh.ini's devices, or the ones named on the command line plus any discovered.

    Args:
        args (argparse.Namespace): parsed arguments.
        topology (Topology | None): the loaded mesh.ini, when it's the source of devices.

    Returns:
        list[client.Target]: the targets, in probe order.

    Raises:
        OSError: the discovery broadcast can't be sent, unifi-overhaul.sh can't be read, or ssh can't be executed.
    """

    if topology is not None:
        return [mesh_apply.target_for(node) for node in topology.ordered()]

    targets = [client.Target.from_arg(text) for text in args.targets]
    if args.discover:
        targets += discovered_targets(args, {target.ip for target in targets})
    return targets


def diagnostics_problems(args):
    """
    Check the --diagnostics / --log inputs: the log must be new, and its directory must exist and be writable.

    Args:
        args (argparse.Namespace): parsed arguments, with args.log already defaulted when --diagnostics is set.

    Returns:
        list[str]: one sentence per problem; empty when the diagnostics inputs are fine (or unused).

    Raises:
        None
    """

    if not args.diagnostics:
        return ["--log only applies with --diagnostics"] if args.log else []
    if args.list:
        return ["--list connects to nothing, so it can't be combined with --diagnostics"]

    folder = os.path.dirname(os.path.abspath(args.log))
    if os.path.exists(args.log):
        return [f"log {args.log!r} already exists -- pick another --log (an existing log is never overwritten)"]
    if not os.path.isdir(folder):
        return [f"log directory {folder!r} does not exist"]
    if not os.access(folder, os.W_OK):
        return [f"log directory {folder!r} is not writable"]
    return []


def apply_problems(args):
    """
    Check the --apply / --yes inputs: --apply works from mesh.ini alone, and needs a way to confirm.

    Args:
        args (argparse.Namespace): parsed arguments.

    Returns:
        list[str]: one sentence per problem; empty when the apply inputs are fine (or unused).

    Raises:
        None
    """

    if not args.apply:
        return ["--yes only applies with --apply"] if args.yes else []

    problems = []
    if args.targets or args.discover:
        problems.append("--apply takes its devices from mesh.ini; drop the device list and --discover")
    if args.list or args.diagnostics:
        problems.append("--apply can't be combined with --list or --diagnostics")
    if not args.yes and not sys.stdin.isatty():
        problems.append("--apply asks before changing anything; with no terminal to ask on, pass --yes")
    return problems


def mesh_file_problems(args):
    """
    Check mesh.ini when it's the source of devices.

    Args:
        args (argparse.Namespace): parsed arguments.

    Returns:
        list[str]: every problem with the file; empty when it's valid or not needed.

    Raises:
        None
    """

    if not uses_mesh_file(args):
        return []
    if not os.path.isfile(args.mesh) and not args.apply:
        return [f"nothing to probe -- give [user@]ip targets, --discover, or describe your mesh in {args.mesh} "
                f"(copy mesh.example.ini)"]
    try:
        Topology.load(args.mesh)
    except TopologyError as error:
        return [f"{error.path}: {problem}" for problem in error.problems]
    return []


def input_problems(args):
    """
    Check every input this run depends on before doing any work.

    Args:
        args (argparse.Namespace): parsed arguments.

    Returns:
        list[str]: one sentence per problem; empty when everything needed is present.

    Raises:
        None
    """

    problems = []

    if not os.path.isfile(args.script):
        problems.append(f"unifi-overhaul.sh not found at {args.script!r} -- it is piped to each device to probe it "
                        f"(pass --script PATH)")
    if not shutil.which("ssh"):
        problems.append("no 'ssh' on PATH -- every device is probed over ssh")
    if args.discover and not args.ap_user:
        problems.append("--discover needs the APs' ssh user (the UniFi Device-SSH user): "
                        "pass --ap-user, or set UNIFI_OVERHAUL_AP_USER")
    try:
        ipaddress.ip_address(args.broadcast)
    except ValueError:
        problems.append(f"--broadcast {args.broadcast!r} is not an IPv4 address")

    problems += mesh_file_problems(args)
    problems += diagnostics_problems(args)
    problems += apply_problems(args)
    return problems


def report_problems(problems):
    """
    Print every input problem found and refuse to run.

    Args:
        problems (list[str]): the problems.

    Returns:
        client.ExitStatus: NOT_RUN.

    Raises:
        None
    """

    for problem in problems:
        print(f"[-] {problem}", file=sys.stderr)
    print("[-] nothing was probed", file=sys.stderr)
    return client.ExitStatus.NOT_RUN


def report_no_targets():
    """
    Explain that discovery found nothing and nothing else was given.

    Args:
        None

    Returns:
        client.ExitStatus: NOT_RUN.

    Raises:
        None
    """

    print("[-] discovery got no replies, so there is nothing to probe -- is this machine on the same LAN as "
          "the UniFi devices? UDP 10001 must not be firewalled; try --broadcast <subnet>.255", file=sys.stderr)
    return client.ExitStatus.NOT_RUN


def build_parser():
    """
    Build the command-line parser.

    Args:
        None

    Returns:
        argparse.ArgumentParser: the parser.

    Raises:
        None
    """

    parser = argparse.ArgumentParser(
        prog="unifi-overhaul-client",
        description="Probe the UniFi mesh over ssh and show, per device, whether the unifi-overhaul 2.4 GHz "
                    "backhaul change is LIVE or REVERTED, plus the ordered re-apply commands. With no devices "
                    "given, the devices come from mesh.ini. --apply brings the mesh in line with mesh.ini; "
                    "--diagnostics collects every read-only diagnostic into one log. "
                    "Exit 0 = all live, 1 = action needed, 3 = nothing probed.")
    parser.add_argument("targets", nargs="*", metavar="[user@]ip",
                        help="devices to probe; a bare ip means root@ip")
    parser.add_argument("--mesh", default=default_mesh_path(), metavar="PATH",
                        help="mesh.ini describing your devices and their intended parents "
                             "(default: %(default)s)")
    parser.add_argument("--apply", action="store_true",
                        help="fix every device that doesn't match mesh.ini, parents first, checking each one")
    parser.add_argument("--yes", action="store_true",
                        help="with --apply, don't ask before making changes")
    parser.add_argument("--discover", action="store_true",
                        help="also find devices with the Ubiquiti discovery broadcast (UDP 10001)")
    parser.add_argument("--list", action="store_true",
                        help="only list what would be probed; connect to nothing")
    parser.add_argument("--ap-user", default=os.environ.get("UNIFI_OVERHAUL_AP_USER", ""),
                        help="ssh user for discovered APs (UniFi Device-SSH user; default $UNIFI_OVERHAUL_AP_USER)")
    parser.add_argument("--gateway-user", default="root",
                        help="ssh user for a discovered gateway/console (default: root)")
    parser.add_argument("--broadcast", default="255.255.255.255",
                        help="discovery broadcast address (default: 255.255.255.255)")
    parser.add_argument("--timeout", type=float, default=2.0,
                        help="seconds to wait for discovery replies (default: 2)")
    parser.add_argument("--script", default=client.DEFAULT_SCRIPT,
                        help="path to unifi-overhaul.sh (default: scripts/unifi-overhaul.sh in this repo)")
    parser.add_argument("--diagnostics", action="store_true",
                        help="also run every read-only diagnostic on each device and on this machine, into --log "
                             "(secrets redacted; takes a minute or two per device)")
    parser.add_argument("--log", metavar="PATH",
                        help=f"diagnostics log to create (default: logs/"
                             f"{client.DEFAULT_LOG_NAME.replace('%', '%%')} in this repo); never overwrites a file")
    return parser


def parse_arguments():
    """
    Parse the command line and fill in the default diagnostics log name, which carries the current time.

    Args:
        None

    Returns:
        argparse.Namespace: the parsed arguments.

    Raises:
        SystemExit: argparse rejected the command line (status 2) or printed --help (status 0).
    """

    args = build_parser().parse_args()
    if args.diagnostics and not args.log:
        args.log = os.path.join(client.LOG_DIR, time.strftime(client.DEFAULT_LOG_NAME))
    return args


def run_apply(args, topology):
    """
    Bring the mesh in line with mesh.ini.

    Args:
        args (argparse.Namespace): parsed arguments (script path, --yes).
        topology (Topology): the loaded mesh.ini.

    Returns:
        client.ExitStatus: the apply's outcome.

    Raises:
        OSError: unifi-overhaul.sh can't be read, or ssh can't be executed.
    """

    runner = mesh_apply.default_runner(topology, read_script(args.script))
    return mesh_apply.run(topology, runner, is_confirmed=args.yes)


def run_map(args, targets, topology):
    """
    Probe the devices, print the mesh map, and flag anything that differs from mesh.ini.

    Args:
        args (argparse.Namespace): parsed arguments (script path).
        targets (list[client.Target]): the devices to probe.
        topology (Topology | None): the loaded mesh.ini, when it's the source of devices.

    Returns:
        client.ExitStatus: ALL_LIVE or NEEDS_ACTION.

    Raises:
        OSError: unifi-overhaul.sh can't be read, or ssh can't be executed.
    """

    if topology is None:
        return client.print_report(client.Mesh(client.probe_all(targets, args.script)), args.script)

    observation = mesh_apply.observe(topology, read_script(args.script), is_quiet=False)
    status      = client.print_report(observation.mesh, args.script)
    flags       = mesh_apply.warnings(topology, observation)
    for line in flags:
        print(line)
    return client.ExitStatus.NEEDS_ACTION if flags else status


def main():
    """
    Validate inputs, gather devices, then show the map, collect diagnostics, or apply.

    Args:
        None

    Returns:
        int: an ExitStatus value.

    Raises:
        OSError: discovery, reading unifi-overhaul.sh, writing the log, or running ssh failed.
    """

    args     = parse_arguments()
    problems = input_problems(args)
    if problems:
        return report_problems(problems)

    topology = Topology.load(args.mesh) if uses_mesh_file(args) else None
    if args.apply:
        return run_apply(args, topology)

    targets = collect_targets(args, topology)
    if not targets:
        return report_no_targets()
    if args.list:
        return client.print_targets(targets)
    if args.diagnostics:
        return client.run_diagnostics(args, targets)

    return run_map(args, targets, topology)
