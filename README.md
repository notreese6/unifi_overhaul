# unifi-overhaul — force a 2.4 GHz wireless backhaul on UniFi mesh APs

`unifi-overhaul.sh` switches a UniFi wireless mesh backhaul (Ubiquiti's hidden "vwire"
SSID) from 5 GHz to **2.4 GHz**, per device, at runtime. 2.4 GHz penetrates walls
far better, so a distant AP that can't hold a solid 5 GHz uplink often gets a
much stronger, more reliable link on 2.4 GHz — at the cost of lower throughput.

UniFi does not expose a "2.4 GHz backhaul" setting, so this edits the device's
live wireless config directly. **It is a non-persistent hack against a
controller-managed device — read the "Non-persistence" section before relying on
it.**

> Developed against UniFi WiFi 7 (Qualcomm IPQ5322) hardware — a wired gateway as
> the root and mesh APs as wireless nodes. Interface/radio names below (`wifi0`=2.4,
> `wifi1`=5, `wifi2`=6, `vwireapN`, `vwirestaN`) are from that platform; other
> models may differ. Nothing here is device-specific — the script autodetects.

---

## TL;DR recipe

Copy the script to each device (root SSH), then, **root first, then relays, then
leaves**:

```sh
# from your workstation (ideally wired to the gateway, so your session doesn't
# ride the backhaul you're about to change):
scp unifi-overhaul.sh root@<device-ip>:/tmp/

# on each device, in chain order:
/tmp/unifi-overhaul.sh status        # shows your chain (uplink/downlink), role, + the apply command
/tmp/unifi-overhaul.sh dryrun        # shows the config diff + the recommended apply line
nohup /tmp/unifi-overhaul.sh apply [--with-ap] > /tmp/vw.log 2>&1 &   # see "Applying"
# reconnect after ~30s:
cat /tmp/vw.log ; /tmp/unifi-overhaul.sh status
```

To undo a device: `/tmp/unifi-overhaul.sh rollback`, or just **reboot / power-cycle it**
(a reboot always restores the stock 5 GHz config).

---

## How it works

