#!/usr/bin/env python3
"""
unifi-overhaul-map.py: find the UniFi gateway and APs, probe each one over ssh with unifi-overhaul.sh's __probe,
and print the mesh tree with whether the 2.4 GHz backhaul change is live on each device.

Runs on your WORKSTATION only. The devices run unifi-overhaul.sh; this tool is never copied to them.
"""

from __future__ import annotations

import argparse
import ctypes
import enum
import ipaddress
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field


class ExitStatus(enum.IntEnum):
    """
    Process exit statuses, so a caller (cron, a watchdog) can tell the outcomes apart.
    """

    ALL_LIVE     = 0
    NEEDS_ACTION = 1    # something reverted, partial, or unreachable
    USAGE        = 2    # argparse's own convention for a bad command line
    NOT_RUN      = 3    # a required input was missing, so nothing was probed


class Tlv(enum.IntEnum):
    """
    Field type codes in a Ubiquiti discovery (v1) reply. Codes not listed here are skipped.
    """

    HWADDR   = 0x01
    IPINFO   = 0x02
    FIRMWARE = 0x03
    HOSTNAME = 0x0B
    PLATFORM = 0x0C
    MODEL    = 0x14


class PacketHeader(ctypes.BigEndianStructure):
    """
    Header at the start of every discovery packet; the probe we send is this header alone.
    """

    _pack_   = 1
    _fields_ = [
        ("version", ctypes.c_uint8),
        ("command", ctypes.c_uint8),
        ("length",  ctypes.c_uint16),    # bytes of fields that follow this header
    ]


class FieldHeader(ctypes.BigEndianStructure):
    """
    Header in front of each field of a discovery reply; `length` bytes of value follow it.
    """

    _pack_   = 1
    _fields_ = [
        ("kind",   ctypes.c_uint8),
        ("length", ctypes.c_uint16),
    ]


MacAddress = ctypes.c_uint8 * 6


class IpInfo(ctypes.Structure):
    """
    Value of an IPINFO field: one interface's MAC followed by its IPv4 address.
    """

    _pack_   = 1
    _fields_ = [
        ("mac", MacAddress),
        ("ip",  ctypes.c_uint8 * 4),
    ]


class State(enum.Enum):
    """
    Whether the 2.4 GHz change is live on one device, judged from what it has up right now.
    """

    LIVE        = "LIVE"
    REVERTED    = "REVERTED"
    PARTIAL     = "PARTIAL"
    UNREACHABLE = "UNREACHABLE"
    NO_WIFI     = "NO_WIFI"


DISCOVERY_PORT      = 10001
DISCOVERY_VERSION   = 1
DISCOVERY_COMMAND   = 0     # "send me your info"; replies come back with the same command
DISCOVERY_PROBE     = bytes(PacketHeader(version=DISCOVERY_VERSION, command=DISCOVERY_COMMAND, length=0))
DISCOVERY_SENDS     = 2
SSH_CONNECT_TIMEOUT = 6
BAND_24             = "2.4GHz"

# Model-code prefixes of UniFi gateways / consoles; every other discovered device is treated as an AP
GATEWAY_MODEL_PREFIXES = ["UX", "UDM", "UDR", "UCG", "UXG", "USG", "UDW"]


