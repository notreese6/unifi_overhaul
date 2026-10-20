"""
Shared test fixtures: the fakes directory, a PATH that puts the fakes first, and a fake device's radio state.
"""

import os
import stat

from unifi_overhaul import client

FAKEBIN  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakebin")
LAUNCHER = os.path.join(client.REPO_ROOT, "unifi-overhaul-client")


def fake_path():
    """
    A PATH with the fake ssh / ping / iw / mongo / systemctl ahead of the real ones.

    Args:
        None

    Returns:
        dict[str, str]: environment overrides to apply with mock.patch.dict(os.environ, ...).

    Raises:
        None
    """

    return {"PATH": FAKEBIN + os.pathsep + os.environ.get("PATH", "")}


class FakeRadio:
    """
    A fake syswrapper plus the files standing in for a device's live state, all in one temporary folder.
    A successful apply-config makes the 2.4 GHz change live (the fake iw then reports it); while the fail
    marker exists, apply-config fails the way the real one does when the radio manager is busy; and the
    fake radiomgrctl reports busy for as many status checks as set_busy() asks.
    """

    # Short waits, so a test of a busy radio manager takes about a second rather than minutes
    POLL_SECONDS = 1
    WAIT_SECONDS = 3

    def __init__(self, folder):
        """
        Create the fake syswrapper and a runtime directory whose uplink interface is the fake iw's vwiresta8.

        Args:
            folder (str): an existing temporary directory to build in.

        Returns:
            None

        Raises:
            OSError: the files can't be written.
        """

        self.syswrapper = os.path.join(folder, "syswrapper.sh")
        self.calls      = os.path.join(folder, "syswrapper.calls")
        self.live       = os.path.join(folder, "radio.live")
        self.fail       = os.path.join(folder, "syswrapper.fail")
        self.busy       = os.path.join(folder, "radiomgr.busy")
        self.run_dir    = os.path.join(folder, "run")

        with open(self.syswrapper, "w", encoding="utf-8") as handle:
            handle.write(f'#!/bin/sh\necho "$@" >> {self.calls}\n'
                         f'if [ -f {self.fail} ]; then echo "apply-config error: radiomgrctl begin failed rc=1"; exit 1; fi\n'
                         f'touch {self.live}\n')
        os.chmod(self.syswrapper, stat.S_IRWXU)

        os.makedirs(self.run_dir)
        with open(os.path.join(self.run_dir, "system.uplink_ifname"), "w", encoding="utf-8") as handle:
            handle.write("vwiresta8\n")

    def env(self):
        """
        The environment overrides that point unifi-overhaul.sh and the fake iw at this radio.

        Args:
            None

        Returns:
            dict[str, str]: the overrides.

        Raises:
            None
        """

        return {"UNIFI_OVERHAUL_SYSWRAPPER": self.syswrapper, "UNIFI_OVERHAUL_RUN": self.run_dir,
                "UNIFI_OVERHAUL_RADIO_POLL": str(self.POLL_SECONDS), "UNIFI_OVERHAUL_RADIO_WAIT": str(self.WAIT_SECONDS),
                "FAKE_IW_LIVE": self.live, "FAKE_RADIO_BUSY": self.busy}

    def set_busy(self, checks):
        """
        Make the radio manager report busy for the next few status checks.

        Args:
            checks (int): how many status checks report busy before it goes idle.

        Returns:
            None

        Raises:
            OSError: the counter file can't be written.
        """

        with open(self.busy, "w", encoding="utf-8") as handle:
            handle.write(f"{checks}\n")

    def call_count(self):
        """
        How many times apply-config was run.

        Args:
            None

        Returns:
            int: the number of calls, successful or not.

        Raises:
            None
        """

        if not os.path.exists(self.calls):
            return 0
        with open(self.calls, encoding="utf-8") as handle:
            return len(handle.readlines())
