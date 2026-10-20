#!/bin/sh
# unifi-overhaul.sh — force a 2.4 GHz UniFi wireless backhaul (vwire) alongside the 5 GHz one.
# Edits /tmp/system.cfg + applies via syswrapper. An AP saves it to flash once its uplink reconnects;
# the gateway loses it on reboot; any controller provision puts stock 5 GHz back. See README.
#   ./unifi-overhaul.sh status|dryrun|apply|rollback|avail|help  [--role root|leaf|relay] [--channel N] [--with-ap]
# Role autodetected: ROOT (wired, adds a 2.4 downlink), LEAF (moves its uplink STA to 2.4),
# RELAY (leaf that feeds other APs: moves uplink AND adds a 2.4 downlink). 'status' shows the chain.
# Apply order: ROOT first, then RELAYS, then LEAVES.
set -u
# The UNIFI_OVERHAUL_* overrides exist for the tests, which run this script against temporary files and fakes
CFG=${UNIFI_OVERHAUL_CFG:-/tmp/system.cfg}; BAK=$CFG.unifi-overhaul.bak; NEW=$CFG.unifi-overhaul.new
PREV=$CFG.unifi-overhaul.prev
SYSWRAPPER=${UNIFI_OVERHAUL_SYSWRAPPER:-/usr/bin/syswrapper.sh}
# The firmware's runtime state (uplink, mesh status); the tests point it at a temporary directory
RUN_DIR=${UNIFI_OVERHAUL_RUN:-/var/run}
# How long apply waits for a busy radio manager, and how often it checks (seconds)
RADIO_WAIT=${UNIFI_OVERHAUL_RADIO_WAIT:-120}; RADIO_POLL=${UNIFI_OVERHAUL_RADIO_POLL:-5}
# Self-heal on the gateway; /data survives firmware updates on UniFi OS. Overridable for the tests too.
HEAL_DIR=${UNIFI_OVERHAUL_DATA:-/data/unifi-overhaul}; UNIT_DIR=${UNIFI_OVERHAUL_UNITS:-/etc/systemd/system}
AUTH_KEYS=${UNIFI_OVERHAUL_AUTH_KEYS:-/root/.ssh/authorized_keys}; UNIT=unifi-overhaul-heal
# The only files self-heal creates in HEAL_DIR; removal deletes exactly these and nothing else
HEAL_FILES="unifi-overhaul.sh settings authorized_keys"
# The device identity file (base MAC); overridable for the tests
BOARD=${UNIFI_OVERHAUL_BOARD:-/proc/ubnthal/board}
CH=1; ROLE=""; WITH_AP=0; FORCE=0; PARENT=""
MODE="${1:-status}"; [ $# -gt 0 ] && shift
# the whole-mesh view lives in the python unifi-overhaul-client, which runs only on your workstation
case "$MODE" in map) echo "[-] map moved to unifi-overhaul-client (python, run it on your workstation, not on a device)"; exit 2 ;; esac
while [ $# -gt 0 ]; do case "$1" in
  --role) ROLE="$2"; shift 2 ;; --channel) CH="$2"; shift 2 ;; --with-ap) WITH_AP=1; shift ;;
  --parent) PARENT=$(echo "$2" | tr A-F a-f); shift 2 ;;
  --force) FORCE=1; shift ;;
  *) echo "[-] unknown arg: $1"; exit 2 ;; esac; done

usage(){ cat <<EOF
unifi-overhaul.sh — add a 2.4 GHz UniFi wireless backhaul (vwire) alongside the 5 GHz one.
An AP keeps it across reboots once saved; the gateway loses it on reboot; a controller
provision reverts everything. See the README's 'What survives what'.

Usage: unifi-overhaul.sh [MODE] [options]
  MODE (default: status):
    status     the chain (uplink / downlink / role), what's in use, backhaul state  (read-only)
    (whole-mesh view: run unifi-overhaul-client on your WORKSTATION -- it finds the devices, probes each,
     and shows per device whether the 2.4 GHz change is LIVE or REVERTED + the re-apply order;
     its --diagnostics collects everything from every device into one log file)
    avail      show current uplink signal (live), then PROMPT to scan other bands     (scan can drop a weak link!)
    dryrun     show the exact config diff + the recommended apply command             (safe)
    apply      make the change (full wifi restart if a vap moves radios)
    rollback   restore this session's pre-apply backup + re-apply
    help       show this text
  options:
    --role root|leaf|relay   override the autodetected role
    --channel N              2.4 GHz channel (default 1); MUST match on every device
    --parent MAC             prefer this parent (its base MAC, as `status` shows it) when picking an uplink
    --with-ap                offer a 2.4 GHz downlink; use on every relay (detection can't see APs
                             that aren't connected yet, e.g. after an outage)
    --force                  skip the avail scan confirmation prompt

Role (autodetected, shown by 'status'): ROOT = wired gateway, adds a 2.4 downlink (keeps 5/6).
LEAF = wireless uplink, moves its uplink STA to 2.4. RELAY = a leaf that other APs mesh through:
moves its uplink AND adds a 2.4 downlink. Apply order: ROOT first, then RELAYS, then LEAVES.
EOF
}
log(){ echo "$@"; }
val(){ awk -F= -v k="$1" '$1==k{print $2; exit}' "$2"; }
maxidx(){ awk -F. -v p="$1" '$1==p{print $2+0}' "$2" | sort -n | tail -1; }
idx_of(){ awk -F'[.=]' -v p="$1" -v d="$2" '$1==p&&$3=="devname"&&$4==d{print $2; exit}' "$3"; }
free_virtual(){ last=$(awk -F. -v r="$1" '$1=="radio"&&$2==r&&$3=="virtual"{print $4+0}' "$2"|sort -n|tail -1); echo $(( ${last:-0} + 1 )); }
set_kv(){ e=$(echo "$1"|sed 's/\./\\./g'); if grep -q "^$e=" "$3"; then sed -i "s/^$e=.*/$1=$2/" "$3"; else echo "$1=$2" >> "$3"; fi; }
# deterministic memorable name from a MAC: md5(mac) -> two hash chunks -> index adjective + animal.
# same MAC -> same name on every device, so a BSSID reads the same mesh-wide (e.g. "brave-otter").
VW_ADJ="amber azure brave brisk calm clever cobalt coral cosmic crimson dapper eager fuzzy gentle glossy golden happy jade jolly keen lively lucky mellow merry nimble noble olive plucky proud quick quiet rapid rustic sable sandy shiny silent silver sleek snappy sober solar spry stout sunny swift teal tidy vivid witty zesty"
VW_ANI="otter fox lynx hawk wren finch heron egret bison moose stoat vole shrew gecko koala tapir dingo raven crane robin marten badger beaver ferret weasel osprey falcon kite ibis crow puma ocelot serval genet civet coati lemur pika hare mink quail swan teal viper wolf yak zebu newt owl"
mac_name(){
  case "$1" in ""|00:00:00:00:00:00) echo "-"; return;; esac
  m=$(printf '%s' "$1" | tr 'A-F' 'a-f')
  h=$(printf '%s' "$m" | md5sum 2>/dev/null | cut -c1-12)
  [ -n "$h" ] || h=$(printf '%s' "$m" | tr -cd '0-9a-f')   # fallback: raw mac hex
  na=$(echo $VW_ADJ | wc -w); nn=$(echo $VW_ANI | wc -w)
  ai=$(( (0x$(echo "$h" | cut -c1-6) % na) + 1 ))
  ni=$(( (0x$(echo "$h" | cut -c7-12) % nn) + 1 ))
  printf '%s-%s' "$(echo $VW_ADJ | cut -d' ' -f$ai)" "$(echo $VW_ANI | cut -d' ' -f$ni)"
}
band_of(){ f=$1; case "$f" in ''|*[!0-9]*) echo "?"; return;; esac; if [ "$f" -lt 2500 ]; then echo 2.4GHz; elif [ "$f" -lt 5925 ]; then echo 5GHz; else echo 6GHz; fi; }
# per-iface facts from the whole-radio `iw dev` tree — per-iface `info` omits channel on secondary vaps
iwtree(){ iw dev 2>/dev/null; }
freq_of(){ iwtree | awk -v w="$1" '$1=="Interface"{c=$2} $1=="channel"&&c==w{g=$3;gsub(/[()]/,"",g);print g;exit}'; }
link_freq(){ iw dev "$1" link 2>/dev/null | awk '$1=="freq:"{print $2; exit}'; }
rate_h(){   # normalize a PHY-rate token to human Mbps/Gbps. "3766.4" (iw MBit/s) or "206M"/"1.2G" -> "3.8G"/"206M".
  case "$1" in ""|-|"?") echo "?"; return;; esac
  # iw gives a bare number in MBit/s; wlanconfig already carries an M/G suffix -- normalize both to Mbps then format
  awk -v r="$1" 'BEGIN{ n=r+0; if(r ~ /[Gg]/) n=n*1000; if(n>=1000) printf "%.1fG", n/1000; else printf "%dM", n+0.5 }'
}

