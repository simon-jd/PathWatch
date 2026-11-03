# Context: static (CGO_ENABLED=0) ingest + adminbridge built by deploy/kind/lab.py.
FROM scratch
LABEL project=pathwatch org.opencontainers.image.source=pathwatch
COPY ingest adminbridge /
USER 65532:65532
ENTRYPOINT ["/ingest"]
