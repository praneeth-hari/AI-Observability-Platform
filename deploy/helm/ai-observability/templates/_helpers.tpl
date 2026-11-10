{{- define "aiobs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "aiobs.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Labels every object gets. Usage: include "aiobs.labels" (dict "root" . "component" "collector") */}}
{{- define "aiobs.labels" -}}
app.kubernetes.io/name: {{ include "aiobs.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- end -}}

{{- define "aiobs.selector" -}}
app.kubernetes.io/name: {{ include "aiobs.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* The Secret that holds every password/key. The chart refuses to render without it. */}}
{{- define "aiobs.secretName" -}}
{{- required "secrets.existingSecret is required (create it with: python scripts/k8s_secrets.py | kubectl apply -f -)" .Values.secrets.existingSecret -}}
{{- end -}}

{{/* One env var read from the Secret. Usage: include "aiobs.secretEnv" (dict "root" . "name" "SALT")
     Optional "as" renames the env var (secret key stays "name"). */}}
{{- define "aiobs.secretEnv" -}}
- name: {{ .as | default .name }}
  valueFrom:
    secretKeyRef:
      name: {{ include "aiobs.secretName" .root }}
      key: {{ .name }}
{{- end -}}

{{/* Settings shared by langfuse-web and langfuse-worker (mirrors docker-compose.yml). */}}
{{- define "aiobs.langfuseEnv" -}}
{{- $r := .root -}}
{{- $fn := include "aiobs.fullname" $r -}}
{{ include "aiobs.secretEnv" (dict "root" $r "name" "POSTGRES_PASSWORD") }}
{{ include "aiobs.secretEnv" (dict "root" $r "name" "CLICKHOUSE_PASSWORD") }}
{{ include "aiobs.secretEnv" (dict "root" $r "name" "MINIO_ROOT_PASSWORD") }}
{{ include "aiobs.secretEnv" (dict "root" $r "name" "REDIS_AUTH") }}
{{ include "aiobs.secretEnv" (dict "root" $r "name" "SALT") }}
{{ include "aiobs.secretEnv" (dict "root" $r "name" "ENCRYPTION_KEY") }}
- name: NEXTAUTH_URL
  value: {{ $r.Values.langfuse.publicUrl | quote }}
- name: DATABASE_URL
  value: "postgresql://postgres:$(POSTGRES_PASSWORD)@{{ $fn }}-postgres:5432/postgres"
- name: TELEMETRY_ENABLED
  value: {{ $r.Values.langfuse.telemetryEnabled | quote }}
- name: CLICKHOUSE_MIGRATION_URL
  value: "clickhouse://{{ $fn }}-clickhouse:9000"
- name: CLICKHOUSE_URL
  value: "http://{{ $fn }}-clickhouse:8123"
- name: CLICKHOUSE_USER
  value: clickhouse
- name: CLICKHOUSE_CLUSTER_ENABLED
  value: "false"
{{- range $kind := list "EVENT" "MEDIA" }}
{{- $prefix := ternary "events/" "media/" (eq $kind "EVENT") }}
- name: LANGFUSE_S3_{{ $kind }}_UPLOAD_BUCKET
  value: langfuse
- name: LANGFUSE_S3_{{ $kind }}_UPLOAD_REGION
  value: auto
- name: LANGFUSE_S3_{{ $kind }}_UPLOAD_ACCESS_KEY_ID
  value: minio
- name: LANGFUSE_S3_{{ $kind }}_UPLOAD_SECRET_ACCESS_KEY
  value: $(MINIO_ROOT_PASSWORD)
- name: LANGFUSE_S3_{{ $kind }}_UPLOAD_ENDPOINT
  {{- if and (eq $kind "MEDIA") $.mediaEndpoint }}
  value: {{ $.mediaEndpoint | quote }}
  {{- else }}
  value: "http://{{ $fn }}-minio:9000"
  {{- end }}
- name: LANGFUSE_S3_{{ $kind }}_UPLOAD_FORCE_PATH_STYLE
  value: "true"
- name: LANGFUSE_S3_{{ $kind }}_UPLOAD_PREFIX
  value: {{ $prefix }}
{{- end }}
- name: REDIS_HOST
  value: "{{ $fn }}-redis"
- name: REDIS_PORT
  value: "6379"
- name: REDIS_TLS_ENABLED
  value: "false"
{{- end -}}