@dataclass
class DiscoveredDevice:
    """
    One UniFi device that answered the discovery broadcast.
    """

    ip:       str
    mac:      str = ""
    hostname: str = ""
    platform: str = ""
    model:    str = ""
    firmware: str = ""

    @property
    def model_code(self):
        """
        The short model code (e.g. "UX7", "U7PRO"), falling back to the long model name.

        Args:
            None

        Returns:
            str: the model code, or "" when the device sent neither field.

        Raises:
            None
        """

        return self.platform or self.model

    @property
    def is_gateway(self):
        """
        Whether this device is a UniFi gateway / console rather than an AP.

        Args:
            None

        Returns:
            bool: True when the model code starts with a known gateway prefix.

        Raises:
            None
        """

        code = self.model_code.upper()
        return any(code.startswith(prefix) for prefix in GATEWAY_MODEL_PREFIXES)

    @classmethod
    def from_reply(cls, data, source_ip):
        """
        Parse one discovery reply packet.

        Args:
            data (bytes): the UDP payload received.
            source_ip (str): the address the reply came from; used as the device's IP.

        Returns:
            DiscoveredDevice | None: the parsed device, or None if this isn't a v1 reply
            (including our own probe, echoed back by the broadcast).

        Raises:
            None
        """

        # Our own probe echoes back with no fields, so anything not longer than a header is skipped
        if len(data) <= ctypes.sizeof(PacketHeader):
            return None
        header = PacketHeader.from_buffer_copy(data)
        if header.version != DISCOVERY_VERSION:
            return None

        device = cls(ip=source_ip)
        pos    = ctypes.sizeof(PacketHeader)
        end    = min(len(data), pos + header.length)

        # Only read a field while a whole field header still fits, so a truncated packet can't overrun
        while pos + ctypes.sizeof(FieldHeader) <= end:
            field_header = FieldHeader.from_buffer_copy(data, pos)
            start        = pos + ctypes.sizeof(FieldHeader)
            pos          = start + field_header.length

            # A value cut off by the end of the packet is dropped, not recorded half-read
            if pos > end:
                break
            device.absorb(field_header.kind, data[start:pos])

        return device

    def absorb(self, kind, value):
        """
        Record one field of a discovery reply on this device.

        Args:
            kind (int): the field type code.
            value (bytes): the raw field value.

        Returns:
            None

        Raises:
            None
        """

        text = value.decode("utf-8", "replace").strip("\x00")

        if kind == Tlv.HWADDR and len(value) == ctypes.sizeof(MacAddress):
            self.mac = mac_text(value)
        elif kind == Tlv.IPINFO and len(value) == ctypes.sizeof(IpInfo) and not self.mac:
            self.mac = mac_text(bytes(IpInfo.from_buffer_copy(value).mac))
        elif kind == Tlv.FIRMWARE:
            self.firmware = text
        elif kind == Tlv.HOSTNAME:
            self.hostname = text
        elif kind == Tlv.PLATFORM:
            self.platform = text
        elif kind == Tlv.MODEL:
            self.model = text


@dataclass
class Target:
    """
    One device to probe: where to ssh, plus what discovery said about it (if anything).
    """

    ssh_target: str
    ip:         str
    found:      DiscoveredDevice | None = None

    @classmethod
    def from_arg(cls, text):
        """
        Build a target from a command-line "[user@]host" argument.

        Args:
            text (str): "user@host" or a bare host (which means root@host).

        Returns:
            Target: the target.

        Raises:
            None
        """

        if "@" in text:
            return cls(ssh_target=text, ip=text.split("@", 1)[1])
        return cls(ssh_target=f"root@{text}", ip=text)


@dataclass
class Device:
    """
    What one __probe reported about a device (or why it couldn't be probed).
    """

    target:        Target
    host:          str = ""
    uplink:        str = ""         # uplink BSSID, "WIRED", or "NONE"
    uplink_band:   str = ""
    uplink_signal: str = ""
    aps:           list = field(default_factory=list)  # (mac, band) of each vwire AP it offers
    wifi_radios:   int = -1         # -1 = the probe didn't say (an older unifi-overhaul.sh)
    error:         str = ""

    @property
    def is_reachable(self):
        """
        Whether the probe ran and produced output.

        Args:
            None

        Returns:
            bool: True when the device answered.

        Raises:
            None
        """

        return not self.error

    @property
    def is_root(self):
        """
        Whether this device is the wired mesh root.

        Args:
            None

        Returns:
            bool: True when its uplink is wired.

        Raises:
            None
        """

        return self.uplink == "WIRED"

    @property
    def has_wifi(self):
        """
        Whether the device has any wifi radio (a switch has none).

        Args:
            None

        Returns:
            bool: True unless the probe reported zero radios.

        Raises:
            None
        """

        return self.wifi_radios != 0

    @property
    def has_downlink_24(self):
        """
        Whether the device offers a vwire backhaul AP on 2.4 GHz.

        Args:
            None

        Returns:
            bool: True when any of its vwire APs is on 2.4 GHz.

        Raises:
            None
        """

        return any(band == BAND_24 for _mac, band in self.aps)

    @property
    def label(self):
        """
        How the device is named in the output: hostname plus IP.

        Args:
            None

        Returns:
            str: e.g. "ap-hall (192.168.1.20)".

        Raises:
            None
        """

        name = self.host or (self.target.found.hostname if self.target.found else "") or self.target.ip
        return f"{name} ({self.target.ip})"


