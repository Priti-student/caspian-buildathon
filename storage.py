"""SQLite persistence for conversation history and StudentPilot records."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
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
                CREATE TABLE IF NOT EXISTS opportunity_records (
                    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    batch_id INTEGER NOT NULL, position INTEGER NOT NULL, rank INTEGER NOT NULL,
                    title TEXT NOT NULL, organization TEXT, category TEXT, role TEXT, eligibility TEXT,
                    skills TEXT, location TEXT, deadline TEXT, urgency TEXT, links TEXT, summary TEXT,
                    relevance_score INTEGER, relevance_reason TEXT, created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, batch_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_opportunities_conversation
                    ON opportunity_records(conversation_id, batch_id, position);
                CREATE TABLE IF NOT EXISTS opportunity_context (
                    conversation_id TEXT PRIMARY KEY, batch_id INTEGER NOT NULL, selected_position INTEGER,
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
                """
            )

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

    def find_items(self, conversation_id: str, query: str = "", include_completed: bool = False) -> list[dict[str, Any]]:
        where = "conversation_id=?"
        params: list[Any] = [conversation_id]
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

    def find_items_on(self, conversation_id: str, on_date: str, include_completed: bool = False) -> list[dict[str, Any]]:
        where = "conversation_id=? AND (event_date=? OR deadline=?)"
        params: list[Any] = [conversation_id, on_date, on_date]
        if not include_completed:
            where += " AND status != 'completed'"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM planner_items WHERE {where} ORDER BY COALESCE(start_time,'99:99'), id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_items(self, conversation_id: str, query: str, updates: dict[str, Any]) -> int:
        allowed = {"title", "item_type", "event_date", "start_time", "end_time", "deadline", "priority", "status", "notes", "recurrence"}
        values = {key: value for key, value in updates.items() if key in allowed and value is not None}
        if not values:
            return 0
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE planner_items SET {assignments}, updated_at=? WHERE conversation_id=? AND lower(title) LIKE ?",
                (*values.values(), self._now(), conversation_id, f"%{query.lower()}%"),
            )
            count = cursor.rowcount
            cursor.close()
        return count

    def delete_items(self, conversation_id: str, query: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM planner_items WHERE conversation_id=? AND lower(title) LIKE ?",
                (conversation_id, f"%{query.lower()}%"),
            )
            count = cursor.rowcount
            cursor.close()
        return count

    def save_opportunities(self, conversation_id: str, user_id: str, opportunities: list[dict[str, Any]]) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(batch_id), 0) + 1 AS next_batch FROM opportunity_records WHERE conversation_id=?",
                (conversation_id,),
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
                "INSERT OR REPLACE INTO opportunity_context (conversation_id,batch_id,selected_position,updated_at) VALUES (?,?,NULL,?)",
                (conversation_id, batch_id, now),
            )

    def latest_opportunities(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT batch_id FROM opportunity_context WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
            if not row:
                return []
            rows = connection.execute(
                "SELECT * FROM opportunity_records WHERE conversation_id=? AND batch_id=? ORDER BY position",
                (conversation_id, row["batch_id"]),
            ).fetchall()
        return [dict(row) for row in rows]

    def selected_opportunity_position(self, conversation_id: str) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT selected_position FROM opportunity_context WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        return row["selected_position"] if row and row["selected_position"] else None

    def select_opportunity(self, conversation_id: str, position: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE opportunity_context SET selected_position=?, updated_at=? WHERE conversation_id=?",
                (position, self._now(), conversation_id),
            )

    def create_reminder(self, conversation_id: str, user_id: str, item_id: int | None, title: str, remind_at: str, recurrence: str | None = None) -> None:
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO reminders
                (conversation_id,user_id,item_id,title,remind_at,recurrence,active,created_at,updated_at)
                VALUES (?,?,?,?,?,?,1,?,?)""",
                (conversation_id, user_id, item_id, title, remind_at, recurrence, now, now),
            )

    def find_reminders(self, conversation_id: str, active_only: bool = True) -> list[dict[str, Any]]:
        where = "conversation_id=?"
        params: list[Any] = [conversation_id]
        if active_only:
            where += " AND active=1"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM reminders WHERE {where} ORDER BY remind_at, id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_reminders(self, conversation_id: str, query: str, updates: dict[str, Any]) -> int:
        allowed = {"remind_at", "recurrence", "active", "title"}
        values = {key: value for key, value in updates.items() if key in allowed and value is not None}
        if not values:
            return 0
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE reminders SET {assignments}, updated_at=? WHERE conversation_id=? AND active=1 AND lower(title) LIKE ?",
                (*values.values(), self._now(), conversation_id, f"%{query.lower()}%"),
            )
            count = cursor.rowcount
            cursor.close()
        return count

    def delete_reminders(self, conversation_id: str, query: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM reminders WHERE conversation_id=? AND lower(title) LIKE ?",
                (conversation_id, f"%{query.lower()}%"),
            )
            count = cursor.rowcount
            cursor.close()
        return count

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
