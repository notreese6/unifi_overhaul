# unifi-overhaul

Move a UniFi mesh backhaul from 5 GHz to 2.4 GHz, for APs too far away for a solid 5 GHz link.

UniFi meshes its APs over a hidden backhaul network ("vwire") on 5 GHz. 5 GHz is fast but
doesn't go through walls well, so a distant AP can end up with a weak, flaky uplink. UniFi has
no setting to move the backhaul to 2.4 GHz. This project does it by editing each device's live
wireless config.

2.4 GHz trades peak speed for reach: a link that was marginal on 5 GHz usually becomes strong
and steady on 2.4 GHz. See [Is it worth it?](#is-it-worth-it) before you start.

> **This is an unofficial edit to controller-managed devices.** The UniFi controller will undo
> it whenever it re-provisions a device, and the gateway forgets it on reboot. Read
> [What survives what](#what-survives-what) before relying on it.

Developed on UniFi WiFi 7 hardware (a UniFi OS gateway as the root, U7-series APs meshed
wirelessly). Interface names below (`wifi0` = 2.4 GHz, `wifi1` = 5 GHz, `wifi2` = 6 GHz,
`vwireapN`, `vwirestaN`) are from that platform; the script detects them rather than assuming.

## Contents

- [Install](#install)
- [One-time setup: SSH access](#one-time-setup-ssh-access)
- [Quick start](#quick-start)
- [Roles](#roles)
- [What survives what](#what-survives-what)
- [Recovery](#recovery)
- [The client](#the-client)
- [On-device modes](#on-device-modes)
- [Is it worth it?](#is-it-worth-it)
- [Glossary](#glossary)
- [Contributing](#contributing)
- [License](#license)

---

## Install

```sh
git clone https://github.com/notreese6/unifi_overhaul.git
cd unifi_overhaul
./unifi-overhaul-client --help
```

Nothing to build or install. You need:

- **On your computer:** Python 3.8 or newer (standard library only) and an `ssh` client.
- **On each UniFi device:** ssh access (set up below). The script uses only tools the stock
  firmware already has (`busybox` shell, `iw`, `awk`, `sed`).

### What's in the repo

| Path | What it is | Runs on |
|---|---|---|
| `scripts/unifi-overhaul.sh` | The change itself: `status`, `dryrun`, `apply`, `rollback` | each UniFi device |
| `./unifi-overhaul-client` | Mesh map, re-apply commands, and full diagnostics, over ssh | your computer |
| `src/unifi_overhaul/` | The client's Python package (standard library only, Python 3.8+) | your computer |
| `src/tests/` | Its tests | your computer |
| `logs/` | Diagnostics output (gitignored) | your computer |

The client pipes `scripts/unifi-overhaul.sh` to each device over ssh, so nothing needs to be
copied to a device just to look at it.

---

## One-time setup: SSH access

Everything here goes through ssh, so set up key-based login once. There are **two different
logins**: the APs share one that the controller manages, and the gateway has its own.

### 1. Make a key

```sh
ssh-keygen -t rsa -b 4096 -f ~/.ssh/unifi_rsa -C unifi
```

RSA, because the UniFi key field only accepted RSA keys in testing.

### 2. APs: Device Authentication

In the UniFi Network app, open **Settings → System → Advanced → Device Authentication** (the
exact location moves between versions). It shows the **SSH username** the controller generated
for your APs. Note it; it's random per controller. Add the contents of `~/.ssh/unifi_rsa.pub`
as a key there and save. The controller pushes it to every AP it manages.

### 3. Gateway: copy the key yourself

Device Authentication covers the devices the console *manages*, not the console itself. The
gateway logs in as `root` with its own password, so copy the key over once:

```sh
ssh-copy-id -i ~/.ssh/unifi_rsa.pub root@<gateway-ip>
```

`ssh-copy-id` logs in with the password one last time and appends your public key to the
gateway's `~/.ssh/authorized_keys`. It survives reboots. A firmware update may remove it; if
`ssh gateway` starts asking for a password again, re-run the command.

### 4. Tell ssh which key and user to use

Add a block per device to `~/.ssh/config`, with your own names and addresses:

```
Host gateway
    HostName <gateway-ip>
    User root
    IdentityFile ~/.ssh/unifi_rsa
    IdentitiesOnly yes

Host ap-hall
    HostName <ap-hall-ip>
Host ap-far
    HostName <ap-far-ip>
Host ap-hall ap-far
    User <device-ssh-user>
    IdentityFile ~/.ssh/unifi_rsa
    IdentitiesOnly yes
    # The APs' ssh server may only accept the older RSA signature
    PubkeyAcceptedAlgorithms +ssh-rsa
    HostKeyAlgorithms +ssh-rsa
```

`IdentitiesOnly yes` stops ssh from trying every key you have first, which can get you
disconnected before it reaches the right one. Then `ssh gateway`, `ssh ap-hall` and so on log
straight in. Give the APs fixed IPs in UniFi so these addresses don't change.

---

## Quick start

### 1. Look at the mesh

```sh
./unifi-overhaul-client root@<gateway-ip> <device-ssh-user>@<ap-ip> <device-ssh-user>@<ap-ip> ...
```

```
[*] mesh map (indented = uplinks to the line above; [band signal] is the uplink; [2.4: ...] = is the change live here)
gateway (192.168.1.1)   (root, wired)   [2.4: REVERTED]
    ap-hall (192.168.1.20)   <--[uplink 5GHz -70dBm]   [2.4: REVERTED]
        ap-far (192.168.1.21)   <--[uplink 5GHz -78dBm]   [2.4: REVERTED]
```

Indentation is the real tree: each device sits under the one it's connected to. `--discover`
finds devices with Ubiquiti's discovery broadcast instead of a list, but APs don't always
answer it, so naming them is more reliable.

### 2. Apply, in tree order

Each device needs the one above it already offering 2.4 GHz, so go **root → relays → leaves**,
one at a time. The client prints the exact command for every device that needs it, in order:

```sh
ssh gateway 'cat > /tmp/unifi-overhaul.sh && chmod +x /tmp/unifi-overhaul.sh &&
  nohup /tmp/unifi-overhaul.sh apply < /dev/null > /tmp/vw.log 2>&1 &' < scripts/unifi-overhaul.sh
```

Use `apply --with-ap` on every **relay** (an AP that others connect through). See
[Roles](#roles) for why.

`apply` restarts wifi on that device for 20–30 seconds, so it runs detached (`nohup … &`) and
your ssh session is allowed to drop. Run it from a computer that isn't connected *through* the
device you're changing, ideally one wired to the gateway.

### 3. Verify

Wait about 30 seconds, re-run the map, and repeat for the next device. Every line should end
in `[2.4: LIVE]`, with each AP indented under the parent you expect.

---

## Roles

| Role | What it is | `apply` does |
|---|---|---|
| **root** | The wired gateway | Adds a 2.4 GHz backhaul network (keeps 5/6 GHz) |
| **relay** | A wireless AP that other APs connect through | Moves its uplink to 2.4 GHz **and** offers a 2.4 GHz network |
| **leaf** | A wireless AP with nothing connected through it | Moves its uplink to 2.4 GHz |

The script works out the role from what's connected *at that moment*. **That's a trap after an
outage:** if a relay is re-applied while the APs below it are still disconnected, it looks like
a leaf, offers no 2.4 GHz network, and the APs below either can't connect or reach past it to a
weaker parent. So always apply relays with `--with-ap`. The map shows the symptom as an AP
indented one level too shallow, usually with a weak uplink signal.

The whole 2.4 GHz chain must share one channel (`--channel N`, default 1), because a relay's
single 2.4 GHz radio is both its uplink and the network it offers.

---

## What survives what

| Event | APs | Gateway |
|---|---|---|
| `apply`, then the AP's uplink reconnects | **Saved:** the AP writes its running config to flash whenever its uplink comes up | n/a |
| AP reboot | Keeps the change, if it was saved | n/a |
| Gateway reboot or power cut | Keep theirs, and look for a 2.4 GHz parent | **Loses it.** Re-apply right away, or the APs stay isolated |
| Any controller provision: a settings change, firmware update, re-adoption, plugging an AP into Ethernet | **Back to stock 5 GHz**, and that gets saved | Back to stock |

The dangerous case is a provision on an AP whose **stock 5 GHz link can't reach its parent**,
which is exactly the kind of AP this project is for. It drops off the network immediately and
can't be reached over ssh to fix. Avoid site-wide UniFi changes while the mesh is applied.
Change per-device settings only on APs that can still connect on 5 GHz.

---

## Recovery

| Situation | Fix |
|---|---|
| A device shows `REVERTED` | Run the command the map prints for it, in the order printed |
| APs isolated after a power cut | Re-apply the **gateway** first; the APs are already looking for it on 2.4 GHz |
| An AP is isolated and unreachable (provisioned back to 5 GHz, out of 5 GHz range) | Move it next to its parent, or wire it temporarily, then re-apply its parent and it (`--with-ap` if it's a relay), then put it back |
| An AP's uplink skipped its relay | Apply the relay with `--with-ap`, then reboot the AP so it picks the stronger parent |
| You want stock back on one device | `rollback` (only from the same boot as the `apply`), or force a re-provision of that device from the UniFi app |

---

## The client

```sh
./unifi-overhaul-client [user@]ip ...                 # mesh map + ordered re-apply commands
./unifi-overhaul-client --discover --ap-user USER     # find devices by broadcast instead
./unifi-overhaul-client ... --list                    # show what would be probed, connect to nothing
./unifi-overhaul-client ... --diagnostics             # everything, into logs/unifi-diagnostics-<time>.log
```

A bare IP means `root@`. With `--discover`, gateways are reached as `--gateway-user` (default
`root`) and everything else as `--ap-user` (or `$UNIFI_OVERHAUL_AP_USER`). The client only reads;
it prints commands and never runs them.

| Tag | Meaning |
|---|---|
| `LIVE` | root offers 2.4 GHz · leaf's uplink is on 2.4 GHz · relay does both |
| `REVERTED` | Back to stock; re-apply it |
| `PARTIAL` | A relay with only one side on 2.4 GHz; check its `status` |
| `UNREACHABLE` | ssh failed; the reason is shown, fix it before trusting the rest |

Exit status: `0` all live, `1` something needs action, `2` bad command line, `3` nothing was
probed (a missing input).

### Diagnostics

`--diagnostics` writes one log with everything worth knowing, so troubleshooting doesn't mean
running commands device by device:

- **From each device:** identity and firmware, `status`, its configs, past apply commands from
  shell history, network and bridge state, every wifi interface's link, stations, channel
  usage and cached scan results, kernel and system logs, and the per-boot logs kept on flash
  (why each boot ended, how long it was isolated). On the gateway, also the UniFi Network
  app's own records of devices, wifi networks and recent events.
- **From your computer:** its own wifi link (band, signal, rate), routes, and pings to each
  device and the internet.

Nothing it runs changes anything. Passwords, keys and wifi passphrases are replaced with
`<redacted>` before the log is written, and the run reports how many. MACs, IPs and hostnames
stay in: fine to share for troubleshooting, not to publish. It never overwrites an existing
log. Allow a minute or two per device.

---

## On-device modes

Run as `/tmp/unifi-overhaul.sh MODE [options]` on a device.

| Mode | Changes anything? | What it does |
|---|---|---|
| `status` (default) | no | This device's uplink, downlinks, role, and whether 2.4 GHz is live |
| `dryrun` | no | The exact config diff `apply` would make, plus the command to run it |
| `apply` | **yes** | Make the change (restarts wifi on this device for 20–30 s) |
| `rollback` | yes | Restore the backup taken by `apply` during this boot |
| `avail` | scans, after asking | Current uplink signal, then optionally scans the other bands |
| `help` | no | Usage |

Options: `--with-ap` (offer a 2.4 GHz network; use on every relay), `--role root|leaf|relay`
(override detection), `--channel N` (default 1, identical on every device), `--force` (skip
`avail`'s confirmation).

**`avail` can knock a weak link offline.** Scanning takes a radio off-channel, and on this WiFi
7 chip that can stall the whole chip. On a marginal or multi-hop link that can drop this AP and
every AP behind it until they're power-cycled. It shows the live signal first and asks before
scanning; on a weak link, read the signal from `status` instead.

### Reading `status`

```
[*] Place in mesh chain   (uplink -> self -> downlink; [local: iface=name] = how neighbours see this end)
      uplink    brave-otter (02:..:a1)   [2.4GHz -60dBm  rx/tx 3.8G/2.0G]   [local: vwiresta9=calm-heron]
      self      ap-hall (sunny-finch / 02:..:b0)   [RELAY]
      downlink  quiet-lynx (02:..:c3)   [2.4GHz -59dBm  rx/tx 137M/206M]   [local: vwireap2=noble-crane]
```

Every MAC gets a stable two-word name (a hash of the MAC picks an adjective and an animal), so
the same radio has the same name on every device. `[local: …]` is the name *the neighbour*
sees this device by, so it matches the neighbour's own `status`. `rx/tx` are negotiated link
rates, useful for spotting a bad hop but not a speed test.

---

## Is it worth it?

2.4 GHz is a trade: less peak throughput for more reach and a steadier link.

**Worth it** when an AP's 5 GHz uplink is weak or drops (worse than roughly −67 dBm), and what
you care about is reliability: video calls, gaming, browsing, smart-home devices. These need
low, steady latency, not bandwidth, so a slower but solid link often *feels* faster than a
fast flaky one.

**Not worth it** when the 5 GHz uplink is already strong (better than about −60 dBm), when the AP
carries heavy traffic like large transfers or several 4K streams, or when you can run a cable.
A wired backhaul beats this on every count.

**Measure before and after**, same device, same spot:

```sh
ping -c 100 1.1.1.1      # avg = latency, mdev = jitter, and packet loss: lower and flatter is better
```

Add a speed test, which will drop on 2.4 GHz, and a bufferbloat test
(`https://www.waveform.com/tools/bufferbloat`). Keep the change if latency, jitter and loss
improve and the speed is still enough for that AP. Otherwise roll back.

---

## Glossary

- **vwire:** Ubiquiti's hidden backhaul network, the AP-to-AP mesh link.
- **BSSID:** the MAC address of one network an AP broadcasts. Each vwire AP interface is one.
- **Uplink / downlink:** the link to this device's parent, and the links to devices connected
  through it.
- **Provision:** the controller pushing a device's full config. It replaces any manual change.
- **mca:** the UniFi agent on each device; it talks to the controller and manages the mesh
  uplink.

---

## Contributing

Issues and pull requests are welcome. Please run the tests before sending a change:

```sh
python3 -m unittest discover -s src/tests -t src
```

They take about 2 seconds and need only the standard library. Fake `ssh`, `ping` and `iw` in
`src/tests/fakebin` run the real `scripts/unifi-overhaul.sh` locally, so the ssh paths are
tested end to end without any UniFi hardware. Never commit a diagnostics log or anything else
taken from a real network; `logs/` is gitignored for that reason.

---

## License

[MIT](LICENSE) © notreese6

Not affiliated with or endorsed by Ubiquiti. This is an unofficial change to firmware
configuration: use it at your own risk, on your own equipment.
