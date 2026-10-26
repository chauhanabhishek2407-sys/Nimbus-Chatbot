"""Stores users, sessions, chats and messages.

Locally it uses a SQLite file. In the cloud, set DATABASE_URL to a PostgreSQL database
(Cloud SQL, Amazon RDS, Azure Database for PostgreSQL, Neon, Supabase...) and the same
code stores everything there instead. Tables are created automatically on startup.
"""
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager

import config

log = logging.getLogger("nimbus.db")

IS_POSTGRES = config.DATABASE_URL.startswith(("postgres://", "postgresql://"))
_pool = None

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
else:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)


def now_ms() -> int:
    return int(time.time() * 1000)


class _Conn:
    """Small wrapper so the same SQL (with ? placeholders) runs on SQLite and PostgreSQL."""

    def __init__(self, raw, postgres: bool):
        self.raw = raw
        self.postgres = postgres

    def execute(self, sql: str, params=()):
        if self.postgres:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def one(self, sql: str, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params=()):
        return [dict(r) for r in self.execute(sql, params).fetchall()]


@contextmanager
def connect():
    """One transaction: commits if the block succeeds, rolls back if it raises."""
    if IS_POSTGRES:
        with _pool.connection() as raw:
            yield _Conn(raw, postgres=True)
            raw.commit()
    else:
        raw = sqlite3.connect(config.SQLITE_PATH, timeout=15)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        try:
            yield _Conn(raw, postgres=False)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    about TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at);
CREATE TABLE IF NOT EXISTS messages (
    id {auto_id},
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{{}}',
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)
"""


def init_db():
    global _pool
    if IS_POSTGRES:
        _pool = ConnectionPool(
            config.DATABASE_URL, min_size=1, max_size=config.DB_POOL_SIZE,
            kwargs={"row_factory": dict_row}, open=True,
            check=ConnectionPool.check_connection,  # cloud databases drop idle connections
        )
        auto_id = "BIGSERIAL PRIMARY KEY"
    else:
        auto_id = "INTEGER PRIMARY KEY AUTOINCREMENT"
        with sqlite3.connect(config.SQLITE_PATH) as raw:
            raw.execute("PRAGMA journal_mode = WAL")
    with connect() as db:
        for statement in SCHEMA.format(auto_id=auto_id).split(";"):
            if statement.strip():
                db.execute(statement)
    log.info("Database ready: %s", "PostgreSQL" if IS_POSTGRES else f"SQLite ({config.SQLITE_PATH})")


def close():
    if _pool is not None:
        _pool.close()


# --- Users -----------------------------------------------------------------------
def create_user(name: str, email: str, password_hash: str):
    user = {"id": uuid.uuid4().hex, "email": email, "name": name,
            "password_hash": password_hash, "about": "", "created_at": now_ms()}
    try:
        with connect() as db:
            db.execute(
                "INSERT INTO users (id, email, name, password_hash, about, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user["id"], email, name, password_hash, "", user["created_at"]),
            )
    except INTEGRITY_ERRORS:
        return None  # email already registered
    return user


def get_user(user_id: str):
    with connect() as db:
        return db.one("SELECT * FROM users WHERE id = ?", (user_id,))


def get_user_by_email(email: str):
    with connect() as db:
        return db.one("SELECT * FROM users WHERE email = ?", (email,))


def update_user(user_id: str, name: str | None = None, about: str | None = None):
    with connect() as db:
        if name is not None:
            db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        if about is not None:
            db.execute("UPDATE users SET about = ? WHERE id = ?", (about, user_id))
    return get_user(user_id)


def delete_user(user_id: str):
    """Deletes the account and everything that belongs to it."""
    with connect() as db:
        db.execute(
            "DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id = ?)",
            (user_id,),
        )
        db.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --- Sessions --------------------------------------------------------------------
def create_session(user_id: str, token_hash: str, days: int):
    created = now_ms()
    with connect() as db:
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, created, created + days * 86_400_000),
        )


def get_session_user(token_hash: str):
    with connect() as db:
        return db.one(
            "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
            (token_hash, now_ms()),
        )


def delete_session(token_hash: str):
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_expired_sessions():
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_ms(),))


# --- Conversations ---------------------------------------------------------------
def list_conversations(user_id: str):
    with connect() as db:
        return db.all(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT 200",
            (user_id,),
        )


def get_conversation(conversation_id: str, user_id: str):
    """Returns the chat only if it belongs to this user."""
    with connect() as db:
        return db.one(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )


def create_conversation(user_id: str, title: str):
    conv = {"id": uuid.uuid4().hex, "title": title, "created_at": now_ms()}
    conv["updated_at"] = conv["created_at"]
    with connect() as db:
        db.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conv["id"], user_id, title, conv["created_at"], conv["updated_at"]),
        )
    return conv


def rename_conversation(conversation_id: str, user_id: str, title: str):
    with connect() as db:
        db.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
            (title, conversation_id, user_id),
        )


def touch_conversation(conversation_id: str):
    with connect() as db:
        db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now_ms(), conversation_id))


def delete_conversation(conversation_id: str, user_id: str):
    with connect() as db:
        if db.one("SELECT id FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)):
            db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


# --- Messages ----------------------------------------------------------------------
def _decode(row):
    row["meta"] = json.loads(row.get("meta") or "{}")
    return row


def add_message(conversation_id: str, role: str, content: str, meta: dict | None = None):
    with connect() as db:
        row = db.one(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (conversation_id, role, content, json.dumps(meta or {}), now_ms()),
        )
    return row["id"]


def get_messages(conversation_id: str):
    with connect() as db:
        rows = db.all(
            "SELECT id, role, content, meta, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
    return [_decode(r) for r in rows]


def delete_message(message_id: int):
    with connect() as db:
        db.execute("DELETE FROM messages WHERE id = ?", (message_id,))


# --- Export ------------------------------------------------------------------------
def export_user(user_id: str):
    user = get_user(user_id)
    chats = []
    for conv in list_conversations(user_id):
        conv["messages"] = [
            {"role": m["role"], "content": m["content"], "created_at": m["created_at"],
             "sources": m["meta"].get("sources", [])}
            for m in get_messages(conv["id"])
        ]
        chats.append(conv)
    return {
        "account": {k: user[k] for k in ("name", "email", "about", "created_at")},
        "conversations": chats,
        "exported_at": now_ms(),
    }
