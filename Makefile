# Разделение намеренное: bootstrap поднимает кластер и платформу, local-up -
# только мессенджер. Если смешать, local-up превратится в чудовище, которое
# никто не будет запускать, потому что оно каждый раз трогает всё.
#
#   make bootstrap    кластер, Cilium, Argo CD, корневое приложение
#   make local-up     ждёт, пока приложения мессенджера станут Healthy
#   make local-test   контракты, миграции, проверка связности
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

.PHONY: help bootstrap local-up local-test local-down contracts migrate smoke status

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

local-test: contracts migrate smoke ## Контракты, миграции и проверка связности

contracts: ## Схемы связны и обратно совместимы
	@echo "· контракты"
	@python3 packages/contracts/validate.py $${CONTRACT_BASE:-}

migrate: ## Применить миграции к локальной базе
	@echo "· миграции"
	@scripts/run-migrations.sh

smoke: ## Связность: каждое хранилище отвечает на настоящую операцию
	@echo "· связность"
	@scripts/smoke-messenger.sh

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
