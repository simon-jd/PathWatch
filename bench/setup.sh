#!/usr/bin/env bash
# Owned prefix only. Requires host build tools and the reviewed project Rust pin.
# Run serially with no benchmark active; resource gate precedes each build phase.
set -euo pipefail
cd "$(dirname "$0")/.."
P="$PWD/.cache/task13"
mkdir -p "$P" docs/evidence/raw/task13/setup
budget() {
 python3 - "$1" <<'PY'
import subprocess,sys
allocated=int(subprocess.check_output(['du','-s','-B1','.']).split()[0])+2124313342
reserve=int(sys.argv[1]);print('conservative allocated',allocated,'stage reserve',reserve,flush=True)
# 12 GB project disk share (since 2026-10-27) minus a 400 MB soft margin.
if allocated+reserve>=11600000000:raise SystemExit('Stage exceeds 11.6GB soft: explicit owned retirement/reforecast required')
PY
}
budget 750000000
python3 - <<'PY'
import hashlib,json,urllib.request
from pathlib import Path
for a in json.loads(Path('bench/tools.lock.json').read_text())['artifacts']:
 p=Path('.cache/task13')/a['name']
 if not p.exists():urllib.request.urlretrieve(a['url'],p)
 with p.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
 if actual!=a['sha256']:raise SystemExit('SHA256 mismatch: '+a['name'])
PY
mkdir -p "$P/deps" "$P/runtime" "$P/gnupg"
chmod 700 "$P/gnupg"
gpg --homedir "$P/gnupg" --import "$P/nginx-pluknet.key"
gpg --homedir "$P/gnupg" --verify "$P/nginx.tar.gz.asc" "$P/nginx.tar.gz"
for deb in "$P"/*.deb; do dpkg-deb -x "$deb" "$P/deps"; done
ln -sf /usr/lib/x86_64-linux-gnu/libssl.so.3 "$P/deps/usr/lib/x86_64-linux-gnu/libssl.so.3"
ln -sf /usr/lib/x86_64-linux-gnu/libcrypto.so.3 "$P/deps/usr/lib/x86_64-linux-gnu/libcrypto.so.3"
for archive in wrk2.tar.gz nginx.tar.gz go.tar.gz; do tar -xzf "$P/$archive" -C "$P"; done
tar -xJf "$P/nghttp2.tar.xz" -C "$P"
chmod +x "$P/envoy"
export C_INCLUDE_PATH="$P/deps/usr/include:$P/deps/usr/include/x86_64-linux-gnu"
export CPLUS_INCLUDE_PATH="$C_INCLUDE_PATH"
export LIBRARY_PATH="$P/deps/usr/lib/x86_64-linux-gnu"
export LD_LIBRARY_PATH="$LIBRARY_PATH"
budget 200000000
make -C "$P/wrk2-44a94c17d8e6a0bac8559b53da76848e430cb7a7" -j4 > docs/evidence/raw/task13/setup/wrk2.txt 2>&1
(
 cd "$P/nginx-1.30.6"
 ./configure --prefix="$P/nginx" --with-http_ssl_module --with-http_v2_module --without-http_rewrite_module --without-http_gzip_module --with-cc-opt="-I$P/deps/usr/include -I$P/deps/usr/include/x86_64-linux-gnu" --with-ld-opt="-L$P/deps/usr/lib/x86_64-linux-gnu"
 make -j4
 make install
) > docs/evidence/raw/task13/setup/nginx.txt 2>&1
cmake -S "$P/nghttp2-1.70.0" -B "$P/nghttp2-build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$P/deps/usr" -DCMAKE_CXX_FLAGS="-I$P/deps/usr/include/x86_64-linux-gnu" -DOPENSSL_INCLUDE_DIR="$P/deps/usr/include" -DOPENSSL_SSL_LIBRARY=/usr/lib/x86_64-linux-gnu/libssl.so.3 -DOPENSSL_CRYPTO_LIBRARY=/usr/lib/x86_64-linux-gnu/libcrypto.so.3 -DLIBEV_INCLUDE_DIR="$P/deps/usr/include" -DLIBEV_LIBRARY="$P/deps/usr/lib/x86_64-linux-gnu/libev.so" -DENABLE_APP=ON -DENABLE_EXAMPLES=OFF -DENABLE_HPACK_TOOLS=OFF -DENABLE_HTTP3=OFF > docs/evidence/raw/task13/setup/h2load.txt 2>&1
cmake --build "$P/nghttp2-build" --target h2load -j4 >> docs/evidence/raw/task13/setup/h2load.txt 2>&1
budget 250000000
GOTOOLCHAIN=local GOWORK=off GO111MODULE=off GOCACHE="$P/go-cache" "$P/go/bin/go" build -trimpath -o "$P/echo" bench/echo.go
gcc -O2 -shared -fPIC -I"$P/deps/usr/include" -I"$P/deps/usr/include/x86_64-linux-gnu" -o "$P/observe.so" bench/observe.c -ldl -L"$P/deps/usr/lib/x86_64-linux-gnu" -lssl -pthread
budget 350000000
source scripts/dev-env.sh
CARGO_BUILD_JOBS=4 cargo build --offline --locked --release --manifest-path proxy/Cargo.toml -p edge-proxy > docs/evidence/raw/task13/setup/rust.txt 2>&1
if [[ ! -e "$P/runtime/cert.pem" ]]; then
 openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$P/runtime/key.pem" -out "$P/runtime/cert.pem" -days 7 -subj /CN=bench.pathwatch.test -addext subjectAltName=DNS:bench.pathwatch.test
 chmod 600 "$P/runtime/key.pem"
fi
budget 80000000
