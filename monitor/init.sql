-- h-cli metrics schema for TimescaleDB

CREATE TABLE IF NOT EXISTS task_metrics (
    time           TIMESTAMPTZ NOT NULL,
    task_id        TEXT NOT NULL,
    chat_id        TEXT,
    model          TEXT,
    input_tokens   INTEGER DEFAULT 0,
    output_tokens  INTEGER DEFAULT 0,
    cache_read     INTEGER DEFAULT 0,
    cache_create   INTEGER DEFAULT 0,
    cost_usd       DOUBLE PRECISION DEFAULT 0,
    duration_ms    INTEGER DEFAULT 0,
    num_turns      INTEGER DEFAULT 1,
    is_error       BOOLEAN DEFAULT FALSE
);

SELECT create_hypertable('task_metrics', 'time', if_not_exists => TRUE);

-- Compression after 7 days (idempotent: skip if already configured)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.compression_settings
        WHERE hypertable_name = 'task_metrics'
    ) THEN
        ALTER TABLE task_metrics SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'chat_id,model'
        );
    END IF;
END $$;
SELECT add_compression_policy('task_metrics', INTERVAL '7 days', if_not_exists => TRUE);

-- Retention: 90 days
SELECT add_retention_policy('task_metrics', INTERVAL '90 days', if_not_exists => TRUE);

-- Tool calls — every run_command that passes through the firewall
CREATE TABLE IF NOT EXISTS tool_calls (
    time           TIMESTAMPTZ NOT NULL,
    command        TEXT NOT NULL,
    gate_result    TEXT,
    blocked        BOOLEAN DEFAULT FALSE,
    duration_ms    INTEGER DEFAULT 0,
    output_length  INTEGER DEFAULT 0
);

SELECT create_hypertable('tool_calls', 'time', if_not_exists => TRUE);

-- Compression (idempotent: skip if already configured)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.compression_settings
        WHERE hypertable_name = 'tool_calls'
    ) THEN
        ALTER TABLE tool_calls SET (
            timescaledb.compress,
            timescaledb.compress_orderby = 'time DESC'
        );
    END IF;
END $$;
SELECT add_compression_policy('tool_calls', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('tool_calls', INTERVAL '90 days', if_not_exists => TRUE);