class Mesh:
    """
    The probed devices joined into a tree by matching each uplink BSSID to the AP that owns it.
    """

    def __init__(self, devices):
        """
        Join the devices into a tree.

        Args:
            devices (list[Device]): every probed device, in the order they were given.

        Returns:
            None

        Raises:
            None
        """

        self.devices  = devices
        self.owner    = {mac: dev for dev in devices for mac, _band in dev.aps}
        self.children = {id(dev): [] for dev in devices}

        for dev in devices:
            parent = self.parent(dev)
            if parent is not None:
                self.children[id(parent)].append(dev)

    def parent(self, dev):
        """
        The device this one uplinks to.

        Args:
            dev (Device): the device.

        Returns:
            Device | None: its parent, or None for the root, an orphan, or an unreachable device.

        Raises:
            None
        """

        return self.owner.get(dev.uplink)

    def is_orphan(self, dev):
        """
        Whether the device is reachable but uplinks to a device that wasn't probed.

        Args:
            dev (Device): the device.

        Returns:
            bool: True for a wireless device whose parent isn't in the mesh.

        Raises:
            None
        """

        return dev.is_reachable and dev.has_wifi and not dev.is_root and self.parent(dev) is None

    def is_relay(self, dev):
        """
        Whether other probed devices uplink through this one.

        Args:
            dev (Device): the device.

        Returns:
            bool: True when it has at least one child.

        Raises:
            None
        """

        return bool(self.children[id(dev)])

    def state(self, dev):
        """
        Whether the 2.4 GHz change is live on a device: root needs a 2.4 downlink, a leaf needs its
        uplink on 2.4, and a relay needs both.

        Args:
            dev (Device): the device.

        Returns:
            State: the device's state.

        Raises:
            None
        """

        if not dev.is_reachable:
            return State.UNREACHABLE
        if not dev.has_wifi:
            return State.NO_WIFI
        if dev.is_root:
            return State.LIVE if dev.has_downlink_24 else State.REVERTED

        is_uplink_24 = dev.uplink_band == BAND_24
        if not self.is_relay(dev):
            return State.LIVE if is_uplink_24 else State.REVERTED
        if is_uplink_24 and dev.has_downlink_24:
            return State.LIVE
        if is_uplink_24 or dev.has_downlink_24:
            return State.PARTIAL
        return State.REVERTED

    def tree_order(self):
        """
        Every device, parents before children: the root -> relays -> leaves apply order.

        Args:
            None

        Returns:
            list[tuple[Device, int]]: each device with its depth in the tree.

        Raises:
            None
        """

        ordered = []

        def walk(dev, depth):
            ordered.append((dev, depth))
            for child in self.children[id(dev)]:
                walk(child, depth + 1)

        for dev in self.devices:
            if dev.is_reachable and dev.is_root:
                walk(dev, 0)

        # Anything not reached from a root still gets reported, at the top level
        seen = {id(dev) for dev, _depth in ordered}
        ordered.extend((dev, 0) for dev in self.devices if id(dev) not in seen)
        return ordered


def mac_text(raw):
    """
    Format 6 raw bytes as a lowercase colon-separated MAC address.

    Args:
        raw (bytes): the 6 address bytes.

    Returns:
        str: e.g. "aa:bb:cc:dd:ee:ff".

    Raises:
        None
    """

    return ":".join(f"{b:02x}" for b in raw)


