FROM ubuntu:24.04
LABEL project=pathwatch owner=codex-pathwatch purpose=dns-lab
RUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools iproute2 dnsutils python3 ca-certificates && rm -rf /var/lib/apt/lists/*
CMD ["sleep", "infinity"]