list_downstream(){   # "apIface apMac apBand staMac staRSSI" per downstream AP meshed to one of our vwire APs.
  # QCA 4addr mesh peers appear in `wlanconfig <ap> list sta` (col1=MAC, col6=RSSI dBm),
  # NOT in `iw station dump` on this driver.
  for dn in $(iwtree | awk '$1=="Interface"{i=$2} $1=="type"{ if($2=="AP" && i ~ /^vwireap/) print i }'); do
    [ -e "/sys/class/net/$dn" ] || continue
    apm=$(cat /sys/class/net/$dn/address 2>/dev/null); ab=$(band_of "$(freq_of "$dn")")
    # wlanconfig cols: 1=MAC 4=TXRATE 5=RXRATE 6=RSSI
    rows=$(wlanconfig "$dn" list sta 2>/dev/null | awk 'NR>1 && length($1)==17 && $1 ~ /^[0-9a-fA-F:]+$/{print $1" "$6" "$4" "$5}')
    # a marginal 4addr peer ages out of `list sta`; if the bridge still shows a downstream on this AP,
    # poll a few times to catch it so we can NAME it (not just report "present")
    if [ -z "$rows" ] && bridge fdb show 2>/dev/null | awk -v d="$dn" '$3==d && $0!~/permanent/{f=1} END{exit !f}'; then
      i=0; while [ $i -lt 4 ]; do i=$((i+1)); sleep 1
        rows=$(wlanconfig "$dn" list sta 2>/dev/null | awk 'NR>1 && length($1)==17 && $1 ~ /^[0-9a-fA-F:]+$/{print $1" "$6" "$4" "$5}')
        [ -n "$rows" ] && break; done
    fi
    [ -n "$rows" ] || rows=$(iw dev "$dn" station dump 2>/dev/null | awk '/^Station/{m=$2} $1=="signal:"{print m" "$2" - -"}')
    printf '%s\n' "$rows" | while read mac sig txr rxr; do [ -n "$mac" ] && echo "$dn $apm $ab $mac ${sig:--} ${rxr:--} ${txr:--}"; done
  done | awk '!seen[$4]++'
}
count_sta_ifaces(){ iwtree | awk '$1=="Interface" && $2 ~ /^vwireap.*\.sta/{c++} END{print c+0}'; }
fdb_downstream(){   # vwire APs with a downstream MAC learned in the bridge -- stable even when a 4addr
  # peer ages out of `wlanconfig list sta`. vwire APs are hidden backhaul-only, so any non-self entry
  # here is mesh traffic, not a client.
  for dn in $(iwtree|awk '$1=="Interface"{i=$2}$1=="type"{if($2=="AP"&&i~/^vwireap/)print i}'); do
    n=$(bridge fdb show 2>/dev/null | awk -v d="$dn" '$3==d && $0 !~ /permanent/{c++} END{print c+0}')
    [ "${n:-0}" -gt 0 ] && echo "$dn"
  done
}
do_probe(){   # machine-readable one-device summary for unifi-overhaul-client (run remotely via ssh). No detect needed.
  echo "H $(cat /proc/sys/kernel/hostname 2>/dev/null || echo unknown)"
  # radio count lets unifi-overhaul-client skip a discovered switch instead of calling it a reverted root
  w=$(iw phy 2>/dev/null | grep -c "^Wiphy"); [ "$w" -gt 0 ] || w=$(ls -d /sys/class/net/wifi[0-9] 2>/dev/null | wc -l)
  echo "W $w"
  echo "S $(heal_state)"
  # Base MAC, and the parent this device's config prefers (mesh.serial1, which the uplink choice weighs)
  echo "B $(awk -F= '/^hwaddrbbase=/{print $2}' "$BOARD" 2>/dev/null | tr A-F a-f)"
  echo "P $(val mesh.serial1 "$CFG" 2>/dev/null | tr A-F a-f)"
  up=$(cat $RUN_DIR/system.uplink 2>/dev/null); st=$(cat $RUN_DIR/system.uplink_ifname 2>/dev/null)
  if [ "$up" = eth ] || [ -z "$st" ]; then echo "U WIRED - -"
  else
    l=$(iw dev "$st" link 2>/dev/null)
    b=$(echo "$l"|awk '/Connected to/{print $3;exit}'); f=$(echo "$l"|awk '$1=="freq:"{print $2;exit}'); s=$(echo "$l"|awk '$1=="signal:"{print $2;exit}')
    echo "U ${b:-NONE} $(band_of "$f") ${s:--}"
  fi
  iwtree | awk '$1=="Interface"{i=$2} $1=="type"{ if($2=="AP" && i ~ /^vwireap/) print i }' | while read dn; do
    echo "A $(cat /sys/class/net/$dn/address 2>/dev/null) $(band_of "$(freq_of "$dn")")"
  done
}

