"""
Shared test fixtures: the fakes directory, and a PATH that puts the fakes first.
"""

import os

from unifi_overhaul import client

FAKEBIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakebin")
LAUNCHER = os.path.join(client.REPO_ROOT, "unifi-overhaul-client")


def fake_path():
    """
    A PATH with the fake ssh / ping / iw ahead of the real ones.

    Args:
        None

    Returns:
        dict[str, str]: environment overrides to apply with mock.patch.dict(os.environ, ...).

    Raises:
        None
    """

    return {"PATH": FAKEBIN + os.pathsep + os.environ.get("PATH", "")}
