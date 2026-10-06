#!/usr/bin/env bash
# systemd supplies the working directory and environment file.
set -euo pipefail

case "${1:-}" in
  market-data) exec python/.venv/bin/python -m user_strategies.market_data ;;
  portfolio-monitor)
    exec python/.venv/bin/python examples/live/binance/portfolio_monitor/monitor.py
    ;;
  portfolio-alert) exec python/.venv/bin/python -m user_strategies.portfolio_alert ;;
  *)
    echo "Unsupported monitoring service: ${1:-missing}" >&2
    exit 2
    ;;
esac
