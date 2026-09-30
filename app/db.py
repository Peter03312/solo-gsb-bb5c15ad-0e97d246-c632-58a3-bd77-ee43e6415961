"""SQLite 连接、建表与初始化数据。"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/exhibit.db")

# SQLite 单写者：用全局锁串行化所有写操作，避免 database is locked。
_write_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('librarian','translator','reviewer')),
    salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS languages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,       -- 如 en / ja
    name TEXT NOT NULL,              -- 如 English / 日本語
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text TEXT NOT NULL,
    source_rev INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    language_id INTEGER NOT NULL REFERENCES languages(id),
    text TEXT NOT NULL,
    trans_rev INTEGER NOT NULL DEFAULT 1,
    source_rev_at_edit INTEGER NOT NULL,  -- 译文最后一次提交时对应的原文修订号
    confirmed INTEGER NOT NULL DEFAULT 0,
    translator_id INTEGER REFERENCES users(id),
    confirmer_id INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL,
    UNIQUE (label_id, language_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language_id INTEGER NOT NULL REFERENCES languages(id),
    version INTEGER NOT NULL,
    published_by INTEGER REFERENCES users(id),
    published_at TEXT NOT NULL,
    payload TEXT NOT NULL,           -- 不可变 JSON
    UNIQUE (language_id, version)
);
"""


def hash_password(password: str, salt: str) -> str:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)

        # 默认账号（可通过环境变量覆盖，README 说明）
        seed_users = [
            ("librarian", "馆员（管理员）", "librarian", os.environ.get("SEED_LIBRARIAN_PW", "lib123")),
            ("translator", "译者小李", "translator", os.environ.get("SEED_TRANSLATOR_PW", "tra123")),
            ("reviewer", "校对老王", "reviewer", os.environ.get("SEED_REVIEWER_PW", "rev123")),
        ]
        for username, display_name, role, pw in seed_users:
            row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                import secrets
                salt = secrets.token_hex(16)
                conn.execute(
                    "INSERT INTO users (username, display_name, role, salt, pw_hash, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (username, display_name, role, salt, hash_password(pw, salt), now()),
                )

        # 默认语言
        for code, name in (("en", "English"), ("ja", "日本語")):
            row = conn.execute("SELECT 1 FROM languages WHERE code = ?", (code,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO languages (code, name, created_at) VALUES (?,?,?)",
                    (code, name, now()),
                )

        # 示例展签（仅首次初始化）
        count = conn.execute("SELECT COUNT(*) c FROM labels").fetchone()["c"]
        if count == 0:
            samples = [
                "青花缠枝莲纹瓶：明代永乐年间景德镇官窑制品，釉色浓艳，纹饰繁密。",
                "铜鎏金转经筒：清代藏传佛教法器，筒身錾刻六字真言，内藏经卷。",
                "活字印刷木活字一套：清代木质活字，用于族谱与典籍刷印。",
            ]
            for text in samples:
                conn.execute(
                    "INSERT INTO labels (source_text, source_rev, created_at, updated_at) "
                    "VALUES (?,1,?,?)",
                    (text, now(), now()),
                )
        conn.commit()
