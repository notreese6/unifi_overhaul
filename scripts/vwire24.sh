#!/bin/sh
# vwire24.sh — force a 2.4 GHz UniFi wireless backhaul (vwire) alongside the 5 GHz one.
# Non-persistent: edits /tmp/system.cfg + applies via syswrapper. Reboot/controller-change reverts.
#   ./vwire24.sh status|dryrun|apply|rollback|avail|help  [--role root|leaf|relay] [--channel N] [--with-ap]
# Role autodetected: ROOT (wired, adds a 2.4 downlink), LEAF (moves its uplink STA to 2.4),
# RELAY (leaf that feeds other APs: moves uplink AND adds a 2.4 downlink). 'status' shows the chain.
# Apply order: ROOT first, then RELAYS, then LEAVES.
set -u
CFG=/tmp/system.cfg; BAK=/tmp/system.cfg.vwire24.bak; NEW=/tmp/system.cfg.vwire24.new
CH=1; ROLE=""; WITH_AP=0; FORCE=0; MAPARGS=""
MODE="${1:-status}"; [ $# -gt 0 ] && shift
# 'map' takes positional IPs (not flags); stash them so the flag parser doesn't choke
case "$MODE" in map) MAPARGS="$*"; set -- ;; esac
while [ $# -gt 0 ]; do case "$1" in
  --role) ROLE="$2"; shift 2 ;; --channel) CH="$2"; shift 2 ;; --with-ap) WITH_AP=1; shift ;;
  --force) FORCE=1; shift ;;
  *) echo "[-] unknown arg: $1"; exit 2 ;; esac; done