def discover(broadcast, timeout):
    """
    Broadcast the Ubiquiti discovery probe and collect every reply until the timeout.

    Args:
        broadcast (str): address to send to (255.255.255.255, or a subnet broadcast).
        timeout (float): seconds to listen for replies.

    Returns:
        list[DiscoveredDevice]: one entry per answering IP, sorted by IP.

    Raises:
        OSError: the socket can't be opened or the broadcast can't be sent.
    """

    found = {}

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))

        # Sent more than once because a single UDP broadcast can be dropped
        for _ in range(0, DISCOVERY_SENDS):
            sock.sendto(DISCOVERY_PROBE, (broadcast, DISCOVERY_PORT))

        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, (source_ip, _port) = sock.recvfrom(4096)
            except socket.timeout:
                break
            device = DiscoveredDevice.from_reply(data, source_ip)
            if device is not None:
                found[source_ip] = device

    return sorted(found.values(), key=lambda dev: ipaddress.ip_address(dev.ip))


def parse_probe(target, output):
    """
    Turn __probe's line-oriented output into a Device.

    Args:
        target (Target): the device that was probed.
        output (str): the probe's stdout (H / U / A / W lines).

    Returns:
        Device: the parsed device.

    Raises:
        None
    """

    dev = Device(target=target)

    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        kind = fields[0]
        if kind == "H" and len(fields) >= 2:
            dev.host = fields[1]
        elif kind == "U" and len(fields) >= 4:
            dev.uplink, dev.uplink_band, dev.uplink_signal = fields[1], fields[2], fields[3]
        elif kind == "A" and len(fields) >= 3:
            dev.aps.append((fields[1].lower(), fields[2]))
        elif kind == "W" and len(fields) >= 2 and fields[1].isdigit():
            dev.wifi_radios = int(fields[1])

    # The uplink BSSID has to match the AP MACs case-for-case to join the tree
    dev.uplink = dev.uplink.lower() if dev.uplink not in ("WIRED", "NONE") else dev.uplink
    return dev


def probe(target, script):
    """
    ssh to one device and run unifi-overhaul.sh's __probe there by piping the script over stdin.

    Args:
        target (Target): the device to probe.
        script (bytes): the contents of unifi-overhaul.sh.

    Returns:
        Device: the probed device; Device.error is set if ssh failed.

    Raises:
        OSError: the ssh binary can't be executed.
    """

    print(f"[*] probing {target.ssh_target} ...", file=sys.stderr)
    command = ["ssh", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}", "-o", "StrictHostKeyChecking=accept-new",
               target.ssh_target, "sh -s __probe"]
    result  = subprocess.run(command, input=script, capture_output=True)

    if result.returncode != 0 or not result.stdout.strip():
        reason = result.stderr.decode("utf-8", "replace").strip().splitlines()
        return Device(target=target, error=reason[-1] if reason else f"ssh exited {result.returncode}")
    return parse_probe(target, result.stdout.decode("utf-8", "replace"))


def reapply_command(dev, mesh, script_path):
    """
    The command that copies unifi-overhaul.sh to a device and runs apply there, detached.

    Args:
        dev (Device): the device to re-apply.
        mesh (Mesh): the mesh it belongs to (a relay needs --with-ap).
        script_path (str): local path of unifi-overhaul.sh.

    Returns:
        str: a shell command to paste on the workstation.

    Raises:
        None
    """

    # A relay whose children already dropped would autodetect as a leaf, so force its 2.4 downlink
    with_ap = " --with-ap" if mesh.is_relay(dev) and not dev.is_root else ""
    # </dev/null so the backgrounded apply lets go of the ssh session instead of holding it open
    remote  = (f"cat > /tmp/unifi-overhaul.sh && chmod +x /tmp/unifi-overhaul.sh && "
               f"nohup /tmp/unifi-overhaul.sh apply{with_ap} < /dev/null > /tmp/vw.log 2>&1 &")
    return f"ssh {dev.target.ssh_target} {shlex.quote(remote)} < {shlex.quote(script_path)}"