# ---- __diag: every read-only fact worth having, for unifi-overhaul-client --diagnostics ----
# Nothing here scans, restarts, or changes config. Secrets are redacted by the client before it writes the log.
have(){ command -v "$1" >/dev/null 2>&1; }
# each command gets /dev/null as stdin, so none can read from the ssh session or wait on it for input
diag_run(){ printf '\n===== %s\n' "$1"; ( eval "$1" ) </dev/null 2>&1; printf -- '----- rc=%s\n' "$?"; }
diag_section(){ printf '\n\n########## %s ##########\n' "$1"; }
DIAG_EVENTS="configure_uplink|need_cfg_save|cfgmtd|STA_ASSOC|uplink-monitor|isolat|m3_dump|assert|recover|apply-config|provision|inform"
diag_state_files(){
  for f in $RUN_DIR/system.uplink $RUN_DIR/system.uplink_ifname $RUN_DIR/system.uplink.prev $RUN_DIR/uplink_eth \
           $RUN_DIR/uplink_wds $RUN_DIR/uplink_bridge $RUN_DIR/mesh.status $RUN_DIR/need_cfg_save; do
    [ -e "$f" ] && { echo "--- $f"; cat "$f"; }
  done; return 0; }
diag_shell_history(){   # the exact apply commands run before, e.g. which --channel
  grep -hE "unifi-overhaul|vwire24" /tmp/log/ash_history /root/.ash_history /root/.bash_history \
    "$HOME/.ash_history" "$HOME/.bash_history" 2>/dev/null | tail -n 80; }
diag_ethernet(){
  for p in /sys/class/net/eth*; do [ -e "$p" ] || continue; n=${p##*/}
    echo "--- $n carrier=$(cat $p/carrier 2>/dev/null) speed=$(cat $p/speed 2>/dev/null) duplex=$(cat $p/duplex 2>/dev/null)"
    have ethtool && ethtool "$n" 2>&1
  done; return 0; }
diag_ping_gateway(){
  gw=$(ip route 2>/dev/null | awk '$1=="default"{print $3; exit}')
  [ -n "$gw" ] || { echo "no default route"; return 1; }
  ping -c 10 -W 2 "$gw"; }
diag_supplicants(){   # uplink STA and vwire AP state as wpa_supplicant/hostapd see it (status only; no keys)
  for d in $RUN_DIR/wpa_supplicant*; do [ -d "$d" ] || continue
    for s in "$d"/*; do echo "--- wpa_cli -p $d -i ${s##*/} status"; wpa_cli -p "$d" -i "${s##*/}" status 2>&1; done
  done
  for d in $RUN_DIR/hostapd*; do [ -d "$d" ] || continue
    for s in "$d"/*; do echo "--- hostapd_cli -p $d -i ${s##*/} status / all_sta"
      hostapd_cli -p "$d" -i "${s##*/}" status 2>&1; hostapd_cli -p "$d" -i "${s##*/}" all_sta 2>&1; done
  done; return 0; }