usage(){ cat <<EOF
vwire24.sh — add a 2.4 GHz UniFi wireless backhaul (vwire) alongside the 5 GHz one.
Non-persistent: edits /tmp/system.cfg + applies the incremental diff. Reboot reverts.

Usage: vwire24.sh [MODE] [options]
  MODE (default: status):
    status     the chain (uplink / downlink / role), what's in use, backhaul state  (read-only)
    map <targets>  ssh each device + print the WHOLE mesh tree; targets are [user@]ip
               (run from your WORKSTATION; APs often use a different SSH user than the gateway)
    avail      show current uplink signal (live), then PROMPT to scan other bands     (scan can drop a weak link!)
    dryrun     show the exact config diff + the recommended apply command             (safe)
    apply      make the change (full wifi restart if a vap moves radios)
    rollback   restore this session's pre-apply backup + re-apply
    help       show this text
  options:
    --role root|leaf|relay   override the autodetected role
    --channel N              2.4 GHz channel (default 1); MUST match on every device
    --with-ap                force the 2.4 downlink on a leaf (only if downstream APs aren't up yet)
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
do_probe(){   # machine-readable one-device summary for 'map' (run remotely via ssh). No detect needed.
  echo "H $(cat /proc/sys/kernel/hostname 2>/dev/null || echo unknown)"
  up=$(cat /var/run/system.uplink 2>/dev/null); st=$(cat /var/run/system.uplink_ifname 2>/dev/null)
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
do_map(){   # from your WORKSTATION: ssh each device, assemble the full chain by hostname.
  # Args: [user@]ip ...  (bare ip => root@ip; APs often use a different SSH user than the gateway).
  [ $# -ge 1 ] || { echo "[-] usage: vwire24.sh map [user@]<gateway-ip> [user@]<ap-ip> ...   (run from your workstation)"; return 2; }
  t="/tmp/vwmap.$$"; : > "$t"
  for tgt in "$@"; do
    case "$tgt" in *@*) sshtgt="$tgt"; ip="${tgt#*@}" ;; *) sshtgt="root@$tgt"; ip="$tgt" ;; esac
    echo "[*] probing $sshtgt ..." >&2; echo "D $ip" >> "$t"
    ssh -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new "$sshtgt" 'sh -s __probe' < "$0" >> "$t" 2>/dev/null \
      || echo "H $ip (unreachable -- wrong user? try user@ip)" >> "$t"
  done
  awk '
    function pr(d, ind,   c,cd){
      if (upband[d]=="-") printf "%s%s   (root, wired)\n", ind, host[d]
      else printf "%s%s   <--[uplink %s%s]\n", ind, host[d], upband[d], (upsig[d]=="-"||upsig[d]==""?"":" "upsig[d]"dBm")
      for(c=1;c<=n;c++){ cd=order[c]; if(cd!=d && parent[cd]==d) pr(cd, ind "    ") }
    }
    $1=="D"{dev=$2; devs[dev]=1}
    $1=="H"{host[dev]=$2" ("dev")"; order[++n]=dev}
    $1=="U"{up[dev]=$2; upband[dev]=$3; upsig[dev]=$4}
    $1=="A"{owner[$2]=dev}
    END{
      for(c=1;c<=n;c++){ d=order[c]; parent[d]=(up[d]=="WIRED"?"ROOT":(up[d] in owner ? owner[up[d]] : "ORPHAN")) }
      print ""; print "[*] mesh map (indented = uplinks to the line above; [band signal] is the uplink)"
      for(c=1;c<=n;c++){ d=order[c]; if(parent[d]=="ROOT") pr(d, "") }
      for(c=1;c<=n;c++){ d=order[c]; if(parent[d]=="ORPHAN") printf "  (?) %s -> uplink %s is on a device not probed (unreachable, or not listed)\n", host[d], up[d] }
    }' "$t"
  rm -f "$t"
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
  up=$(cat /var/run/system.uplink 2>/dev/null)
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
transform(){ out=$1; cp -f "$CFG" "$out"
  set_kv radio.1.channel "$CH" "$out"
  # root & relay OFFER a 2.4 downlink (clone AP); leaf & relay MOVE their uplink STA to 2.4
  { [ "$ROLE" = root ] || [ "$ROLE" = relay ] || [ "$WITH_AP" = 1 ]; } && clone_ap "$out"
  { [ "$ROLE" = leaf ] || [ "$ROLE" = relay ]; } && move_sta "$out"; }
apply_cfg(){
  # syswrapper's own apply path: fast-apply when it can, and an AUTOMATIC full wifi restart for
  # changes fast-apply can't do (e.g. moving a vap between radios -- 'parent unhandled'). 'nopersist'
  # skips the /data write, so a reboot still reverts. A full restart briefly drops ALL wifi (~20-30s).
  /usr/bin/syswrapper.sh apply-config "$CFG" nopersist; }

change_applied(){   # live ground-truth: is a 2.4GHz vwire in use on this device?
  if [ "$ROLE" = root ]; then
    for i in $(iwtree|awk '$1=="Interface"{print $2}'|grep '^vwireap'); do
      f=$(freq_of "$i"); [ -n "$f" ] && [ "$f" -lt 2500 ] && { echo YES; return; }
    done; echo no
  else
    st=$(cat /var/run/system.uplink_ifname 2>/dev/null)
    f=$(link_freq "$st"); [ -n "$f" ] && [ "$f" -lt 2500 ] && echo YES || echo no
  fi; }


show_status(){
  host=$(cat /proc/sys/kernel/hostname 2>/dev/null)
  basemac=$(awk -F= '/^hwaddrbbase=/{print $2}' /proc/ubnthal/board 2>/dev/null)   # canonical device MAC
  echo "==== vwire24 status: ${host:-?}  (role=$ROLE) ===="
  echo "[*] 2.4GHz backhaul in use here (live): $(change_applied)"
  echo ""
  echo "[*] Place in mesh chain   (uplink -> self -> downlink; [local: iface=name] = how neighbours see this end)"
  # uplink: who this device connects UP to. [local:...] is the name up-chain devices see it by.
  st=$(cat /var/run/system.uplink_ifname 2>/dev/null)
  if [ -n "$st" ] && [ -e "/sys/class/net/$st" ]; then
    l=$(iw dev "$st" link 2>/dev/null); ub=$(echo "$l"|awk '/Connected to/{print $3;exit}')
    uf=$(echo "$l"|awk '$1=="freq:"{print $2;exit}'); us=$(echo "$l"|awk '$1=="signal:"{print $2;exit}')
    urx=$(echo "$l"|awk '/rx bitrate/{print $3;exit}'); utx=$(echo "$l"|awk '/tx bitrate/{print $3;exit}')
    om=$(cat /sys/class/net/$st/address 2>/dev/null)
    if [ -n "$ub" ]; then echo "      uplink    $(mac_name "$ub") ($ub)   [$(band_of "$uf") ${us}dBm  rx/tx $(rate_h "${urx:-?}")/$(rate_h "${utx:-?}")]   [local: $st=$(mac_name "$om")]"
    else echo "      uplink    $st -- NOT ASSOCIATED"; fi
  elif [ "$(cat /var/run/system.uplink 2>/dev/null)" = eth ]; then
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
  ui=$(cat /var/run/uplink_wds 2>/dev/null)
  # only show uplink_iface when there is one -- a wired root has no wireless uplink interface, so the field would be blank
  echo "[*] mca (Management Console Agent): uplink=$(cat /var/run/system.uplink 2>/dev/null)${ui:+  uplink_iface=$ui}"
  gw=$(awk -F= '/^gateway=/{print $2}' /var/run/mesh.status 2>/dev/null)
  um=$(awk -F= '/^uplink_mac=/{print $2}' /var/run/mesh.status 2>/dev/null)
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
  st=$(cat /var/run/system.uplink_ifname 2>/dev/null); uphy=""; uf=""
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
    echo "  #  Nothing persists (a reboot restores 5 GHz), but you may lose   #"
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
# map (workstation) and __probe (remote helper) don't touch /tmp/system.cfg -> handle before detect
case "$MODE" in
  map)     do_map $MAPARGS; exit $? ;;
  __probe) do_probe; exit 0 ;;
esac
detect
[ "$MODE" = status ] || [ "$MODE" = avail ] || \
  log "[*] role=$ROLE ch=$CH clone-from=$SRC_DEV new-AP=$NEW_AP sta=${STA_DEV:-none}(r${STA_R:-}.v${STA_V:-}) with_ap=$WITH_AP"
case "$MODE" in
  status) show_status ;;
  avail)  show_avail ;;
  dryrun) transform "$NEW"
    if command -v diff >/dev/null 2>&1; then diff -u "$CFG" "$NEW"
    else log "ADDED:"; grep -vxF -f "$CFG" "$NEW"; log "REMOVED:"; grep -vxF -f "$NEW" "$CFG"; fi
    rm -f "$NEW"
    wa=""; [ "$WITH_AP" = 1 ] && wa=" --with-ap"
    echo ""
    log "[*] dryrun only -- nothing changed.  autodetected role: $ROLE  (with-ap=$WITH_AP)"
    log "[*] APPLY this DETACHED -- your SSH will freeze ~20-30s during the full wifi restart:"
    log "      nohup /tmp/vwire24.sh apply$wa > /tmp/vw.log 2>&1 &"
    log "      # reconnect, then:  cat /tmp/vw.log ; /tmp/vwire24.sh status"
    log "[*] WHY detached: moving the uplink vap between radios forces a FULL wifi restart that"
    log "    drops your backhaul mid-command; nohup lets apply finish even if your SSH drops."
    log "[*] role is autodetected root/leaf/relay (see 'status'). RELAY auto-adds the 2.4 downlink,"
    log "    so plain 'apply' is right -- only add --with-ap if downstream APs aren't connected yet." ;;
  apply) cp -f "$CFG" "$BAK"; log "[+] backup: $BAK"; transform "$NEW"; mv -f "$NEW" "$CFG"
    log "[*] applying via syswrapper (a vap moving radios triggers a full wifi restart -- all wifi drops ~20-30s)..."
    apply_cfg
    log "[+] apply issued. wait ~30s for wifi to settle, then run: /tmp/vwire24.sh status"
    [ "$ROLE" = leaf ] && log "[!] if this AP stays silent past ~1min, power-cycle it (reboot reverts to 5 GHz)" ;;
  rollback) if [ -f "$BAK" ]; then cp -f "$BAK" "$CFG"; log "[*] restoring backup + applying..."; apply_cfg
    log "[+] rolled back. wait ~30s then run: /tmp/vwire24.sh status"
    else log "[-] no backup — reboot to revert"; fi ;;
  *) log "[-] mode: status|map|dryrun|apply|rollback|avail|help"; exit 2 ;;
esac
