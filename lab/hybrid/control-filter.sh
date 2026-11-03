#!/usr/bin/env bash
# Azure guest only (also usable inside project router container); never host.
set -euo pipefail
iptables -N PATHWATCH_CONTROL 2>/dev/null || true
iptables -F PATHWATCH_CONTROL
for interface in pw-wg0 pw-wg1; do
  iptables -A PATHWATCH_CONTROL -i "$interface" -j RETURN
done
iptables -A PATHWATCH_CONTROL -j DROP
for proto in tcp udp; do
  ports=179
  [[ "$proto" == udp ]] && ports=3784,3785,4784
  iptables -C INPUT -p "$proto" -m multiport --dports "$ports" -j PATHWATCH_CONTROL 2>/dev/null || iptables -I INPUT 1 -p "$proto" -m multiport --dports "$ports" -j PATHWATCH_CONTROL
done
