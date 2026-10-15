"""
Secret redaction on the config and JSON formats the diagnostics log carries.
"""

import unittest

from unifi_overhaul import client

SECRETS = ["SuperSecret123", "vwireMeshKey", "$6$hash", "0123456789abcdef", "AAAAB3Nza", "wifipass", "sshpw",
           "12345", '"abc"']
KEPT    = ["example-ssid", "radio.1.channel=1", "0f1e2d3c4b5a6978", '"country"', "840", "wpapsk", "wpa2"]
SAMPLE  = """aaa.1.wpa.psk=SuperSecret123
aaa.3.wpa.psk=vwireMeshKey
users.1.password=$6$hash
mgmt.authkey=0123456789abcdef
sshd.auth.key.1.value=AAAAB3Nza
wireless.1.ssid=example-ssid
radio.1.channel=1
mgmt.cfgversion=0f1e2d3c4b5a6978
{ "x_passphrase" : "wifipass", "name" : "Home", "x_iapp_key" : "abc", "key" : "country", "code" : 840,
  "wpa_mode" : "wpa2", "x_ssh_password": "sshpw", "token": 12345, "security" : "wpapsk" }"""


class TestRedactedText(unittest.TestCase):
    """
    RedactedText.of removes every planted secret and keeps the facts we diagnose with.
    """

    def test_no_secret_survives(self):
        text = client.RedactedText.of(SAMPLE).text
        self.assertEqual([s for s in SECRETS if s in text], [])

    def test_diagnostic_values_survive(self):
        text = client.RedactedText.of(SAMPLE).text
        self.assertEqual([s for s in KEPT if s not in text], [])

    def test_counts_each_replacement(self):
        self.assertEqual(client.RedactedText.of(SAMPLE).count, len(SECRETS))


if __name__ == "__main__":
    unittest.main()
