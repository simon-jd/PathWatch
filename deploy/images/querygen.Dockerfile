FROM scratch
LABEL project=pathwatch
COPY querygen /
USER 65532:65532
ENTRYPOINT ["/querygen"]