def tree_line(dev, depth, mesh):
    """
    One line of the mesh tree for a device.

    Args:
        dev (Device): the device.
        depth (int): its depth in the tree, for indentation.
        mesh (Mesh): the mesh it belongs to.

    Returns:
        str: the formatted line.

    Raises:
        None
    """

    indent = "    " * depth
    state  = mesh.state(dev)

    if state is State.UNREACHABLE:
        return f"  (x) {dev.label} -> could not ssh in: {dev.error}   [2.4: {state.value}]"
    if state is State.NO_WIFI:
        return f"  (-) {dev.label} -> no wifi radios (a switch?), not part of the mesh"
    if mesh.is_orphan(dev):
        return f"  (?) {dev.label} -> uplink {dev.uplink} is on a device not probed   [2.4: {state.value}]"
    if dev.is_root:
        return f"{indent}{dev.label}   (root, wired)   [2.4: {state.value}]"

    signal = f" {dev.uplink_signal}dBm" if dev.uplink_signal not in ("", "-") else ""
    return f"{indent}{dev.label}   <--[uplink {dev.uplink_band}{signal}]   [2.4: {state.value}]"


def print_report(mesh, script_path):
    """
    Print the tree, the live summary, and the ordered re-apply list.

    Args:
        mesh (Mesh): the probed mesh.
        script_path (str): local path of unifi-overhaul.sh, used in the re-apply commands.

    Returns:
        ExitStatus: ALL_LIVE when every mesh device is live, else NEEDS_ACTION.

    Raises:
        None
    """

    ordered = mesh.tree_order()
    in_mesh = [dev for dev, _depth in ordered if mesh.state(dev) is not State.NO_WIFI]
    live    = [dev for dev in in_mesh if mesh.state(dev) is State.LIVE]
    dark    = [dev for dev in in_mesh if mesh.state(dev) is State.UNREACHABLE]
    todo    = [dev for dev in in_mesh if mesh.state(dev) in (State.REVERTED, State.PARTIAL)]

    print("")
    print("[*] mesh map (indented = uplinks to the line above; [band signal] is the uplink; "
          "[2.4: ...] = is the change live here)")
    for dev, depth in ordered:
        print(tree_line(dev, depth, mesh))

    print("")
    print(f"[*] 2.4 GHz backhaul live on {len(live)} of {len(in_mesh)} mesh devices probed")
    if dark:
        print(f"[-] {len(dark)} device(s) could not be checked -- fix ssh access and re-run before trusting this")
    if not todo:
        if not dark:
            print("[+] nothing to re-apply")
        return ExitStatus.ALL_LIVE if not dark else ExitStatus.NEEDS_ACTION

    print("[-] re-apply these, in this order, waiting ~30s (and re-running this) between each:")
    for dev in todo:
        if mesh.state(dev) is State.PARTIAL:
            print(f"      {dev.label}:  PARTIAL (only one side of the relay is on 2.4) -- "
                  f"check its status, rollback or reboot it, then re-apply")
        else:
            print(f"      {dev.label}:  {reapply_command(dev, mesh, script_path)}")
    return ExitStatus.NEEDS_ACTION


def discovered_targets(args, explicit_ips):
    """
    Run discovery and turn each answering device into a target with the right ssh user.

    Args:
        args (argparse.Namespace): parsed arguments (broadcast, timeout, users).
        explicit_ips (set[str]): IPs already given on the command line; those are not overridden.

    Returns:
        list[Target]: one target per newly discovered device.

    Raises:
        OSError: the discovery broadcast can't be sent.
    """

    print(f"[*] discovering UniFi devices (UDP {DISCOVERY_PORT} broadcast to {args.broadcast}, "
          f"{args.timeout:g}s) ...", file=sys.stderr)
    targets = []

    for found in discover(args.broadcast, args.timeout):
        if found.ip in explicit_ips:
            continue
        user   = args.gateway_user if found.is_gateway else args.ap_user
        target = Target(ssh_target=f"{user}@{found.ip}", ip=found.ip, found=found)
        targets.append(target)

    print(f"[*] discovery found {len(targets)} device(s) not already listed", file=sys.stderr)
    return targets


