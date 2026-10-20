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
| `./unifi-overhaul-client` | Mesh map, `--apply`, gateway self-heal, and full diagnostics, over ssh | your computer |
| `mesh.example.ini` | Template for describing your mesh | copy to `~/.config/unifi-overhaul/` |
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
gateway's `~/.ssh/authorized_keys`. It survives reboots and, on the Express 7 tested, firmware
updates. If `ssh gateway` ever asks for a password again, re-run the command.

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

### 1. Describe your mesh (once)

Copy `mesh.example.ini` to `~/.config/unifi-overhaul/mesh.ini` and fill it in. It lives outside
the repo because it holds your own addresses. One section per device; give each AP the device
it should connect through:

```ini
[mesh]
channel = 1

[gateway]
host = <gateway-ip>
user = root

[ap-hall]
host   = ap-hall          # an IP, or an alias from ~/.ssh/config
parent = gateway

[ap-far]
host   = ap-far
parent = ap-hall
```

Leave `user` out to let `~/.ssh/config` choose it. Roles follow from the tree: the device with no
parent is the root, an AP with children is a relay, and one without is a leaf.

### 2. Look at the mesh

```sh
./unifi-overhaul-client
```

```
[*] mesh map (indented = uplinks to the line above; [band signal] is the uplink; [2.4: ...] = is the change live here)
gateway (192.168.1.1)   (root, wired)   [2.4: LIVE]
    ap-hall (192.168.1.20)   <--[uplink 2.4GHz -60dBm]   [2.4: LIVE]
    ap-far (192.168.1.21)   <--[uplink 2.4GHz -78dBm]   [2.4: LIVE]

[-] ap-far is connected to gateway, mesh.ini says ap-hall -- run --apply
[-] ap-far's uplink is weak (-78 dBm, below -75)
```

Indentation is the real tree: each device sits under the one it's connected to. Here `ap-far`
has gone around its relay to the gateway, which `mesh.ini` makes visible.

### 3. Apply

```sh
./unifi-overhaul-client --apply
```

```
[*] plan (parents first):
      gateway (root): ok -- offering 2.4 GHz
      ap-hall (relay): apply -- not offering 2.4 GHz to ap-far
      ap-far (leaf): reboot -- connected to gateway, mesh.ini says ap-hall
Make these changes? [y/N]
```

It shows the plan and asks first. Then it works down the tree, parents first, and after each
change waits until that device matches `mesh.ini` before touching the next. If a device doesn't
settle, it stops there and says why, leaving everything below it alone. A device that's already
right is skipped, so `--apply` is safe to run any time: after a power cut, a trip, or whenever
the map shows something wrong. `--yes` skips the question.

It also points each AP at its `mesh.ini` parent. An AP's config names a preferred parent
(`mesh.serial1`), which its uplink weighs when choosing between parents. The controller fills
that in from whichever parent the AP was last connected to, so an AP that once latched onto the
wrong parent keeps being nudged back to it. `--apply` sets it to the right one, and reboots an AP
that's still on the wrong parent so it chooses again.

Each change restarts wifi on that device for 20–30 seconds, so run it from a computer that isn't
connected *through* the APs being changed, ideally one wired to the gateway.

### Without mesh.ini

Name the devices instead (`./unifi-overhaul-client root@<gateway-ip> <user>@<ap-ip> ...`). The map
then prints a ready-to-paste command for each device that needs applying, in order, like this:

```sh
ssh gateway 'cat > /tmp/unifi-overhaul.sh && chmod +x /tmp/unifi-overhaul.sh &&
  nohup /tmp/unifi-overhaul.sh apply < /dev/null > /tmp/vw.log 2>&1 &' < scripts/unifi-overhaul.sh
```

