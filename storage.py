"""SQLite persistence for conversation history, StudentPilot records, and identity linking."""

import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class StudentPilotStore:
    def __init__(self, path: str | Path = "studentpilot.db") -> None:
        self.path = Path(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_history_conversation
                    ON conversation_messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS planner_items (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    title TEXT NOT NULL, item_type TEXT NOT NULL, event_date TEXT,
                    start_time TEXT, end_time TEXT, deadline TEXT, priority TEXT,
                    status TEXT NOT NULL DEFAULT 'active', notes TEXT, recurrence TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_planner_conversation
                    ON planner_items(conversation_id, status);
                CREATE INDEX IF NOT EXISTS idx_planner_user
                    ON planner_items(user_id, status);
                CREATE TABLE IF NOT EXISTS opportunity_records (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    batch_id INTEGER NOT NULL, position INTEGER NOT NULL, rank INTEGER NOT NULL,
                    title TEXT NOT NULL, organization TEXT, category TEXT, role TEXT, eligibility TEXT,
                    skills TEXT, location TEXT, deadline TEXT, urgency TEXT, links TEXT, summary TEXT,
                    relevance_score INTEGER, relevance_reason TEXT, created_at TEXT NOT NULL,
                    UNIQUE(user_id, batch_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_opportunities_user
                    ON opportunity_records(user_id, batch_id, position);
                CREATE TABLE IF NOT EXISTS opportunity_context (
                    conversation_id TEXT PRIMARY KEY, batch_id INTEGER NOT NULL, selected_position INTEGER,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_opportunity_context (
                    user_id TEXT PRIMARY KEY, batch_id INTEGER NOT NULL, selected_position INTEGER,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    item_id INTEGER, title TEXT NOT NULL, remind_at TEXT NOT NULL,
                    recurrence TEXT, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_conversation
                    ON reminders(conversation_id, active);
                CREATE INDEX IF NOT EXISTS idx_reminders_user
                    ON reminders(user_id, active);
                CREATE TABLE IF NOT EXISTS user_identities (
                    id INTEGER PRIMARY KEY, canonical_user_id TEXT NOT NULL,
                    channel TEXT NOT NULL, address TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(channel, address)
                );
                CREATE INDEX IF NOT EXISTS idx_identities_canonical
                    ON user_identities(canonical_user_id);
                CREATE TABLE IF NOT EXISTS otp_codes (
                    id INTEGER PRIMARY KEY, canonical_user_id TEXT NOT NULL,
                    email_address TEXT NOT NULL, code_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    verified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_otp_user
                    ON otp_codes(canonical_user_id, email_address);
                """
            )
        self._migrate_legacy_user_ids()

    def _migrate_legacy_user_ids(self) -> None:
        """One-time backfill: re-key pre-identity structured rows from raw channel
        addresses to canonical user ids so existing data stays visible after the
        user-scoped memory change."""
        tables = ("planner_items", "opportunity_records", "reminders", "conversation_messages")
        with self._connection() as connection:
            for table in tables:
                rows = connection.execute(
                    f"SELECT DISTINCT user_id FROM {table} WHERE user_id NOT LIKE 'user_%'"
                ).fetchall()
                for row in rows:
                    legacy = row["user_id"]
                    canonical = self._resolve_user_id_locked(connection, "legacy", legacy)
                    connection.execute(
                        f"UPDATE {table} SET user_id=? WHERE user_id=?", (canonical, legacy)
                    )

    @staticmethod
    def _resolve_user_id_locked(connection: sqlite3.Connection, channel: str, address: str) -> str:
        """Resolve within an existing connection; helper for migrations."""
        address = address.strip().lower()
        row = connection.execute(
            "SELECT canonical_user_id FROM user_identities WHERE channel=? AND address=?",
            (channel, address),
        ).fetchone()
        if row:
            return row["canonical_user_id"]
        # Cross-channel fallback: legacy rows registered under 'legacy' channel
        # should resolve to the same canonical as the real channel address.
        row = connection.execute(
            "SELECT canonical_user_id FROM user_identities WHERE address=? ORDER BY id LIMIT 1",
            (address,),
        ).fetchone()
        if row:
            return row["canonical_user_id"]
        canonical = f"user_{uuid.uuid4().hex[:12]}"
        connection.execute(
            "INSERT INTO user_identities (canonical_user_id, channel, address, created_at) VALUES (?,?,?,?)",
            (canonical, channel, address, datetime.now().isoformat(timespec="seconds")),
        )
        return canonical

    def _resolve_user_id(self, channel: str, address: str) -> str:
        address = address.strip().lower()
        with self._connection() as connection:
            return self._resolve_user_id_locked(connection, channel, address)

    # ── Identity resolution and linking ─────────────────────────────────────

    def resolve_user_id(self, channel: str, address: str) -> str:
        """Return the canonical user id for a channel identity, creating one if
        unknown. Cross-channel fallback keeps legacy/linked identities unified."""
        return self._resolve_user_id(channel, address)

    def link_identity(self, canonical_user_id: str, channel: str, address: str) -> None:
        """Associate a channel identity with a canonical user id (idempotent)."""
        address = address.strip().lower()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT canonical_user_id FROM user_identities WHERE channel=? AND address=?",
                (channel, address),
            ).fetchone()
            if row:
                # If this identity already belongs to a different user, keep it as-is
                # (prevents accidental merging of two different logical users).
                return
            connection.execute(
                "INSERT INTO user_identities (canonical_user_id, channel, address, created_at) VALUES (?,?,?,?)",
                (canonical_user_id, channel, address, self._now()),
            )

    def unlink_identity(self, channel: str, address: str) -> bool:
        """Remove a channel identity link. The user's data is preserved."""
        address = address.strip().lower()
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM user_identities WHERE channel=? AND address=?",
                (channel, address),
            )
            return cursor.rowcount > 0

    def identities_for_user(self, canonical_user_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT channel, address, created_at FROM user_identities WHERE canonical_user_id=? ORDER BY channel, address",
                (canonical_user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def email_linked_to_user(self, canonical_user_id: str, email_address: str) -> bool:
        email_address = email_address.strip().lower()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM user_identities WHERE canonical_user_id=? AND channel='email' AND address=?",
                (canonical_user_id, email_address),
            ).fetchone()
            return row is not None

    def pending_otp_email(self, canonical_user_id: str) -> str | None:
        """Return the most recent unexpired, unverified email awaiting OTP, if any."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connection() as connection:
            row = connection.execute(
                """SELECT email_address FROM otp_codes
                   WHERE canonical_user_id=? AND verified=0 AND expires_at > ?
                   ORDER BY id DESC LIMIT 1""",
                (canonical_user_id, now),
            ).fetchone()
        return row["email_address"] if row else None

    # ── OTP ──────────────────────────────────────────────────────────────────

    def create_otp(
        self, canonical_user_id: str, email_address: str, ttl_seconds: int = 600, max_attempts: int = 5
    ) -> str:
        """Generate a 6-digit OTP, store only its SHA-256 hash, and return the plaintext."""
        email_address = email_address.strip().lower()
        # Invalidate any previously pending (unverified) OTPs for this user+email to
        # prevent multiple active verification requests.
        with self._connection() as connection:
            connection.execute(
                """UPDATE otp_codes SET verified=1
                   WHERE canonical_user_id=? AND email_address=? AND verified=0""",
                (canonical_user_id, email_address),
            )
        code = f"{secrets.randbelow(1000000):06d}"
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        expires_at = (datetime.now() + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO otp_codes
                   (canonical_user_id,email_address,code_hash,expires_at,attempts,max_attempts,verified,created_at)
                   VALUES (?,?,?,?,0,?,0,?)""",
                (canonical_user_id, email_address, code_hash, expires_at, max_attempts, self._now()),
            )
        return code

    def verify_otp(self, canonical_user_id: str, email_address: str, code: str) -> tuple[bool, str]:
        """Verify a submitted OTP. Returns (success, message)."""
        email_address = email_address.strip().lower()
        code_hash = hashlib.sha256(code.strip().encode()).hexdigest()
        now = datetime.now().isoformat(timespec="seconds")
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM otp_codes
                   WHERE canonical_user_id=? AND email_address=? AND verified=0
                   ORDER BY id DESC LIMIT 1""",
                (canonical_user_id, email_address),
            ).fetchone()
            if not row:
                return False, "No pending verification for that email address."
            if row["expires_at"] <= now:
                connection.execute("UPDATE otp_codes SET verified=1 WHERE id=?", (row["id"],))
                return False, "That code has expired. Please request a new one."
            if row["attempts"] >= row["max_attempts"]:
                connection.execute("UPDATE otp_codes SET verified=1 WHERE id=?", (row["id"],))
                return False, "Too many incorrect attempts. Please request a new code."
            if row["code_hash"] != code_hash:
                connection.execute(
                    "UPDATE otp_codes SET attempts=attempts+1 WHERE id=?", (row["id"],)
                )
                return False, "Incorrect code. Please try again."
            connection.execute("UPDATE otp_codes SET verified=1 WHERE id=?", (row["id"],))
            # Persist the email as a verified identity of this user.
            connection.execute(
                "INSERT OR IGNORE INTO user_identities (canonical_user_id, channel, address, created_at) VALUES (?,?,?,?)",
                (canonical_user_id, "email", email_address, self._now()),
            )
            return True, "Email successfully verified and linked."

    # ── Conversation history (conversation-scoped, unchanged) ────────────────

    def add_message(self, conversation_id: str, user_id: str, role: str, content: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO conversation_messages (conversation_id,user_id,role,content,created_at) VALUES (?,?,?,?,?)",
                (conversation_id, user_id, role, content, self._now()),
            )

    def recent_messages(self, conversation_id: str, limit: int = 16) -> list[dict[str, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT role,content FROM conversation_messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    # ── Planner / task & event (user-scoped structured memory, conversation as provenance) ──

    def create_item(self, conversation_id: str, user_id: str, item: dict[str, Any]) -> None:
        fields = ("title", "item_type", "event_date", "start_time", "end_time", "deadline", "priority", "notes", "recurrence")
        values = [item.get(field) for field in fields]
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO planner_items
                (conversation_id,user_id,title,item_type,event_date,start_time,end_time,deadline,priority,notes,recurrence,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (conversation_id, user_id, *values, now, now),
            )

    def find_items(self, user_id: str, query: str = "", include_completed: bool = False) -> list[dict[str, Any]]:
        where = "user_id=?"
        params: list[Any] = [user_id]
        if not include_completed:
            where += " AND status != 'completed'"
        if query:
            where += " AND lower(title) LIKE ?"
            params.append(f"%{query.lower()}%")
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM planner_items WHERE {where} ORDER BY COALESCE(event_date,deadline,'9999-12-31'), id DESC LIMIT 20",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def find_items_on(self, user_id: str, on_date: str, include_completed: bool = False) -> list[dict[str, Any]]:
        where = "user_id=? AND (event_date=? OR deadline=?)"
        params: list[Any] = [user_id, on_date, on_date]
        if not include_completed:
            where += " AND status != 'completed'"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM planner_items WHERE {where} ORDER BY COALESCE(start_time,'99:99'), id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_items(self, user_id: str, query: str, updates: dict[str, Any]) -> int:
        allowed = {"title", "item_type", "event_date", "start_time", "end_time", "deadline", "priority", "status", "notes", "recurrence"}
        values = {key: value for key, value in updates.items() if key in allowed and value is not None}
        if not values:
            return 0
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE planner_items SET {assignments}, updated_at=? WHERE user_id=? AND lower(title) LIKE ?",
                (*values.values(), self._now(), user_id, f"%{query.lower()}%"),
            )
            count = cursor.rowcount
            cursor.close()
        return count

    def delete_items(self, user_id: str, query: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM planner_items WHERE user_id=? AND lower(title) LIKE ?",
                (user_id, f"%{query.lower()}%"),
            )
            count = cursor.rowcount
            cursor.close()
        return count

    # ── Opportunities (user-scoped structured memory) ─────────────────────────

    def save_opportunities(self, conversation_id: str, user_id: str, opportunities: list[dict[str, Any]]) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(batch_id), 0) + 1 AS next_batch FROM opportunity_records WHERE user_id=?",
                (user_id,),
            ).fetchone()
            batch_id = row["next_batch"]
            now = self._now()
            for position, item in enumerate(opportunities, start=1):
                connection.execute(
                    """INSERT INTO opportunity_records
                    (conversation_id,user_id,batch_id,position,rank,title,organization,category,role,eligibility,skills,location,deadline,urgency,links,summary,relevance_score,relevance_reason,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (conversation_id, user_id, batch_id, position, item["rank"], item["title"], item["organization"],
                     item["category"], item["role"], item["eligibility"], item["skills"], item["location"],
                     item["deadline"], item["urgency"], item["links"], item["summary"], item["relevance_score"],
                     item["relevance_reason"], now),
                )
            connection.execute(
                "INSERT OR REPLACE INTO user_opportunity_context (user_id,batch_id,selected_position,updated_at) VALUES (?,?,NULL,?)",
                (user_id, batch_id, now),
            )

    def latest_opportunities(self, user_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT batch_id FROM user_opportunity_context WHERE user_id=?", (user_id,)
            ).fetchone()
            if not row:
                return []
            rows = connection.execute(
                "SELECT * FROM opportunity_records WHERE user_id=? AND batch_id=? ORDER BY position",
                (user_id, row["batch_id"]),
            ).fetchall()
        return [dict(row) for row in rows]

    def selected_opportunity_position(self, user_id: str) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT selected_position FROM user_opportunity_context WHERE user_id=?", (user_id,)
            ).fetchone()
        return row["selected_position"] if row and row["selected_position"] else None

    def select_opportunity(self, user_id: str, position: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE user_opportunity_context SET selected_position=?, updated_at=? WHERE user_id=?",
                (position, self._now(), user_id),
            )

    # ── Reminders (user-scoped structured memory) ─────────────────────────────

    def create_reminder(self, conversation_id: str, user_id: str, item_id: int | None, title: str, remind_at: str, recurrence: str | None = None) -> None:
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO reminders
                (conversation_id,user_id,item_id,title,remind_at,recurrence,active,created_at,updated_at)
                VALUES (?,?,?,?,?,?,1,?,?)""",
                (conversation_id, user_id, item_id, title, remind_at, recurrence, now, now),
            )

    def find_reminders(self, user_id: str, active_only: bool = True) -> list[dict[str, Any]]:
        where = "user_id=?"
        params: list[Any] = [user_id]
        if active_only:
            where += " AND active=1"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM reminders WHERE {where} ORDER BY remind_at, id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_reminders(self, user_id: str, query: str, updates: dict[str, Any]) -> int:
        allowed = {"remind_at", "recurrence", "active", "title"}
        values = {key: value for key, value in updates.items() if key in allowed and value is not None}
        if not values:
            return 0
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE reminders SET {assignments}, updated_at=? WHERE user_id=? AND active=1 AND lower(title) LIKE ?",
                (*values.values(), self._now(), user_id, f"%{query.lower()}%"),
            )
            count = cursor.rowcount
            cursor.close()
        return count

    def delete_reminders(self, user_id: str, query: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM reminders WHERE user_id=? AND lower(title) LIKE ?",
                (user_id, f"%{query.lower()}%"),
            )
            count = cursor.rowcount
            cursor.close()
        return count

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")