diag_boot_history(){   # per-boot logs kept on flash: why each boot ended and the mesh events in it
  for d in /tmp/log/boot/history/*/; do [ -d "$d" ] || continue
    echo "=== $d  reason=$(cat "$d"boot_reason 2>/dev/null)  version=$(cat "$d"version 2>/dev/null)"
    echo "    first: $(head -n 1 "$d"messages 2>/dev/null | cut -c1-24)   last: $(tail -n 1 "$d"messages 2>/dev/null | cut -c1-24)"
    echo "    isolated ~$(( $(grep -c 'uplink addr is null' "$d"messages 2>/dev/null) * 15 / 60 )) min (15 s per 'uplink addr is null')"
    grep -m3 -E "vwiresta[0-9]+ create wlandev" "$d"rc 2>/dev/null
    grep -hiE "$DIAG_EVENTS" "$d"messages 2>/dev/null | cut -c1-260 | tail -n 80
  done; return 0; }
diag_controller(){   # the UniFi Network app's own view (gateway/console only), read-only, secret fields left out
  m=$(command -v mongo || command -v mongosh) || { echo "no mongo client -- not a UniFi console, skipped"; return 0; }
  for q in 'db.device.find({}, {name: 1, mac: 1, ip: 1, model: 1, version: 1, state: 1, adopted: 1, cfgversion: 1, uplink: 1, last_uplink: 1, radio_table: 1, mesh_sta_vap_enabled: 1}).forEach(printjson)' \
           'db.wlanconf.find({}, {x_passphrase: 0, x_iapp_key: 0, private_preshared_keys: 0, sae_psk: 0}).forEach(printjson)' \
           'db.setting.find({key: {$in: ["country", "connectivity", "mgmt", "radio_ai", "network_optimization"]}}, {x_ssh_password: 0, x_ssh_sha512passwd: 0, x_mgmt_key: 0}).forEach(printjson)' \
           'db.site.find().forEach(printjson)' \
           'db.event.find().sort({time: -1}).limit(150).forEach(printjson)' \
           'db.alarm.find().sort({time: -1}).limit(50).forEach(printjson)'; do
    echo "--- $q"; $TO "$m" --quiet --port 27117 ace --eval "$q" 2>&1
  done; }
# __devices: the adopted devices the UniFi Network app knows about, one "D ip mac type model name" line each
# (tab-separated), for unifi-overhaul-client --discover. Read-only; only a console runs the app.
do_devices(){
  m=$(command -v mongo || command -v mongosh) || { echo "E no mongo client -- not a UniFi console"; return 0; }
  $m --quiet --port 27117 ace --eval 'db.device.find({adopted: true}, {ip: 1, mac: 1, type: 1, model: 1, name: 1}).forEach(function (d) { print(["D", d.ip, d.mac, d.type, d.model, d.name].join("\t")); })' 2>&1
}
heal_state(){   # the gateway self-heal timer: active, inactive (installed but stopped), or absent
  { have systemctl && [ -f "$UNIT_DIR/$UNIT.timer" ]; } || { echo absent; return 0; }
  systemctl is-active "$UNIT.timer" 2>/dev/null || true; }
do_install(){   # gateway only: re-apply the root's 2.4 GHz backhaul after every boot and controller provision
  { have systemctl && [ -d "$(dirname "$HEAL_DIR")" ]; } || { log "[-] no systemd or no $(dirname "$HEAL_DIR") here -- self-heal is for the UniFi OS gateway only"; exit 1; }
  if [ "$(cat $RUN_DIR/system.uplink 2>/dev/null)" = wds ]; then log "[-] this device uplinks wirelessly -- self-heal is for the wired gateway only"; exit 1; fi
  [ -f "$HEAL_DIR/unifi-overhaul.sh" ] || { log "[-] $HEAL_DIR/unifi-overhaul.sh missing -- install with unifi-overhaul-client --install-self-heal"; exit 1; }
  echo "CHANNEL=$CH" > "$HEAL_DIR/settings"
  # Snapshot root's keys, so a firmware update that wipes them doesn't end key-based ssh for good
  if [ -s "$AUTH_KEYS" ]; then cp -f "$AUTH_KEYS" "$HEAL_DIR/authorized_keys"; fi
  printf '%s\n' "[Unit]" "Description=unifi-overhaul: keep the 2.4 GHz mesh backhaul applied on this gateway" "" \
    "[Service]" "Type=oneshot" "ExecStart=/bin/sh $HEAL_DIR/unifi-overhaul.sh __heal" > "$UNIT_DIR/$UNIT.service"
  printf '%s\n' "[Unit]" "Description=unifi-overhaul: re-check the 2.4 GHz backhaul after boot and every 5 minutes" "" \
    "[Timer]" "OnBootSec=90s" "OnUnitActiveSec=5min" "" "[Install]" "WantedBy=timers.target" > "$UNIT_DIR/$UNIT.timer"
  { systemctl daemon-reload && systemctl enable --now "$UNIT.timer"; } || { log "[-] couldn't enable $UNIT.timer"; exit 1; }
  log "[+] self-heal installed ($UNIT.timer: $(heal_state)): 90s after boot, then every 5 min. Log: journalctl -u $UNIT"
}
# Undo do_install: exact file names only, ${VAR:?} so an empty path aborts instead of deleting, and rmdir
# (which refuses a non-empty directory) rather than a recursive delete
do_remove(){
  if have systemctl; then systemctl disable --now "$UNIT.timer" >/dev/null 2>&1; fi
  rm -f "${UNIT_DIR:?}/${UNIT:?}.service" "${UNIT_DIR:?}/${UNIT:?}.timer"
  if have systemctl; then systemctl daemon-reload; fi
  for f in $HEAL_FILES; do rm -f "${HEAL_DIR:?}/${f:?}"; done
  if [ -d "$HEAL_DIR" ] && ! rmdir "${HEAL_DIR:?}" 2>/dev/null; then
    log "[-] left $HEAL_DIR in place: it holds files self-heal didn't create"; fi
  log "[+] self-heal removed"
}
heal_keys(){   # put back any snapshot key that's missing from root's authorized_keys
  [ -s "$HEAL_DIR/authorized_keys" ] || return 0
  mkdir -p "$(dirname "$AUTH_KEYS")"; chmod 700 "$(dirname "$AUTH_KEYS")"; touch "$AUTH_KEYS"; chmod 600 "$AUTH_KEYS"
  while IFS= read -r key; do
    if [ -n "$key" ] && ! grep -qxF "$key" "$AUTH_KEYS"; then echo "$key" >> "$AUTH_KEYS"; log "[+] restored an ssh key"; fi
  done < "$HEAL_DIR/authorized_keys"; return 0; }
do_diag(){
  TO=""; have timeout && TO="timeout 60"
  diag_section "identity"
  for c in "date" "uptime" "cat /proc/sys/kernel/hostname" "uname -a" "cat /etc/version /usr/lib/version /etc/os-release" \
           "cat /etc/board.info" "cat /proc/ubnthal/system.info" "cat /proc/ubnthal/board"; do diag_run "$c"; done
  have ubnt-device-info && diag_run "ubnt-device-info summary"
  have mca-cli-op && diag_run "$TO mca-cli-op info"
  have dpkg && diag_run "dpkg -l | grep -iE 'unifi|ubnt|uos|mongo'"

  diag_section "resources"
  for c in "free" "df -h" "cat /proc/loadavg" "ps w 2>/dev/null || ps aux" "top -bn1 | head -n 40" "cat /proc/meminfo"; do diag_run "$c"; done

  diag_section "unifi-overhaul state"
  diag_run "detect && show_status"
  diag_run "do_probe"
  diag_run "ls -la /tmp/system.cfg* /tmp/running.cfg /tmp/default.cfg /tmp/vw.log"
  have diff && diag_run "diff -u /tmp/running.cfg /tmp/system.cfg"
  for c in "cat /tmp/vw.log" "diag_state_files" "diag_shell_history" "ls -la /etc/persistent/ /etc/persistent/cfg/" \
           "cat /etc/persistent/cfg/mgmt" "cat /tmp/system.cfg"; do diag_run "$c"; done

  diag_section "network"
  for c in "ip addr" "ip route" "ip neigh" "brctl show" "bridge link" "bridge fdb show | grep -v permanent" \
           "cat /proc/net/dev" "diag_ethernet" "diag_ping_gateway"; do diag_run "$c"; done

  diag_section "wireless"
  for c in "iw dev" "iw reg get" "iwconfig" "cat /proc/net/wireless" "diag_supplicants"; do diag_run "$c"; done
  have radiomgrctl && diag_run "radiomgrctl status"
  for i in $(iwtree | awk '$1=="Interface"{print $2}'); do
    for c in info link "station dump" "survey dump" "scan dump"; do diag_run "iw dev $i $c"; done
    have wlanconfig && diag_run "wlanconfig $i list sta"
  done
  for p in /sys/class/net/wifi[0-9]; do [ -e "$p" ] && have cfg80211tool || continue
    for g in getCountry getCountryID; do diag_run "cfg80211tool ${p##*/} $g"; done
  done
  diag_run "iw phy"
  have mca-dump && diag_run "$TO mca-dump"

  diag_section "logs"
  diag_run "ls -la /tmp/log /tmp/log/crash /tmp/log/boot/history"
  diag_run "diag_boot_history"
  have logread && diag_run "logread"
  diag_run "dmesg | tail -n 500"
  if have journalctl; then
    for c in "journalctl --list-boots --no-pager | tail -n 20" "$TO journalctl -b --no-pager -n 2000" \
             "$TO journalctl -b -1 --no-pager -n 500" "journalctl -k -b --no-pager -n 500" \
             "systemctl --failed --no-pager"; do diag_run "$c"; done
  fi

  diag_section "controller (UniFi Network app)"
  diag_run "diag_controller"
}
detect(){
  [ -s "$CFG" ] || { log "[-] $CFG missing/empty"; exit 1; }
  MESH_SSID=$(val mesh.essid "$CFG")
  SRC_IDX=""
  for i in $(awk -F. '$1=="wireless"{print $2}' "$CFG"|sort -nu); do
    [ "$(val wireless.$i.vwire "$CFG")" = enabled ]  || continue
    [ "$(val wireless.$i.usage "$CFG")" = downlink ] || continue
    SRC_IDX=$i; break; done
  [ -n "$SRC_IDX" ] || { log "[-] no vwire backhaul AP to clone"; exit 1; }
  SRC_DEV=$(val wireless.$SRC_IDX.devname "$CFG"); SRC_PARENT=$(val wireless.$SRC_IDX.parent "$CFG")
  STA_IDX=""; STA_DEV=""; STA_R=""; STA_V=""
  for i in $(awk -F. '$1=="wireless"{print $2}' "$CFG"|sort -nu); do
    [ "$(val wireless.$i.usage "$CFG")" = uplink ] || continue
    dn=$(val wireless.$i.devname "$CFG"); case "$dn" in vwiresta*) STA_IDX=$i; STA_DEV=$dn; break ;; esac; done
  if [ -n "$STA_DEV" ]; then
    STA_R=$(awk -F'[.=]' -v d="$STA_DEV" '$1=="radio"&&$5=="devname"&&$6==d{print $2; exit}' "$CFG")
    STA_V=$(awk -F'[.=]' -v d="$STA_DEV" '$1=="radio"&&$5=="devname"&&$6==d{print $4; exit}' "$CFG"); fi
  n=2; while grep -q "devname=vwireap$n" "$CFG"; do n=$((n+1)); done; NEW_AP="vwireap$n"
  # role: root (wired), or leaf/relay (wireless uplink). relay = a leaf that other APs mesh through
  # (a downstream AP is associated to one of our vwire downlink APs).
  up=$(cat $RUN_DIR/system.uplink 2>/dev/null)
  DOWN_N=0
  DOWN_N=$(list_downstream | grep -c .)
  # fallbacks (a marginal 4addr peer ages out of `list sta`): a vwireapN.staN interface, or a
  # downstream MAC still learned in the bridge FDB -> still a relay/has-downstream
  [ "${DOWN_N:-0}" -eq 0 ] && DOWN_N=$(count_sta_ifaces)
  [ "${DOWN_N:-0}" -eq 0 ] && DOWN_N=$(fdb_downstream | grep -c .)
  if [ -z "$ROLE" ]; then
    if [ "$up" != wds ] && [ -z "$STA_DEV" ]; then ROLE=root
    elif [ "${DOWN_N:-0}" -gt 0 ]; then ROLE=relay
    else ROLE=leaf; fi
  fi
}

