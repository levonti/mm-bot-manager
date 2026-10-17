{{/* Expand the name of the chart. */}}
{{- define "mm-bot-manager.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Create a DNS-safe fully qualified application name. */}}
{{- define "mm-bot-manager.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := include "mm-bot-manager.name" . }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* Chart name and version for labels. */}}
{{- define "mm-bot-manager.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Immutable labels used by workload selectors. */}}
{{- define "mm-bot-manager.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mm-bot-manager.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Standard labels. */}}
{{- define "mm-bot-manager.labels" -}}
helm.sh/chart: {{ include "mm-bot-manager.chart" . }}
{{ include "mm-bot-manager.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* ServiceAccount name. */}}
{{- define "mm-bot-manager.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mm-bot-manager.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Application image, preferring an immutable digest. */}}
{{- define "mm-bot-manager.appImage" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{/* PostgreSQL image, preferring an immutable digest. */}}
{{- define "mm-bot-manager.postgresImage" -}}
{{- if .Values.database.internal.image.digest }}
{{- printf "%s@%s" .Values.database.internal.image.repository .Values.database.internal.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.database.internal.image.repository .Values.database.internal.image.tag }}
{{- end }}
{{- end }}

{{/* Names for internal PostgreSQL resources. */}}
{{- define "mm-bot-manager.postgresqlFullname" -}}
{{- printf "%s-postgresql" (include "mm-bot-manager.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mm-bot-manager.backupFullname" -}}
{{- printf "%s-backup" (include "mm-bot-manager.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mm-bot-manager.restoreFullname" -}}
{{- printf "%s-restore" (include "mm-bot-manager.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mm-bot-manager.validateMaintenance" -}}
{{- $db := .Values.database.internal -}}
{{- $count := add (ternary 1 0 $db.inspect.enabled) (ternary 1 0 $db.verification.enabled) (ternary 1 0 $db.restore.enabled) -}}
{{- if gt (int $count) 0 -}}
{{- if or (ne .Values.database.mode "internal") (not $db.backup.enabled) -}}
{{- fail "Database maintenance requires internal mode and an enabled backup PVC" -}}
{{- end -}}
{{- if gt (int $count) 1 -}}
{{- fail "Select only one database maintenance Job at a time" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "mm-bot-manager.postgresClientEnv" -}}
- name: PGHOST
  value: {{ include "mm-bot-manager.postgresqlFullname" . | quote }}
- name: PGPORT
  value: {{ .Values.database.internal.service.port | toString | quote }}
- name: PGDATABASE
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.internal.auth.existingSecret }}
      key: POSTGRES_DB
- name: PGUSER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.internal.auth.existingSecret }}
      key: POSTGRES_USER
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.internal.auth.existingSecret }}
      key: POSTGRES_PASSWORD
{{- end }}
