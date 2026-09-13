# Пакет через два слоя

> kubernetes-finops-platform · стенд local-v1

Как на этом стенде устроен меш, где именно лежит его конфигурация, и как ограничивать трафик двумя разными слоями — Cilium в ядре и Istio поверх него. Все числа и статусы ниже сняты с работающего кластера, а не взяты из документации.

## Что происходит с запросом

Ambient не добавляет контейнер в под. Перехват ставит `istio-cni`, дописывая себя в цепочку CNI рядом с Cilium — в `/etc/cni/net.d/05-cilium.conflist` сейчас два плагина подряд: `cilium-cni` и `istio-cni`. Дальше трафик пода уходит на `ztunnel`, который живёт **один на узел**, и тот заворачивает соединение в HBONE — HTTP CONNECT поверх mTLS на порту `15008`.

Измерено на стенде: **94% входящих соединений** (503 из 534) приходят с идентичностью SPIFFE. Оставшийся 31 — вход извне в ingress, probes от kubelet и host-network поды, то есть места, где идентичности нет по определению.

## Три поверхности конфигурации

Всё управление мешем здесь сводится к трём вещам, и ни одна из них не трогает манифесты приложений — это и есть главное практическое отличие ambient от сайдкаров.

### 1. Включение — метка на namespace

istio · enrolment

Одна метка. Никаких правок в Deployment, никакого контейнера в поде. Отсюда же и обратный ход: снял метку — но поды нужно перезапустить, потому что перехват ставится при создании пода.

```
# gitops/02-infra/infra-bootstrap/values.yaml
- name: prod
  labels:
    istio.io/dataplane-mode: ambient
```

### 2. Требование шифрования — PeerAuthentication

istio · ztunnel

Без этого меш *разрешает* mTLS, но не *требует*: ztunnel примет и открытое соединение. Пока политики нет, шифрование — свойство части трафика, а не гарантия.

```
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: prod-strict
  namespace: prod
spec:
  mtls:
    mode: STRICT
```

### 3. Кому куда можно — AuthorizationPolicy

istio · ztunnel · L4

mTLS доказывает, *кто* пришёл. Ограничивает, *куда* ему можно, уже это. Правило ниже пускает в PgBouncer только checkout — и, что важнее, тем самым **запрещает всё остальное**: одна ALLOW-политика на воркладе делает его закрытым по умолчанию.

```
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: pooler-only-checkout
  namespace: prod
spec:
  selector:
    matchLabels:
      cnpg.io/poolerName: checkout-db-pooler
  action: ALLOW
  rules:
    - from:
        - source:
            principals: ["cluster.local/ns/prod/sa/checkout"]
      to:
        - operation:
            ports: ["5432"]
```

## Чего ztunnel не умеет

ztunnel работает на L4. Идентичность, порт, направление — да; метод HTTP, путь, заголовок — нет. Для L7-правил в ambient нужен **waypoint**: отдельный прокси, который разворачивается на namespace или сервис и через который трафик идёт дополнительным хопом.

на этом стенде пока нельзя

Waypoint — это ресурс `Gateway` из Gateway API, а CRD Gateway API в кластере **нет ни одного** (`gateway.networking.k8s.io` → 0). Есть только устаревший `networking.istio.io/v1 Gateway`, который для waypoint не годится. Так что L7-политики Istio здесь пока не развернуть — сначала ставятся CRD Gateway API.

### L7 через waypoint — как это будет выглядеть

istio · waypoint · L7

Сначала прокси, затем метка на сервисе или namespace, и только после этого в `AuthorizationPolicy` начинают работать `methods` и `paths`. Платится за это ещё одним сетевым хопом и отдельным подом — то есть частью той экономии, ради которой выбирали ambient.

```
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: prod-waypoint
  namespace: prod
  labels:
    istio.io/waypoint-for: service
spec:
  gatewayClassName: istio-waypoint
  listeners:
    - name: mesh
      port: 15008
      protocol: HBONE
---
# и на сервисе: istio.io/use-waypoint: prod-waypoint
# только теперь заработает такое правило:
      to:
        - operation:
            methods: ["GET"]
            paths: ["/checkout", "/checkout/lookup"]
```

## Тот же трафик, но со стороны Cilium

Cilium раздаёт подам identity по меткам и применяет правила в eBPF, до того как пакет вообще дойдёт до сокета. На L3–L4 он дешевле меша, а на L7 поднимает Envoy (`cilium-envoy` на этом узле запущен).

неочевидное следствие включённого ambient

Как только namespace в меше, под перестаёт открывать соединение на `5432` — он говорит HBONE с ztunnel на `15008`. Политика Cilium, написанная на порт `5432`, **перестаёт совпадать**. Это ровно то, что видно в Hubble: у каждой базы две двери — `15008` и прямые `5432 / 9187 / 9121`. Правила для трафика внутри меша пишите по `15008`, а порты приложений оставьте тем, кто ходит мимо меша, — скрейпам и оператору.

### L3–L4 по меткам

cilium · eBPF

Селектор — это метки пода, а не IP. Политика переживает пересоздание пода и смену адреса, что и отличает её от классических firewall-правил.

```
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: checkout-egress
  namespace: prod
spec:
  endpointSelector:
    matchLabels:
      app.kubernetes.io/name: checkout
  egress:
    # внутри меша разговор идёт с ztunnel по 15008
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: prod
      toPorts:
        - ports:
            - port: "15008"
              protocol: TCP
```