clone_ap(){ out=$1
  for P in wireless aaa netconf; do
    pi=$(idx_of $P "$SRC_DEV" "$out"); [ -z "$pi" ] && continue
    ni=$(( $(maxidx $P "$out") + 1 ))
    grep -E "^$P\.$pi\." "$out" | sed -e "s/^$P\.$pi\./$P.$ni./" \
        -e "s/devname=$SRC_DEV/devname=$NEW_AP/" -e "s/parent=$SRC_PARENT/parent=wifi0/" >> "$out"; done
  v=$(free_virtual 1 "$out")
  printf 'radio.1.virtual.%s.devname=%s\nradio.1.virtual.%s.status=enabled\nradio.1.virtual.%s.mode=master\n' "$v" "$NEW_AP" "$v" "$v" >> "$out"
  bp=$(awk -F'[.=]' -v d="$SRC_DEV" '$1=="bridge"&&$5=="devname"&&$6==d{print $4; exit}' "$out")
  if [ -n "$bp" ]; then np=$(( $(awk -F. '$1=="bridge"&&$2==1&&$3=="port"{print $4+0}' "$out"|sort -n|tail -1) + 1 ))
    grep -E "^bridge\.1\.port\.$bp\." "$out" | sed -e "s/^bridge\.1\.port\.$bp\./bridge.1.port.$np./" -e "s/devname=$SRC_DEV/devname=$NEW_AP/" >> "$out"; fi
}
move_sta(){ out=$1
  [ -n "$STA_DEV" ] || { log "[-] no uplink STA to move"; return 0; }
  set_kv "wireless.$STA_IDX.parent" wifi0 "$out"
  grep -v -E "^radio\.$STA_R\.virtual\.$STA_V\." "$out" > "$out.t" && mv "$out.t" "$out"
  v=$(free_virtual 1 "$out")
  printf 'radio.1.virtual.%s.devname=%s\nradio.1.virtual.%s.status=enabled\nradio.1.virtual.%s.mode=managed\n' "$v" "$STA_DEV" "$v" "$v" >> "$out"
}
has_24_downlink(){   # $1=config: is a vwire backhaul AP already on the 2.4 GHz radio?
  for i in $(awk -F. '$1=="wireless"{print $2}' "$1"|sort -nu); do
    [ "$(val wireless.$i.vwire "$1")" = enabled ] && [ "$(val wireless.$i.usage "$1")" = downlink ] && \
      [ "$(val wireless.$i.parent "$1")" = wifi0 ] && return 0
  done; return 1; }
