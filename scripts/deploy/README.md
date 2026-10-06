# Monitoring deployment

Application releases consume an approved prebuilt NautilusTrader wheel. This repository
never compiles the engine. The initial deployment starts public market data, Binance
Portfolio Margin monitoring, and portfolio alerts; it does not start trading.

## Build and deploy through the local runner

The application workflows use `[self-hosted, linux, x64, monitoring-build]` on the
same server holding engine releases. `monitoring build` runs on pushes to `main`
or manually; `monitoring deploy` runs manually on `main` with a successful application
build run ID. No wheel or release archive is uploaded to GitHub.

Configure the application's repository variable `ENGINE_RELEASE_DIR` with the absolute
directory printed by a successful engine **monitoring build**, for example:

```text
<ENGINE_RUNNER_TOOL_CACHE>/monitoring/<ENGINE_OWNER>/<ENGINE_REPO>/releases/<ENGINE_SHA>-<BUILD_RUN_ID>-<ATTEMPT>
```

This directory must contain `monitoring.tar.gz`, its `.sha256` file,
`install-monitoring.bash`, and `release.sha256`. The application verifies those checksums
and the archived engine revision, extracts only the engine wheel, and verifies its
version against `engine-version.txt` before installation. The chosen engine revision,
release directory name, and wheel hash are recorded in the application package's
`ENGINE.json`. Other old application sources in the engine archive are not reused.
To update the engine, deliberately select a new successful release directory. For a
one-off manual build, override it with the `engine_release_dir` workflow input.

A repository-level runner registered to the engine repository cannot also accept
application jobs merely because its labels match. Register a separate runner service
for the application on the same server in its own installation/work directory, or allow
both repositories to use an organization runner. The application runner user needs read
access to the original engine artifact directory. Both application build and deploy jobs
must use the same local artifact storage; assign `monitoring-build` only to that server
unless all matching application runners share storage. The engine runner's tool-cache
path may differ from the application's: `ENGINE_RELEASE_DIR` always uses the original
absolute engine release path.

Create the application's `monitoring-production` environment and configure
`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, and `DEPLOY_KNOWN_HOSTS` there. Install
`gh`, `jq`, `ssh`, and `scp` on the application runner. Runtime account credentials
continue to live on the target server in `/etc/arbitrage-monitor/monitoring.env`.

The application build saves releases at:

```text
<APP_RUNNER_TOOL_CACHE>/arbitrage-app/<APP_OWNER>/<APP_REPO>/releases/<APP_SHA>-<BUILD_RUN_ID>-<ATTEMPT>
```

The manual deployment verifies that the selected run succeeded on `main` in this
application repository, verifies local release hashes, and deploys that exact application
archive through the existing SSH installer. It never deploys directly from an engine run ID.

## Package directly on Linux

Use a clean, committed application checkout on Linux x86_64 with uv and standard
CPython 3.14.4. Obtain the approved Linux CPython 3.14 engine wheel and its trusted
SHA256 from the engine release. Its version must match `engine-version.txt`.

```bash
export ENGINE_WHEEL=/absolute/path/nautilus_trader-2.0.0rc6-cp314-cp314-manylinux_2_39_x86_64.whl
export ENGINE_SHA256=replace-with-the-approved-64-character-sha256
bash scripts/deploy/build-monitoring.bash
```

The script verifies the engine hash and metadata, installs it in `python/.venv`, tests
application code, and packages monitoring sources, the engine wheel, locked runtime
wheels, application revision, and `ENGINE.json` into `dist/monitoring.tar.gz`.
Use a clean release workspace instead of reusing previous wheels in `dist/`.
No Cargo or maturin is invoked. Build a new wheel in the engine repository only when
engine code or Python/platform compatibility changes.

The local Windows migration snapshot cannot be deployed to Linux. Its compiled binary
build revision is not independently known, so it is not an approved production release.

## Install and roll back

The existing installer, systemd service names, configuration paths, and Redis contracts
are retained. The server requires Linux x86_64, standard CPython 3.14.4, uv, Redis, and
the glibc version required by the engine wheel. Configure the account and notification
credentials in `/etc/arbitrage-monitor/monitoring.env` on the server.

```bash
sudo bash scripts/deploy/install-monitoring.bash /path/to/monitoring.tar.gz APP_COMMIT_SHA-RUN_ID
```

The version is the application's full Git commit SHA followed by a numeric release ID.
The installer verifies the archive revision, installs wheels offline before switching
`/opt/arbitrage-monitor/current`, checks service startup, and attempts to restore the
previous release on failure. Existing retained releases remain usable for rollback.

Workflow files are prepared locally. No remote repository, secrets, runner configuration,
or server services were changed. Configure the new application repository and its runner
before switching production releases. The engine checkout's
old application files and pipeline remain as a transition backup. Develop application
changes here. The dashboard remains a separate repository.

## Local checks

```bash
bash -n scripts/deploy/build-monitoring.bash
bash -n scripts/deploy/install-monitoring.bash
bash scripts/deploy/test-monitoring-deploy.bash
```

Deployment tests use temporary directories and fake tools. They check configuration,
release switching, rollback, revision mismatch, and rejection of trading workers.
They do not deploy, access trading accounts, or send notifications.
