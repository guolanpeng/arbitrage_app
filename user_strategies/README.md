# Public market data collector

## NautilusTrader basis strategy

[`basis_watch.py`](basis_watch.py) buys spot at the best bid with a post-only limit
order and immediately sells each spot fill with a perpetual market order. Create
`BasisWatchConfig` with the required `target_notional` (the USDT purchase amount as a `Decimal` or decimal
string) and `alert_basis_percent`. There is no read-only monitoring mode.

The entry basis is `(perp_bid / spot_order_price - 1) * 100`, using the actual resting
order price. Below the threshold, it cancels the unfilled spot quantity. After
cancellation confirmation and completion of outstanding hedges, fresh books meeting the
threshold allow a new best-bid order. Its base quantity is the remaining USDT purchase
amount divided by the new best bid, rounded down to the spot quantity step. Remaining
USDT is `target_notional - sum(fill_quantity * actual_fill_price)` across all spot fills,
including fills from canceled orders. Fees are excluded from this purchase target.
Fills during cancellation are hedged too. Stale or missing books cancel the spot entry.
Order rejection or an incomplete terminal hedge pauses new entries and logs the exposure.
Cancellation confirmation and hedge fills immediately check whether entry can resume;
they do not wait for the next market update or timer. Cancellation rejection rechecks
the entry immediately, with a one-second delay before retrying cancellation.

`basis_watch.py` exposes the strategy and configuration classes for creation by
`dynamic_basis_watch`; it has no standalone node or command-line entry point.
Execution requires matching base/quote assets, a non-inverse perpetual with multiplier
1, and spot quantity steps divisible by perpetual quantity steps. Both instruments
must quote in USDT. Exchange minimums can prevent hedging a very small partial fill;
a failed hedge pauses entry rather than increasing or rounding the hedge quantity.
Standalone instances retain the original exit behavior unless persistent exit control is enabled.
Automatic repair of failed or incomplete hedges is not implemented.
The strategy subscribes to live perpetual funding rates. Negative funding and an exit
basis `(perp_ask / spot_bid - 1) * 100 <= -0.2%` trigger an exit. It cancels remaining
spot buys and waits for their fills to be hedged before selling its remaining spot
quantity with a post-only limit order at the best ask. Each sell fill immediately
triggers an equal-quantity perpetual market buy with `reduce_only=True`. The resting
sell uses its actual order price against the perpetual ask to check the exit basis.
Nonnegative or stale funding, stale books, or an exit basis above the threshold cancel
the remaining sell; it resumes when conditions recover, without reopening spot buys.
The strategy stops after both legs have fully closed; the node remains running.
Rejected orders or failed exit hedges pause further orders and log the exposure.
Each pair needs its own strategy instance, USDT target notional, execution clients,
and unique `strategy_id`. The calculation is a gross price gap; fees, funding, and
slippage affect actual profit.

The dynamic node uses the framework's PostgreSQL cache backend. Set `POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_USERNAME`, `POSTGRES_PASSWORD`, and `POSTGRES_DATABASE` in
the ignored application-root `.env`; see `../.env.example`. The local `arbitrage-postgres` container
exposes PostgreSQL at `127.0.0.1:5432` and stores data in the
`arbitrage-postgres-data` Docker volume. Its schema has been initialized from this
engine release's `schema/sql` files. Bulk cache loading on startup remains disabled; restart
recovery uses the persisted instance and its order-event ledger as described below. Trading
order events are persisted in `order_event` through the asynchronous database writer.

[`dynamic_basis_watch.py`](dynamic_basis_watch.py) reads Gate spot, Binance perpetual,
and Binance funding snapshots from Redis. At startup it selects the highest-ranked
fresh candidate and loads only that pair. It exits without connecting execution clients
if no candidate is available. The node registers `GATE_SPOT` and `BINANCE_FUTURES`
execution clients, requests 2x leverage for the selected Binance symbol on connection,
and creates at most one strategy after a fresh scan confirms that symbol. The entry
threshold is 0.5%. It does not switch symbols or replace a stopped or completed strategy.
If instrument compatibility checks fail, the selected strategy stops without placing orders.

