"""
unifi_overhaul.client: find the UniFi gateway and APs, probe each one over ssh with unifi-overhaul.sh's __probe,
and print the mesh tree with whether the 2.4 GHz backhaul change is live on each device. With --diagnostics it
also runs unifi-overhaul.sh's read-only __diag on every device, plus checks of this machine, into one log file.

Runs on your WORKSTATION only. The devices run unifi-overhaul.sh; this tool is never copied to them.
"""

from __future__ import annotations

import ctypes
import enum
import hashlib
import io
import ipaddress
import os
import platform
import re
import shlex
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
DIAG_TIMEOUT        = 300   # seconds for one device's __diag; mca-dump and the controller queries are the slow part
LOCAL_TIMEOUT       = 90    # seconds for one check of this machine (system_profiler alone can take ~20)
DEFAULT_LOG_NAME    = "unifi-diagnostics-%Y%m%d-%H%M%S.log"
INTERNET_HOST       = "1.1.1.1"
REDACTED            = "<redacted>"
REMOTE_SCRIPT       = "/tmp/unifi-overhaul.sh"
# Must match HEAL_DIR's default in unifi-overhaul.sh (a test checks they agree): self-heal's copy of the script
REMOTE_HEAL_DIR     = "/data/unifi-overhaul"

# This file is src/unifi_overhaul/client.py, so the repo root is three levels up
REPO_ROOT      = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SCRIPT = os.path.join(REPO_ROOT, "scripts", "unifi-overhaul.sh")
LOG_DIR        = os.path.join(REPO_ROOT, "logs")

# Model-code prefixes of UniFi gateways / consoles; every other discovered device is treated as an AP
GATEWAY_MODEL_PREFIXES = ["UX", "UDM", "UDR", "UCG", "UXG", "USG", "UDW"]

# A name that holds a secret: credentials and keys, plus every controller x_ field (where it stores its secrets)
SECRET_NAME = r"(?:x_\w*|[\w.\-]*(?:psk|passphrase|password|passwd|secret|authkey|auth_key|token|private_key|\.key\b)[\w.\-]*)"
# system.cfg / mgmt style: name=value on its own line
CFG_SECRET  = re.compile(rf"^(\s*{SECRET_NAME}\s*=)(.*)$", re.IGNORECASE | re.MULTILINE)
# JSON style (mca-dump, the controller database): "name" : value, the value quoted or bare
JSON_SECRET = re.compile(rf'("{SECRET_NAME}"\s*:\s*)("(?:[^"\\]|\\.)*"|[^,}}\s]+)', re.IGNORECASE)


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
    self_heal:     str = ""         # gateway self-heal timer: active / inactive / absent; "" = not reported
    base_mac:      str = ""         # the device's own MAC, as the controller and mesh.serial1 name it
    prefers:       str = ""         # base MAC of the parent its config prefers (mesh.serial1); "" = none set
    heal_checksum: str = ""         # sha256 of the gateway's self-heal copy of the script; "" = none
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


@dataclass
class RedactedText:
    """
    Text with every secret-looking value replaced, and how many were replaced.
    """

    text:  str
    count: int

    @classmethod
    def of(cls, text):
        """
        Redact config-style (name=value) and JSON-style ("name": value) secrets in a block of text.

        Args:
            text (str): the raw text.

        Returns:
            RedactedText: the redacted text and the number of values replaced.

        Raises:
            None
        """

        text, cfg_count  = CFG_SECRET.subn(rf"\1{REDACTED}", text)
        text, json_count = JSON_SECRET.subn(rf'\1"{REDACTED}"', text)
        return cls(text=text, count=cfg_count + json_count)


@dataclass
class RemoteRun:
    """
    The outcome of piping unifi-overhaul.sh to one device over ssh and running one of its modes.
    """

    target:       Target
    returncode:   int
    stdout:       str
    stderr:       str
    seconds:      float
    is_timed_out: bool = False

    @property
    def error(self):
        """
        Why the run failed, in one line.

        Args:
            None

        Returns:
            str: the reason, or "" when ssh succeeded and the device printed something.

        Raises:
            None
        """

        if self.is_timed_out:
            return f"timed out after {self.seconds:.0f}s (output so far is kept)"
        if self.returncode == 0 and self.stdout.strip():
            return ""
        reason = self.stderr.strip().splitlines()
        return reason[-1] if reason else f"ssh exited {self.returncode}"


