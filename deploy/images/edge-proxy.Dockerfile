# The release binary links glibc dynamically; ubuntu:24.04 (glibc 2.39) is
# already on the host. lab.py checks the binary starts in this base.
FROM ubuntu:24.04
LABEL project=pathwatch
COPY edge-proxy /usr/local/bin/edge-proxy
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/edge-proxy"]
