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

**`image.spec` — вторая форма, и она тоже полная ссылка.** Так выглядит
значение, которое пишет argocd-image-updater: он адресует образ тегом
(`репозиторий:тег`), потому что следит за тегами, а не за диджестами.
Строку принимаем целиком и не пересобираем: разбирать её здесь значило бы
завести вторую сборку ссылки, которая разъедется с первой. Путь по умолчанию
— по-прежнему digest из git: он остаётся тем, чем выкатывается этаж, если
апдейтер не тронул Application.
*/}}
{{- define "messenger.image" -}}
{{- $svc := .svc -}}
{{- $root := .root -}}
{{- $repo := $svc.image.repository | default $root.Values.image.repository | default (printf "%s/%s" $root.Values.image.registry .name) -}}
{{- if $svc.image.spec -}}
{{ $svc.image.spec }}
{{- else if $svc.image.digest -}}
{{ $repo }}@{{ $svc.image.digest }}
{{- else if $root.Values.image.allowMutableTag -}}
{{ $repo }}:{{ $svc.image.tag | default $root.Values.image.tag | default "latest" }}
{{- else -}}
{{ fail (printf "сервис %s: не задан image.digest. Тег подвижен и не годится для выкатки; для временной заглушки задайте image.allowMutableTag=true" .name) }}
{{- end -}}
{{- end -}}

{{/*
Версия сервиса: digest, если он есть, иначе тег.

Раньше здесь был только digest, и при выкатке по тегу версия во всех
журналах и в метрике становилась "unversioned" — то есть «когда это
началось» переставало связываться с выкаткой ровно в том окружении,
где отлаживают. Вскрылось при первом подъёме настоящего образа.

Из полной ссылки наружу идёт тег, а не вся строка: репозиторий и так
известен, а значение это попадает в подпись сборки, где длинная строка
с адресом реестра ничего не добавляет.
*/}}
{{- define "messenger.version" -}}
{{- $svc := .svc -}}
{{- $root := .root -}}
{{- if $svc.image.spec -}}
{{ $svc.image.spec | splitList ":" | last }}
{{- else if $svc.image.digest -}}
{{ $svc.image.digest }}
{{- else if ($svc.image.tag | default $root.Values.image.tag) -}}
{{ $svc.image.tag | default $root.Values.image.tag }}
{{- else -}}
unversioned
{{- end -}}
{{- end -}}
