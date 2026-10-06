#!/usr/bin/env bash
# Exercise deployment switching and rollback with isolated paths and fake system tools.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/bin" "$fixture/etc/arbitrage-monitor" "$fixture/etc/systemd/system"
export TEST_LOG="$fixture/systemctl.log"
export TEST_UNIT="$fixture/etc/systemd/system/arbitrage-monitor@.service"
export TEST_FAIL_SERVICE=""
export PATH="$fixture/bin:$PATH"

cat > "$fixture/bin/systemctl" << 'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_LOG"
if [[ "$1" == stop && ! -f "$TEST_UNIT" ]]; then
  exit 5
fi
if [[ "$1" == is-active && "$*" == *"${TEST_FAIL_SERVICE:-never-match}"* ]]; then
  exit 1
fi
if [[ "$1" == show ]]; then
  echo 0
fi
MOCK
cat > "$fixture/bin/uv" << 'MOCK'
#!/usr/bin/env bash
if [[ "$1" == venv ]]; then
  mkdir -p "$4/bin"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$4/bin/python"
  chmod +x "$4/bin/python"
fi
MOCK
cat > "$fixture/bin/runuser" << 'MOCK'
#!/usr/bin/env bash
shift 3
exec "$@"
MOCK
for command in python3.14 sleep id; do
  printf '#!/usr/bin/env bash\nexit 0\n' > "$fixture/bin/$command"
done
chmod +x "$fixture/bin/"*

# Adapt a temporary copy only; the production installer has no testing switches.
# shellcheck disable=SC2016 # Match the literal EUID check rather than expanding it here
sed -e "s|/opt/arbitrage-monitor|$fixture/opt/arbitrage-monitor|g" \
  -e "s|/etc/arbitrage-monitor|$fixture/etc/arbitrage-monitor|g" \
  -e "s|/etc/systemd/system|$fixture/etc/systemd/system|g" \
  -e 's/\[\[ "$EUID" -eq 0 \]\]/true/' \
  "$script_dir/install-monitoring.bash" > "$fixture/install.bash"

sha=1111111111111111111111111111111111111111
mkdir -p "$fixture/bundle/scripts/deploy" "$fixture/bundle/wheelhouse"
printf '%s\n' "$sha" > "$fixture/bundle/REVISION"
touch "$fixture/bundle/monitoring-requirements.lock"
touch "$fixture/bundle/wheelhouse/nautilus_trader-test.whl"
cp "$script_dir/arbitrage-monitor@.service" "$fixture/bundle/scripts/deploy/"
tar -czf "$fixture/bundle.tar.gz" -C "$fixture/bundle" .

if bash "$fixture/install.bash" "$fixture/bundle.tar.gz" "$sha-1" > /dev/null 2>&1; then
  echo "Missing server configuration should reject deployment" >&2
  exit 1
fi
[[ ! -e "$TEST_LOG" ]]
touch "$fixture/etc/arbitrage-monitor/monitoring.env"
base="$fixture/opt/arbitrage-monitor"
export TEST_FAIL_SERVICE=portfolio-alert
if bash "$fixture/install.bash" "$fixture/bundle.tar.gz" "$sha-5" \
  > "$fixture/first-failure.log" 2>&1; then
  echo "An unhealthy first deployment should fail" >&2
  exit 1
fi
[[ "$(readlink -e "$base/current")" == "$base/releases/$sha-5" ]]
grep -q 'First deployment failed; monitoring services stopped' "$fixture/first-failure.log"
rm "$base/current" "$TEST_UNIT"
: > "$TEST_LOG"
export TEST_FAIL_SERVICE=""
bash "$fixture/install.bash" "$fixture/bundle.tar.gz" "$sha-2" > /dev/null
[[ "$(readlink "$base/current")" == "$base/releases/$sha-2" ]]
grep -q 'is-active --quiet arbitrage-monitor@portfolio-alert' "$TEST_LOG"

export TEST_FAIL_SERVICE=portfolio-alert
if bash "$fixture/install.bash" "$fixture/bundle.tar.gz" "$sha-3" > /dev/null 2>&1; then
  echo "An unhealthy worker should reject deployment" >&2
  exit 1
fi
[[ "$(readlink "$base/current")" == "$base/releases/$sha-2" ]]
grep -q '^restart arbitrage-monitor@market-data' "$TEST_LOG"

# A revision mismatch must fail before stopping the active release.
before="$(wc -l < "$TEST_LOG")"
if bash "$fixture/install.bash" "$fixture/bundle.tar.gz" \
  2222222222222222222222222222222222222222-4 > /dev/null 2>&1; then
  echo "A mismatched revision should reject deployment" >&2
  exit 1
fi
[[ "$(wc -l < "$TEST_LOG")" == "$before" ]]

if bash "$script_dir/run-monitoring.bash" dynamic-basis-watch > /dev/null 2>&1; then
  echo "Trading must not be a supported monitoring service" >&2
  exit 1
fi
echo "Monitoring deployment tests passed"