Each strategy defaults to 250 USDT of Gate spot purchases; `--target-notional` overrides
this amount. It excludes Binance margin and fees. Binance margin requirements follow Portfolio Margin account
rules, rather than simply dividing the contract notional by two. Available balances
are enforced by the exchanges; the shared `ArbitrageBudget` helper is not yet wired
into this entry point. The Portfolio Margin client fails connection if leverage
initialization fails. Configure `GATE_API_KEY`,
`GATE_API_SECRET`, optional `GATE_USER_ID`, `BINANCE_API_KEY`, and `BINANCE_API_SECRET`
in `.env`. Gate uses an ordinary spot account. Binance uses the Portfolio Margin
unified account and REST trading;
Portfolio Margin Pro is not supported by this backend.

The following command enables live trading with the default Gate spot target of 250 USDT:

```powershell
$env:MARKET_REDIS_URL = "redis://127.0.0.1:6379/0"
uv run --project python --no-sync --env-file .env python -m user_strategies.dynamic_basis_watch
```

Keep the REST collector running so its Redis snapshots stay fresh. The scanner logs the
perpetual's original funding rate, settlement interval, eight-hour equivalent rate,
turnover, and the selected pair's reference basis. The controller polls every five seconds;
the trading strategy evaluates the native live order books for actual entry and cancellation.

The controller also writes its public configuration, selected symbol, creation ID,
and heartbeat to Redis key `strategy:v1:dynamic-basis:controller` from its existing
background polling thread. No account credentials are included. Display-write failures
do not discard candidate updates or pause strategy execution. This heartbeat describes
the controller, not strategy liveness. The dashboard marks it stale after 20 seconds;
the last snapshot is retained for inspection. Existing running processes must load the
updated code on their next normal restart; the dashboard never starts or restarts trading.

### Configurable venue adapters

The default remains `gate_spot` / `binance_perp`. Select registered profiles with
`BASIS_SPOT_PROFILE` and `BASIS_PERP_PROFILE` in `.env`. Each strategy persists both
legs' profile, venue, market, full instrument ID, canonical/native symbols, account ID
and client ID. API credentials stay in the local environment. Restart uses the original
profiles and IDs, even when the profiles selected for new entries have changed. Missing
profiles, changed account routes and incompatible instruments stop recovery; unknown legacy
instances are not guessed. The known old Gate/Binance records are migrated in memory.

To add another venue, implement the adapter interface in `basis_adapters.py` and register
its local class path through `BASIS_ADAPTERS`, for example:

```dotenv
BASIS_ADAPTERS='{"my_spot":"user_strategies.my_adapter:MySpotAdapter","my_perp":"user_strategies.my_adapter:MyPerpAdapter"}'
BASIS_SPOT_PROFILE=my_spot
BASIS_PERP_PROFILE=my_perp
```

An adapter supplies `venue`, `market`, `client_id`, `account_id`, and these methods:

- `leg(symbol)` returns exact `instrument_id` and `native_symbol` strings.
- `add_data(builder, leg)` and `add_exec(builder, leg)` register the matching Nautilus
  factories/configurations and return the builder. Resolve credentials locally, not from the DB.
- `account(reader, leg)` performs read-only requests. Spot returns `balances` (rows with
  `currency`, `available`, `locked`); perpetual returns `positions` (canonical `symbol`,
  `positionSide`, signed `positionAmt`, `markPrice`). Both return `open_orders` rows with full
  `instrument_id`. Decimal amounts must remain strings, never floats. Failed or unavailable
  data must raise an error rather than return an empty list.
- Perpetual `funding_history(reader, leg, start, end)` returns fully paginated settled records
  with `tranId`, `asset`, `income`, `symbol`, `incomeType`, and `time`, matching the existing
  funding ledger. Unsupported history must raise an error rather than claim zero income.

The collector writes venue-independent `spot_account` / `perp_account` snapshots and includes
both routes for reconciliation. The dashboard classifies fills by the recorded full instrument
IDs and displays the actual venues. Custom class paths come only from local configuration;
the database cannot instruct the program to import executable code. The market collector must
also provide `market:v1:{venue}:spot/perp/funding` snapshots for the selected venues. This is an
extension interface, not a claim that every exchange adapter is already implemented or tested.
Only matching USDT linear instruments with unit multiplier and compatible quantity steps are
accepted by the trading strategy. The current single-instance controller/recovery limit is
unchanged by this venue compatibility work.

### Strategy monitoring ledger

