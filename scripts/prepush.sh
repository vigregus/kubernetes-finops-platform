#!/usr/bin/env bash
# Те же проверки, что в .github/workflows/contracts.yml (кроме сборки образов и Storybook).
# Останавливается на первой ошибке: `bash scripts/prepush.sh && git push`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
for s in check-dashboards check-kafka-security check-layers check-migrations check-log-streams \
         check-chart-env; do
  echo "· $s"; python3 "scripts/$s.py" >/dev/null
done
echo "· контракты"
python3 packages/contracts/test_validate.py >/dev/null
python3 packages/contracts/validate.py "$(git merge-base origin/main HEAD)" >/dev/null
echo "· сервер: ruff + pytest"
# В отдельном worktree своего .venv может не быть: берётся из основной копии.
PY="${MESSENGER_PYTHON:-apps/messenger/.venv/bin/python}"
[ -x "$PY" ] || PY="$(git worktree list --porcelain | awk 'NR==1{print $2}')/apps/messenger/.venv/bin/python"
(cd apps/messenger && "$PY" -m ruff check messenger tests && "$PY" -m pytest tests -q)
echo "· веб: типы, тесты, сборка"
(cd apps/web && npx tsc -b && npx vitest run --project unit && npm run build:app >/dev/null)
# Эти две читают собранный `apps/web/dist`, поэтому идут после сборки.
for s in check-web-purge check-runtime-config; do
  echo "· $s"; python3 "scripts/$s.py" >/dev/null
done
echo "· storybook"
(cd apps/web && npm run build-storybook >/dev/null)
echo "✓ можно пушить"
