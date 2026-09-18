# Разделение намеренное: bootstrap поднимает кластер и платформу, local-up -
# только мессенджер. Если смешать, local-up превратится в чудовище, которое
# никто не будет запускать, потому что оно каждый раз трогает всё.
#
#   make bootstrap    кластер, Cilium, Argo CD, корневое приложение
#   make local-up     ждёт, пока приложения мессенджера станут Healthy
#   make local-test   контракты, юнит, миграции, связность, интеграция
#   make local-down   снимает только нагрузки мессенджера
#
# Всё идемпотентно: повторный запуск на готовом окружении ничего не ломает
# и почти ничего не делает.

SHELL           := /bin/bash
.SHELLFLAGS     := -eu -o pipefail -c
.DEFAULT_GOAL   := help

PROFILE         ?= minikube
K8S_VERSION     ?= v1.33.1
CPUS            ?= 14
MEMORY          ?= 22000
DISK            ?= 60g
CILIUM_VERSION  ?= 1.20.1
ARGOCD_NAMESPACE ?= argocd

# Приложения, которые обязаны стать Healthy, чтобы окружение считалось готовым.
MESSENGER_APPS  := messenger-secrets messenger-postgres messenger-redis \
                   messenger-kafka-topics messenger-minio messenger-mailpit \
                   messenger-keycloak messenger-centrifugo messenger-services \
                   messenger-routes

# Та же версия, что в образе (apps/messenger/Dockerfile) и в конвейере.
PYTHON ?= python3.12
PYTHON_VERSION = (3, 12)

.PHONY: help bootstrap local-up local-test local-down contracts kafka-security layers sql venv lint unit migrate smoke integration send-message status

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------

bootstrap: ## Кластер и платформа: minikube, Cilium, Argo CD, корневое приложение
	@if minikube status -p $(PROFILE) >/dev/null 2>&1; then \
		echo "· кластер $(PROFILE) уже запущен"; \
	else \
		echo "· создаю кластер"; \
		minikube start -p $(PROFILE) --cpus=$(CPUS) --memory=$(MEMORY) \
			--disk-size=$(DISK) --kubernetes-version=$(K8S_VERSION) \
			--cni=false --extra-config=kubeadm.skip-phases=addon/kube-proxy; \
	fi
	@# Cilium ставится руками ровно один раз: подам Argo CD нужен CNI,
	@# чтобы вообще запуститься. Дальше Argo CD перенимает этот релиз.
	@if kubectl -n kube-system get ds cilium >/dev/null 2>&1; then \
		echo "· Cilium на месте"; \
	else \
		echo "· ставлю Cilium"; \
		helm repo add cilium https://helm.cilium.io/ >/dev/null; \
		helm repo update cilium >/dev/null; \
		helm install cilium cilium/cilium -n kube-system \
			--version $(CILIUM_VERSION) -f gitops/02-infra/cilium/values.yaml --wait; \
	fi
	@$(MAKE) --no-print-directory _coredns
	@if kubectl -n $(ARGOCD_NAMESPACE) get deploy argocd-server >/dev/null 2>&1; then \
		echo "· Argo CD на месте"; \
	else \
		echo "· ставлю Argo CD"; \
		kubectl create namespace $(ARGOCD_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -; \
		kubectl apply -n $(ARGOCD_NAMESPACE) \
			-f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml; \
		kubectl -n $(ARGOCD_NAMESPACE) rollout status deploy/argocd-server --timeout=300s; \
	fi
	@echo "· корневое приложение"
	@kubectl apply -f gitops/01-root/project.yaml -f gitops/01-root/root-application.yaml
	@echo "bootstrap готов"

_coredns:
	@# На драйвере Docker /etc/resolv.conf узла указывает на внутренний
	@# резолвер Docker Desktop, недостижимый из подов. Симптом - repo-server
	@# Argo CD в цикле перезапусков с "lookup github.com", что выглядит как
	@# проблема git или прав и не является ни тем, ни другим.
	@if kubectl -n kube-system get cm coredns -o jsonpath='{.data.Corefile}' | grep -q '/etc/resolv.conf'; then \
		echo "· правлю CoreDNS на публичный резолвер"; \
		kubectl -n kube-system get cm coredns -o jsonpath='{.data.Corefile}' \
			| sed 's|forward . /etc/resolv.conf|forward . 8.8.8.8 1.1.1.1|' > /tmp/Corefile; \
		kubectl -n kube-system create cm coredns --from-file=Corefile=/tmp/Corefile \
			--dry-run=client -o yaml | kubectl apply -f -; \
		kubectl -n kube-system rollout restart deploy/coredns; \
	else \
		echo "· CoreDNS уже настроен"; \
	fi

# ---------------------------------------------------------------------------

local-up: ## Поднять мессенджер (сам вызовет bootstrap, если платформы нет)
	@if ! kubectl -n $(ARGOCD_NAMESPACE) get deploy argocd-server >/dev/null 2>&1; then \
		echo "· платформы нет, запускаю bootstrap"; \
		$(MAKE) --no-print-directory bootstrap; \
	fi
	@echo "· жду приложения мессенджера"
	@for app in $(MESSENGER_APPS); do \
		printf "  %-24s " "$$app"; \
		for i in $$(seq 1 120); do \
			health=$$(kubectl -n $(ARGOCD_NAMESPACE) get app $$app \
				-o jsonpath='{.status.health.status}' 2>/dev/null || true); \
			[ "$$health" = "Healthy" ] && break; \
			sleep 5; \
		done; \
		echo "$${health:-нет приложения}"; \
		[ "$$health" = "Healthy" ] || exit 1; \
	done
	@echo "мессенджер поднят"

local-test: contracts kafka-security layers log-streams sql lint unit migrate smoke integration ## Контракты, безопасность Kafka, слои, журналы, SQL, линтер, юнит, миграции, связность, интеграция

contracts: ## Схемы связны и обратно совместимы
	@echo "· контракты"
	@python3 packages/contracts/validate.py $${CONTRACT_BASE:-}

kafka-security: ## SEC-010: потребитель непрочитанного не читает содержимое
	@echo "· безопасность Kafka"
	@python3 scripts/check-kafka-security.py

layers: ## Зависимости между слоями идут только вниз
	@echo "· слои"
	@python3 scripts/check-layers.py

log-streams: ## У каждой записи журнала есть событие из каталога и свой поток
	@echo "· журналы"
	@python3 scripts/check-log-streams.py

sql: ## Миграции не содержат операторов, запрещённых ADR 0006
	@echo "· миграции"
	@python3 scripts/check-migrations.py

lint: venv ## Линтер — ровно та же команда, что в конвейере
	@echo "· линтер"
	@# Проверяются и messenger, и tests. Пока здесь стоял только пакет,
	@# правила для тестов проверял один конвейер, и расхождение всплывало
	@# уже после push - ровно так и случилось с S105 в проверке входа.
	@cd apps/messenger && .venv/bin/python -m ruff check messenger tests

venv: ## Окружение для проверок; создаётся само
	@# Создаётся здесь, а не документируется как «сначала установите
	@# pytest»: шаг, который надо помнить, однажды забудут.
	@#
	@# Версия интерпретатора закреплена и совпадает с образом и конвейером.
	@# Пока здесь стоял `python3`, окружение уехало на 3.14, и установка
	@# cryptography пошла собираться из исходников: колеса для этой версии
	@# ещё нет. Локально это выглядело как сломанный Xcode, а на деле было
	@# расхождение с тем, что поедет в кластер.
	@command -v $(PYTHON) >/dev/null || { 		echo "нет $(PYTHON) — поставьте его или задайте PYTHON=" >&2; exit 1; }
	@test -d apps/messenger/.venv || $(PYTHON) -m venv apps/messenger/.venv
	@# Окружение, созданное другой версией, молча остаётся прежним.
	@apps/messenger/.venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info[:2] == $(PYTHON_VERSION) else 1)' 2>/dev/null 		|| { echo "  пересоздаю окружение под $(PYTHON)"; rm -rf apps/messenger/.venv; $(PYTHON) -m venv apps/messenger/.venv; }
	@apps/messenger/.venv/bin/pip install -q \
		-r apps/messenger/requirements.txt \
		-r apps/messenger/requirements-dev.txt

