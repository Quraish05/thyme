{{/*
Common labels stamped on every object. Defined once and included everywhere, so
"managed-by / chart version / part-of" stay consistent across all tiers — the
kind of boilerplate a chart exists to stop you copy-pasting.
*/}}
{{- define "thyme.labels" -}}
app.kubernetes.io/part-of: thyme
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