def collect_targets(args):
    """
    Every device to probe: the ones named on the command line first, then any discovered.

    Args:
        args (argparse.Namespace): parsed arguments.

    Returns:
        list[Target]: the targets, in probe order.

    Raises:
        OSError: the discovery broadcast can't be sent.
    """

    targets = [Target.from_arg(text) for text in args.targets]
    if args.discover:
        targets += discovered_targets(args, {target.ip for target in targets})
    return targets


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
    if not args.targets and not args.discover:
        problems.append("nothing to probe -- give [user@]ip targets, or --discover")
    if args.discover and not args.ap_user:
        problems.append("--discover needs the APs' ssh user (the UniFi Device-SSH user): "
                        "pass --ap-user, or set UNIFI_OVERHAUL_AP_USER")
    try:
        ipaddress.ip_address(args.broadcast)
    except ValueError:
        problems.append(f"--broadcast {args.broadcast!r} is not an IPv4 address")

    return problems


def report_problems(problems):
    """
    Print every input problem found and refuse to run.

    Args:
        problems (list[str]): the problems.

    Returns:
        ExitStatus: NOT_RUN.

    Raises:
        None
    """

    for problem in problems:
        print(f"[-] {problem}", file=sys.stderr)
    print("[-] nothing was probed", file=sys.stderr)
    return ExitStatus.NOT_RUN


def report_no_targets():
    """
    Explain that discovery found nothing and nothing else was given.

    Args:
        None

    Returns:
        ExitStatus: NOT_RUN.

    Raises:
        None
    """

    print("[-] discovery got no replies, so there is nothing to probe -- is this machine on the same LAN as "
          "the UniFi devices? UDP 10001 must not be firewalled; try --broadcast <subnet>.255", file=sys.stderr)
    return ExitStatus.NOT_RUN


def print_targets(targets):
    """
    List what would be probed, without connecting to anything.

    Args:
        targets (list[Target]): the targets.

    Returns:
        ExitStatus: ALL_LIVE (nothing was checked, so nothing failed).

    Raises:
        None
    """

    print(f"[*] {len(targets)} device(s) would be probed:")
    for target in targets:
        found = target.found
        if found is None:
            print(f"      {target.ssh_target:28} (given on the command line)")
            continue
        # Only the probe can tell an AP from a switch (radio count), so non-gateways stay unlabelled here
        kind = "gateway" if found.is_gateway else "AP or switch" if found.model_code else "unknown (no model sent)"
        print(f"      {target.ssh_target:28} {found.hostname or '-':18} {found.model_code or '-':10} "
              f"{found.mac or '-':17} {kind}")
    return ExitStatus.ALL_LIVE


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

    here   = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        prog="unifi-overhaul-map.py",
        description="Probe the UniFi mesh over ssh and show, per device, whether the unifi-overhaul 2.4 GHz "
                    "backhaul change is LIVE or REVERTED, plus the ordered re-apply commands. "
                    "Run on your workstation; exit 0 = all live, 1 = action needed, 3 = nothing probed.")
    parser.add_argument("targets", nargs="*", metavar="[user@]ip",
                        help="devices to probe; a bare ip means root@ip")
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
    parser.add_argument("--script", default=os.path.join(here, "unifi-overhaul.sh"),
                        help="path to unifi-overhaul.sh (default: next to this tool)")
    return parser


def probe_all(targets, script_path):
    """
    Probe every target in order (one at a time, so ssh password prompts don't interleave).

    Args:
        targets (list[Target]): the devices.
        script_path (str): local path of unifi-overhaul.sh.

    Returns:
        list[Device]: one per target.

    Raises:
        OSError: unifi-overhaul.sh can't be read, or ssh can't be executed.
    """

    with open(script_path, "rb") as handle:
        script = handle.read()
    return [probe(target, script) for target in targets]


def main():
    """
    Validate inputs, gather targets, probe them, and report.

    Args:
        None

    Returns:
        int: an ExitStatus value.

    Raises:
        OSError: discovery, reading unifi-overhaul.sh, or running ssh failed.
    """

    args     = build_parser().parse_args()
    problems = input_problems(args)
    if problems:
        return report_problems(problems)

    targets = collect_targets(args)
    if not targets:
        return report_no_targets()
    if args.list:
        return print_targets(targets)

    return print_report(Mesh(probe_all(targets, args.script)), args.script)


if __name__ == "__main__":
    sys.exit(main())
