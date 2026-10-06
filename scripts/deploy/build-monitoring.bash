#!/usr/bin/env bash
# Package the application with an approved prebuilt Linux engine wheel.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
engine_wheel="${ENGINE_WHEEL:?Set ENGINE_WHEEL to an approved Linux CPython 3.14 wheel}"
engine_sha256="${ENGINE_SHA256:?Set ENGINE_SHA256 to its trusted SHA256}"
[[ -f "$engine_wheel" && "$engine_wheel" == /* ]] || {
  echo "ENGINE_WHEEL must be an absolute path to an existing wheel" >&2
  exit 2
}
[[ "$engine_sha256" =~ ^[0-9a-fA-F]{64}$ ]] || exit 2
case "$(basename "$engine_wheel")" in
  nautilus_trader-*-cp314-cp314-*linux*x86_64.whl) ;;
  *) echo "Requires a Linux x86_64 CPython 3.14 engine wheel" >&2; exit 2 ;;
esac
[[ ! -e dist/monitoring.tar.gz && ! -e dist/wheelhouse ]] || {
  echo "Use a clean release workspace; dist contains a previous release" >&2
  exit 2
}
git diff --quiet
git diff --cached --quiet
git rev-parse HEAD > /dev/null
uv sync --project python --frozen --group test --python 3.14.4
uv run --project python --no-sync python scripts/install-engine.py "$engine_wheel" "$engine_sha256"
mkdir -p dist/wheelhouse
cp "$engine_wheel" dist/wheelhouse/
uv export --project python --frozen --no-dev --no-emit-project --no-hashes \
  --output-file dist/monitoring-requirements.lock
uv run --project python --no-sync python - "$engine_wheel" "$engine_sha256" <<'PY'
import json
import os
import sys
from pathlib import Path
engine = {
    "version": Path("engine-version.txt").read_text().strip(),
    "wheel": Path(sys.argv[1]).name,
    "sha256": sys.argv[2].lower(),
}
source = os.getenv("ENGINE_SOURCE_MANIFEST")
if source:
    selected = json.loads(Path(source).read_text())
    assert selected["sha256"] == sys.argv[2].lower()
    engine["source"] = selected
Path("dist/ENGINE.json").write_text(json.dumps(engine) + "\n")
PY
uv run --project python --no-sync python -m pytest \
  user_strategies examples/live/binance/portfolio_monitor scripts -q
uv run --project python --no-sync python \
  examples/live/binance/portfolio_monitor/monitor.py --help
for script in scripts/deploy/*.bash; do
  bash -n "$script"
done
bash scripts/deploy/test-monitoring-deploy.bash

# Download the exact runtime dependencies so deployment can install offline.
uv run --project python --no-sync --with pip python -m pip download --only-binary=:all: \
  -r dist/monitoring-requirements.lock \
  --dest dist/wheelhouse
git rev-parse HEAD > dist/REVISION
tar --exclude='__pycache__' --exclude='test_*.py' -czf dist/monitoring.tar.gz \
  user_strategies/__init__.py user_strategies/market_data.py user_strategies/portfolio_alert.py \
  examples/live/binance/portfolio_monitor/monitor.py \
  scripts/deploy/arbitrage-monitor@.service scripts/deploy/run-monitoring.bash \
  -C dist wheelhouse monitoring-requirements.lock REVISION ENGINE.json
(
  cd dist
  sha256sum monitoring.tar.gz > monitoring.tar.gz.sha256
)
mkdir -p dist/monitoring-release
cp dist/monitoring.tar.gz dist/monitoring.tar.gz.sha256 scripts/deploy/install-monitoring.bash \
  dist/monitoring-release/
