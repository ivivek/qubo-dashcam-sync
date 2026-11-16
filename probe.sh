#!/usr/bin/env bash
# Probe the dashcam after joining its Wi-Fi AP.
# Finds its IP, scans open ports, and tries the HTTP/RTSP APIs used by
# common dashcam chipsets (Novatek, MStar/SigmaStar, HiSilicon, Ambarella)
# to see whether videos + GPS can be pulled without the phone app.
#
# Usage: bash probe.sh [camera-ip]
# Output is also saved to probe-<timestamp>.log next to this script.

set -u
cd "$(dirname "$0")"
LOG="probe-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee "$LOG") 2>&1

section() { printf '\n===== %s =====\n' "$*"; }

# --- 1. Which interface / SSID are we on? ---------------------------------
section "Wi-Fi link"
nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | grep '^yes' || echo "(nmcli unavailable)"
IFACE=$(ip route show default | awk '{print $5; exit}')
echo "default iface: ${IFACE:-none}"
ip -br addr show "${IFACE:-lo}"

# --- 2. Find the camera IP --------------------------------------------------
section "Camera IP"
IP="${1:-}"
if [ -z "$IP" ]; then
  # On a dashcam AP the camera is almost always the default gateway.
  IP=$(ip route show default | awk '{print $3; exit}')
  echo "default gateway: ${IP:-none}"
fi
if [ -z "$IP" ]; then
  # No gateway handed out: try well-known dashcam addresses.
  for c in 192.168.1.254 192.168.0.1 192.168.1.1 192.168.169.1 192.168.42.1 192.168.25.1 193.168.0.1; do
    if ping -c1 -W1 "$c" >/dev/null 2>&1; then IP=$c; break; fi
  done
fi
[ -z "$IP" ] && { echo "Could not find camera IP. Pass it: bash probe.sh <ip>"; exit 1; }
echo "camera IP: $IP"
ping -c2 -W1 "$IP"

section "Other hosts on the subnet (ARP)"
ip neigh show dev "$IFACE" 2>/dev/null

# --- 3. Port / service scan -------------------------------------------------
section "Full TCP port scan"
nmap -Pn -T4 -p- --open "$IP" -oG - | grep -E 'Ports:|Host:' || true
PORTS=$(nmap -Pn -T4 -p- --open "$IP" -oG - | grep -oE '[0-9]+/open' | cut -d/ -f1 | paste -sd,)
echo "open ports: ${PORTS:-none}"

if [ -n "$PORTS" ]; then
  section "Service/version detection"
  nmap -Pn -sV --version-light -p "$PORTS" "$IP"
fi

section "Common UDP ports (needs sudo; skipped if it fails)"
sudo -n nmap -Pn -sU -p 53,67,69,554,1900,5353,8600,48899 "$IP" 2>/dev/null || echo "(skipped - run with sudo for UDP)"

# --- 4. Try known dashcam HTTP APIs ----------------------------------------
probe() {  # probe <url>
  local out code
  out=$(curl -s -m 4 -w '\n__HTTP %{http_code} %{content_type} %{size_download}B' "$1")
  code=$(tail -n1 <<<"$out")
  case "$code" in
    "__HTTP 000"*) return ;;   # no response, keep output quiet
  esac
  printf '\n--- %s\n%s\n' "$1" "$code"
  head -c 800 <<<"$(sed '$d' <<<"$out")"; echo
}

section "HTTP: generic"
probe "http://$IP/"
probe "http://$IP/DCIM/"
probe "http://$IP/sd/"
probe "http://$IP:8080/"

section "HTTP: Novatek API (?custom=1&cmd=...)"
probe "http://$IP/?custom=1&cmd=3012"   # firmware version
probe "http://$IP/?custom=1&cmd=3014"   # current settings
probe "http://$IP/?custom=1&cmd=3015"   # FILE LIST (videos)
probe "http://$IP/?custom=1&cmd=3016"   # heartbeat
probe "http://$IP/?custom=1&cmd=3017"   # disk free
probe "http://$IP/NOVATEK/"

section "HTTP: MStar/SigmaStar CGI"
probe "http://$IP/cgi-bin/Config.cgi?action=get&property=Camera.Menu.*"
probe "http://$IP/cgi-bin/Config.cgi?action=dir&property=Normal&format=all&count=16&from=0"
probe "http://$IP/cgi-bin/Config.cgi?action=dir&property=Event&format=all&count=16&from=0"

section "HTTP: HiSilicon hisnet CGI"
probe "http://$IP/cgi-bin/hisnet/getdeviceattr.cgi"
probe "http://$IP/cgi-bin/hisnet/getdirfilecount.cgi?-dir=norm"
probe "http://$IP/cgi-bin/hisnet/getdirfilelist.cgi?-dir=norm&-start=0&-end=10"

section "HTTP: other vendor paths"
probe "http://$IP/app/getdeviceattr"
probe "http://$IP/app/getfilelist?folder=loop&start=0&end=10"
probe "http://$IP/vcam/cmd.cgi?cmd=API_GetBatteryStatus"
probe "http://$IP/api/v1/files"

# --- 5. Streaming / control ports ------------------------------------------
section "RTSP (port 554)"
for path in "" live liveRTSP/av4 xxx.mov stream0 h264; do
  r=$(printf 'OPTIONS rtsp://%s:554/%s RTSP/1.0\r\nCSeq: 1\r\n\r\n' "$IP" "$path" | nc -w2 "$IP" 554 2>/dev/null | head -3)
  [ -n "$r" ] && { echo "--- rtsp://$IP:554/$path"; echo "$r"; }
done

section "Ambarella JSON API (port 7878)"
r=$(printf '{"msg_id":257,"token":0}' | nc -w2 "$IP" 7878 2>/dev/null | head -c 400)
echo "${r:-(no response)}"

section "Banners: FTP 21 / Telnet 23 / Novatek notify 3333"
for p in 21 23 3333; do
  r=$(timeout 3 nc -w2 "$IP" "$p" </dev/null 2>/dev/null | head -c 300)
  echo "port $p: ${r:-(closed or silent)}"
done

section "Done"
echo "Log saved to $(pwd)/$LOG"
