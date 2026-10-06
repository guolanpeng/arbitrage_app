#!/usr/bin/env bash
# Ubuntu server deployment; invoke with sudo, archive path, and SHA-run ID.
set -euo pipefail
umask 022

archive="${1:?Usage: install-monitoring.bash ARCHIVE SHA-RUN_ID}"
version="${2:?Missing SHA-RUN_ID}"
[[ "$version" =~ ^[0-9a-f]{40}-[0-9]+$ ]] || exit 2
[[ "$EUID" -eq 0 ]] || {
  echo "Run with sudo" >&2
  exit 2
}
[[ "$(uname -m)" == x86_64 ]] || {
  echo "Requires x86_64" >&2
  exit 2
}
python3.14 -c 'import sys; assert sys.version_info[:3] == (3, 14, 4)'
command -v uv > /dev/null
[[ -f /etc/arbitrage-monitor/monitoring.env ]] || {
  echo "Create /etc/arbitrage-monitor/monitoring.env before deploying" >&2
  exit 2
}

base=/opt/arbitrage-monitor
mkdir -p "$base/releases"
exec 9> "$base/deploy.lock"
flock -n 9 || {
  echo "Another deployment is running" >&2
  exit 2
}
[[ ! -e "$base/current" || -L "$base/current" ]] || exit 2
release="$base/releases/$version"
[[ ! -e "$release" ]] || {
  echo "Release already exists: $release" >&2
  exit 2
}
mkdir "$release"
tar --no-same-owner --no-same-permissions -xzf "$archive" -C "$release"
[[ "$(cat "$release/REVISION")" == "${version%-*}" ]] || {
  echo "Archive revision differs from selected build" >&2
  exit 2
}
uv venv --python python3.14 "$release/python/.venv"
uv pip install --python "$release/python/.venv/bin/python" --no-index \
  --find-links "$release/wheelhouse" -r "$release/monitoring-requirements.lock" \
  "$release"/wheelhouse/nautilus_trader-*.whl
id arbitrage-monitor > /dev/null 2>&1 ||
  useradd --system --home-dir /var/lib/arbitrage-monitor --shell /usr/sbin/nologin arbitrage-monitor
(
  cd "$release"
  runuser -u arbitrage-monitor -- python/.venv/bin/python \
    -c 'import nautilus_trader, httpx, redis, lark_oapi'
  runuser -u arbitrage-monitor -- python/.venv/bin/python \
    examples/live/binance/portfolio_monitor/monitor.py --help
)

services=(arbitrage-monitor@market-data arbitrage-monitor@portfolio-monitor arbitrage-monitor@portfolio-alert)
previous=""
if [[ -L "$base/current" ]]; then
  previous="$(readlink -e "$base/current" || true)"
fi
switched=false
rollback() {
  if [[ "$switched" == true ]]; then
    systemctl stop "${services[@]}" || true
    if [[ -n "$previous" && -d "$previous" ]]; then
      ln -sfn "$previous" "$base/current.next"
      mv -Tf "$base/current.next" "$base/current"
      install -m 644 "$previous/scripts/deploy/arbitrage-monitor@.service" /etc/systemd/system/
      systemctl daemon-reload
      systemctl restart "${services[@]}" || true
      echo "Deployment failed; restored $previous" >&2
    else
      echo "First deployment failed; monitoring services stopped" >&2
    fi
  fi
}
trap 'rollback; exit 1' ERR INT TERM

# Stop all old workers before switching, so notifications and collectors do not overlap.
switched=true
if [[ -L "$base/current" ]]; then
  systemctl stop "${services[@]}"
fi
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
install -m 644 "$release/scripts/deploy/arbitrage-monitor@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "${services[@]}"
systemctl start "${services[@]}"
sleep 15
for service in "${services[@]}"; do
  systemctl is-active --quiet "$service"
  [[ "$(systemctl show --property=NRestarts --value "$service")" == 0 ]]
done
trap - ERR INT TERM
echo "Deployed $version; all three monitoring processes are active"