unit: venv ## Модульные тесты, без базы и сети
	@echo "· модульные тесты"
	@cd apps/messenger && .venv/bin/python -m pytest tests -q

migrate: ## Применить миграции к локальной базе
	@echo "· миграции"
	@scripts/run-migrations.sh

smoke: ## Связность: каждое хранилище отвечает на настоящую операцию
	@echo "· связность"
	@scripts/smoke-messenger.sh

send-message: ## Отправить сообщение и получить его: HTTP → Kafka → WebSocket
	@# Две проверки подряд: первая доводит сообщение до потоков Kafka,
	@# вторая - до получателя в канале беседы, и там же доказывает, что
	@# повтор публикации не превращается во второе сообщение.
	@#
	@# KEEP_ACCOUNTS=1 оставит учётные записи первой проверки и напечатает
	@# токен с беседой - тогда тем же токеном можно продолжить руками.
	@echo "· отправка и получение сообщения"
	@INTEGRATION_ONLY=send_message_check.py,realtime_receive_check.py \
		scripts/integration-messenger.sh

integration: ## Код против настоящей базы, тем же образом и тем же путём
	@echo "· интеграция"
	@scripts/integration-messenger.sh

status: ## Что сейчас в кластере
	@kubectl get app -n $(ARGOCD_NAMESPACE) -o custom-columns=\
NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status

# ---------------------------------------------------------------------------

local-down: ## Снять нагрузки мессенджера. Данные остаются, если не задать WIPE=1
	@# Приложения удаляются из Argo CD, а не подавляются на месте: иначе
	@# selfHeal вернёт их через минуту, и это выглядит как «не удалилось».
	@for app in $(MESSENGER_APPS); do \
		kubectl -n $(ARGOCD_NAMESPACE) delete app $$app --ignore-not-found; \
	done
	@if [ "$${WIPE:-0}" = "1" ]; then \
		echo "· удаляю тома - данные будут потеряны"; \
		kubectl -n messenger delete pvc --all --ignore-not-found; \
	else \
		echo "· тома оставлены; WIPE=1 чтобы удалить"; \
	fi
	@echo "нагрузки мессенджера сняты; платформа не тронута"