# Only adds what's missing, so running it on an already-applied config changes nothing (no second AP)
transform(){ out=$1; cp -f "$CFG" "$out"
  set_kv radio.1.channel "$CH" "$out"
  # root & relay OFFER a 2.4 downlink (clone AP); leaf & relay MOVE their uplink STA to 2.4
  if { [ "$ROLE" = root ] || [ "$ROLE" = relay ] || [ "$WITH_AP" = 1 ]; } && ! has_24_downlink "$out"; then clone_ap "$out"; fi
  if { [ "$ROLE" = leaf ] || [ "$ROLE" = relay ]; } && [ "$(val "wireless.$STA_IDX.parent" "$out")" != wifi0 ]; then move_sta "$out"; fi
  # The controller writes mesh.serial1 from the uplink it last saw; pointing it at the intended parent makes the
  # uplink prefer that parent instead of whichever one it happened to latch onto
  if [ -n "$PARENT" ] && [ "$ROLE" != root ]; then set_kv mesh.serial1 "$PARENT" "$out"; fi; }
same_file(){ if have cmp; then cmp -s "$1" "$2"; else diff "$1" "$2" >/dev/null 2>&1; fi; }
# radiomgrd (UniFi OS) takes one config change at a time and refuses apply-config while it's mid-change, e.g.
# finishing its own boot-time config. Firmware without radiomgrctl has nothing to wait for.
radio_busy(){   # prints why the radio manager can't take a change right now; prints nothing when it can
  have radiomgrctl || return 0
  s=$(radiomgrctl status 2>/dev/null)
  c=$(echo "$s" | sed -n 's/.*"controller_state": *"\([^"]*\)".*/\1/p' | head -n 1)
  a=$(echo "$s" | sed -n 's/.*"apply_state": *"\([^"]*\)".*/\1/p' | head -n 1)
  w=$(echo "$s" | sed -n 's/.*"walk_in_flight": *\([a-z]*\).*/\1/p' | head -n 1)
  if [ "$c" = READY ] && [ "$a" = APPLY_IDLE ] && [ "$w" != true ]; then return 0; fi
  echo "controller=${c:-?} apply=${a:-?} walk_in_flight=${w:-?}"; }
wait_for_radio(){   # 0 once the radio manager can take a change; 1 if it's still busy after RADIO_WAIT seconds
  waited=0
  RADIO_STATE=$(radio_busy)
  while [ -n "$RADIO_STATE" ]; do
    if [ "$waited" -ge "$RADIO_WAIT" ]; then return 1; fi
    if [ "$waited" -eq 0 ]; then log "[*] radio manager busy ($RADIO_STATE) -- waiting up to ${RADIO_WAIT}s"; fi
    sleep "$RADIO_POLL"; waited=$((waited + RADIO_POLL))
    RADIO_STATE=$(radio_busy)
  done
  return 0; }
apply_cfg(){
  # syswrapper's own apply path: fast-apply when it can, and an AUTOMATIC full wifi restart for
  # changes fast-apply can't do (e.g. moving a vap between radios -- 'parent unhandled'). 'nopersist'
  # skips the immediate flash write; an AP still saves its running config once its uplink reconnects.
  # A full restart briefly drops ALL wifi (~20-30s).
  "$SYSWRAPPER" apply-config "$CFG" nopersist; }

change_applied(){   # live ground-truth: root offers 2.4 GHz, leaf uplinks on it, relay does both
  offers=no; uplink=no
  for i in $(iwtree|awk '$1=="Interface"{print $2}'|grep '^vwireap'); do
    f=$(freq_of "$i"); if [ -n "$f" ] && [ "$f" -lt 2500 ]; then offers=YES; fi
  done
  st=$(cat $RUN_DIR/system.uplink_ifname 2>/dev/null)
  f=$(link_freq "$st"); if [ -n "$f" ] && [ "$f" -lt 2500 ]; then uplink=YES; fi
  case "$ROLE" in
    root)  echo "$offers" ;;
    relay) if [ "$offers" = YES ] && [ "$uplink" = YES ]; then echo YES; else echo no; fi ;;
    *)     echo "$uplink" ;;
  esac; }


