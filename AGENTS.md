# Application development

This repository contains application code, not NautilusTrader engine source.
Use the uv-managed `python/.venv` from the repository root. The Windows development
project uses `engine-wheels/`; Linux release configuration is generated from that same
dependency declaration with the runner's flat index. Let uv resolve, lock, and install
the engine. Never compile the engine as part of application builds. Engine wheel
versions must identify their full source commit. Publish releases with their uv.lock
and uv-generated hashed requirements; offline installation must require hashes.

Preserve exact prices, quantities, fees, and money with domain types or Decimal.
Run relevant tests after changes. Do not start live trading, send notifications,
commit, push, create remote repositories, or deploy without explicit user authorization.
Keep credentials, binary wheels, environments, logs, and release archives out of Git.
Preserve user_strategies module paths and Redis contracts during migration.
