#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export DOCKER_CONFIG="$ROOT/.cache/docker"
mkdir -p "$DOCKER_CONFIG"
NAME=pathwatch-dns-home
owned() {
  [[ "$(docker inspect --format '{{index .Config.Labels "project"}}/{{index .Config.Labels "owner"}}/{{index .Config.Labels "purpose"}}' "$NAME")" == pathwatch/codex-pathwatch/dns-lab ]]
}
case "${1:-}" in
  keys)
    dir="$ROOT/.cache/dns-keys";mkdir -p "$dir";chmod 700 "$dir";umask 077
    for site in a b home-a home-b; do [[ ! -e "$dir/$site.key" && ! -e "$dir/$site.pub" ]] || { echo 'Refuse key overwrite' >&2;exit 1; }; done
    docker build -t pathwatch-dns-home:local "$ROOT/lab/dns"
    docker run --rm --user "$(id -u):$(id -g)" --cap-drop ALL --network none --label project=pathwatch --label owner=codex-pathwatch --label purpose=dns-lab --mount "type=bind,src=$dir,dst=/keys" pathwatch-dns-home:local bash -c 'set -euo pipefail;umask 077;for site in a b home-a home-b;do wg genkey > /keys/$site.key;wg pubkey < /keys/$site.key > /keys/$site.pub;done' ;;
  up)
    # No Task3 reuse or clobber: fail while any of its owned containers exist.
    if docker ps -aq --filter name=pathwatch-hybrid | read -r _; then echo 'Finish Task3 home cleanup first' >&2;exit 1;fi
    config="$(realpath -- "${2:?rendered private DNS config directory required}")"
    [[ "$config" == "$ROOT/.cache/"* ]]
    [[ -f "$config/home-a.conf" && -f "$config/home-b.conf" ]]
    if docker inspect "$NAME" >/dev/null 2>&1; then echo 'DNS home already exists; inspect or run owned down first' >&2;exit 1;fi
    docker build -t pathwatch-dns-home:local "$ROOT/lab/dns"
    docker run -d --name "$NAME" --label project=pathwatch --label owner=codex-pathwatch --label purpose=dns-lab --cap-add NET_ADMIN --memory 256m --cpus 1 --pids-limit 64 --mount "type=bind,src=$config,dst=/config,readonly" pathwatch-dns-home:local
    trap 'owned && docker rm -f "$NAME"' ERR
    owned
    docker exec "$NAME" ip address add 10.30.0.10/32 dev lo
    docker exec "$NAME" wg-quick up /config/home-a.conf
    docker exec "$NAME" wg-quick up /config/home-b.conf ;;
  down)
    if docker inspect "$NAME" >/dev/null 2>&1;then owned;docker rm -f "$NAME";fi ;;
  *) echo "usage: $0 {keys|up PRIVATE_CONFIG_DIR|down}" >&2;exit 2 ;;
esac
