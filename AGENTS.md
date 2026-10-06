# Application development

This repository contains trading application code, not NautilusTrader engine source.
Use `python/.venv` with `uv run --project python --no-sync` from the repository root.
Install the engine from a prebuilt wheel matching `engine-version.txt`, the operating
system, and Python version. Never build or install the engine from a source checkout
as part of application development or release packaging.

Preserve exact prices, quantities, fees, and money with domain types or Decimal.
Run relevant tests after changes. Do not start live trading, send notifications,
commit, push, create remote repositories, or deploy without explicit user authorization.
Keep credentials, wheel binaries, environments, logs, and release archives out of Git.
Preserve the `user_strategies` module paths and Redis contracts during migration.
