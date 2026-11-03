{{- define "ingest.labels" -}}
app.kubernetes.io/part-of: pathwatch
app.kubernetes.io/managed-by: argocd
project: pathwatch
{{- end -}}

{{- define "ingest.securityContext" -}}
runAsNonRoot: true
runAsUser: 65532
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: { drop: [ALL] }
{{- end -}}

{{/* Metrics-only mTLS bridge; readiness checks the loopback /readyz. */}}
{{- define "ingest.adminbridge" -}}
name: adminbridge
image: {{ .image }}
imagePullPolicy: Never
command: ["/adminbridge"]
args: ["serve", "-upstream", "127.0.0.1:9091", "-listen", "0.0.0.0:9445",
       "-ca", "/metrics-tls/ca.pem", "-cert", "/metrics-tls/metrics.pem", "-key", "/metrics-tls/metrics-key.pem"]
ports:
  - { name: metrics, containerPort: 9445 }
readinessProbe:
  exec:
    command: ["/adminbridge", "check-ready", "-upstream", "127.0.0.1:9091", "-ready-format", {{ .format | quote }}]
  periodSeconds: 5
  timeoutSeconds: 3
resources:
  requests: { cpu: 10m, memory: 16Mi }
  limits: { memory: 64Mi }
securityContext:
  {{- include "ingest.securityContext" . | nindent 2 }}
volumeMounts:
  - { name: metrics-tls, mountPath: /metrics-tls, readOnly: true }
{{- end -}}