- The device's live wireless config is `/tmp/system.cfg` (on tmpfs). The script
  edits a copy and applies it via the firmware's own `syswrapper.sh apply-config
  <cfg> nopersist`.
- **Root (wired gateway):** *additive* — it adds a hidden 2.4 GHz vwire AP
  (`vwireap2`) so the gateway now offers backhaul on 2.4 GHz **and** keeps 5/6 GHz.
- **Leaf (wireless AP):** it **moves** the one uplink STA (`vwirestaN`) from the
  5 GHz radio to the 2.4 GHz radio. Its downlink APs are untouched.
- **Relay (a leaf that other APs mesh through):** same STA move **plus** it adds
  a 2.4 GHz downlink AP (`--with-ap`) so the APs below it can uplink on 2.4 GHz.
- Moving a vap between radios can't be done incrementally, so `apply` triggers a
  **full wifi restart** (~20–30s, all SSIDs on that AP drop and return). The root
  (additive) usually does a fast-apply with far less disruption.

`nopersist` means the change is **not** written to `/data`, so a reboot reverts it.

---

## Roles & the chain (shown by `status`)

Your mesh is a chain/tree from the wired gateway outward:

```
GATEWAY(root, wired) ── 2.4 ── AP-A(relay) ── 2.4 ── AP-B(leaf)
```

`status` shows this device's place in that chain — who it uplinks to (by name),
who meshes through it (downlink), and its **autodetected role**:

| Role | What it is | `apply` does |
|---|---|---|
| **ROOT** | Wired uplink; offers the backhaul | adds a 2.4 GHz downlink (keeps 5/6) |
| **LEAF** | Wireless uplink; nothing meshes through it | moves its uplink STA to 2.4 GHz |
| **RELAY** | Wireless uplink **and** feeds other APs | moves its uplink to 2.4 **and** adds a 2.4 downlink |

RELAY is detected by checking whether any of this AP's vwire downlink APs have a
station (another AP) associated. Because the role is autodetected, **plain `apply`
does the right thing for all three** — you don't pass `--with-ap` in the normal
flow. Only force `--with-ap` on a leaf if you *know* a downstream AP will mesh
through it but none is connected yet (so autodetect can't see it).

**Apply order matters:** each device needs its upstream already offering 2.4 GHz
before it can switch. So: **root → relays → leaves.** Do them one at a time and
confirm each with `status` before the next.

### Seeing the whole chain

A single device only knows **one hop each way** — its own uplink and the APs
directly meshed to it (the controller is the only thing that holds the full tree).
So there are two ways to see the topology:

- **Per-device (`status`):** shows the immediate neighbours as
  `uplink -> self -> downlink`, with the negotiated PHY rate on each hop:
  ```
  [*] Place in mesh chain   (uplink -> self -> downlink; [local: iface=name] = how neighbours see this end)
        uplink    brave-otter (02:..:a1)   [2.4GHz -60dBm  rx/tx 3.8G/2.0G]   [local: vwiresta9=calm-heron]
        self      ap-hall (sunny-finch / 02:..:b0)   [RELAY]
        downlink  quiet-lynx (02:..:c3)   [2.4GHz -59dBm  rx/tx 137M/206M]   [local: vwireap2=noble-crane]
  ```
  Each line names the neighbour, and `[local: iface=name]` is the name **that
  neighbour** sees this device by — so it lines up with the neighbour's own `status`:
  the uplink's `downlink` line will show `calm-heron`, and the downlink's `uplink`
  line will show `noble-crane`. The **`self`** line is this device's stable identity —
  its **hostname** plus the name of its base MAC (what `unifi-overhaul-client` keys on). Each vwire
  interface has its own MAC/name, so the per-hop names differ from the base name;
  that's expected. The `rx/tx` values are **PHY link rates** (negotiated, not
  throughput) — useful for spotting a hop that negotiated low, not a speed test.

- **Whole tree at once (`unifi-overhaul-client`, a separate Python tool for your workstation):**
  finds the devices, SSHes to each, probes it, and prints the assembled tree by
  **hostname**. It is deliberately not part of `unifi-overhaul.sh`, so it can never be run on a
  device (`unifi-overhaul.sh map` now just points here). It's the `unifi_overhaul` package in `src/`,
  run from the repo root with `./unifi-overhaul-client` (or `PYTHONPATH=src python3 -m unifi_overhaul`), and
  it pipes `scripts/unifi-overhaul.sh` to each device to probe it.
  ```
  ./unifi-overhaul-client --discover --ap-user <device-ssh-user>      # find everything automatically
  ./unifi-overhaul-client --discover --ap-user <user> --list          # just show what it found
  ./unifi-overhaul-client [user@]<gateway-ip> [user@]<ap-ip> ...      # or name the devices yourself
  ```
  **`--discover`** sends the Ubiquiti discovery broadcast (UDP 10001) and every UniFi device
  on your LAN answers with its IP, MAC, hostname and model. Gateways (model codes `UX`,
  `UDM`, `UDR`, `UCG`, `UXG`, `USG`, `UDW`…) are reached as `--gateway-user` (default
  `root`); everything else as `--ap-user`, the controller's Device-SSH user (or set
  `UNIFI_OVERHAUL_AP_USER`). A switch that answers is probed too, found to have no wifi radios,
  and skipped. If nothing answers, try `--broadcast <subnet>.255`. Named targets are
  `[user@]ip` (a bare IP means `root@`) and take precedence over discovered ones.
  Exit status: `0` all live, `1` something to re-apply or unreachable, `3` nothing probed
  (a missing input), so it can drive a cron job later.
  ```
  [*] mesh map (indented = uplinks to the line above; [band signal] is the uplink; [2.4: ...] = is the change live here)
  gateway (192.168.1.1)   (root, wired)   [2.4: LIVE]
      ap-hall (192.168.1.20)   <--[uplink 2.4GHz -58dBm]   [2.4: LIVE]
          ap-far (192.168.1.21)   <--[uplink 5GHz -70dBm]   [2.4: REVERTED]

  [*] 2.4 GHz backhaul live on 2 of 3 devices probed
  [-] re-apply these, in this order, waiting ~30s (and re-running this) between each:
        ap-far (192.168.1.21):  ssh admin@192.168.1.21 'cat > /tmp/unifi-overhaul.sh && ... apply ...' < ./unifi-overhaul.sh
  ```
  **Run `unifi-overhaul-client` after a trip, reboot or controller change** to see which nodes reverted.
  Each device gets a `[2.4: ...]` tag, judged from what is live on it right now:

  | Tag | Meaning |
  |---|---|
  | `LIVE` | root: a 2.4 GHz vwire AP is up · leaf: its uplink is on 2.4 GHz · relay: both |
  | `REVERTED` | back to stock 5 GHz — re-apply it |
  | `PARTIAL` | a relay with only one side on 2.4 GHz — check `status`, `rollback`/reboot, then re-apply |
  | `UNREACHABLE` | ssh failed — fix access and re-run before trusting the summary |

  Any device that isn't `LIVE` is listed with a ready-to-paste re-apply command, **in
  tree order** (root → relays → leaves), so you can work down the list. The command pipes
  the script over ssh and runs `apply` detached, so the ~20–30s wifi restart doesn't cut it
  off. A reverted relay gets `--with-ap`: if its children have already dropped, it would
  otherwise autodetect as a leaf and never offer them the 2.4 GHz downlink.
  It joins the tree by matching each device's uplink BSSID to whichever device owns
  that AP MAC. `unifi-overhaul-client` is **read-only** (it prints re-apply commands, never runs
  them) and needs SSH to each device (the controller's Device-SSH credentials); it pipes
  the script over SSH, so it doesn't need to be pre-copied. A device it can't SSH into is
  shown as `(x) … could not ssh in: <ssh's reason>`; one whose uplink is on a device that
  wasn't probed is flagged `(?) … uplink … on a device not probed`.

