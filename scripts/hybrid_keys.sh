#!/usr/bin/env bash
set -euo pipefail
umask 077
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export DOCKER_CONFIG="$root/.cache/docker"
mkdir -p "$DOCKER_CONFIG"
keys="$root/.cache/hybrid/keys"
mkdir -p "$keys"
for site in azure home; do
  for tunnel in 0 1; do
    private="$keys/$site-$tunnel.key"
    public="$keys/$site-$tunnel.pub"
    [[ ! -e "$private" && ! -e "$public" ]] || { echo 'Refuse overwriting existing tunnel keys' >&2; exit 1; }
  done
done
# Keys generated in an unprivileged ephemeral project image; no host package install.
docker run --rm --user "$(id -u):$(id -g)" --cap-drop ALL --network none --label project=pathwatch --entrypoint /bin/bash -v "$keys:/keys" pathwatch-hybrid:task3 -c 'set -euo pipefail; umask 077; for site in azure home; do for tunnel in 0 1; do wg genkey > /keys/$site-$tunnel.key; wg pubkey < /keys/$site-$tunnel.key > /keys/$site-$tunnel.pub; done; done'
# Distinct key material for each logical tunnel and side; no keys printed.
[[ "$(sha256sum "$keys"/*.key | cut -d' ' -f1 | sort -u | wc -l)" == 4 ]]
echo 'Created four distinct private tunnel keys in ignored project cache'
