CREATE TABLE IF NOT EXISTS basis_strategy (
    strategy_id TEXT PRIMARY KEY,
    trader_id TEXT NOT NULL,
    spot_id TEXT NOT NULL,
    perp_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'waiting', 'opening', 'hedging', 'holding', 'exiting',
        'paused', 'reconciling', 'stopped', 'closed'
    )),
    spot_remaining NUMERIC NOT NULL CHECK (spot_remaining >= 0),
    perp_remaining NUMERIC NOT NULL CHECK (perp_remaining >= 0),
    started_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    stopped_at_ms BIGINT,
    snapshot JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS basis_strategy_recovery_idx ON basis_strategy (trader_id)
WHERE state NOT IN ('closed', 'stopped') OR spot_remaining <> 0 OR perp_remaining <> 0;

-- Route the framework's asynchronous Cache.add write into the strategy business table.
-- Returning NULL prevents a duplicate strategy snapshot from being stored in general.
CREATE OR REPLACE FUNCTION persist_basis_strategy() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    data JSONB;
BEGIN
    data := convert_from(NEW.value, 'UTF8')::jsonb;
    IF NEW.id <> 'basis:instance:v1:' || (data->>'strategy_id')
       OR data->>'strategy_id' IS NULL THEN
        RAISE EXCEPTION 'Strategy snapshot identity mismatch';
    END IF;
    INSERT INTO basis_strategy (
        strategy_id, trader_id, spot_id, perp_id, state,
        spot_remaining, perp_remaining, started_at_ms, updated_at_ms,
        stopped_at_ms, snapshot
    ) VALUES (
        data->>'strategy_id', data->>'trader_id', data->>'spot_id', data->>'perp_id',
        data->>'state', (data->>'spot_remaining')::numeric,
        (data->>'perp_remaining')::numeric, (data->>'started_at_ms')::bigint,
        (data->>'updated_at_ms')::bigint, (data->>'stopped_at_ms')::bigint, data
    )
    ON CONFLICT (strategy_id) DO UPDATE SET
        trader_id = EXCLUDED.trader_id,
        spot_id = EXCLUDED.spot_id,
        perp_id = EXCLUDED.perp_id,
        state = EXCLUDED.state,
        spot_remaining = EXCLUDED.spot_remaining,
        perp_remaining = EXCLUDED.perp_remaining,
        started_at_ms = EXCLUDED.started_at_ms,
        updated_at_ms = EXCLUDED.updated_at_ms,
        stopped_at_ms = EXCLUDED.stopped_at_ms,
        snapshot = EXCLUDED.snapshot
    WHERE EXCLUDED.updated_at_ms >= basis_strategy.updated_at_ms;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS basis_strategy_write ON general;
CREATE TRIGGER basis_strategy_write
BEFORE INSERT OR UPDATE ON general
FOR EACH ROW WHEN (starts_with(NEW.id, 'basis:instance:v1:'))
EXECUTE FUNCTION persist_basis_strategy();