show_status(){
  host=$(cat /proc/sys/kernel/hostname 2>/dev/null)
  basemac=$(awk -F= '/^hwaddrbbase=/{print $2}' "$BOARD" 2>/dev/null)   # canonical device MAC
  echo "==== unifi-overhaul status: ${host:-?}  (role=$ROLE) ===="
  echo "[*] 2.4GHz backhaul in use here (live): $(change_applied)"
  echo ""
  echo "[*] Place in mesh chain   (uplink -> self -> downlink; [local: iface=name] = how neighbours see this end)"
  # uplink: who this device connects UP to. [local:...] is the name up-chain devices see it by.
  st=$(cat $RUN_DIR/system.uplink_ifname 2>/dev/null)
  if [ -n "$st" ] && [ -e "/sys/class/net/$st" ]; then
    l=$(iw dev "$st" link 2>/dev/null); ub=$(echo "$l"|awk '/Connected to/{print $3;exit}')
    uf=$(echo "$l"|awk '$1=="freq:"{print $2;exit}'); us=$(echo "$l"|awk '$1=="signal:"{print $2;exit}')
    urx=$(echo "$l"|awk '/rx bitrate/{print $3;exit}'); utx=$(echo "$l"|awk '/tx bitrate/{print $3;exit}')
    om=$(cat /sys/class/net/$st/address 2>/dev/null)
    if [ -n "$ub" ]; then echo "      uplink    $(mac_name "$ub") ($ub)   [$(band_of "$uf") ${us}dBm  rx/tx $(rate_h "${urx:-?}")/$(rate_h "${utx:-?}")]   [local: $st=$(mac_name "$om")]"
    else echo "      uplink    $st -- NOT ASSOCIATED"; fi
  elif [ "$(cat $RUN_DIR/system.uplink 2>/dev/null)" = eth ]; then
    echo "      uplink    WIRED  (gateway / root)"
  else echo "      uplink    (none)"; fi
  # self: the one stable identity is the hostname (each vwire iface has its own MAC/name)
  echo "      self      ${host:-?}${basemac:+ ($(mac_name "$basemac") / $basemac)}   [$(echo "$ROLE" | tr 'a-z' 'A-Z')]"
  # downlink: who connects DOWN, via which vwire AP. [local:...] is the name they see this end by.
  ds=$(list_downstream)
  if [ -n "$ds" ]; then
    echo "$ds" | while read ap apm ab sm ss rxr txr; do
      echo "      downlink  $(mac_name "$sm") ($sm)   [$ab${ss:+ ${ss}dBm}  rx/tx $(rate_h "${rxr:-?}")/$(rate_h "${txr:-?}")]   [local: $ap=$(mac_name "$apm")]"
    done
  else
    fd=$(fdb_downstream)
    if [ -n "$fd" ]; then
      for ap in $fd; do echo "      downlink  present on $ap [$(band_of "$(freq_of "$ap")")] -- peer aged out of station table (marginal link); seen via bridge"; done
    elif [ "$(count_sta_ifaces)" -gt 0 ]; then echo "      downlink  connected (peer MAC not readable right now)"
    else echo "      downlink  (none -- end leaf)"; fi
  fi
  echo ""
  ui=$(cat $RUN_DIR/uplink_wds 2>/dev/null)
  # only show uplink_iface when there is one -- a wired root has no wireless uplink interface, so the field would be blank
  echo "[*] mca (Management Console Agent): uplink=$(cat $RUN_DIR/system.uplink 2>/dev/null)${ui:+  uplink_iface=$ui}"
  gw=$(awk -F= '/^gateway=/{print $2}' $RUN_DIR/mesh.status 2>/dev/null)
  um=$(awk -F= '/^uplink_mac=/{print $2}' $RUN_DIR/mesh.status 2>/dev/null)
  # mesh.status 'uplink_mac' is the GATEWAY / mesh-root MAC (its base MAC), not the immediate uplink
  [ -n "$gw" ] && echo "[*] mca: gateway=$gw${um:+  gateway_mac=$um ($(mac_name "$um"))}"
  echo "[*] backhaul SSID (vwire): $MESH_SSID"
  echo "[*] live vwire interfaces   (iface  type  band  chan  name  ssid)"
  iwtree | awk '
    function flush(){ if(i ~ /^vwire/) print i"|"t"|"s"|"c"|"f"|"a }
    $1=="Interface"{ flush(); i=$2;t="";s="";c="";f="";a="" }
    $1=="type"{t=$2} $1=="ssid"{s=$2} $1=="addr"{a=$2} $1=="channel"{c=$2;f=$3;gsub(/[()]/,"",f)}
    END{ flush() }' | while IFS='|' read i t s c f a; do
      printf '  %-14s %-8s %-6s ch%-5s %-14s %s\n' "$i" "$t" "$(band_of "$f")" "${c:-?}" "$(mac_name "$a")" "${s:-(hidden)}"
    done
  echo "[*] vwire BSS configured in /tmp/system.cfg"
  for i in $(awk -F. '$1=="wireless"{print $2}' "$CFG"|sort -nu); do
    dn=$(val wireless.$i.devname "$CFG"); case "$dn" in vwire*) : ;; *) continue;; esac
    printf '  wireless.%-3s %-12s %-9s parent=%-6s vwire=%s\n' "$i" "$dn" "$(val wireless.$i.usage "$CFG")" "$(val wireless.$i.parent "$CFG")" "$(val wireless.$i.vwire "$CFG")"
  done
}
scan_band(){   # $1=wifiN  -- directed-probe the hidden SSID on that radio; print band/signal per BSS
  w=$1; phy=$(cat /sys/class/net/$w/phy80211/name 2>/dev/null); [ -n "$phy" ] || return
  tmp=""; sc=""
  if iw phy "$phy" interface add "vwscan_$w" type managed >/dev/null 2>&1; then
    tmp="vwscan_$w"; sc="$tmp"; ip link set "$tmp" up 2>/dev/null
  else
    sc=$(iw dev 2>/dev/null|awk '$1=="Interface"{print $2}'|grep -E "^${w}ap"|head -1)
  fi
  [ -n "$sc" ] || return
  iw dev "$sc" scan ssid "$MESH_SSID" 2>/dev/null | awk -v S="$MESH_SSID" '
    /^BSS /{bss=$2; sub(/\(.*/,"",bss)}
    /\tfreq:/{f=$2} /\tsignal:/{sig=$2}
    /\tSSID:/{ if($2==S){ b=(f<2500?"2.4GHz":(f<5925?"5GHz":"6GHz")); print bss"|"b"|"f"|"sig; bss="" } }' | \
  while IFS='|' read bss b f sig; do
    printf '  %-20s %-14s %-6s freq=%s  signal=%s dBm  (via %s)\n' "$bss" "($(mac_name "$bss"))" "$b" "$f" "$sig" "$w"
  done
  [ -n "$tmp" ] && iw dev "$tmp" del >/dev/null 2>&1
}
show_avail(){
  # 1) show the CURRENT uplink signal live (no scan -- always safe, whatever band you're on)
  # 2) to show what's available if you SWITCH, scan the bands you're NOT on -- but that scan can
  #    drop a weak/multi-hop backhaul (chip-coupled WiFi7 radios), so it's gated behind a prompt.
  st=$(cat $RUN_DIR/system.uplink_ifname 2>/dev/null); uphy=""; uf=""
  if [ -n "$st" ] && [ -e "/sys/class/net/$st" ]; then
    uphy=$(cat /sys/class/net/$st/phy80211/name 2>/dev/null)
    l=$(iw dev "$st" link 2>/dev/null)
    uf=$(echo "$l"|awk '$1=="freq:"{print $2;exit}'); sg=$(echo "$l"|awk '/signal:/{print $2;exit}'); bs=$(echo "$l"|awk '/Connected to/{print $3;exit}')
  fi
  if [ -n "$uf" ]; then
    log "[*] CURRENT uplink (live, no scan): $st -> $bs  $(band_of "$uf")  signal:${sg} dBm  [radio ${uphy:-?}]"
  else
    log "[*] no wireless uplink detected (wired?) -- scanning all radios below."
  fi

  # big warning + hard confirmation (skip only with --force)
  if [ "$FORCE" != 1 ]; then
    echo ""
    echo "  ##########################  READ THIS  ##########################"
    echo "  #  To show the OTHER bands, avail must SCAN them. Scanning takes  #"
    echo "  #  a radio off-channel, and on this WiFi7 chip that can stall the #"
    echo "  #  whole chip. If your backhaul is WEAK or MULTI-HOP, the scan    #"
    echo "  #  can DROP the uplink -- and can knock out any AP that meshes    #"
    echo "  #  THROUGH this one -- freezing them until a PHYSICAL POWER-CYCLE.#"
    echo "  #  A power-cycle brings it back, but you may lose                 #"
    echo "  #  your connection to this AP right now. If in doubt: don't.      #"
    echo "  #################################################################"
    printf "  Type exactly 'yes' to scan the other band(s), anything else aborts: "
    read ans 2>/dev/null || ans=""
    if [ "$ans" != "yes" ]; then echo ""; log "[*] aborted -- no scan performed. (current uplink shown above.)"; return 0; fi
  fi

  # scan every radio that is NOT the current uplink radio (i.e. the bands you could switch to)
  log "[*] scanning the band(s) you are NOT on for '$MESH_SSID' (this is the risky part)..."
  scanned=0
  for w in wifi0 wifi1 wifi2; do
    [ -e "/sys/class/net/$w" ] || continue
    phy=$(cat /sys/class/net/$w/phy80211/name 2>/dev/null); [ -n "$phy" ] || continue
    if [ -n "$uphy" ] && [ "$phy" = "$uphy" ]; then
      log "  [skip] $w = the current uplink radio (shown live above, not scanned)"; continue
    fi
    scan_band "$w"; scanned=1
  done
  [ "$scanned" = 1 ] && log "[*] done. each line = an AP offering the backhaul on a band you could switch to." \
                     || log "[*] nothing to scan."
}


case "$MODE" in -h|--help|help) usage; exit 0 ;; esac
# __probe / __diag / __devices / self-heal (remote helpers for unifi-overhaul-client) must run even when detect would fail -> handle before it
case "$MODE" in
  __probe)   do_probe; exit 0 ;;
  __diag)    do_diag; exit 0 ;;
  __devices) do_devices; exit 0 ;;
  __install) do_install; exit 0 ;;
  __remove)  do_remove; exit 0 ;;
  # The self-heal timer: restore keys, then fall through to an idempotent root apply below
  __heal)    heal_keys
             [ -s "$CFG" ] || { log "[*] $CFG not there yet -- checking again next run"; exit 0; }
             c=$(sed -n 's/^CHANNEL=//p' "$HEAL_DIR/settings" 2>/dev/null); CH=${c:-$CH}; ROLE=root; MODE=apply ;;
