#!/usr/bin/env bash
# LOCAL-CAPACITY-001 (docs/messenger/15-load-testing-platform.md, раздел 29)
# — единственная точка входа, которой достаточно и человеку, и CI, чтобы
# запустить прогон: submit WorkflowTemplate(parameters) одной командой, а не
# переписывать kubectl/bash orchestration заново на каждый вызов (раздел 29
# явно называет это неправильной моделью). Вся логика прогона — preflight,
# provision, k6/identity, reconciliation, cleanup — уже живёт в самом
# WorkflowTemplate (gitops/04-messenger/load-testing/manifests/); этот скрипт
# только собирает Workflow из параметров и (опционально) ждёт исход.
#
# Namespace, ServiceAccount и сам WorkflowTemplate заводятся ArgoCD из
# gitops/04-messenger/load-testing/ — если их нет, значит стенд ещё не
# синхронизирован, а не что этот скрипт сломан.
set -euo pipefail

PROFILE="messages"
USERS="100"
TARGET_RATE="10"
DURATION="2m"
K6_PARALLELISM="1"
CLEANUP="true"
RUN_ID=""
WAIT="false"
NAMESPACE="load-testing"
EXTRA_PARAMS=""

usage() {
	cat <<'USAGE'
Использование: scripts/load-testing-run.sh [опции]

  --profile <messages|identity|connections|stress|recovery|mixed|media>
                            профиль нагрузки (по умолчанию messages)
  --users <N>               число synthetic-пользователей (по умолчанию 100)
  --target-rate <N>         целевой rate сообщений/с для open-model k6 (по умолчанию 10)
  --duration <k6-duration>  длительность k6-сценария, напр. 2m, 30s (по умолчанию 2m)
  --k6-parallelism <N>      TestRun.spec.parallelism (по умолчанию 1)
  --run-id <строка>         run_id; по умолчанию генерируется как local-<epoch>-<random>
  --no-cleanup              не удалять synthetic-данные после прогона (диагностика)
  --param <имя=значение>    дополнительный параметр Workflow (повторяемый), напр. call_pairs=3
  --wait                    дождаться терминального статуса Workflow;
                            код возврата ненулевой при Failed/Error/таймауте
  -h, --help                эта справка

git_sha берётся из `git rev-parse HEAD` автоматически — параметр отдельно не
передаётся: он существует в самом Workflow как метка происхождения прогона,
а не как настройка, которую имеет смысл переопределять руками.
USAGE
}

while [ $# -gt 0 ]; do
	case "$1" in
	--profile)
		PROFILE="$2"
		shift 2
		;;
	--users)
		USERS="$2"
		shift 2
		;;
	--target-rate)
		TARGET_RATE="$2"
		shift 2
		;;
	--duration)
		DURATION="$2"
		shift 2
		;;
	--k6-parallelism)
		K6_PARALLELISM="$2"
		shift 2
		;;
	--run-id)
		RUN_ID="$2"
		shift 2
		;;
	--param)
		name="${2%%=*}"
		value="${2#*=}"
		EXTRA_PARAMS="${EXTRA_PARAMS}      - name: ${name}
        value: \"${value}\"
"
		shift 2
		;;
	--no-cleanup)
		CLEANUP="false"
		shift
		;;
	--wait)
		WAIT="true"
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		echo "неизвестный аргумент: $1" >&2
		usage >&2
		exit 1
		;;
	esac
done

if [ -z "$RUN_ID" ]; then
	RUN_ID="local-$(date +%s)-$((RANDOM % 10000))"
fi

GIT_SHA="$(git rev-parse HEAD)"

MANIFEST="$(mktemp)"
trap 'rm -f "$MANIFEST"' EXIT

cat >"$MANIFEST" <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: local-capacity-${PROFILE}-
  namespace: ${NAMESPACE}
spec:
  workflowTemplateRef:
    name: ${WORKFLOW_TEMPLATE:-messenger-local-capacity}
  arguments:
    parameters:
      - name: run_id
        value: "${RUN_ID}"
      - name: git_sha
        value: "${GIT_SHA}"
      - name: profile
        value: "${PROFILE}"
      - name: users
        value: "${USERS}"
      - name: target_rate
        value: "${TARGET_RATE}"
      - name: duration
        value: "${DURATION}"
      - name: k6_parallelism
        value: "${K6_PARALLELISM}"
      - name: cleanup
        value: "${CLEANUP}"
${EXTRA_PARAMS}EOF

WF_NAME="$(kubectl create -f "$MANIFEST" -o jsonpath='{.metadata.name}')"
echo "Workflow: ${WF_NAME} (namespace ${NAMESPACE}, run_id=${RUN_ID}, profile=${PROFILE})"
echo "kubectl get wf -n ${NAMESPACE} ${WF_NAME}"

if [ "$WAIT" != "true" ]; then
	exit 0
fi

echo "жду терминального статуса..."
# 240×15s = 60 минут — заведомо больше самого долгого профиля (stress/mixed
# из раздела 18/17 ещё не реализованы, но бюджет времени рассчитан на них
# заранее, а не только на messages/identity).
for _ in $(seq 1 240); do
	phase="$(kubectl get wf -n "$NAMESPACE" "$WF_NAME" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
	case "$phase" in
	Succeeded)
		echo "Succeeded"
		exit 0
		;;
	Failed | Error)
		echo "статус: ${phase}" >&2
		kubectl get wf -n "$NAMESPACE" "$WF_NAME" -o jsonpath='{.status.message}' >&2
		echo >&2
		exit 1
		;;
	esac
	sleep 15
done

echo "Workflow не завершился за отведённое время (60 минут)" >&2
exit 1