New dynamic strategies use `BASIS-{SPOT_VENUE}-{PERP_VENUE}-{SYMBOL}-{NUMBER}`, for example
`BASIS-GATE-BINANCE-BTCUSDT-42`. A global counter in `general` under
`basis:strategy-sequence:v1` is atomically incremented and committed before starting the live
node. Concurrent allocations and restarts do not reuse numbers; unused allocations leave gaps.
Do not delete or reset this counter. Snapshots, account records, exit settings and order events
all use `strategy_id`; there is no separate instance ID. Recovery reuses the saved strategy ID
without allocating another number. Client order IDs retain the framework's UUID option.

`basis_watch.py` captures cached opening inventory baselines and publishes lifecycle snapshots
through `Cache.add`, using the framework's existing asynchronous PostgreSQL writer. The SQL in
`basis_schema.sql` installs `basis_strategy` and a trigger routing `basis:instance:v1:` cache
writes directly into this table, without leaving duplicate snapshots in `general`. Strategy ID,
trader ID, both instrument IDs, state, remaining quantities and lifecycle timestamps have typed
columns; the full display/configuration snapshot is JSONB. Quantities use exact NUMERIC values.
Only an equal or newer `updated_at_ms` may update the snapshot. State transitions and ancillary
account/exit/income/sample records remain in `general`. No synchronous database or exchange
calls are added to the execution loop. Missing baselines remain unknown rather than assumed zero.

The dynamic runner installs the schema under its execution lease before building the live node.
To initialize it independently without starting trading:

```powershell
uv run --project python --no-sync --env-file .env python -c "from user_strategies.basis_exit_control import initialize_strategy_schema; initialize_strategy_schema()"
```

Recovery queries `basis_strategy` by trader ID using a partial index: nonterminal states or
nonzero remaining quantity on either leg. Finished empty strategies stay available to the
dashboard but are excluded from recovery. The collector queries active strategies and recently
finished strategies for delayed funding income; the dashboard and exit-settings endpoint read
`basis_strategy` directly. There is no migration of old general-table strategy snapshots.

