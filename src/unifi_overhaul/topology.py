"""
unifi_overhaul.topology: the intended mesh, read from mesh.ini — which devices exist, how to reach each one,
and which device each AP should uplink to. Roles come from this tree rather than from what happens to be
connected when a device is applied, so a relay can't be mistaken for a leaf after an outage.
"""

from __future__ import annotations

import configparser
import enum
import os
from dataclasses import dataclass, field

MESH_SECTION    = "mesh"
DEFAULT_CHANNEL = 1
# 2.4 GHz channels; 12-13 exist outside the US, so they're accepted and left to the radio's regulatory limits
CHANNELS        = list(range(1, 14))


def default_mesh_path():
    """
    Where mesh.ini lives by default: outside the repo, since it holds the network's own addresses.

    Args:
        None

    Returns:
        str: $XDG_CONFIG_HOME/unifi-overhaul/mesh.ini, or ~/.config/unifi-overhaul/mesh.ini.

    Raises:
        None
    """

    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(config_home, "unifi-overhaul", "mesh.ini")


class Role(enum.Enum):
    """
    A device's place in the intended tree.
    """

    ROOT  = "root"     # no parent: the wired gateway, offers the 2.4 GHz backhaul
    RELAY = "relay"    # has a parent and children: uplinks on 2.4 GHz and offers it
    LEAF  = "leaf"     # has a parent, no children: uplinks on 2.4 GHz


class TopologyError(Exception):
    """
    mesh.ini is missing or invalid; carries every problem found, not just the first.
    """

    def __init__(self, path, problems):
        """
        Record which file failed and why.

        Args:
            path (str): the mesh.ini path.
            problems (list[str]): one sentence per problem.

        Returns:
            None

        Raises:
            None
        """

        super().__init__(f"{path}: " + "; ".join(problems))
        self.path     = path
        self.problems = problems


@dataclass
class Node:
    """
    One device in mesh.ini.
    """

    name:   str
    host:   str
    user:   str = ""     # empty: let ~/.ssh/config (or ssh's default) pick the user
    parent: str = ""     # empty: this is the root

    @property
    def ssh_target(self):
        """
        What to pass to ssh for this device.

        Args:
            None

        Returns:
            str: "user@host", or the bare host when no user is set so ~/.ssh/config applies.

        Raises:
            None
        """

        return f"{self.user}@{self.host}" if self.user else self.host


@dataclass
class Topology:
    """
    The intended mesh: every device, its parent, and the 2.4 GHz channel they share.
    """

    path:    str
    channel: int = DEFAULT_CHANNEL
    nodes:   list = field(default_factory=list)

    def node(self, name):
        """
        Look up a device by its section name.

        Args:
            name (str): the section name in mesh.ini.

        Returns:
            Node | None: the device, or None if there is no such section.

        Raises:
            None
        """

        return next((node for node in self.nodes if node.name == name), None)

    def children(self, node):
        """
        The devices whose intended parent is this one.

        Args:
            node (Node): the device.

        Returns:
            list[Node]: its children, in file order.

        Raises:
            None
        """

        return [child for child in self.nodes if child.parent == node.name]

    def role(self, node):
        """
        The device's role, derived from the tree.

        Args:
            node (Node): the device.

        Returns:
            Role: ROOT without a parent, RELAY with children, otherwise LEAF.

        Raises:
            None
        """

        if not node.parent:
            return Role.ROOT
        return Role.RELAY if self.children(node) else Role.LEAF

    def ordered(self):
        """
        Every device, parents before children: the order changes have to be applied in.

        Args:
            None

        Returns:
            list[Node]: the root, then each level of the tree in file order.

        Raises:
            None
        """

        ordered = [node for node in self.nodes if not node.parent]
        for node in ordered:
            ordered.extend(self.children(node))
        return ordered

    @classmethod
    def load(cls, path):
        """
        Read and validate mesh.ini.

        Args:
            path (str): the file to read.

        Returns:
            Topology: the validated topology.

        Raises:
            TopologyError: the file is missing, unreadable, or describes an invalid tree (every problem is listed).
        """

        if not os.path.isfile(path):
            raise TopologyError(path, ["file not found -- copy mesh.example.ini there and fill it in"])

        # Inline comments allowed, so `host = ap-hall  # upstairs` reads as just ap-hall
        parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError) as error:
            raise TopologyError(path, [f"can't be read: {error}"]) from error

        topology = cls(path=path)
        problems = topology.absorb(parser)
        problems += topology.tree_problems()
        if problems:
            raise TopologyError(path, problems)
        return topology

    def absorb(self, parser):
        """
        Fill in the channel and nodes from a parsed file.

        Args:
            parser (configparser.ConfigParser): the parsed mesh.ini.

        Returns:
            list[str]: problems with individual sections or values.

        Raises:
            None
        """

        problems = []

        if parser.has_section(MESH_SECTION):
            channel = parser.get(MESH_SECTION, "channel", fallback=str(DEFAULT_CHANNEL)).strip()
            if channel.isdigit() and int(channel) in CHANNELS:
                self.channel = int(channel)
            else:
                problems.append(f"[{MESH_SECTION}] channel {channel!r} is not a 2.4 GHz channel (1-13)")

        for name in parser.sections():
            if name == MESH_SECTION:
                continue
            section = parser[name]
            node    = Node(name=name, host=section.get("host", "").strip(), user=section.get("user", "").strip(),
                           parent=section.get("parent", "").strip())
            if not node.host:
                problems.append(f"[{name}] has no host")
            self.nodes.append(node)

        return problems

    def tree_problems(self):
        """
        Check that the nodes form one tree: a single root, known parents, and no loops.

        Args:
            None

        Returns:
            list[str]: one sentence per problem.

        Raises:
            None
        """

        if not self.nodes:
            return ["no devices -- add a [section] per device"]

        problems = []
        roots    = [node.name for node in self.nodes if not node.parent]
        if len(roots) != 1:
            problems.append(f"needs exactly one device without a parent (the gateway), found {len(roots)}: {roots}")

        for node in self.nodes:
            if node.parent and self.node(node.parent) is None:
                problems.append(f"[{node.name}] parent {node.parent!r} is not a device in this file")

        # Anything not reachable from the root is either orphaned or part of a loop
        unreached = {node.name for node in self.nodes} - {node.name for node in self.ordered()}
        if unreached and not problems:
            problems.append(f"parents form a loop through {sorted(unreached)}")
        return problems
