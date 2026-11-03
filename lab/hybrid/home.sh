#!/usr/bin/env bash
# Creates/deletes only exact named and labeled project objects. No privileged mode,
# host networking, host sysctl, default routes, global prune or other Docker network.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export DOCKER_CONFIG="$root/.cache/docker"
mkdir -p "$DOCKER_CONFIG"
network=pathwatch-hybrid-home
router=pathwatch-hybrid-home-router
client=pathwatch-hybrid-home-client
image=pathwatch-hybrid:task3
owned() { [[ "$(docker "$1" inspect "$2" --format '{{index .Config.Labels "project"}}' 2>/dev/null || true)" == pathwatch ]]; }
case "${1:-}" in
  build)
    du -sh "$root/.cache" "$root/lab/hybrid"
    docker build --label project=pathwatch --label owner=codex-pathwatch -t "$image" "$root/lab/hybrid"
    docker image inspect "$image" --format 'image={{.Id}} size={{.Size}}'
    ;;
  up)
    config="$(realpath "${2:?private rendered home config directory}")"
    [[ "$config" == "$root/.cache/"* && -f "$config/frr.conf" && -f "$config/pw-wg0.conf" ]]
    for container in "$router" "$client"; do
      if docker container inspect "$container" >/dev/null 2>&1; then echo 'Refuse overlapping up; use own down first' >&2; exit 1; fi
    done
    # Current real route overlap inspection is mandatory and never alters host.
    python3 "$root/scripts/hybrid_preflight.py" --host-routes
    if docker network inspect "$network" >/dev/null 2>&1; then
      [[ "$(docker network inspect "$network" --format '{{index .Labels "project"}}')" == pathwatch ]]
    else
      docker network create --label project=pathwatch --label owner=codex-pathwatch --subnet 10.30.0.0/16 "$network"
    fi
    trap '"$root/lab/hybrid/home.sh" down' ERR INT TERM
    # FRR changes user/caps; CHOWN/DAC/SETUID/SETGID are for daemon startup only.
    # NET_ADMIN changes only these containers' namespaces. Ubuntu zebra explicitly
    # requests SYS_ADMIN for its built-in VRF/netns capability set; FOWNER lets
    # frrinit preserve frr-owned config permissions. Both stay router-only, under
    # default Docker AppArmor/seccomp, no host namespace or writable host mount.
    docker run -d --name "$router" --label project=pathwatch --label owner=codex-pathwatch --network "$network" --ip 10.30.0.2 --memory 256m --pids-limit 100 --cap-drop ALL --cap-add NET_ADMIN --cap-add NET_RAW --cap-add NET_BIND_SERVICE --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SYS_ADMIN --sysctl net.ipv4.ip_forward=1 --sysctl net.ipv4.conf.all.rp_filter=2 -v "$config:/run/pathwatch:ro" "$image" router
    docker run -d --name "$client" --label project=pathwatch --label owner=codex-pathwatch --network "$network" --ip 10.30.0.10 --memory 128m --pids-limit 40 --cap-drop ALL --cap-add NET_ADMIN --cap-add NET_RAW "$image" client
    ready=false
    for attempt in {1..30}; do
      if docker exec "$router" sh -c 'pgrep -x zebra >/dev/null && pgrep -x bgpd >/dev/null && pgrep -x bfdd >/dev/null && ip link show pw-wg0 >/dev/null && ip link show pw-wg1 >/dev/null' >/dev/null 2>&1 && docker exec "$client" pgrep -x iperf3 >/dev/null 2>&1; then ready=true; break; fi
      sleep 1
    done
    [[ "$ready" == true ]] || { echo 'Own router/client startup failed; restoring own resources' >&2; false; }
    trap - ERR INT TERM
    ;;
  down)
    for container in "$client" "$router"; do
      if docker container inspect "$container" >/dev/null 2>&1; then
        owned container "$container" || { echo 'Refuse foreign container' >&2; exit 1; }
        docker rm -f "$container"
      fi
    done
    if docker network inspect "$network" >/dev/null 2>&1; then
      [[ "$(docker network inspect "$network" --format '{{index .Labels "project"}}')" == pathwatch ]]
      docker network rm "$network"
    fi
    ;;
  *) echo 'usage: home.sh build | up project/.cache/private-rendered-home | down' >&2; exit 2;;
esac
