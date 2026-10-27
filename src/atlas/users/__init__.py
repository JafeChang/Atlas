"""Passwordless named identities and persistent browser sessions."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SESSION_SECONDS = 30 * 24 * 60 * 60
RENEW_BEFORE_SECONDS = SESSION_SECONDS // 2


class UserStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, created_at INTEGER NOT NULL, user_id TEXT NOT NULL UNIQUE)")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_name TEXT NOT NULL, expires_at INTEGER NOT NULL)")
            if "user_id" not in {column[1] for column in db.execute("PRAGMA table_info(users)")}:
                # Serialize schema migration and re-check after waiting for another request.
                db.execute("BEGIN IMMEDIATE")
                if "user_id" not in {column[1] for column in db.execute("PRAGMA table_info(users)")}:
                    db.execute("ALTER TABLE users ADD COLUMN user_id TEXT")
                    for (name,) in db.execute("SELECT name FROM users").fetchall():
                        db.execute("UPDATE users SET user_id = ? WHERE name = ?", (secrets.token_hex(16), name))
                    db.execute("CREATE UNIQUE INDEX users_user_id ON users(user_id)")
                db.commit()
        except Exception:
            db.close()
            raise
        return db

    def login(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO users (name, created_at, user_id) VALUES (?, ?, ?) ON CONFLICT(name) DO NOTHING", (name, now, secrets.token_hex(16)))
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (self._hash(token), name, now + SESSION_SECONDS))
        return token

    def lookup(self, token: str) -> str | None:
        session = self.authenticate(token, renew=False)
        return session[0] if session else None

    def authenticate(self, token: str, *, renew: bool = True) -> tuple[str, str, int] | None:
        """Return name, stable user ID and remaining lifetime for a valid session."""
        if not token or len(token) > 128 or not token.isascii() or not self.path.exists():
            return None
        now = int(time.time())
        token_hash = self._hash(token)
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT sessions.user_name, sessions.expires_at, users.user_id FROM sessions JOIN users ON users.name = sessions.user_name WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
            if row and renew and row[1] <= now + RENEW_BEFORE_SECONDS:
                db.execute(
                    "UPDATE sessions SET expires_at = ? WHERE token_hash = ? AND expires_at > ? AND expires_at <= ? RETURNING user_name, expires_at",
                    (now + SESSION_SECONDS, token_hash, now, now + RENEW_BEFORE_SECONDS),
                ).fetchone()
                # Another request may have renewed or revoked this session meanwhile.
                row = db.execute(
                    "SELECT sessions.user_name, sessions.expires_at, users.user_id FROM sessions JOIN users ON users.name = sessions.user_name WHERE token_hash = ? AND expires_at > ?",
                    (token_hash, now),
                ).fetchone()
        return (row[0], row[2], max(0, row[1] - now)) if row else None

    def logout(self, token: str) -> None:
        if not token or len(token) > 128 or not token.isascii() or not self.path.exists():
            return
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash(token),))

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode('ascii')).hexdigest()
