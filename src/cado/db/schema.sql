-- DuckDB schema for the CADO registry mirror.
--
-- Design notes:
--
-- * The raw HTML on disk is the source of truth. Everything in this database
--   is *derived* and can be rebuilt by re-running ``cado ingest``.
-- * Company number is a TEXT primary key (legacy filings use ``2D`` /
--   ``100CM`` suffixes — see ``src/cado/models.py``).
-- * Full-text search uses DuckDB's ``fts`` extension. It's bound to a
--   *materialised* view so we can index any subset of columns we like.
-- * Dates are stored as ``DATE``; ``NULL`` where the upstream omitted them.

CREATE TABLE IF NOT EXISTS companies (
    number                      TEXT      PRIMARY KEY,
    name                        TEXT      NOT NULL,
    corporation_type            TEXT      NOT NULL,
    category                    TEXT,
    status                      TEXT,

    incorporation_date          DATE,
    registration_date           DATE,
    last_annual_return          DATE,

    business_type               TEXT,
    incorporation_jurisdiction  TEXT,
    filing_type                 TEXT,
    min_max_directors           TEXT,

    additional_info             TEXT,

    -- Registered office
    ro_contact                  TEXT,
    ro_line1                    TEXT,
    ro_line2                    TEXT,
    ro_line3                    TEXT,
    ro_city                     TEXT,
    ro_province_state           TEXT,
    ro_country                  TEXT,
    ro_postal_zip               TEXT,

    -- Mailing address
    ma_contact                  TEXT,
    ma_line1                    TEXT,
    ma_line2                    TEXT,
    ma_line3                    TEXT,
    ma_city                     TEXT,
    ma_province_state           TEXT,
    ma_country                  TEXT,
    ma_postal_zip               TEXT,
    ma_same_as_registered       BOOLEAN   NOT NULL DEFAULT FALSE,

    -- Ingest metadata
    ingested_at                 TIMESTAMP NOT NULL DEFAULT current_timestamp,
    source_html_sha256          TEXT
);

CREATE INDEX IF NOT EXISTS idx_companies_name              ON companies (name);
CREATE INDEX IF NOT EXISTS idx_companies_corp_type         ON companies (corporation_type);
CREATE INDEX IF NOT EXISTS idx_companies_status            ON companies (status);
CREATE INDEX IF NOT EXISTS idx_companies_category          ON companies (category);
CREATE INDEX IF NOT EXISTS idx_companies_incorporation     ON companies (incorporation_date);


CREATE TABLE IF NOT EXISTS company_directors (
    company_number  TEXT  NOT NULL,
    seq             INTEGER NOT NULL,
    full_name       TEXT  NOT NULL,
    first_name      TEXT,
    last_name       TEXT,
    PRIMARY KEY (company_number, seq)
);

CREATE INDEX IF NOT EXISTS idx_directors_full_name  ON company_directors (full_name);
CREATE INDEX IF NOT EXISTS idx_directors_last_name  ON company_directors (last_name);


CREATE TABLE IF NOT EXISTS company_previous_names (
    company_number  TEXT  NOT NULL,
    seq             INTEGER NOT NULL,
    name            TEXT  NOT NULL,
    effective_date  DATE,
    PRIMARY KEY (company_number, seq)
);

CREATE INDEX IF NOT EXISTS idx_prev_names_name  ON company_previous_names (name);


CREATE TABLE IF NOT EXISTS company_historical_remarks (
    company_number  TEXT  NOT NULL,
    seq             INTEGER NOT NULL,
    remark          TEXT  NOT NULL,
    PRIMARY KEY (company_number, seq)
);


CREATE TABLE IF NOT EXISTS lobbyist_registrations (
    registration_number     TEXT      PRIMARY KEY,
    lobbyist_type           TEXT,
    status                  TEXT,

    registration_date       DATE,
    effective_date          DATE,
    amended_date            DATE,
    approval_date           DATE,

    contact_name            TEXT,
    contact_line1           TEXT,
    contact_city            TEXT,
    contact_province_state  TEXT,
    contact_postal_zip      TEXT,

    firm_name               TEXT,
    firm_line1              TEXT,
    firm_city               TEXT,
    firm_province_state     TEXT,
    firm_postal_zip         TEXT,

    particulars             TEXT,
    organization_description TEXT,
    organization_membership TEXT,

    -- Everything else, as JSON keyed by lblXxx
    raw_fields              JSON,

    ingested_at             TIMESTAMP NOT NULL DEFAULT current_timestamp,
    source_html_sha256      TEXT
);

CREATE INDEX IF NOT EXISTS idx_lobbyists_contact_name    ON lobbyist_registrations (contact_name);
CREATE INDEX IF NOT EXISTS idx_lobbyists_firm_name       ON lobbyist_registrations (firm_name);
CREATE INDEX IF NOT EXISTS idx_lobbyists_status          ON lobbyist_registrations (status);


CREATE SEQUENCE IF NOT EXISTS ingest_log_id_seq;

CREATE TABLE IF NOT EXISTS ingest_log (
    id                  BIGINT      PRIMARY KEY DEFAULT nextval('ingest_log_id_seq'),
    kind                TEXT        NOT NULL,    -- 'company' | 'lobbyist'
    record_key          TEXT        NOT NULL,    -- e.g. '25166', 'IHL-867-1005'
    parsed_ok           BOOLEAN     NOT NULL,
    error               TEXT,
    ingested_at         TIMESTAMP   NOT NULL DEFAULT current_timestamp,
    source_html_sha256  TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_log_kind_key  ON ingest_log (kind, record_key);
