#!/usr/bin/env bash
# Resolve and package application dependencies with uv; never compile the engine.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
artifact_root="${ENGINE_ARTIFACT_ROOT:-/var/lib/arbitrage-engine/artifacts}"
[[ "$artifact_root" == /* ]] || exit 2
[[ ! -e dist ]] || {
  echo "Use a clean release workspace; dist contains a previous build" >&2
  exit 2
}
git diff --quiet
git diff --cached --quiet
git rev-parse HEAD > /dev/null
python3.14 scripts/release-project.py --index "$artifact_root"
export UV_PROJECT_ENVIRONMENT="$PWD/python/.venv"
uv lock --project dist/project --python 3.14.4 --no-build --upgrade-package nautilus-trader
uv sync --project dist/project --frozen --group test --python 3.14.4 --no-build
uv run --project dist/project --no-sync python -c \
  'import importlib.metadata as m, re; v=m.version("nautilus-trader"); assert re.fullmatch(r"[^+]+\+build\.[1-9][0-9]*\.attempt\.[1-9][0-9]*\.g[0-9a-f]{40}", v), v; print("Selected engine:", v)'
uv run --project dist/project --no-sync python -m pytest \
  user_strategies examples/live/binance/portfolio_monitor scripts -q
uv run --project dist/project --no-sync python \
  examples/live/binance/portfolio_monitor/monitor.py --help
for script in scripts/deploy/*.bash; do
  bash -n "$script"
done
bash scripts/deploy/test-monitoring-deploy.bash

# Local flat indexes omit hashes in uv.lock; uv computes them in the deployment requirements.
uv export --project dist/project --frozen --no-dev --no-emit-project --no-hashes \
  --output-file dist/requirements.in
uv pip compile dist/requirements.in --no-config --no-build --python-version 3.14 \
  --find-links "$artifact_root" --generate-hashes --output-file dist/monitoring-requirements.lock
mkdir -p dist/wheelhouse
uv run --project dist/project --no-sync --with pip python -m pip download \
  --only-binary=:all: --require-hashes --find-links "$artifact_root" \
  -r dist/monitoring-requirements.lock --dest dist/wheelhouse
cp dist/project/uv.lock dist/uv.lock
git rev-parse HEAD > dist/REVISION
tar --exclude='__pycache__' --exclude='test_*.py' -czf dist/monitoring.tar.gz \
  user_strategies/__init__.py user_strategies/market_data.py user_strategies/portfolio_alert.py \
  examples/live/binance/portfolio_monitor/monitor.py \
  scripts/deploy/arbitrage-monitor@.service scripts/deploy/run-monitoring.bash \
  -C dist wheelhouse monitoring-requirements.lock REVISION uv.lock
(
  cd dist
  sha256sum monitoring.tar.gz > monitoring.tar.gz.sha256
)
mkdir -p dist/monitoring-release
cp dist/monitoring.tar.gz dist/monitoring.tar.gz.sha256 scripts/deploy/install-monitoring.bash \
  dist/monitoring-release/
