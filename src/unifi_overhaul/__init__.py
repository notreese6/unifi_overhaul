"""
unifi_overhaul: the workstation side of unifi-overhaul -- discover the UniFi mesh, probe each device over ssh,
report whether the 2.4 GHz backhaul change is live, and collect diagnostics. The on-device half is
scripts/unifi-overhaul.sh, which this package pipes to each device; nothing here runs on a device.
"""
