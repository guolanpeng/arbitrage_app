# Binance Portfolio Margin monitor

This small NautilusTrader project connects one Binance Portfolio Margin execution
client and displays its account state and reconciled open futures positions. It
registers no data client and submits no orders. The unified client uses the
Portfolio Margin account stream and PAPI account and position queries.

From the application repository root, after installing the approved engine wheel, run:

```bash
uv run --project python --no-sync --env-file .env python examples/live/binance/portfolio_monitor/monitor.py
```

Use `--once` for one snapshot or `--interval 60` to display every 60 seconds.
The default interval is 30 seconds. Press Ctrl+C to stop.

Console output uses UTF-8. ANSI colors are enabled only for an interactive terminal,
so redirected logs can be opened as plain UTF-8 text. Restart the monitor after updating
the script to apply these settings; existing log files retain their original encoding.

The monitor also writes each refreshed snapshot as ordinary JSON to Redis at
`account:v1:binance:portfolio`, with a default TTL of 60 seconds. It uses
`MARKET_REDIS_URL` and defaults to `redis://localhost:6379/0`, matching
`user_strategies/market_data.py`. Use `--redis-url` or `--redis-ttl` to override
those settings. The JSON contains the full account summary, asset balances, and
all non-zero UM and CM `positionRisk` rows without converting exchange numeric
strings.

Put `BINANCE_API_KEY` and `BINANCE_API_SECRET` in the root `.env`. The monitor
prints the exchange's account-wide `uniMMR`, account status, USD equity and
margin, plus balances and liabilities by asset. It queries the UM and CM
`positionRisk` HTTP endpoints and displays each open futures position's exact
quantity, entry price, mark price, liquidation price, unrealized PnL, leverage,
notional value, and exchange update time. Margin holdings are shared by asset
rather than represented as separate pair positions.

The execution client loads public metadata for all three markets on startup,
so initial connection can take time. Position risk is refreshed over HTTP with
the account snapshot; no public market-data client is required. ADL quantiles
are not part of `positionRisk` and are not shown. The `uniMMR` is account-wide
rather than per-position.
