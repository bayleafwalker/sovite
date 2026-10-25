{{- define "sovite.fullname" -}}
{{- default (printf "%s-%s" .Release.Name .Chart.Name) .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "sovite.labels" -}}
app.kubernetes.io/name: sovite
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
{{- define "sovite.image" -}}
{{- if .digest -}}{{ .repository }}@{{ .digest }}{{- else -}}{{ .repository }}:{{ .tag }}{{- end -}}
{{- end -}}
{{- define "sovite.runtimeEnv" -}}
- name: SOVITE_DISPLAY_NAME
  value: {{ .Values.displayName | quote }}
- name: SOVITE_DATABASE_URL
  valueFrom:
    secretKeyRef: {name: {{ .Values.runtimeSecret }}, key: SOVITE_DATABASE_URL}
- name: SOVITE_DATABASE_SCHEMA
  value: {{ .Values.database.schema | quote }}
- name: SOVITE_DATABASE_MIGRATE
  value: {{ .Values.database.migrate | quote }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef: {name: {{ .Values.runtimeSecret }}, key: AWS_ACCESS_KEY_ID}
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef: {name: {{ .Values.runtimeSecret }}, key: AWS_SECRET_ACCESS_KEY}
- name: AWS_EC2_METADATA_DISABLED
  value: "true"
- name: SOVITE_BUNDLE_PATH
  value: /config/bundle.yaml
- name: SOVITE_ARTIFACT_BACKEND
  value: s3
- name: SOVITE_S3_ENDPOINT
  value: {{ default (printf "http://%s-garage:3900" (include "sovite.fullname" .)) .Values.artifacts.endpoint | quote }}
- name: SOVITE_S3_BUCKET
  value: {{ .Values.artifacts.bucket | quote }}
- name: SOVITE_S3_REGION
  value: {{ .Values.artifacts.region | quote }}
- name: SOVITE_REQUIRE_QUALIFICATION
  value: {{ .Values.qualification.required | quote }}
- name: SOVITE_QUALIFICATION_PUBLIC_KEYS
  value: {{ $paths := list }}{{ range .Values.qualification.publicKeyFiles }}{{ $paths = append $paths (printf "/trust/%s" .) }}{{ end }}{{ $paths | toJson | quote }}
- name: SOVITE_PUBLIC_ORIGIN
  value: {{ .Values.publicOrigin | quote }}
- name: SOVITE_SECURE_COOKIES
  value: {{ .Values.secureCookies | quote }}
- name: SOVITE_EMBEDDED_WORKER
  value: "false"
- name: SOVITE_DEPLOYMENT_BINDING
  value: {{ .Values.deploymentBinding | quote }}
{{- end -}}

{{- define "sovite.controlAuthEnv" -}}
- name: SOVITE_OPERATOR_TOKEN
  valueFrom:
    secretKeyRef: {name: {{ .Values.runtimeSecret }}, key: SOVITE_OPERATOR_TOKEN}
- name: SOVITE_CALLER_TOKENS
  valueFrom:
    secretKeyRef: {name: {{ .Values.runtimeSecret }}, key: SOVITE_CALLER_TOKENS}
{{- if .Values.remoteWorker.enabled }}
- name: SOVITE_EXECUTION_TOKENS
  valueFrom:
    secretKeyRef: {name: {{ .Values.runtimeSecret }}, key: SOVITE_EXECUTION_TOKENS}
{{- end }}
{{- end -}}

{{- define "sovite.modelCredentialEnv" -}}
{{- range .Values.modelCredentials }}
- name: {{ .env }}
  valueFrom:
    secretKeyRef: {name: {{ $.Values.runtimeSecret }}, key: {{ .key | default .env }}}
{{- end }}
{{- end -}}

{{- define "sovite.remoteWorkerCredentialEnv" -}}
{{- range .Values.remoteWorker.credentials }}
- name: {{ .env }}
  valueFrom:
    secretKeyRef: {name: {{ $.Values.runtimeSecret }}, key: {{ .key }}}
{{- end }}
{{- end -}}

{{- define "sovite.modelTrustEnv" -}}
{{- if .Values.modelTrust }}
- {name: SOVITE_MODEL_TRUST_DIR, value: /model-trust}
{{- end }}
{{- end -}}
