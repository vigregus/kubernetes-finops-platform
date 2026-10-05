#!/usr/bin/env bash
# Те же проверки, что в .github/workflows/contracts.yml (кроме сборки образов и Storybook).
# Останавливается на первой ошибке: `bash scripts/prepush.sh && git push`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
for s in check-dashboards check-kafka-security check-layers check-migrations check-log-streams \
         check-web-purge check-runtime-config check-chart-env; do
  echo "· $s"; python3 "scripts/$s.py" >/dev/null
done
echo "· контракты"
python3 packages/contracts/test_validate.py >/dev/null
python3 packages/contracts/validate.py "$(git merge-base origin/main HEAD)" >/dev/null
echo "· сервер: ruff + pytest"
(cd apps/messenger && .venv/bin/python -m ruff check messenger tests && .venv/bin/python -m pytest tests -q)
echo "· веб: типы, тесты, сборка"
(cd apps/web && npx tsc --noEmit -p . && npx vitest run --project unit && npm run build:app >/dev/null)
echo "✓ можно пушить"