@dataclass
class LocalCheck:
    """
    One diagnostic command run on this machine with /bin/sh, on the platforms it applies to.
    """

    title:     str
    command:   str
    platforms: list    # sys.platform prefixes, e.g. ["darwin", "linux"]

    @property
    def applies_here(self):
        """
        Whether this check is meant for the platform we are running on.

        Args:
            None

        Returns:
            bool: True when sys.platform starts with one of the check's platforms.

        Raises:
            None
        """

        return any(sys.platform.startswith(name) for name in self.platforms)


POSIX = ["darwin", "linux"]

LOCAL_CHECKS = [
    LocalCheck("os",                        "uname -a; sw_vers 2>/dev/null; cat /etc/os-release 2>/dev/null", POSIX),
    LocalCheck("addresses",                 "ip addr 2>/dev/null || ifconfig",                             POSIX),
    LocalCheck("routes",                    "ip route 2>/dev/null || netstat -rn",                         POSIX),
    LocalCheck("neighbours (arp)",          "ip neigh 2>/dev/null || arp -an",                             POSIX),
    LocalCheck("dns",                       "cat /etc/resolv.conf; scutil --dns 2>/dev/null | head -n 60", POSIX),
    LocalCheck("wifi (macOS)",              "system_profiler SPAirPortDataType",                           ["darwin"]),
    LocalCheck("network ports (macOS)",     "networksetup -listallhardwareports",                          ["darwin"]),
    LocalCheck("wifi links (iw)",
               "iw dev; for i in $(iw dev | awk '$1==\"Interface\"{print $2}'); do "
               "echo \"--- $i\"; iw dev \"$i\" link; iw dev \"$i\" station dump; done",                  ["linux"]),
    LocalCheck("wifi (NetworkManager)",
               "nmcli -f IN-USE,BSSID,SSID,CHAN,FREQ,RATE,SIGNAL,SECURITY dev wifi list",                 ["linux"]),
]


class DiagnosticLog:
    """
    The diagnostics log file: titled sections, with every body redacted before it is written.
    """

    def __init__(self, handle):
        """
        Wrap an open, writable text file.

        Args:
            handle (TextIO): the log file.

        Returns:
            None

        Raises:
            None
        """

        self.handle     = handle
        self.redactions = 0

    def section(self, title):
        """
        Start a new titled section.

        Args:
            title (str): the section title.

        Returns:
            None

        Raises:
            OSError: the log can't be written.
        """

        print(f"\n\n{'#' * 24} {title} {'#' * 24}", file=self.handle)
        self.handle.flush()

    def write(self, text):
        """
        Write a block of text, redacted.

        Args:
            text (str): the raw text.

        Returns:
            None

        Raises:
            OSError: the log can't be written.
        """

        redacted         = RedactedText.of(text)
        self.redactions += redacted.count
        print(redacted.text.rstrip("\n"), file=self.handle)
        self.handle.flush()


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


def parse_device_list(output):
    """
    Turn unifi-overhaul.sh __devices output (the controller's adopted devices) into discovered devices.

    Args:
        output (str): tab-separated "D ip mac type model name" lines; anything else is ignored.

    Returns:
        list[DiscoveredDevice]: one per adopted device with a usable IP; [] when the gateway had no list.

    Raises:
        None
    """

    found = []

    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) < 6 or fields[0] != "D":
            continue
        # A device the controller hasn't seen online has no IP, and isn't reachable to probe anyway
        try:
            ipaddress.ip_address(fields[1])
        except ValueError:
            continue
        name   = "\t".join(fields[5:])
        device = DiscoveredDevice(ip=fields[1], mac=fields[2].lower(), platform=fields[4],
                                  hostname="" if name == "undefined" else name)
        found.append(device)

    return found


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
        elif kind == "S" and len(fields) >= 2:
            dev.self_heal = fields[1]
        elif kind == "B" and len(fields) >= 2:
            dev.base_mac = fields[1].lower()
        elif kind == "P" and len(fields) >= 2:
            dev.prefers = fields[1].lower()
        elif kind == "C" and len(fields) >= 2:
            dev.heal_checksum = fields[1]

    # The uplink BSSID has to match the AP MACs case-for-case to join the tree
    dev.uplink = dev.uplink.lower() if dev.uplink not in ("WIRED", "NONE") else dev.uplink
    return dev


def text_of(raw):
    """
    Decode captured process output, which is None when nothing was captured.

    Args:
        raw (bytes | None): the captured bytes.

    Returns:
        str: the decoded text, "" for None.

    Raises:
        None
    """

    return raw.decode("utf-8", "replace") if raw else ""


