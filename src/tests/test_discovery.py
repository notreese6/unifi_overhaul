"""
Discovery reply parsing: field decoding, truncation, our own echoed probe, and gateway detection.
"""

import ctypes
import unittest

from unifi_overhaul import client


def field(kind, value):
    """
    Encode one discovery field the way a device does.

    Args:
        kind (int): the field type code.
        value (bytes): the field value.

    Returns:
        bytes: the field header followed by the value.

    Raises:
        None
    """

    return bytes(client.FieldHeader(kind=kind, length=len(value))) + value


def reply(*fields):
    """
    Wrap encoded fields in a v1 discovery packet header.

    Args:
        *fields (bytes): encoded fields, in order.

    Returns:
        bytes: the whole packet.

    Raises:
        None
    """

    body   = b"".join(fields)
    header = client.PacketHeader(version=client.DISCOVERY_VERSION, command=client.DISCOVERY_COMMAND, length=len(body))
    return bytes(header) + body


MAC = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])


class TestFromReply(unittest.TestCase):
    """
    DiscoveredDevice.from_reply on well-formed, truncated and foreign packets.
    """

    def test_reads_every_known_field(self):
        packet = reply(field(client.Tlv.HWADDR, MAC), field(client.Tlv.HOSTNAME, b"gateway"),
                       field(client.Tlv.PLATFORM, b"UDR"), field(client.Tlv.FIRMWARE, b"1.0.0"))
        device = client.DiscoveredDevice.from_reply(packet, "192.0.2.1")

        self.assertEqual(device.ip, "192.0.2.1")
        self.assertEqual(device.mac, "02:00:00:00:00:01")
        self.assertEqual(device.hostname, "gateway")
        self.assertEqual(device.firmware, "1.0.0")
        self.assertTrue(device.is_gateway)

    def test_mac_falls_back_to_ipinfo(self):
        ipinfo = bytes(client.IpInfo(mac=client.MacAddress(*MAC), ip=(ctypes.c_uint8 * 4)(192, 0, 2, 21)))
        device = client.DiscoveredDevice.from_reply(reply(field(client.Tlv.IPINFO, ipinfo)), "192.0.2.21")
        self.assertEqual(device.mac, "02:00:00:00:00:01")

    def test_truncated_value_is_dropped(self):
        packet = reply(field(client.Tlv.HOSTNAME, b"ap-far"), field(client.Tlv.PLATFORM, b"UAPX"))[:-2]
        device = client.DiscoveredDevice.from_reply(packet, "192.0.2.21")
        self.assertEqual(device.hostname, "ap-far")
        self.assertEqual(device.platform, "")

    def test_own_probe_echo_is_ignored(self):
        self.assertIsNone(client.DiscoveredDevice.from_reply(client.DISCOVERY_PROBE, "192.0.2.50"))

    def test_other_version_is_ignored(self):
        packet = bytearray(reply(field(client.Tlv.HOSTNAME, b"x")))
        packet[0] = 2
        self.assertIsNone(client.DiscoveredDevice.from_reply(bytes(packet), "192.0.2.9"))

    def test_access_point_is_not_a_gateway(self):
        device = client.DiscoveredDevice(ip="192.0.2.21", platform="UAPX")
        self.assertFalse(device.is_gateway)


if __name__ == "__main__":
    unittest.main()
