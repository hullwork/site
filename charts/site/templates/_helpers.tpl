{{- define "site.image" -}}
{{- $image := index . 0 -}}
{{- if $image.digest -}}
{{ printf "%s@%s" $image.repository $image.digest }}
{{- else -}}
{{ printf "%s:%s" $image.repository $image.tag }}
{{- end -}}
{{- end -}}

{{/*
Shared HULL_BROKER_DB_* block for the three Broker roles in
13-hull-broker.yaml. CONTRACT.md section 4 (db.py) reads it the same way
sites-api/sites-operator read SITES_DB_* (see 10-control-plane.yaml): discrete
host/port/name/user values plus a password file, never a value inline.
*/}}
{{- define "site.hullBrokerDatabaseEnv" -}}
- name: HULL_BROKER_DB_HOST
  value: {{ .Values.broker.database.host | quote }}
- name: HULL_BROKER_DB_PORT
  value: {{ .Values.broker.database.port | quote }}
- name: HULL_BROKER_DB_NAME
  value: {{ .Values.broker.database.name | quote }}
- name: HULL_BROKER_DB_USER
  value: {{ .Values.broker.database.user | quote }}
- name: HULL_BROKER_DB_PASSWORD_FILE
  value: /var/run/hull-broker/db-password
{{- with .Values.broker.database.sslmode }}
- name: HULL_BROKER_DB_SSLMODE
  value: {{ . | quote }}
{{- end }}
{{- end -}}