def run_ssh(target, remote, stdin, timeout=None):
    """
    ssh to one device, run a command there, and feed it `stdin`.

    Args:
        target (Target): the device.
        remote (str): the command line to run on the device.
        stdin (bytes): what to send on the command's stdin (b"" for nothing).
        timeout (float | None): seconds before ssh is killed; None waits forever.

    Returns:
        RemoteRun: what the device printed, and how ssh exited.

    Raises:
        OSError: the ssh binary can't be executed.
    """

    command = ["ssh", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}", "-o", "StrictHostKeyChecking=accept-new",
               target.ssh_target, remote]
    started = time.monotonic()

    try:
        result = subprocess.run(command, input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        return RemoteRun(target=target, returncode=-1, stdout=text_of(expired.stdout), stderr=text_of(expired.stderr),
                         seconds=time.monotonic() - started, is_timed_out=True)

    return RemoteRun(target=target, returncode=result.returncode, stdout=text_of(result.stdout),
                     stderr=text_of(result.stderr), seconds=time.monotonic() - started)


def run_remote(target, script, mode, timeout=None):
    """
    Run one mode of unifi-overhaul.sh on a device by piping the script to `sh -s` over ssh.

    Args:
        target (Target): the device.
        script (bytes): the contents of unifi-overhaul.sh.
        mode (str): the script mode to run, e.g. "__probe" or "__diag".
        timeout (float | None): seconds before ssh is killed; None waits forever.

    Returns:
        RemoteRun: what the device printed, and whether ssh succeeded.

    Raises:
        OSError: the ssh binary can't be executed.
    """

    return run_ssh(target, f"sh -s {mode}", script, timeout)


def probe(target, script, is_quiet=False):
    """
    ssh to one device and run unifi-overhaul.sh's __probe there.

    Args:
        target (Target): the device to probe.
        script (bytes): the contents of unifi-overhaul.sh.
        is_quiet (bool): skip the progress line (for repeated polling).

    Returns:
        Device: the probed device; Device.error is set if ssh failed.

    Raises:
        OSError: the ssh binary can't be executed.
    """

    if not is_quiet:
        print(f"[*] probing {target.ssh_target} ...", file=sys.stderr)
    run = run_remote(target, script, "__probe")

    if run.error:
        return Device(target=target, error=run.error)
    return parse_probe(target, run.stdout)


def apply_remote_command(options=""):
    """
    The device-side command line that saves the piped unifi-overhaul.sh and runs apply detached.

    Args:
        options (str): extra apply options, e.g. "--role relay --channel 1"; "" for none.

    Returns:
        str: the command to run on the device, with the script arriving on stdin.

    Raises:
        None
    """

    apply = f"{REMOTE_SCRIPT} apply {options}".rstrip()
    # </dev/null so the backgrounded apply lets go of the ssh session instead of holding it open
    return (f"cat > {REMOTE_SCRIPT} && chmod +x {REMOTE_SCRIPT} && "
            f"nohup {apply} < /dev/null > /tmp/vw.log 2>&1 &")


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
    options = "--with-ap" if mesh.is_relay(dev) and not dev.is_root else ""
    remote  = apply_remote_command(options)
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
        heal = ""
        if dev.self_heal:
            shown = "not installed" if dev.self_heal == "absent" else dev.self_heal
            heal  = f"   [self-heal: {shown}]"
        return f"{indent}{dev.label}   (root, wired)   [2.4: {state.value}]{heal}"

    signal = f" {dev.uplink_signal}dBm" if dev.uplink_signal not in ("", "-") else ""
    return f"{indent}{dev.label}   <--[uplink {dev.uplink_band}{signal}]   [2.4: {state.value}]"


def print_report(mesh, script_path, out=None):
    """
    Print the tree, the live summary, and the ordered re-apply list.

    Args:
        mesh (Mesh): the probed mesh.
        script_path (str): local path of unifi-overhaul.sh, used in the re-apply commands.
        out (TextIO | None): where to print; None means stdout.

    Returns:
        ExitStatus: ALL_LIVE when every mesh device is live, else NEEDS_ACTION.

    Raises:
        OSError: `out` can't be written.
    """

    out     = out or sys.stdout
    ordered = mesh.tree_order()
    in_mesh = [dev for dev, _depth in ordered if mesh.state(dev) is not State.NO_WIFI]
    live    = [dev for dev in in_mesh if mesh.state(dev) is State.LIVE]
    dark    = [dev for dev in in_mesh if mesh.state(dev) is State.UNREACHABLE]
    todo    = [dev for dev in in_mesh if mesh.state(dev) in (State.REVERTED, State.PARTIAL)]

    print("", file=out)
    print("[*] mesh map (indented = uplinks to the line above; [band signal] is the uplink; "
          "[2.4: ...] = is the change live here)", file=out)
    for dev, depth in ordered:
        print(tree_line(dev, depth, mesh), file=out)

    print("", file=out)
    print(f"[*] 2.4 GHz backhaul live on {len(live)} of {len(in_mesh)} mesh devices probed", file=out)
    if dark:
        print(f"[-] {len(dark)} device(s) could not be checked -- fix ssh access and re-run before trusting this",
              file=out)
    if not todo:
        if not dark:
            print("[+] nothing to re-apply", file=out)
        return ExitStatus.ALL_LIVE if not dark else ExitStatus.NEEDS_ACTION

    print("[-] re-apply these, in this order, waiting ~30s (and re-running this) between each:", file=out)
    for dev in todo:
        if mesh.state(dev) is State.PARTIAL:
            print(f"      {dev.label}:  PARTIAL (only one side of the relay is on 2.4) -- "
                  f"check its status, rollback or reboot it, then re-apply", file=out)
        else:
            print(f"      {dev.label}:  {reapply_command(dev, mesh, script_path)}", file=out)
    return ExitStatus.NEEDS_ACTION


def print_targets(targets, out=None):
    """
    List what would be probed, without connecting to anything.

    Args:
        targets (list[Target]): the targets.
        out (TextIO | None): where to print; None means stdout.

    Returns:
        ExitStatus: ALL_LIVE (nothing was checked, so nothing failed).

    Raises:
        OSError: `out` can't be written.
    """

    out = out or sys.stdout
    print(f"[*] {len(targets)} device(s) would be probed:", file=out)
    for target in targets:
        found = target.found
        if found is None:
            print(f"      {target.ssh_target:28} (given on the command line)", file=out)
            continue
        # Only the probe can tell an AP from a switch (radio count), so non-gateways stay unlabelled here
        kind = "gateway" if found.is_gateway else "AP or switch" if found.model_code else "unknown (no model sent)"
        print(f"      {target.ssh_target:28} {found.hostname or '-':18} {found.model_code or '-':10} "
              f"{found.mac or '-':17} {found.firmware or '-':24} {kind}", file=out)
    return ExitStatus.ALL_LIVE


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


def local_checks_for(targets):
    """
    The checks to run on this machine: the platform's own, then a ping to every device and to the internet.

    Args:
        targets (list[Target]): the devices being diagnosed.

    Returns:
        list[LocalCheck]: the checks that apply on this platform, in run order.

    Raises:
        None
    """

    checks = [check for check in LOCAL_CHECKS if check.applies_here]

    for host in [target.ip for target in targets] + [INTERNET_HOST]:
        ping = LocalCheck(f"ping {host}", f"ping -c 10 {shlex.quote(host)}", POSIX)
        checks.append(ping)

    return [check for check in checks if check.applies_here]


def run_local_check(check):
    """
    Run one check on this machine and capture everything it prints.

    Args:
        check (LocalCheck): the check.

    Returns:
        str: stdout and stderr together, then the exit status (or a note that it timed out).

    Raises:
        None
    """

    try:
        # stderr folded into stdout so an error shows up next to the output it interrupted
        result = subprocess.run(["/bin/sh", "-c", check.command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=LOCAL_TIMEOUT)
    except subprocess.TimeoutExpired as expired:
        return f"{text_of(expired.stdout)}\n----- timed out after {LOCAL_TIMEOUT}s"
    except OSError as error:
        return f"----- could not run: {error}"
    return f"{text_of(result.stdout)}\n----- rc={result.returncode}"


def log_header(log, args, targets, script):
    """
    Write what this run was: when, from where, against what, and with which unifi-overhaul.sh.

    Args:
        log (DiagnosticLog): the log.
        args (argparse.Namespace): parsed arguments.
        targets (list[Target]): the devices being diagnosed.
        script (bytes): the contents of unifi-overhaul.sh piped to each device.

    Returns:
        None

    Raises:
        OSError: the log can't be written.
    """

    log.section("unifi-overhaul-client --diagnostics")
    log.write(f"started:   {time.strftime('%Y-%m-%d %H:%M:%S %Z')}  ({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})\n"
              f"command:   {shlex.join(sys.argv)}\n"
              f"client:    {platform.node()}  {platform.platform()}  python {platform.python_version()}\n"
              f"script:    {args.script}  sha256 {hashlib.sha256(script).hexdigest()[:16]}\n"
              f"devices:   {len(targets)}\n"
              f"note:      values that look like passwords, keys or x_ secrets are replaced with {REDACTED}")
    log.section("devices")
    listing = io.StringIO()
    print_targets(targets, out=listing)
    log.write(listing.getvalue())


def log_local_checks(log, targets):
    """
    Run every check of this machine (addresses, routes, its own wifi link, pings) into the log.

    Args:
        log (DiagnosticLog): the log.
        targets (list[Target]): the devices being diagnosed, to ping.

    Returns:
        None

    Raises:
        OSError: the log can't be written.
    """

    print("[*] checking this machine (wifi, routes, pings) ...", file=sys.stderr)
    log.section(f"this machine ({platform.node()})")
    for check in local_checks_for(targets):
        log.write(f"\n===== {check.title}: {check.command}")
        log.write(run_local_check(check))


def log_mesh_report(log, targets, script_path):
    """
    Probe every device and write the same mesh map the plain run prints.

    Args:
        log (DiagnosticLog): the log.
        targets (list[Target]): the devices.
        script_path (str): local path of unifi-overhaul.sh.

    Returns:
        ExitStatus: the mesh report's status (ALL_LIVE or NEEDS_ACTION).

    Raises:
        OSError: the log or unifi-overhaul.sh can't be accessed, or ssh can't be executed.
    """

    log.section("mesh map")
    report = io.StringIO()
    status = print_report(Mesh(probe_all(targets, script_path)), script_path, out=report)
    log.write(report.getvalue())
    print(report.getvalue(), end="")
    return status


def log_device_diagnostics(log, targets, script):
    """
    Run unifi-overhaul.sh's __diag on each device in turn and write everything it prints.

    Args:
        log (DiagnosticLog): the log.
        targets (list[Target]): the devices.
        script (bytes): the contents of unifi-overhaul.sh.

    Returns:
        list[RemoteRun]: the runs that failed or timed out (empty when every device answered in full).

    Raises:
        OSError: the log can't be written, or ssh can't be executed.
    """

    failed = []

    for target in targets:
        print(f"[*] collecting diagnostics from {target.ssh_target} (up to {DIAG_TIMEOUT}s) ...", file=sys.stderr)
        run = run_remote(target, script, "__diag", timeout=DIAG_TIMEOUT)

        log.section(f"device {target.ssh_target}   ({run.seconds:.0f}s, ssh rc={run.returncode})")
        if run.error:
            log.write(f"[-] diagnostics incomplete: {run.error}")
            failed.append(run)
        log.write(run.stdout)
        if run.stderr.strip():
            log.write(f"\n===== ssh stderr\n{run.stderr}")

    return failed


def report_diagnostics(log_path, log, failed):
    """
    Tell the operator where the log is, how much was redacted, and which devices didn't answer in full.

    Args:
        log_path (str): the log file written.
        log (DiagnosticLog): the finished log.
        failed (list[RemoteRun]): the device runs that failed or timed out.

    Returns:
        None

    Raises:
        None
    """

    for run in failed:
        print(f"[-] {run.target.ssh_target}: diagnostics incomplete -- {run.error}", file=sys.stderr)
    print(f"[+] diagnostics written to {os.path.abspath(log_path)} "
          f"({os.path.getsize(log_path) // 1024} KiB, {log.redactions} secret value(s) redacted)", file=sys.stderr)
    print("[*] it still holds MACs, IPs and hostnames -- fine to share for troubleshooting, but don't commit it",
          file=sys.stderr)


def run_diagnostics(args, targets):
    """
    Write the full diagnostics log: header, this machine, the mesh map, then every device's __diag output.

    Args:
        args (argparse.Namespace): parsed arguments (log path, script path).
        targets (list[Target]): the devices to diagnose.

    Returns:
        ExitStatus: NEEDS_ACTION when any device couldn't be diagnosed in full, else the mesh map's status.

    Raises:
        OSError: the log can't be created or written, unifi-overhaul.sh can't be read, or ssh can't be executed.
    """

    with open(args.script, "rb") as handle:
        script = handle.read()

    print(f"[*] writing diagnostics to {args.log}", file=sys.stderr)
    # "x" creates the file and fails if it appeared since the inputs were checked, so nothing is ever overwritten
    with open(args.log, "x", encoding="utf-8") as handle:
        log = DiagnosticLog(handle)
        log_header(log, args, targets, script)
        log_local_checks(log, targets)
        status = log_mesh_report(log, targets, args.script)
        failed = log_device_diagnostics(log, targets, script)
        log.section("end")
        log.write(f"finished:  {time.strftime('%Y-%m-%d %H:%M:%S %Z')}   redacted values: {log.redactions}")

    report_diagnostics(args.log, log, failed)
    return ExitStatus.NEEDS_ACTION if failed else status
