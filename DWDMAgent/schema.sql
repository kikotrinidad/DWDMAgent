-- =============================================================================
-- AIExperts Platform — Database Schema
-- Database  : aiexperts
-- User      : aiexpert
-- PostgreSQL: 14+
--
-- Design intent:
--   - `devices`     : platform-wide registry (DWDM, routers, switches, …)
--   - `raw_scrapes` : generic audit/blob store for any device type
--   - `ai_briefings`: generic AI output store for any device type
--   - `dwdm_*`      : DWDM-specific tables (Cisco NCS 2000 / TL1 protocol)
--   Future device types add their own prefixed table groups without touching
--   the shared tables above.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- devices — platform-wide device registry (one row per managed device)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id           SERIAL PRIMARY KEY,
    device_type  VARCHAR(32)  NOT NULL,          -- e.g. DWDM, ROUTER, SWITCH
    hostname     VARCHAR(128) NOT NULL UNIQUE,    -- primary identifier (TID, hostname, etc.)
    ip_address   INET         NOT NULL,
    site_name    VARCHAR(128),
    model        VARCHAR(64),
    vendor       VARCHAR(64),
    protocol     VARCHAR(16),                     -- TELNET, SSH, SNMP, etc.
    port         INTEGER,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_devices_type
    ON devices(device_type);

-- ---------------------------------------------------------------------------
-- raw_scrapes — verbatim command/query responses for any device type
-- Audit log blobs must be committed here BEFORE CLR-AUDIT-LOG is sent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_scrapes (
    id             SERIAL PRIMARY KEY,
    device_id      INTEGER      NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    command        VARCHAR(64)  NOT NULL,
    ctag           VARCHAR(16),
    response_text  TEXT         NOT NULL,
    compld         BOOLEAN      NOT NULL DEFAULT TRUE,
    scraped_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_raw_scrapes_device_cmd
    ON raw_scrapes(device_id, command, scraped_at DESC);

-- ---------------------------------------------------------------------------
-- ai_briefings — Gemini-generated analysis reports for any device type
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_briefings (
    id              SERIAL PRIMARY KEY,
    device_id       INTEGER      NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    briefing_json   JSONB,
    briefing_html   TEXT,
    generated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_briefings_device
    ON ai_briefings(device_id, generated_at DESC);


-- =============================================================================
-- DWDM-specific tables (Cisco NCS 2000 / TL1 protocol)
-- All reference devices(id) where device_type = 'DWDM'
-- =============================================================================

-- ---------------------------------------------------------------------------
-- dwdm_topology — peer map from RTRV-MAP-NETWORK
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dwdm_topology (
    id          SERIAL PRIMARY KEY,
    device_id   INTEGER      NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    peer_ip     INET         NOT NULL,
    peer_tid    VARCHAR(64),
    peer_model  VARCHAR(64),
    scraped_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (device_id, peer_ip)
);

-- ---------------------------------------------------------------------------
-- dwdm_inventory — hardware cards from RTRV-INV
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dwdm_inventory (
    id               SERIAL PRIMARY KEY,
    device_id        INTEGER      NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    aid              VARCHAR(64)  NOT NULL,
    card_type        VARCHAR(64),
    part_number      VARCHAR(64),
    hw_rev           VARCHAR(32),
    fw_rev           VARCHAR(64),
    serial_number    VARCHAR(64),
    clei             VARCHAR(32),
    pid              VARCHAR(64),
    vid              VARCHAR(16),
    actual_card_name VARCHAR(64),
    scraped_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (device_id, aid)
);

-- ---------------------------------------------------------------------------
-- dwdm_alarms — active alarms from RTRV-ALM-ALL (replaced each scrape)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dwdm_alarms (
    id              SERIAL PRIMARY KEY,
    device_id       INTEGER      NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    aid             VARCHAR(128) NOT NULL,
    layer           VARCHAR(16),
    severity        CHAR(2),                 -- CR, MJ, MN, NA, NR, NE
    condition_type  VARCHAR(64),
    service_effect  CHAR(3),                 -- SA, NSA
    alarm_date      CHAR(5),                 -- MM-DD
    alarm_time      CHAR(8),                 -- HH-MM-SS
    location        VARCHAR(16),             -- NEND, FEND, NEND-FEND
    direction       VARCHAR(16),
    description     TEXT,
    card_type       VARCHAR(64),
    scraped_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dwdm_alarms_device_sev
    ON dwdm_alarms(device_id, severity);

-- ---------------------------------------------------------------------------
-- dwdm_conditions — standing conditions from RTRV-COND-ALL (replaced each scrape)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dwdm_conditions (
    id              SERIAL PRIMARY KEY,
    device_id       INTEGER      NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    aid             VARCHAR(128) NOT NULL,
    layer           VARCHAR(16),
    severity        CHAR(2),
    condition_type  VARCHAR(64),
    service_effect  CHAR(3),
    event_date      CHAR(5),
    event_time      CHAR(8),
    location        VARCHAR(16),
    direction       VARCHAR(16),
    description     TEXT,
    scraped_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dwdm_conditions_device
    ON dwdm_conditions(device_id);

-- ---------------------------------------------------------------------------
-- dwdm_optical_channels — per-wavelength power from RTRV-OCH (replaced each scrape)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dwdm_optical_channels (
    id              SERIAL PRIMARY KEY,
    device_id       INTEGER      NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    aid             VARCHAR(64)  NOT NULL,
    slot            SMALLINT,
    port            SMALLINT,
    direction       CHAR(2),                -- RX, TX
    wavelength_nm   NUMERIC(8,3),           -- NULL for PCHAN entries
    op_type         VARCHAR(16),
    opwr_dbm        NUMERIC(7,2),           -- NULL on passive/unlit channels
    voa_mode        VARCHAR(16),
    voa_attn_db     NUMERIC(7,2),
    voa_ref_attn    NUMERIC(7,2),
    if_index        VARCHAR(16),           -- hex string from TL1 (e.g. 212C)
    admin_state     VARCHAR(64),
    exp_wavelength  VARCHAR(16),            -- PCHAN planned wavelength
    scraped_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dwdm_och_device
    ON dwdm_optical_channels(device_id);

-- ---------------------------------------------------------------------------
-- dwdm_network_reports — network-level AI reports (PM + Executive)
-- One row per run. Used for replay / historical trending.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dwdm_network_reports (
    id                  SERIAL PRIMARY KEY,
    run_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    model               VARCHAR(128) NOT NULL,
    pm_briefing_json    JSONB,
    exec_briefing_json  JSONB,
    node_count          SMALLINT,
    network_health_score SMALLINT,
    overall_status      VARCHAR(32),
    report_html_path    TEXT         -- filesystem path of generated HTML
);

CREATE INDEX IF NOT EXISTS idx_dwdm_network_reports_run_at
    ON dwdm_network_reports(run_at DESC);
