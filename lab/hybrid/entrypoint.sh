#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-router}" == client ]]; then
  ip route replace 10.10.0.0/16 via 10.30.0.2
  exec iperf3 -s -B 10.30.0.10
fi
install -m600 /run/pathwatch/pw-wg0.conf /etc/wireguard/pw-wg0.conf
install -m600 /run/pathwatch/pw-wg1.conf /etc/wireguard/pw-wg1.conf
install -o frr -g frr -m640 /run/pathwatch/frr.conf /etc/frr/frr.conf
sed -i -e 's/^zebra=.*/zebra=yes/' -e 's/^bgpd=.*/bgpd=yes/' -e 's/^bfdd=.*/bfdd=yes/' /etc/frr/daemons
wg-quick up pw-wg0
wg-quick up pw-wg1
/usr/local/bin/pathwatch-control-filter
trap 'wg-quick down pw-wg1 || true; wg-quick down pw-wg0 || true; /usr/lib/frr/frrinit.sh stop || true' EXIT INT TERM
/usr/lib/frr/frrinit.sh start
while sleep 2; do pgrep -x zebra >/dev/null && pgrep -x bgpd >/dev/null || exit 1; done
