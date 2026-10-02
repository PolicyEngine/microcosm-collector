CREATE TABLE IF NOT EXISTS telemetry_runs (
    run_id TEXT PRIMARY KEY,
    country_code TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    candidate_id TEXT,
    release_id TEXT,
    run_kind TEXT NOT NULL,
    owner_hf_id TEXT NOT NULL,
    owner_hf_username TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    resources JSONB,
    work JSONB,
    failure JSONB
);

CREATE INDEX IF NOT EXISTS telemetry_runs_country_updated_idx
    ON telemetry_runs (country_code, updated_at DESC);

CREATE TABLE IF NOT EXISTS telemetry_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES telemetry_runs(run_id) ON DELETE CASCADE,
    producer_id TEXT NOT NULL,
    sequence BIGINT NOT NULL,
    emitted_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    stage_id TEXT,
    status TEXT NOT NULL,
    message TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    resources JSONB,
    UNIQUE (run_id, producer_id, sequence)
);

CREATE INDEX IF NOT EXISTS telemetry_events_run_order_idx
    ON telemetry_events (run_id, emitted_at, producer_id, sequence);