### L7 HTTP — без waypoint и без Gateway API

cilium · envoy

Здесь Cilium даёт то, чего ztunnel не умеет, и без установки Gateway API. Но работает это только по трафику, который *не* завёрнут в HBONE, — иначе содержимое для Envoy непрозрачно.

```
  ingress:
    - fromEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: observability
      toPorts:
        - ports:
            - port: "8080"
              protocol: TCP
          rules:
            http:
              # скрейпу нужен ровно один путь и ровно один метод
              - method: "GET"
                path: "/metrics"
```

## DNS: сначала правила, потом лимиты

DNS в Cilium — это не только резолвинг, но и точка контроля: агент поднимает прозрачный DNS-прокси (`dnsproxy-enable-transparent-mode = true` в текущем конфиге) и решает, какие имена поду вообще позволено спросить. На основе ответов строятся правила `toFQDNs` — политика по именам, а не по адресам.

### Разрешить только нужные имена

cilium · dns proxy

Два блока обязательны вместе. Первый разрешает *спросить* имя, второй — *пойти* по полученному адресу. Без первого `toFQDNs` не на чем работать: прокси не увидит ответа и не узнает адрес.

```
  egress:
    # 1. какие имена вообще можно резолвить
    - toEndpoints:
        - matchLabels:
            io.kubernetes.pod.namespace: kube-system
            k8s-app: kube-dns
      toPorts:
        - ports:
            - port: "53"
              protocol: ANY
          rules:
            dns:
              - matchPattern: "*.prod.svc.cluster.local"
              - matchName: "api.payments.example.com"
    # 2. куда можно пойти по результатам резолва
    - toFQDNs:
        - matchName: "api.payments.example.com"
      toPorts:
        - ports:
            - port: "443"
              protocol: TCP
```

### Лимиты на DNS

cilium · флаги агента

Эти три флага есть в установленной версии 1.20.1 — проверено через `cilium-agent --help`. Первый ограничивает одновременную обработку DNS-сообщений прокси, второй задаёт, сколько ждать при достижении предела, третий не даёт одному имени раздуть таблицу идентичностей (по умолчанию 1000 адресов на имя на эндпоинт).

```
# gitops/02-infra/cilium/values.yaml
extraArgs:
  # потолок одновременной обработки DNS-запросов
  - --dnsproxy-concurrency-limit=1000
  # при достижении потолка — ждать, а не ронять запрос
  - --dnsproxy-concurrency-processing-grace-period=15ms
  # защита от раздувания identity одним именем
  - --tofqdns-endpoint-max-ip-per-hostname=50
```

Лимит прокси — это защита самого агента, а не квота на приложение. Если задача — *не дать поду долбить DNS*, то тормозом служит правило `rules.dns` выше (всё лишнее отвергается, причём кодом `refused` — см. `tofqdns-dns-reject-response-code`), а разгрузкой — NodeLocal DNSCache и уменьшение `ndots` в `dnsConfig` пода, которое убирает лишние переборы суффиксов на каждое внешнее имя.

## Какой слой для чего

| Задача | Cilium | Istio ambient | Чем платите |
| --- | --- | --- | --- |
| Кто с кем может говорить (L3–L4) | да, в eBPF | да, в ztunnel | почти ничем |
| Криптографическая идентичность | нет | SPIFFE + mTLS | +9.7% CPU/запрос |
| Правила по HTTP-пути и методу | да, Envoy | нужен waypoint | хоп и под |
| Политика по DNS-именам | toFQDNs | нет | DNS-прокси в тракте |
| Шифрование между узлами | WireGuard/IPsec | mTLS между подами | разные границы |
| Видимость потока | Hubble, L3–L4 | логи ztunnel, идентичности | две разные линзы |

Строка про шифрование — не дубль. Cilium шифрует **между узлами** (и сейчас на стенде выключен: `Encryption: Disabled`), а mTLS меша работает **между подами** и переживает любой промежуточный хоп. Это разные границы доверия, и они складываются, а не заменяют друг друга.

## Как это проверять, а не предполагать

Политика, которая «применена», и политика, которая *отказывает*, — разные состояния. Проверка отказа занимает полминуты и стоит того:

```
# из namespace вне меша — должно оборваться
kubectl run probe --rm -i --restart=Never --image=curlimages/curl -n loadgen -- \
  curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://checkout.prod.svc.cluster.local/checkout
# -> curl rc=56 (connection reset) — STRICT работает
#    код 000, а не 403: отказ происходит на транспорте, до HTTP

# кто реально ходит с идентичностью
kubectl logs -n istio-system ds/ztunnel --since=2m | grep src.identity

# тот же трафик глазами ядра
kubectl exec -n kube-system ds/cilium -c cilium-agent -- hubble observe --last 20
```
Обратите внимание на код `000` и `rc=56` вместо привычного `403`: STRICT рвёт соединение на транспортном уровне, HTTP до приложения не доходит. Если в такой проверке приходит `403` — значит сработала `AuthorizationPolicy`, а не mTLS, и это разные причины отказа.

Все значения — с кластера local-v1: Cilium 1.20.1 (kube-proxy replacement, Envoy external), Istio 1.30.4 ambient, замер стоимости меша — 332 → 293 rps при p95 500 мс.
