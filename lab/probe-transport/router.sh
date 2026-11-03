#!/usr/bin/env bash
# Runs ONLY as the entrypoint of the project router's separate network namespace.
set -euo pipefail
[[ "${PATHWATCH_ROUTER_NAMESPACE:-}" == task10-owned && -f /.dockerenv ]]
site="${1:?azure-a or azure-b}"
case "$site" in
  azure-a) cloud=10.10.1.4/32; router=10.30.0.2;;
  azure-b) cloud=10.20.1.4/32; router=10.30.0.3;;
  *) exit 2;;
esac
# Only ingest TCP receives namespace SNAT; host has a direct bridge return route.
iptables -t nat -A POSTROUTING -s "$cloud" -d 10.30.0.1/32 -p tcp --dport 19443 -j SNAT --to-source "$router"
iptables -P FORWARD DROP
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Home observer/echo live in home-A's namespace; local traffic traverses INPUT.
if [[ "$site" == azure-a ]]; then
  iptables -A INPUT -i lo -d 10.30.0.2/32 -p tcp --dport 9464 -j ACCEPT
  iptables -A INPUT -s 10.30.0.1/32 -d 10.30.0.2/32 -p tcp --dport 9464 -j ACCEPT
  iptables -A INPUT -d 10.30.0.2/32 -p tcp --dport 9464 -j DROP
  for proto in tcp udp; do
    for source in 10.10.1.4 10.20.1.4 10.30.0.1 10.30.0.2; do
      iptables -A INPUT -s "$source/32" -d 10.30.0.2/32 -p "$proto" --dport 18443 -j ACCEPT
    done
    iptables -A INPUT -d 10.30.0.2/32 -p "$proto" --dport 18443 -j DROP
  done
  for source in 10.10.1.4 10.20.1.4 10.30.0.1 10.30.0.2; do
    iptables -A INPUT -s "$source/32" -d 10.30.0.2/32 -p icmp --icmp-type echo-request -j ACCEPT
  done
  iptables -A INPUT -d 10.30.0.2/32 -p icmp --icmp-type echo-request -j DROP
fi
for pair in '10.10.1.4 10.20.1.4' '10.20.1.4 10.10.1.4' \
            '10.10.1.4 10.30.0.2' '10.30.0.2 10.10.1.4' \
            '10.20.1.4 10.30.0.2' '10.30.0.2 10.20.1.4'; do
  read -r source destination <<< "$pair"
  for proto in tcp udp; do
    iptables -A FORWARD -s "$source/32" -d "$destination/32" -p "$proto" --dport 18443 -j ACCEPT
  done
  iptables -A FORWARD -s "$source/32" -d "$destination/32" -p icmp -j ACCEPT
done
for interface in pw-wg0 pw-wg1; do
  iptables -A FORWARD -i "$interface" -s "$cloud" -d 10.30.0.1/32 -p tcp --dport 19443 -j ACCEPT
  iptables -A FORWARD -i "$interface" -j DROP
done
exec /usr/local/bin/pathwatch-entrypoint router
