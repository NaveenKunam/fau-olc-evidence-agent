"""SQLite persistence for the evidence repository."""
import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'reviewer',          -- admin | reviewer | contributor | viewer
    clearance TEXT NOT NULL DEFAULT 'INTERNAL',     -- PUBLIC | INTERNAL | RESTRICTED
    pw_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS indicators (
    id TEXT PRIMARY KEY,
    category_code TEXT NOT NULL,
    category TEXT NOT NULL,
    number INTEGER NOT NULL,
    sort_key INTEGER NOT NULL,
    text TEXT,
    alt_text TEXT,
    text_source TEXT NOT NULL,
    handbook_text TEXT,
    handbook_source TEXT,
    required_level TEXT NOT NULL,
    config_json TEXT NOT NULL,                       -- anchors/terms/elements/artifacts/owners/hint
    updated_by TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,                              -- pdf | docx | xlsx | csv | txt | html | url | matrix
    doc_type TEXT NOT NULL,                          -- PLAN | POLICY | WEBPAGE | REPORT | DATA | MINUTES | STANDARD | MATRIX | OTHER
    source_org TEXT,
    source_url TEXT,
    filename TEXT,
    stored_path TEXT,
    sha256 TEXT,
    doc_date TEXT,
    version_note TEXT,
    is_draft INTEGER NOT NULL DEFAULT 0,
    is_stale INTEGER NOT NULL DEFAULT 0,
    superseded_by INTEGER,
    authority TEXT NOT NULL DEFAULT 'UNKNOWN',       -- FAU | STATE | ACCREDITOR | OTHER | UNKNOWN
    classification TEXT NOT NULL DEFAULT 'INTERNAL',
    origin TEXT NOT NULL DEFAULT 'upload',           -- upload | url | seed | connector | matrix
    page_count INTEGER,
    char_count INTEGER,
    notes TEXT,
    added_by TEXT,
    added_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    page TEXT,
    section TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_passages_doc ON passages(document_id);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    evidence_code TEXT UNIQUE,
    indicator_id TEXT NOT NULL REFERENCES indicators(id),
    document_id INTEGER REFERENCES documents(id),
    passage_id INTEGER REFERENCES passages(id),
    title TEXT NOT NULL,
    evidence_type TEXT,
    source_org TEXT,
    source_ref TEXT,
    doc_date TEXT,
    page TEXT,
    section TEXT,
    passage TEXT,
    summary TEXT,
    mapping_rationale TEXT,
    implementation_level TEXT,
    level_rationale TEXT,
    match_score REAL,
    ai_status TEXT NOT NULL,                         -- FOUND | PARTIAL | NEEDS VERIFICATION | PROPOSED/DRAFT | SUPERSEDED/STALE
    strength TEXT NOT NULL,                          -- STRONG | MODERATE | WEAK | NONE
    missing_note TEXT,
    next_artifact TEXT,
    likely_owner TEXT,
    origin TEXT NOT NULL,                            -- ai_rules | ai_llm | matrix | human
    prior_claim_json TEXT,                           -- for matrix-imported claims
    corroborated_by TEXT,
    workflow_stage TEXT NOT NULL DEFAULT 'NEEDS HUMAN REVIEW',
    review_status TEXT NOT NULL DEFAULT 'NOT REVIEWED',   -- NOT REVIEWED | APPROVED | REJECTED
    include_in_submission INTEGER NOT NULL DEFAULT 0,
    classification TEXT NOT NULL DEFAULT 'INTERNAL',
    human_edited INTEGER NOT NULL DEFAULT 0,
    reviewer TEXT,
    reviewer_notes TEXT,
    fingerprint TEXT,
    added_by TEXT,
    added_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_evidence_ind ON evidence(indicator_id);
CREATE INDEX IF NOT EXISTS ix_evidence_doc ON evidence(document_id);

CREATE TABLE IF NOT EXISTS indicator_reviews (
    indicator_id TEXT PRIMARY KEY REFERENCES indicators(id),
    review_status TEXT NOT NULL DEFAULT 'NOT REVIEWED',   -- NOT REVIEWED | IN REVIEW | NEEDS DECISION | APPROVED
    human_prelim_score INTEGER,
    official_score INTEGER,
    reviewer TEXT,
    notes TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS evidence_requests (
    id INTEGER PRIMARY KEY,
    indicator_id TEXT,
    request TEXT NOT NULL,
    owner TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    assigned_to TEXT,
    notes TEXT,
    source TEXT,
    created_at TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def audit(conn, actor, action, target=None, detail=None):
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)
    conn.execute("INSERT INTO audit_log(at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                 (now(), actor, action, target, detail))


CLEARANCE_RANK = {"PUBLIC": 0, "INTERNAL": 1, "RESTRICTED": 2}


def visible_classes(clearance):
    r = CLEARANCE_RANK.get(clearance or "PUBLIC", 0)
    return [c for c, v in CLEARANCE_RANK.items() if v <= r]


def ensure_dirs(instance_path):
    os.makedirs(instance_path, exist_ok=True)
    os.makedirs(os.path.join(instance_path, "uploads"), exist_ok=True)
