{{/*
The edge proxy terminates TLS on the pod IP and forwards to loopback-only
listeners in the same pod (ingest/README.md: query and metrics never leave
the network namespace). Call with a dict: image, sni, port, routes, secret, and
optionally management (loopback IP:port for GET /metrics and POST /drain).
Each route: prefix, upstream, timeoutMs.
*/}}

{{- define "edge-proxy.config" -}}
listeners = [{ addr = "0.0.0.0:{{ .port }}", tls = { certs = [{ sni = {{ .sni | quote }}, cert = "/tls/edge.pem", key = "/tls/edge-key.pem" }] } }]
{{- with .management }}
management = { addr = {{ . | quote }} }
{{- end }}
routes = [
{{- range $i, $r := .routes }}
  { host = {{ $.sni | quote }}, path_prefix = {{ $r.prefix | quote }}, cluster = "r{{ $i }}" },
{{- end }}
]
clusters = [
{{- range $i, $r := .routes }}
  { name = "r{{ $i }}", endpoints = [{{ $r.upstream | quote }}], lb = "least_request", circuit = { max_requests = 64, max_pending = 64, request_timeout_ms = {{ $r.timeoutMs }} } },
{{- end }}
]

[pool]
max_connections = 32
request_timeout_ms = 40000
drain_timeout_ms = 30000
{{- end -}}

{{- define "edge-proxy.sidecar" -}}
name: edge-proxy
image: {{ required "edge-proxy image" .image }}
imagePullPolicy: Never
# Native sidecar: starts before and outlives the frontend's 35 s drain.
restartPolicy: Always
args: ["/etc/edge-proxy/config.toml"]
ports:
  - name: https
    containerPort: {{ .port }}
readinessProbe:
  tcpSocket:
    port: https
  periodSeconds: 5
resources:
  requests: { cpu: 50m, memory: 32Mi }
  limits: { memory: 256Mi }
securityContext:
  runAsNonRoot: true
  runAsUser: 65532
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: [ALL] }
volumeMounts:
  - { name: edge-proxy-config, mountPath: /etc/edge-proxy, readOnly: true }
  - { name: edge-tls, mountPath: /tls, readOnly: true }
{{- end -}}
