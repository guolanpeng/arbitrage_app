# Arbitrage application

Strategies, public market collection, account monitoring, and application deployment
live here. NautilusTrader engine source remains in `D:/WorkSpace/arbitrage_platform`;
the dashboard remains in `D:/WorkSpace/market_dashboard`.

## Windows setup

From `D:/WorkSpace/arbitrage_app`:

```powershell
uv sync --project python --group test --python 3.13.2
uv run --project python --no-sync python scripts/install-engine.py engine-wheels/nautilus_trader-2.0.0rc6-cp313-cp313-win_amd64.whl WHEEL_SHA256
uv run --project python --no-sync python -m pytest user_strategies examples/live/binance/portfolio_monitor scripts/test_install_engine.py -q
```

Replace `WHEEL_SHA256` with the trusted artifact hash. The local migration package and its
hash/provenance are in the ignored `engine-wheels/` directory. It was repacked from the
existing Windows CPython 3.13 editable installation without compiling. The Python
facades and compiled binary were copied together, and the independent installation
must pass the application tests. It is a local migration snapshot, not a Linux release.

Engine installation is separate from dependency synchronization. After changing application
dependencies, synchronize them and reinstall the approved wheel. Use `--no-sync` for normal
application commands so uv does not remove the separately installed engine.
Only engine changes require building a new engine wheel in the engine repository.

The existing local `.env` was copied during migration and is ignored by Git.
For a clean machine, copy `.env.example` to `.env` and configure it locally.

## Run

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.market_data
uv run --project python --no-sync --env-file .env python examples/live/binance/portfolio_monitor/monitor.py
```

Use `scripts/start-monitoring.ps1 -ValidateOnly` to check configuration without starting
workers. The monitoring launcher starts no trading strategy. See
[application documentation](user_strategies/README.md) for strategy and alert commands.
Keep the dashboard's `MARKET_REDIS_URL` consistent with the application's configuration.

## Deploy

See [monitoring deployment](scripts/deploy/README.md). Application release packaging
requires a trusted Linux CPython 3.14.4 engine wheel and its SHA256; it never invokes
Cargo or maturin. The existing installer, systemd names, environment location, Redis keys,
and rollback behavior remain in use. No server or remote GitHub configuration was changed
by the local migration. Local-runner build and deployment workflow files are included;
configure `ENGINE_RELEASE_DIR`, runner access, and deployment secrets as described in the
deployment guide before using them. Old application files remain in the engine checkout as a transition
backup for its existing deployment workflow; develop new application changes here.
