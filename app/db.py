"""SQLite schema, connection helpers and first-run seeding."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("DB_PATH", "/data/museum.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('curator','translator','reviewer')),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS languages (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name_zh       TEXT NOT NULL,
    name_en       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    current_rev   INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS label_revisions (
    id          INTEGER PRIMARY KEY,
    label_id    INTEGER NOT NULL REFERENCES labels(id),
    rev         INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    author_id   INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL,
    UNIQUE (label_id, rev)
);
CREATE INDEX IF NOT EXISTS idx_label_rev_label ON label_revisions(label_id, rev);

CREATE TABLE IF NOT EXISTS translations (
    id           INTEGER PRIMARY KEY,
    label_id     INTEGER NOT NULL REFERENCES labels(id),
    language_id  INTEGER NOT NULL REFERENCES languages(id),
    current_rev  INTEGER NOT NULL DEFAULT 1,
    source_rev   INTEGER NOT NULL,
    confirmed    INTEGER NOT NULL DEFAULT 0,
    translator_id INTEGER REFERENCES users(id),
    confirmed_by INTEGER REFERENCES users(id),
    confirmed_at TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE (label_id, language_id)
);
CREATE INDEX IF NOT EXISTS idx_translations_lookup ON translations(label_id, language_id);

CREATE TABLE IF NOT EXISTS translation_revisions (
    id            INTEGER PRIMARY KEY,
    translation_id INTEGER NOT NULL REFERENCES translations(id),
    rev           INTEGER NOT NULL,
    text          TEXT NOT NULL,
    source_rev    INTEGER NOT NULL,
    translator_id INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL,
    UNIQUE (translation_id, rev)
);
CREATE INDEX IF NOT EXISTS idx_tr_rev_trans ON translation_revisions(translation_id, rev);

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY,
    language_id  INTEGER NOT NULL REFERENCES languages(id),
    version      INTEGER NOT NULL,
    label_count  INTEGER NOT NULL,
    publisher_id INTEGER NOT NULL REFERENCES users(id),
    published_at TEXT NOT NULL,
    UNIQUE (language_id, version)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_lang ON snapshots(language_id, version DESC);

CREATE TABLE IF NOT EXISTS snapshot_items (
    id           INTEGER PRIMARY KEY,
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
    label_code   TEXT NOT NULL,
    source_rev   INTEGER NOT NULL,
    source_text  TEXT NOT NULL,
    trans_rev    INTEGER NOT NULL,
    trans_text   TEXT NOT NULL,
    UNIQUE (snapshot_id, label_code)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_items ON snapshot_items(snapshot_id);
"""

# ---------------------------------------------------------------------------
# Connection / transaction
# ---------------------------------------------------------------------------


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit BEGIN/COMMIT/ROLLBACK (connection runs in autocommit mode)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed(conn: sqlite3.Connection) -> None:
    from .security import hash_password

    now = _utcnow()
    users = [
        ("curator", "周馆员", "curator123", "curator"),
        ("translator", "李译", "trans123", "translator"),
        ("reviewer", "王校", "review123", "reviewer"),
    ]
    for username, display_name, password, role in users:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, role, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (username, display_name, hash_password(password), role, now),
            )

    if conn.execute("SELECT COUNT(*) c FROM languages").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO languages (code, name_zh, name_en, created_at) VALUES (?, ?, ?, ?)",
            ("en", "英语", "English", now),
        )
        conn.execute(
            "INSERT INTO languages (code, name_zh, name_en, created_at) VALUES (?, ?, ?, ?)",
            ("ja", "日语", "Japanese", now),
        )

    if os.environ.get("SEED_DEMO", "1") != "1":
        return

    if conn.execute("SELECT COUNT(*) c FROM labels").fetchone()["c"] != 0:
        return

    curator_id = conn.execute(
        "SELECT id FROM users WHERE username = 'curator'"
    ).fetchone()["id"]
    trans_id = conn.execute(
        "SELECT id FROM users WHERE username = 'translator'"
    ).fetchone()["id"]
    reviewer_id = conn.execute(
        "SELECT id FROM users WHERE username = 'reviewer'"
    ).fetchone()["id"]
    en_id = conn.execute("SELECT id FROM languages WHERE code = 'en'").fetchone()["id"]
    ja_id = conn.execute("SELECT id FROM languages WHERE code = 'ja'").fetchone()["id"]

    demo_labels = [
        ("A001", "青花瓷瓶，明代永乐年间，釉色温润。"),
        ("A002", "本展品为金代铜坐龙，出土于哈尔滨市。"),
        ("A003", "请勿使用闪光灯拍照。"),
        ("A004", "丝绸之路促进了东西方文化交流。"),
    ]
    label_ids: list[int] = []
    for code, text in demo_labels:
        cur = conn.execute(
            "INSERT INTO labels (code, current_rev, created_at, updated_at)"
            " VALUES (?, 1, ?, ?)",
            (code, now, now),
        )
        lid = cur.lastrowid
        label_ids.append(lid)
        conn.execute(
            "INSERT INTO label_revisions (label_id, rev, source_text, author_id, created_at)"
            " VALUES (?, 1, ?, ?, ?)",
            (lid, text, curator_id, now),
        )

    def add_translation(label_id: int, lang_id: int, text: str, *,
                        source_rev: int, confirmed: bool, confirmed_at: str | None) -> None:
        cur = conn.execute(
            "INSERT INTO translations (label_id, language_id, current_rev, source_rev,"
            " confirmed, translator_id, confirmed_by, confirmed_at, updated_at)"
            " VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (label_id, lang_id, source_rev, 1 if confirmed else 0,
             trans_id, reviewer_id if confirmed else None, confirmed_at, now),
        )
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO translation_revisions (translation_id, rev, text, source_rev,"
            " translator_id, created_at) VALUES (?, 1, ?, ?, ?, ?)",
            (tid, text, source_rev, trans_id, now),
        )

    # English: confirmed / pending / stale / missing — showcases every state.
    add_translation(label_ids[0], en_id,
                    "Blue-and-white porcelain vase, Yongle period, Ming dynasty.",
                    source_rev=1, confirmed=True, confirmed_at=now)
    add_translation(label_ids[1], en_id,
                    "This is a seated bronze dragon of the Jin dynasty, unearthed in Harbin.",
                    source_rev=1, confirmed=False, confirmed_at=None)
    # Label A003 at source rev 2, translation still based on rev 1 -> stale.
    conn.execute(
        "UPDATE labels SET current_rev = 2, updated_at = ? WHERE id = ?",
        (now, label_ids[2]),
    )
    conn.execute(
        "INSERT INTO label_revisions (label_id, rev, source_text, author_id, created_at)"
        " VALUES (?, 2, ?, ?, ?)",
        (label_ids[2], "感谢您不使用闪光灯拍照，共同保护珍贵文物。", curator_id, now),
    )
    add_translation(label_ids[2], en_id,
                    "Please do not take photographs with flash.",
                    source_rev=1, confirmed=True, confirmed_at=now)
    # A004 has no English translation -> pending (missing).

    # Japanese: only one pending submission, everything else missing.
    add_translation(label_ids[0], ja_id,
                    "青花磁の瓶、明代永楽年間のもの。",
                    source_rev=1, confirmed=False, confirmed_at=None)


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        with tx(conn):
            seed(conn)
    finally:
        conn.close()
