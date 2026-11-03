FROM ubuntu:24.04
LABEL project=pathwatch owner=codex-pathwatch purpose=hybrid-routing
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends frr wireguard-tools iproute2 iptables iputils-ping fping iperf3 python3 ca-certificates procps && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/pathwatch-entrypoint
COPY control-filter.sh /usr/local/bin/pathwatch-control-filter
ENTRYPOINT ["/usr/local/bin/pathwatch-entrypoint"]