Run them one at a time, root first, re-running the map in between. Use `apply --with-ap` on every
relay, for the reason in [Roles](#roles).

---

## Roles

| Role | What it is | `apply` does |
|---|---|---|
| **root** | The wired gateway | Adds a 2.4 GHz backhaul network (keeps 5/6 GHz) |
| **relay** | A wireless AP that other APs connect through | Moves its uplink to 2.4 GHz **and** offers a 2.4 GHz network |
| **leaf** | A wireless AP with nothing connected through it | Moves its uplink to 2.4 GHz |

With `mesh.ini`, roles come from the tree you wrote down. Without it, the script works the role
out from what's connected *at that moment*, **which is a trap after an outage**. A relay re-applied
while the APs below it are still disconnected looks like a leaf and offers no 2.4 GHz network.
The APs below then either can't connect, or reach past it to a weaker parent. So pass `--with-ap`
to every relay when applying by hand.

`apply` only adds what's missing, so running it again on a device that's already applied
changes nothing.

The whole 2.4 GHz chain must share one channel (`channel` in `mesh.ini`, or `--channel N`;
default 1), because a relay's single 2.4 GHz radio is both its uplink and the network it offers.
Changing the channel of a mesh that's already applied isn't automated yet, so keep `channel` set
to the one your mesh is on.

---

## What survives what

| Event | APs | Gateway |
|---|---|---|
| `apply`, then the AP's uplink reconnects | **Saved:** the AP writes its running config to flash whenever its uplink comes up | n/a |
| AP reboot | Keeps the change, if it was saved | n/a |
| Gateway reboot or power cut | Keep theirs, and look for a 2.4 GHz parent | **Loses it.** Re-apply right away (or let [self-heal](#gateway-self-heal) do it), or the APs stay isolated |
| Any controller provision: a settings change, firmware update, re-adoption, plugging an AP into Ethernet | **Back to stock 5 GHz**, and that gets saved | Back to stock (self-heal re-applies it within 5 minutes) |

The dangerous case is a provision on an AP whose **stock 5 GHz link can't reach its parent**,
which is exactly the kind of AP this project is for. It drops off the network immediately and
can't be reached over ssh to fix. Avoid site-wide UniFi changes while the mesh is applied.
Change per-device settings only on APs that can still connect on 5 GHz.

### Gateway self-heal

The gateway is the one device that forgets the change on every reboot, and it's always reachable,
so it can fix itself:

```sh
./unifi-overhaul-client --install-self-heal     # asks first; reads the gateway and channel from mesh.ini
./unifi-overhaul-client --remove-self-heal
```

This installs a systemd timer on the gateway that re-applies the root 90 seconds after every boot
and every 5 minutes after that. Because `apply` changes nothing when the 2.4 GHz network is already
live, a run that finds nothing wrong does nothing. At boot the gateway's radio manager is still
applying its own config and refuses changes, so `apply` waits (up to 2 minutes) for it to go idle
first; if a change still fails, nothing is left half-done and the next run tries again. It also
keeps a copy of root's `authorized_keys` and puts back any key a firmware update removes.

| On the gateway | What it is |
|---|---|
| `/data/unifi-overhaul/` | A copy of the script, the channel, and the key snapshot. `/data` survives firmware updates |
| `/etc/systemd/system/unifi-overhaul-heal.{service,timer}` | The timer. UniFi OS keeps its root filesystem's changes across firmware updates on the Express 7 tested, so these should survive one. A factory reset removes everything |

The map shows its state on the gateway's line (`[self-heal: active]`). Check it after the
gateway's next reboot, and after a firmware update, to confirm the timer survived; if it shows
`not installed`, run `--install-self-heal` again. Its log: `journalctl -u unifi-overhaul-heal`.
`--remove-self-heal` deletes exactly the files above. If anything else has ended up in
`/data/unifi-overhaul/`, it leaves the folder and says so.

Self-heal only fixes the gateway. APs that a provision put back on stock 5 GHz still need
`--apply`, and one out of 5 GHz range still needs the recovery below.

---

## Recovery

| Situation | Fix |
|---|---|
| Anything shows `REVERTED`, or the map flags a wrong parent | `./unifi-overhaul-client --apply` |
| APs isolated after a power cut | Wait 2 minutes if self-heal is installed; otherwise `--apply`, which fixes the gateway first and then waits for the APs to come back on 2.4 GHz |
| An AP stays unreachable (provisioned back to 5 GHz, out of 5 GHz range) | `--apply` stops at it. Move it next to its parent, or wire it temporarily, run `--apply`, then put it back |
| You want stock back on one device | `rollback` (only from the same boot as the first `apply`), or force a re-provision of that device from the UniFi app |

---

## The client

```sh
./unifi-overhaul-client                               # mesh map for the devices in mesh.ini, plus warnings
./unifi-overhaul-client --apply [--yes]               # make the mesh match mesh.ini
./unifi-overhaul-client --install-self-heal           # let the gateway re-apply itself (--remove-self-heal undoes)
./unifi-overhaul-client [user@]ip ...                 # mesh map for these devices instead
./unifi-overhaul-client --discover --ap-user USER     # find the devices instead of listing them
./unifi-overhaul-client ... --list                    # show what would be probed, connect to nothing
./unifi-overhaul-client ... --diagnostics             # everything, into logs/unifi-diagnostics-<time>.log
```

`--mesh PATH` reads a different `mesh.ini`. A bare IP on the command line means `root@`.

`--discover` sends Ubiquiti's discovery broadcast, then asks the controller on any gateway that
answers for every device it has adopted, since APs don't always answer the broadcast themselves.
Gateways are reached as `--gateway-user` (default `root`) and everything else as `--ap-user` (or
`$UNIFI_OVERHAUL_AP_USER`).

Only `--apply` and the self-heal options change anything; every other mode only reads.

| Tag | Meaning |
|---|---|
| `LIVE` | root offers 2.4 GHz · leaf's uplink is on 2.4 GHz · relay does both |
| `REVERTED` | Back to stock; re-apply it |
| `PARTIAL` | A relay with only one side on 2.4 GHz; check its `status` |
| `UNREACHABLE` | ssh failed; the reason is shown, fix it before trusting the rest |

Exit status: `0` all live (or `--apply` finished), `1` something needs action (or `--apply`
stopped), `2` bad command line, `3` nothing was probed (a missing input, or you declined the plan).

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
| `dryrun` | no | The exact config diff `apply` would make (or that it would change nothing) |
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