- **Everything at once, into one file (`unifi-overhaul-client --diagnostics`):** for
  troubleshooting, instead of running commands device by device. It writes the mesh map
  above plus every read-only fact from each device and from your workstation into one log:
  ```
  ./unifi-overhaul-client --discover --ap-user <user> --diagnostics             # logs/unifi-diagnostics-<timestamp>.log
  ./unifi-overhaul-client --discover --ap-user <user> --diagnostics --log mesh.log
  ```

  | From | What it collects |
  |---|---|
  | each device | identity and firmware; memory, processes; `status`; `/tmp/system.cfg`, `running.cfg` vs `system.cfg`, the persisted `mgmt` config, uplink state files, past `unifi-overhaul`/`vwire24` commands from shell history; addresses, routes, bridge, ethernet link speed, ping to its gateway; `iw dev`/`reg`/`phy`, per-interface `info`/`link`/`station dump`/`survey dump`/cached `scan dump`, `wlanconfig list sta`, supplicant/hostapd status, `mca-dump`; `logread`, `dmesg`, the per-boot logs kept on flash (boot reason, minutes isolated, mesh events); on a UniFi OS console also `journalctl` and the Network app's own device/WiFi/country/event records |
  | your workstation | OS, addresses, routes, DNS, its own wifi link (macOS `system_profiler`, Linux `iw`/`nmcli`), 10 pings to each device and to the internet |

  Nothing it runs scans, restarts, or changes config; the device side is `unifi-overhaul.sh`'s
  `__diag`, piped over ssh like the probe. Values that look like secrets (passphrases, PSKs,
  passwords, auth keys, the controller's `x_` fields) are replaced with `<redacted>`, and the
  run says how many. MACs, IPs and hostnames stay in, so the log is fine to share for
  troubleshooting but shouldn't be committed (`logs/` is gitignored). An existing log is never overwritten. Expect a
  minute or two per device; one that doesn't answer within 5 minutes is cut off, keeping what
  it printed, and the run exits `1`.

---

## Applying (and why `nohup … &`)

```sh
nohup /tmp/unifi-overhaul.sh apply [--with-ap] > /tmp/vw.log 2>&1 &
```

- **`nohup`** = "no hangup." The full wifi restart drops this AP's backhaul, so
  **your SSH to it will freeze and probably disconnect.** Normally a disconnect
  sends SIGHUP to the running command and kills it — mid-apply. `nohup` makes it
  ignore that, so `apply` finishes even if your session drops.
- **`> /tmp/vw.log 2>&1`** — send stdout **and** stderr to a file. Plain `>` only
  redirects stdout; `2>&1` adds stderr (the `syswrapper` provisioning messages
  come out there). Your terminal is about to freeze, so anything not in the file
  is lost.
- **`&`** — background it, so your shell returns immediately instead of hanging.

After ~30s, reconnect and `cat /tmp/vw.log ; /tmp/unifi-overhaul.sh status`.

Run apply from a connection that does **not** ride the AP you're changing —
ideally your workstation wired to the gateway. Then only that AP's SSH blips, not
your whole session.

`dryrun` prints this exact command (with `--with-ap` if you passed it) plus the
reason, so you don't have to remember it.

---

## Verifying — `status`

`status` is always safe (it only *reads* the live link, never scans). Good signs
after applying a leaf/relay:

- `2.4GHz backhaul in use here (live): YES`
- `vwirestaN -> <BSSID>  band:2.4GHz  signal:-XX dBm` (associated to the upstream
  AP's 2.4 GHz vwire)
- the AP still reachable, and downstream APs re-meshed

One cosmetic caveat: `mca-mesh-band` (mca's own label for the mesh band) can lag
after a manual switch — the live `iw link` read the chain shows is the truth.

### Readable MAC names

Every BSSID is shown with a memorable name in parentheses, e.g.
`<bssid> (brave-otter)`. The name is `md5(mac)` sliced into two chunks
that index an adjective and an animal list — so it's **deterministic and stable
mesh-wide**: the same MAC reads as the same name on every device. When one AP's
uplink shows `-> brave-otter` and another AP's interface list shows one of its
downlink APs as `brave-otter`, you know the first is uplinking to the second — no
need to memorize hex. Names appear in `status` (uplink, downlink, and the
interface list) and `avail`. (Hashing first matters: mesh MACs are often
near-sequential, and hashing decorrelates them so neighbors get very different
names. Widen `VW_ADJ`/`VW_ANI` at the top of the script for a bigger namespace.)

---

## `avail` — measuring the other bands (⚠ can drop a weak link)

`avail` shows your **current** uplink signal live (safe), then **prompts** before
scanning the bands you're *not* on to show what you'd get if you switched.

**The scan is the dangerous part.** On this WiFi 7 chip the radios are coupled, so
scanning any radio briefly stalls the whole chip. On a **weak or multi-hop**
backhaul that stall can drop the uplink — and knock out any AP that meshes through
this one — freezing them until a **physical power-cycle**. So `avail`:

- reads your current uplink live (no scan, always safe);
- shows a warning and requires you to type `yes`;
- then scans only the radios that are **not** carrying your uplink.

Run `avail` **interactively** (it waits for `yes`). Backgrounded/`nohup`, it can't
read your answer and safely aborts without scanning. `--force` skips the prompt
(accepting the risk). On a solid single-hop link the scan is usually fine; on a
marginal one, don't — use the live `status` number instead.

---

## Non-persistence — important

This **reverts** on:

1. **Reboot / power-cycle** — `/tmp/system.cfg` is on tmpfs and is regenerated on
   boot from `/data/udapi-config/unifi`.
2. **Any controller config change** — the UniFi controller is the source of truth.
   When you change a setting, it pushes a **full** config to the device (`mcad
   setparam -P <system.cfg> -V <cfgversion>`), which replaces the whole file and
   wipes this edit.

It does **not** revert on routine controller check-ins: editing `/tmp/system.cfg`
directly doesn't bump the device's saved `cfgversion`, so the controller sees no
mismatch and pushes nothing. In practice the change survives until a reboot or a
real config change.

**Reverting is safe** — a controller re-provision or reboot applies the original,
known-good 5 GHz config in full (not a broken half-state). So "losing" the hack
just means you're back to stock and re-run `apply` if you want it again.

There is **no supported way** to make the controller keep 2.4 GHz backhaul (it
doesn't expose the setting). Persisting it would require a re-apply hook that
fights the controller on every provision — fragile, not recommended. If you need a
permanent low-latency link, **wire the AP** (Ethernet backhaul) instead.

---

## Recovery

| Situation | Fix |
|---|---|
| Change looks wrong, AP still reachable | `/tmp/unifi-overhaul.sh rollback` (restores this session's backup + re-applies) |
| AP went silent after `apply` | wait ~1 min; if still dark, **power-cycle it** — it returns on 5 GHz |
| AP frozen after `avail` scan | same — wait, then power-cycle |
| Want everything back to stock | reboot the device, or change any setting in the UniFi app (forces a re-provision) |

Nothing here is written to persistent storage, so a reboot is always a clean
escape hatch.

---

## Modes

| Mode | Reads/writes | What it does |
|---|---|---|
| `status` (default) | read-only | your chain (uplink/downlink/role) + live backhaul state |
| `unifi-overhaul-client [--discover] [user@ip…]` | read-only | separate workstation tool: find + ssh each device, print the whole mesh tree, `LIVE`/`REVERTED` per device, and the ordered re-apply commands |
| `unifi-overhaul-client … --diagnostics [--log PATH]` | read-only | the same, plus every diagnostic from each device and the workstation in one log (secrets redacted) |
| `dryrun` | read-only | the exact config diff + the recommended (nohup) apply command |
| `avail` | scans (gated) | current uplink live, then prompts to scan other bands |
| `apply` | **writes+applies** | make the change (full wifi restart if a vap moves radios) |
| `rollback` | writes+applies | restore this session's pre-apply backup |
| `help` | read-only | usage |

Options: `--role root|leaf|relay` (override autodetect), `--channel N` (2.4 GHz
channel, default 1 — **must be identical on every device**), `--with-ap` (force
the leaf downlink), `--force` (skip the `avail` scan prompt).

---

## When is this actually worth it?

2.4 GHz is not "better" — it's a **trade**. You give up peak throughput for reach
and a stronger, steadier signal. Whether that's a win depends entirely on what's
bottlenecking you, so **measure before and after** (next section) rather than
guessing.

**Good candidates — use it when:**

- A remote AP's **5 GHz backhaul is weak or drops** — thick walls, floors,
  distance, or a marginal signal (roughly worse than about −67 dBm). 2.4 GHz
  penetrates far better and typically comes back several to ~10 dB stronger, so a
  flaky link becomes solid.
- You care about **reliability and consistency** over raw speed — a stable
  150 Mbps beats a multi-Gbps-rated link that keeps stalling.
- The AP mostly serves **latency-sensitive or light traffic**: game consoles,
  video calls, browsing, smart-home devices, phones.

**Poor candidates — don't bother (or revert) when:**

- The 5 GHz backhaul is already **strong and stable** (say better than −60 dBm) —
  you'd trade a lot of speed for margin you don't need.
- The AP carries **heavy throughput** — 4K streaming to many clients, large file
  transfers, backups. Keep 5 GHz.
- The AP can be **wired** (Ethernet backhaul). A cable beats any wireless tuning
  on every axis — do that instead if it's at all possible.

### Throughput vs. latency — why "slower" can *feel* faster

Two different things, often confused:

- **Throughput (Mbps)** — how much data per second. 2.4 GHz (20 MHz, congested)
  is genuinely **lower** than 5 GHz (80 MHz). For big downloads/streaming, that's
  simply slower — no spin.
- **Latency (ping) & jitter** — how quickly and *consistently* each packet makes
  the round trip. This is what most interactive things actually feel:
  - **Gaming**: games use tiny bandwidth (~1–10 Mbps) — Mbps is irrelevant. What
    matters is **low, steady ping and near-zero packet loss.** A strong 2.4 GHz
    link with few retransmissions can cut the lag spikes/rubber-banding a weak,
    flaky 5 GHz link was causing — even though the speed-test number drops.
  - **Web browsing**: pages are lots of small requests; they feel snappier with
    low latency than with high bandwidth. A steadier link *feels* faster to load.
  - **Video calls**: care about jitter and loss, not peak Mbps — a stable link
    holds the call together.

So a lower speed-test number with a stronger, steadier link is often a **net win**
for how the network *feels* — as long as nothing on that AP is throughput-bound.

### Measure before & after (don't guess)

Run the same tests on the **same device in the same spot**, once on 5 GHz and once
on 2.4 GHz, and compare:

- **Signal:** `status` shows the live uplink dBm. Stronger (less negative) is the
  first sign 2.4 is helping.
- **Latency & jitter (the important one):**
  ```
  ping -c 100 1.1.1.1
  ```
  Look at **avg** (latency), **mdev** (jitter — how much it wobbles), and **packet
  loss**. Lower and flatter is better. Do it *while gaming / on a call* for a real
  picture.
- **Latency under load (bufferbloat):** `https://www.waveform.com/tools/bufferbloat`
  or `https://speed.cloudflare.com` — shows how much latency spikes during a
  transfer. That spike is what makes a "fast" link feel laggy.
- **Throughput sanity check:** any speed test (`https://fast.com`,
  `https://speedtest.net`) — expect this to *drop* on 2.4 GHz. That's fine unless
  it drops below what the AP's clients actually need.

If ping/jitter/loss improve and throughput is still enough for what that AP does,
keep it. If not, `rollback` (or reboot) and leave it on 5 GHz.

---

## Glossary

- **vwire** — Ubiquiti's hidden wireless-backhaul SSID (the AP-to-AP mesh link).
- **BSS** (Basic Service Set) — one AP's network: the AP plus the devices associated
  to it, identified by a **BSSID** (the AP's MAC). "vwire BSS" = the backhaul network
  an AP broadcasts on a given radio. Each vwire AP (`vwireapN`) is one BSS.
- **WDS** (Wireless Distribution System) — the method for bridging traffic between
  APs *over Wi-Fi*. It uses **4-address** frames (you'll see `4addr: on`) that carry
  the real source/dest **plus** both APs' MACs, so the hop bridges transparently.
  A UniFi wireless mesh backhaul is a WDS link — hence `system.uplink=wds`.
- **mca** — **Management Console Agent** (per the firmware's own package description
  and `mcad` copyright string). The on-device UniFi agent (`mcad` = its daemon). It talks to the
  controller (the "inform" protocol), **manages the wireless uplink/mesh** (which
  AP/band to backhaul on, link monitoring, reconvergence), reports stats, and applies
  pushed config. The `/var/run/system.*`, `uplink_wds`, and `system.mesh` files are
  mca's runtime state. `mca-mesh-band` is just mca's *label* for the mesh band — it
  does **not** control the link, and it goes stale after a manual switch (harmless).
- **root / leaf / relay** — a device's place in the tree: root = wired gateway that
  offers the backhaul; leaf = wireless AP with nothing meshed through it; relay = a
  leaf that other APs mesh through.
- **gateway_mac** (in `status`) — the mesh **root's** MAC (final L3 hop). On a
  multi-hop leaf this is *not* the immediate wireless uplink (that's in the chain).

## Requirements & notes

- Root-level SSH on each device (the controller's Device-SSH login — note the
  **gateway and APs may use different usernames**); `iw`, `awk`, `sed`, `grep`,
  `md5sum` (all present on the stock firmware); `busybox ash` shell (POSIX `sh`).
- `unifi-overhaul-client` runs on your workstation only: Python 3.8+ (standard library only)
  and an `ssh` client. Layout: `scripts/unifi-overhaul.sh` (on-device), `src/unifi_overhaul/`
  (the workstation package), `src/tests/` (its tests), `logs/` (diagnostics output, gitignored).
- Tests (standard library `unittest`, no install, ~2 s; fakes for `ssh`/`ping`/`iw` in `src/tests/fakebin`
  run the real `unifi-overhaul.sh` locally): `python3 -m unittest discover -s src/tests -t src`
- `--channel` must match across every device (one radio = one channel; both ends
  of a hop must be on the same 2.4 GHz channel).
- This is an unofficial modification of Ubiquiti firmware config. Use at your own
  risk on your own equipment.
