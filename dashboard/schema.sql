-- Vulture dashboard D1 schema.
-- Apply with: wrangler d1 execute vulture --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS scans (
  id          TEXT PRIMARY KEY,          -- uuid from the engine
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  posts_seen  INTEGER DEFAULT 0,
  scored      INTEGER DEFAULT 0,
  posted      INTEGER DEFAULT 0,
  trig        TEXT DEFAULT 'cron'        -- cron | manual
);

CREATE TABLE IF NOT EXISTS candidates (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id         TEXT REFERENCES scans(id),
  post_id         TEXT NOT NULL,
  ticker          TEXT NOT NULL,
  composite       REAL NOT NULL,
  thesis          REAL, community REAL, news REAL, technical REAL,
  cross_platform  INTEGER DEFAULT 0,
  prior_mentions  INTEGER DEFAULT 0,
  momentum_bonus  REAL DEFAULT 0,
  radar           INTEGER DEFAULT 0,
  posted          INTEGER DEFAULT 0,
  briefing        TEXT,
  briefing_short  TEXT,
  red_flags       TEXT,
  url             TEXT,
  subreddit       TEXT,
  post_created_utc TEXT,
  scored_at_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_scored ON candidates(scored_at_utc);
CREATE INDEX IF NOT EXISTS idx_candidates_ticker ON candidates(ticker);

CREATE TABLE IF NOT EXISTS plays (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER REFERENCES candidates(id),
  ticker       TEXT NOT NULL,
  direction    TEXT,                     -- bullish | bearish | neutral
  structure    TEXT,
  strike       REAL,
  expiry       TEXT,                     -- YYYY-MM-DD, nullable
  rationale    TEXT,
  promoted     INTEGER DEFAULT 0,       -- parent candidate reached Discord/dashboard
  promoted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_plays_expiry ON plays(expiry);

CREATE TABLE IF NOT EXISTS play_results (
  play_id          INTEGER PRIMARY KEY REFERENCES plays(id),
  graded_at        TEXT NOT NULL,
  method           TEXT NOT NULL,        -- contract_eod | underlying_itm | underlying_14d
  entry_underlying REAL, exit_underlying REAL,
  entry_contract   REAL, exit_contract REAL,
  return_pct       REAL,
  verdict          TEXT NOT NULL         -- HIT | MISS | WASH | UNGRADEABLE
);

CREATE TABLE IF NOT EXISTS cramer_mentions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  extracted_at TEXT NOT NULL,
  ticker       TEXT NOT NULL,
  stance       TEXT NOT NULL,
  quote        TEXT,
  source_url   TEXT
);

CREATE TABLE IF NOT EXISTS cramer_verdicts (
  mention_id       INTEGER PRIMARY KEY REFERENCES cramer_mentions(id),
  baseline_date    TEXT, baseline_close REAL,
  eval_date        TEXT, eval_close REAL,
  stock_return_pct REAL, spy_return_pct REAL, alpha_pct REAL,
  verdict          TEXT NOT NULL
);

-- Fallback admin run-now queue (used only if the engine URL isn't reachable)
CREATE TABLE IF NOT EXISTS runs_requested (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  requested_by TEXT,
  requested_at TEXT NOT NULL,
  picked_up_at TEXT
);

-- Never-purged aggregates, updated during retention
CREATE TABLE IF NOT EXISTS summary_totals (
  key   TEXT PRIMARY KEY,               -- e.g. plays_HIT, cramer_MISS
  value INTEGER NOT NULL DEFAULT 0
);
