# Monitoring deployment with uv

Application builds consume prebuilt engine wheels and never compile the engine.
The initial deployment starts market data, Portfolio Margin monitoring, and portfolio
alerts. It does not start trading.

## Shared local wheel index

Both repositories use `/var/lib/arbitrage-engine/artifacts` by default. Set the same
`ENGINE_ARTIFACT_ROOT` repository variable in both only if choosing another absolute
path. `ENGINE_RELEASE_DIR` is obsolete. Create the shared directory once, as the engine
runner user, with engine write access and application read access:

```bash
sudo install -d -m 0755 -o "$(id -un)" /var/lib/arbitrage-engine/artifacts
```

Successful engine builds publish immutable wheels directly into this directory. Each
package version includes the base version, increasing workflow run number, attempt, and
full engine commit. The index contains wheel files, not engine.json or custom subdirectory
indexes. Existing old manifest directories do not participate in uv resolution.
Run the updated engine workflow once before building the application through this flow.

The engine base version is declared as `nautilus-trader==2.0.0rc6` in the application's
`python/pyproject.toml`. Updating the base version is a deliberate dependency change.
New internal builds of that base version are selected automatically at the next application
release with `uv lock --upgrade-package nautilus-trader`. uv checks compatibility using
wheel tags. The Linux project requires CPython 3.14 and Linux x86_64 wheel availability;
uv's native installation further checks the runner's platform and glibc compatibility.
The engine is bound to an explicit local index, not allowed to fall back to public PyPI.

## Local-runner workflows

Both application workflows use `[self-hosted, linux, x64, monitoring-build]`.
Builds run on pushes to `main` or manually. Deployment runs manually on `main` with a
successful application build run ID. Wheel and archive uploads to GitHub are not used.

A repository-level runner must be registered for the application too, in a separate
installation/work directory on the same server, or both repositories must have access to
an organization runner. The runner user needs read access to the shared wheel directory.
Application build and deployment jobs must share local release storage. Assign the
monitoring-build label only to that server unless matching runners share storage.
Install gh, jq, ssh, and scp. Configure the application's monitoring-production environment
with DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, and DEPLOY_KNOWN_HOSTS.

The build generates `dist/project/pyproject.toml` from the application manifest, using
Linux settings and the shared index. It seeds standard uv.lock from the development lock,
updates the engine dependency, installs with `--frozen --no-build`, and tests the application.
The actual release uv.lock is bundled as `uv.lock`. uv export emits exact runtime pins;
uv pip compile --generate-hashes adds hashes, including local engine wheel hashes.
The packaging step downloads only binary wheels with hash verification.

Release archives remain under:

```text
<APP_RUNNER_TOOL_CACHE>/arbitrage-app/<OWNER>/<REPO>/releases/<APP_SHA>-<RUN_ID>-<ATTEMPT>
```

The manual deployment verifies a successful application run and the local archive, then
uses the existing SSH installer. It never resolves a newer engine while deploying.
The installed distribution version reveals the exact engine source commit.

## Direct Linux packaging

Use a clean committed application checkout on Linux x86_64 with uv 0.12.19 and standard
CPython 3.14.4. The compatible engine wheel must already be in the shared directory:

```bash
export ENGINE_ARTIFACT_ROOT=/var/lib/arbitrage-engine/artifacts
bash scripts/deploy/build-monitoring.bash
```

Use a clean release workspace with no previous dist directory. Windows migration wheels
are development artifacts and are not published as Linux production dependencies.

## Installation and rollback

The target needs Linux x86_64, CPython 3.14.4, uv, Redis, and the glibc required by the wheel.
Configure account and notification credentials in /etc/arbitrage-monitor/monitoring.env.
Existing systemd names, server paths, Redis contracts, and service rollback remain unchanged.

```bash
sudo bash scripts/deploy/install-monitoring.bash /path/to/monitoring.tar.gz APP_COMMIT_SHA-RUN_ID
```

The installer checks the application revision, installs all runtime wheels offline from
hash-locked requirements, and checks imports before switching services. Startup failures
attempt to restore the previous release. Retained release archives use their original
locks and wheels. No runner registration, secrets, remote repository configuration, or
server deployment was performed by this local code change.

## Checks

```bash
uv run --project python --frozen python -m pytest user_strategies examples/live/binance/portfolio_monitor scripts -q
bash -n scripts/deploy/build-monitoring.bash
bash scripts/deploy/test-monitoring-deploy.bash
```

Deployment tests use isolated directories and fake system tools; they do not install
services, access trading accounts, or send notifications.