Run the independent read-only account and funding collector from the repository root:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.basis_account_monitor
```

It uses the same PostgreSQL, Redis, Gate and Binance credentials as above, with authenticated
GET requests only. It polls every 60 seconds, reads registered instances, paginates settled
Binance UM funding income, and saves `basis:account:v1:`, `basis:income:v1:` and
`basis:sample:v1:` records in `general`. `--once` performs one collection. With no instances it
does not contact the exchanges. Closed instances are rechecked for one day for delayed funding
records. Initial funding history older than 89 days is marked incomplete. This worker is started
by the four-worker monitoring launcher below, or separately.

The independent dashboard's **策略列表** reads PostgreSQL and displays balances, fee-adjusted
inventory, realized/unrealized PnL, settled funding, state history and sampled net PnL. Accounts
must be dedicated to this strategy type, with one active instance per symbol. Baselines exclude
old inventory; account mismatches, stale/missing data and unsupported fee currencies prevent
confirmed net-profit display. Net PnL excludes borrowing interest and other account-level charges.
The existing hedge logic uses gross fills; base-currency spot fees can leave a residual exposure
which the dashboard displays. Existing running trading processes only acquire monitoring metadata
when they load the updated code on their next normal start.

### Persistent per-strategy exit settings

New dynamic instances enable persistent exit control. The card's **平仓设置** and detail page's
**平仓/卖出** open the same parameter dialog. The server validates and commits settings to
`general` under `basis:exit:v1:{strategy_id}`, incrementing a version under a row lock. A stale
form is rejected rather than overwriting a newer change. Saving settings can cause conditional
execution once the running strategy reads them; it is not a forced market exit.

The default condition is the exchange's native funding rate **<= -2%** (decimal rate `-0.02`),
with no basis condition. Funding and basis each support `<=` or `>=`; either enabled condition
triggers exit. Exit basis is `(perpetual ask / spot bid - 1) * 100` using fresh books. Percentages
are entered as decimal strings, so `-2` in the form means `-2%`, not `-0.02%`.

Spot defaults to Maker and may be changed to Taker; the perpetual defaults to the existing Taker
hedge and may be changed to Maker. Spot sells execute first. Each actual fill triggers an equal
quantity reduce-only perpetual buy. Maker buys rest at the fresh perpetual bid, post-only.
No automatic Maker-to-Taker fallback is implemented. A nonmatching condition cancels the unfilled
spot exit and waits, while already filled spot still requires its hedge. Remaining quantity is
not submitted again while hedges are pending. Maker rejects, unavailable hedge books or failed
hedges pause execution and expose the unresolved quantity for reconciliation.

`basis_exit_control.py` reads settings in a background thread every five seconds; its database
calls never run on the trading event loop. The strategy acknowledges the applied version through
its ordinary monitoring snapshot. The UI distinguishes saved settings from an applied version,
and database failures prevent new orders until the control reader recovers.

On startup, the dynamic runner looks for one retained non-closed instance before choosing a new
symbol. It retains the instance/strategy IDs, parameters, baseline and accumulated fill amounts.
UUID client-order IDs prevent restart collisions. Multiple retained instances or a still-fresh
heartbeat prevent duplicate startup. An old instance resumes only after all recorded orders are
terminal, fresh Gate/Binance account snapshots have no open orders for the pair, spot and perpetual
quantities match the ledger and baseline, and the live framework cache agrees with the perpetual
quantity. Pending/unknown orders, stale snapshots, missing baselines, base-fee quantity mismatches
and unfinished hedges remain **待核对**; the runner does not cancel, repair or resubmit them blindly.
The independent account monitor must be running to provide these checks. This is a bounded recovery
path for an already reconciled instance, not automatic repair of every interrupted execution.

API references: [Binance Portfolio Margin account endpoints](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/account)
and [Gate Spot API](https://www.gate.com/docs/developers/apiv4/en/spot/).

## Pre-order capital budgeting

`arbitrage_budget.py` provides a shared `ArbitrageBudget` ledger for the execution workflow.
Create one ledger for the controller and share it across all venue combinations. Before submitting
an entry, call `reserve` with the cached dedicated account objects, existing position capital by
base asset, current spot asks, the perpetual maker price, and both instruments' quantity steps and
order minimums. Use the client order ID as the reservation ID.

The ledger reads fresh Binance Portfolio Margin or Gate unified-account snapshots and counts each
account ID once. A coin may consume at most 10% of combined account equity, including its spot
purchase and estimated perpetual margin at 2x leverage. All account equity is eligible for the
total budget. Each entry uses at most 50% of the spot's first three ask levels; its VWAP is computed
only for the planned quantity. Contract multipliers and both quantity steps are applied exactly.
Venue-reported USD account equity is used as the dollar budgeting basis for USDT pairs; this does
not model a USD/USDT exchange-rate deviation.

Reservations are atomic within one process and constrain spot cash and available collateral on
each account. Missing or stale account snapshots prevent allocation. `release` frees only a
confirmed unfilled base quantity after a rejection or cancellation; filled quantities stay
reserved. Existing position capital passed to `reserve` must exclude the positions already
represented by retained reservations to avoid double counting.

This module does not submit orders, set exchange leverage, reconcile retained filled reservations,
or register authenticated execution clients. The ledger is not yet connected to the
execution strategy in `basis_watch.py`.

## REST collector

`market_data.py` collects public **USDT spot** and **USDT-margined perpetual** snapshots from
Binance, Gate, Bybit, Bitget, and OKX. It does not place orders or host a web page. The collector
polls exchange REST APIs every 15 seconds by default; it is a dashboard data source, not a
low-latency trading feed.

Start Redis, then run from the repository root in PowerShell:

```powershell
$env:MARKET_REDIS_URL = "redis://localhost:6379/0"
uv run --project python --no-sync python -m user_strategies.market_data
```

Use `--once` to collect a single snapshot, or `--interval 30 --ttl 120` to adjust polling and
expiry. For a Redis server on another machine, set `MARKET_REDIS_URL` to that server's address and
credentials. Run one collector instance per Redis namespace; concurrent instances would overwrite
the same snapshot keys.

The collector writes 15 JSON keys (five venues × three data categories):

```
market:v1:{venue}:spot
market:v1:{venue}:perp
market:v1:{venue}:funding
```

Each key contains `schema_version`, `venue`, `market`, `collected_at_ms`, and an `instruments`
object keyed by a comparable USDT symbol such as `BTCUSDT`. Each instrument also retains the
exchange's original `symbol`. Example:

```json
{
  "schema_version": 1,
  "venue": "okx",
  "market": "spot",
  "collected_at_ms": 1790485165000,
  "instruments": {
    "BTCUSDT": {
      "symbol": "BTC-USDT",
      "base": "BTC",
      "quote": "USDT",
      "bid": "84298.2",
      "ask": "84298.3",
      "bid_size": "1.0",
      "ask_size": "2.0",
      "last": "84298.2",
      "quote_turnover_24h": "2281593.73",
      "source_at_ms": 1790485164000
    }
  }
}
```

The `funding` category uses `rate` as a decimal ratio (for example `0.0001` means `0.01%`),
`next_funding_at_ms`, `interval_hours`, and `source_at_ms`. A missing field is `null` rather than
an assumed value. Binance uses the official 8-hour default and overrides it with each symbol's
current `fundingIntervalHours` from `/fapi/v1/fundingInfo` when provided. OKX's interval is
calculated from its next two settlement times.
For
OKX perpetuals, `quote_turnover_24h` is `null`: its ticker reports base-currency volume, not
quote-currency turnover. Bid/ask sizes retain each venue's native units; perpetual contract sizes
must not be compared with spot base-asset quantities without checking contract specifications.
Some exchanges do not provide an exchange-side timestamp on a bulk
ticker; use `collected_at_ms` and the key TTL to check freshness.

The independent web application can read these 15 keys with `MGET` and join them by instrument.
Missing keys mean no fresh snapshot is available. Keep this Redis connection on the **web server**,
not in browser JavaScript. Matching a symbol across venues does not prove the underlying token or
contract terms are identical; validate asset identity and contract specifications before using
these snapshots for any trade.

## Portfolio Margin Feishu alerts

`portfolio_alert.py` is a separate read-only worker. It reads the Binance Portfolio Margin account
snapshot, Binance funding snapshot, and dashboard-managed alert settings from Redis. It sends a
normal message, in-app urgent notification, or QQ email according to the configured
`uniMMR` thresholds. For UM positions, a long position pays positive funding and a short position
pays negative funding; the worker can alert when that payment direction is active. A negative
funding rate on any held UM symbol can independently trigger a QQ email. Redis delivery
state prevents duplicate messages until the configured repeat interval and supports recovery notices.
Each alert is an interactive card with an acknowledgement button. Clicking **已知晓，暂停此项 1
小时** writes a one-hour Redis mute for that risk item, suppressing both Feishu messages and emails
while leaving other alert conditions active. Configure Feishu callbacks to use a long
connection and add `card.action.trigger`; no public callback URL is required.

The dynamic execution runner holds a PostgreSQL session advisory lock for its lifetime to reject a second runner for the same trader. A retained instance is checked before selecting a new candidate.

## Start the four monitoring workers

On Windows, the launcher starts the REST market collector, Portfolio Margin account monitor,
basis strategy account monitor, and Feishu alert worker. It checks the current process command lines and skips any worker that is already
running, so running it again does not create duplicate collectors. The dashboard is not started.

```powershell
.\scripts\start-monitoring.ps1
```

Validate `.env` and show what would be started without changing any processes:

```powershell
.\scripts\start-monitoring.ps1 -ValidateOnly
```

Worker output is written under `logs/monitoring/`. Stop these four workers without stopping the web
dashboard with:

```powershell
.\scripts\stop-monitoring.ps1
```

Set the Feishu credentials in the root `.env` and run from the repository root:

```powershell
uv run --project python --no-sync --env-file .env python -m user_strategies.portfolio_alert
```

Required environment variables are `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, and one of
`FEISHU_RECEIVER_OPEN_ID`, `FEISHU_RECEIVER_MOBILE`, or `FEISHU_RECEIVER_EMAIL`. Mobile or email is
used only to resolve and cache the receiver's `open_id` in memory. QQ email delivery additionally
requires `QQ_SMTP_EMAIL` and `QQ_SMTP_AUTH_CODE`; `ALERT_EMAIL_TO` is optional and defaults to the
sender address. Use the QQ Mail SMTP authorization code, not the QQ account password. Use
`--dry-run --once` to evaluate the current Redis snapshots without contacting Feishu or QQ Mail.
The worker never submits or modifies orders.

Source endpoints: [Binance Spot](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints),
[Binance USD-M](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api),
[Gate](https://www.gate.com/docs/developers/apiv4/en/),
[Bybit](https://bybit-exchange.github.io/docs/v5/market/tickers),
[Bitget](https://www.bitget.com/docs/catalog/market/market-data), and
[OKX](https://www.okx.com/docs-v5/en/).
