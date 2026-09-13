{{/*
Шесть обязательных меток. Kyverno проверяет их на каждой нагрузке, но дело
не в проверке: без них под не попадает в разбивку затрат, ради которой
существует вся платформа.
*/}}
{{- define "messenger.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: messenger
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
finops.internal/product: messenger
finops.internal/team: {{ .root.Values.ownership.team }}
finops.internal/environment: {{ .root.Values.ownership.environment }}
finops.internal/component: {{ .component }}
{{- end -}}

{{- define "messenger.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: messenger
{{- end -}}

{{/*
Образ всегда по digest, никогда по тегу.

Тег подвижен: один и тот же `:v1.4.0` сегодня и через неделю может быть
разными байтами, и тогда «откатились на предыдущую версию» ничего не
гарантирует. Конвейер собирает образ один раз и дальше везде ссылается
на sha256 — это и есть DEP-001.
*/}}
{{- define "messenger.image" -}}
{{- $svc := .svc -}}
{{- $root := .root -}}
{{- $repo := $svc.image.repository | default (printf "%s/%s" $root.Values.image.registry .name) -}}
{{- if $svc.image.digest -}}
{{ $repo }}@{{ $svc.image.digest }}
{{- else if $root.Values.image.allowMutableTag -}}
{{ $repo }}:{{ $svc.image.tag | default "latest" }}
{{- else -}}
{{ fail (printf "сервис %s: не задан image.digest. Тег подвижен и не годится для выкатки; для временной заглушки задайте image.allowMutableTag=true" .name) }}
{{- end -}}
{{- end -}}