esac
detect
[ "$MODE" = status ] || [ "$MODE" = avail ] || \
  log "[*] role=$ROLE ch=$CH clone-from=$SRC_DEV new-AP=$NEW_AP sta=${STA_DEV:-none}(r${STA_R:-}.v${STA_V:-}) with_ap=$WITH_AP"
case "$MODE" in
  status) show_status ;;
  avail)  show_avail ;;
  dryrun) transform "$NEW"
    if same_file "$CFG" "$NEW"; then
      rm -f "${NEW:?}"
      if [ "$(change_applied)" = YES ]; then log "[+] already applied (role=$ROLE ch=$CH) -- apply would change nothing"
      else log "[*] the config already has the change but it isn't live -- apply would push it again"; fi
      exit 0
    fi
    if command -v diff >/dev/null 2>&1; then diff -u "$CFG" "$NEW"
    else log "ADDED:"; grep -vxF -f "$CFG" "$NEW"; log "REMOVED:"; grep -vxF -f "$NEW" "$CFG"; fi
    rm -f "${NEW:?}"
    wa=""; [ "$WITH_AP" = 1 ] && wa=" --with-ap"
    echo ""
    log "[*] dryrun only -- nothing changed.  autodetected role: $ROLE  (with-ap=$WITH_AP)"
    log "[*] APPLY this DETACHED -- your SSH will freeze ~20-30s during the full wifi restart:"
    log "      nohup /tmp/unifi-overhaul.sh apply$wa > /tmp/vw.log 2>&1 &"
    log "      # reconnect, then:  cat /tmp/vw.log ; /tmp/unifi-overhaul.sh status"
    log "[*] WHY detached: moving the uplink vap between radios forces a FULL wifi restart that"
    log "    drops your backhaul mid-command; nohup lets apply finish even if your SSH drops."
    log "[*] role is autodetected from what's connected now (see 'status'); on a relay pass --with-ap,"
    log "    since an AP below it that's disconnected right now makes it look like a leaf." ;;
  apply) transform "$NEW"
    is_pushing_again=no
    if same_file "$CFG" "$NEW"; then
      rm -f "${NEW:?}"
      if [ "$(change_applied)" = YES ]; then log "[+] already applied (role=$ROLE ch=$CH) -- nothing to change"; exit 0; fi
      is_pushing_again=yes
    fi
    # Checked before the config is touched, so a busy radio manager leaves nothing to undo
    if ! wait_for_radio; then
      rm -f "${NEW:?}"
      log "[-] the radio manager is still busy ($RADIO_STATE) after ${RADIO_WAIT}s, so nothing was changed -- run apply again shortly"
      exit 1
    fi
    if [ "$is_pushing_again" = yes ]; then
      # The file has the change but the radio doesn't: an earlier apply-config failed, or something reloaded.
      # Nothing to put back if this push fails too, so drop any leftover copy from an interrupted run.
      rm -f "${PREV:?}"
      log "[*] the config already has the change but it isn't live -- pushing it again"
    else
      # Keep the first pre-apply config of this boot, so rollback still reaches stock after a second apply
      [ -f "$BAK" ] || cp -f "$CFG" "$BAK"; log "[+] backup: $BAK"
      cp -f "$CFG" "$PREV"; mv -f "$NEW" "$CFG"
    fi
    log "[*] applying via syswrapper (a vap moving radios triggers a full wifi restart -- all wifi drops ~20-30s)..."
    if ! apply_cfg; then
      # Put the file back, so the next run retries instead of finding an edit the radio never got
      if [ -f "$PREV" ]; then mv -f "$PREV" "$CFG"; fi
      log "[-] syswrapper failed, so the config was put back and nothing changed -- run apply again in a minute"
      exit 1
    fi
    rm -f "${PREV:?}"
    log "[+] apply issued. wait ~30s for wifi to settle, then run: /tmp/unifi-overhaul.sh status"
    if [ "$ROLE" = leaf ]; then log "[-] if this AP stays silent past ~1min, power-cycle it; if it still can't connect, see the README's Recovery"; fi ;;
  rollback) if [ -f "$BAK" ]; then cp -f "$BAK" "$CFG"; log "[*] restoring backup + applying..."; apply_cfg
    log "[+] rolled back. wait ~30s then run: /tmp/unifi-overhaul.sh status"
    else log "[-] no backup from this boot -- force a re-provision of this device from the UniFi app to restore stock"; fi ;;
  *) log "[-] mode: status|dryrun|apply|rollback|avail|help"; exit 2 ;;
esac
