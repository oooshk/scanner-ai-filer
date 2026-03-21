from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class EntityObservation:
    entity_type: str
    entity_key: str
    payload: dict[str, Any]
    source_ref: str
    observed_at: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class EntityStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities_current (
                    entity_type TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_last TEXT NOT NULL,
                    PRIMARY KEY (entity_type, entity_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_ref TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_messages (
                    account_name TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uid INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (account_name, mailbox, uid)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities_suppressed (
                    entity_type TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    suppressed_at TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    PRIMARY KEY (entity_type, entity_key)
                )
                """
            )
            conn.commit()

    def is_seen(self, account_name: str, mailbox: str, uid: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_messages WHERE account_name = ? AND mailbox = ? AND uid = ?",
                (account_name, mailbox, uid),
            ).fetchone()
            return row is not None

    def mark_seen(self, account_name: str, mailbox: str, uid: int, message_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO seen_messages(account_name, mailbox, uid, message_id, seen_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (account_name, mailbox, int(uid), message_id, utc_now_iso()),
            )
            conn.commit()

    def upsert_observation(self, obs: EntityObservation) -> str:
        if self.is_suppressed(obs.entity_type, obs.entity_key):
            return "suppressed"

        event_type = "created"
        with self._conn() as conn:
            existing = conn.execute(
                """
                SELECT payload_json, version
                FROM entities_current
                WHERE entity_type = ? AND entity_key = ?
                """,
                (obs.entity_type, obs.entity_key),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO entities_current(entity_type, entity_key, payload_json, first_seen_at, last_seen_at, version, source_last)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        obs.entity_type,
                        obs.entity_key,
                        json.dumps(obs.payload, ensure_ascii=True),
                        obs.observed_at,
                        obs.observed_at,
                        1,
                        obs.source_ref,
                    ),
                )
            else:
                current_payload = json.loads(str(existing["payload_json"]))
                if current_payload == obs.payload:
                    event_type = "observed"
                else:
                    event_type = "updated"
                conn.execute(
                    """
                    UPDATE entities_current
                    SET payload_json = ?, last_seen_at = ?, version = ?, source_last = ?
                    WHERE entity_type = ? AND entity_key = ?
                    """,
                    (
                        json.dumps(obs.payload, ensure_ascii=True),
                        obs.observed_at,
                        int(existing["version"]) + 1,
                        obs.source_ref,
                        obs.entity_type,
                        obs.entity_key,
                    ),
                )

            conn.execute(
                """
                INSERT INTO entities_events(event_time, entity_type, entity_key, event_type, payload_json, source_ref)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    obs.observed_at,
                    obs.entity_type,
                    obs.entity_key,
                    event_type,
                    json.dumps(obs.payload, ensure_ascii=True),
                    obs.source_ref,
                ),
            )
            conn.commit()

        return event_type

    def is_suppressed(self, entity_type: str, entity_key: str) -> bool:
        et = str(entity_type).strip().lower()
        ek = str(entity_key).strip()
        if not et or not ek:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM entities_suppressed WHERE entity_type = ? AND entity_key = ?",
                (et, ek),
            ).fetchone()
            return row is not None

    def suppress_entity(self, entity_type: str, entity_key: str, reason: str, source_ref: str = "system") -> bool:
        et = str(entity_type).strip().lower()
        ek = str(entity_key).strip()
        rsn = str(reason).strip() or "suppressed"
        src = str(source_ref).strip() or "system"
        if not et or not ek:
            return False

        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM entities_current WHERE entity_type = ? AND entity_key = ?",
                (et, ek),
            ).fetchone()

            conn.execute(
                """
                INSERT OR REPLACE INTO entities_suppressed(entity_type, entity_key, reason, suppressed_at, source_ref)
                VALUES(?, ?, ?, ?, ?)
                """,
                (et, ek, rsn, utc_now_iso(), src),
            )

            if row is not None:
                payload_json = str(row["payload_json"])
                conn.execute(
                    "DELETE FROM entities_current WHERE entity_type = ? AND entity_key = ?",
                    (et, ek),
                )
                conn.execute(
                    """
                    INSERT INTO entities_events(event_time, entity_type, entity_key, event_type, payload_json, source_ref)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (utc_now_iso(), et, ek, "suppressed", payload_json, src),
                )

            conn.commit()
        return True

    def export_current(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT entity_type, entity_key, payload_json, first_seen_at, last_seen_at, version, source_last
                FROM entities_current
                ORDER BY last_seen_at DESC
                """
            ).fetchall()
        for row in rows:
            out.append(
                {
                    "entity_type": str(row["entity_type"]),
                    "entity_key": str(row["entity_key"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "first_seen_at": str(row["first_seen_at"]),
                    "last_seen_at": str(row["last_seen_at"]),
                    "version": int(row["version"]),
                    "source_last": str(row["source_last"]),
                }
            )
        return out

    def export_recent_events(self, limit: int = 500) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT event_time, entity_type, entity_key, event_type, payload_json, source_ref
                FROM entities_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        for row in rows:
            out.append(
                {
                    "event_time": str(row["event_time"]),
                    "entity_type": str(row["entity_type"]),
                    "entity_key": str(row["entity_key"]),
                    "event_type": str(row["event_type"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "source_ref": str(row["source_ref"]),
                }
            )
        return out

    def query_current(self, entity_type: str = "", limit: int = 500) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._conn() as conn:
            if entity_type:
                rows = conn.execute(
                    """
                    SELECT entity_type, entity_key, payload_json, first_seen_at, last_seen_at, version, source_last
                    FROM entities_current
                    WHERE entity_type = ?
                    ORDER BY last_seen_at DESC
                    LIMIT ?
                    """,
                    (entity_type, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT entity_type, entity_key, payload_json, first_seen_at, last_seen_at, version, source_last
                    FROM entities_current
                    ORDER BY last_seen_at DESC
                    LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
        for row in rows:
            out.append(
                {
                    "entity_type": str(row["entity_type"]),
                    "entity_key": str(row["entity_key"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "first_seen_at": str(row["first_seen_at"]),
                    "last_seen_at": str(row["last_seen_at"]),
                    "version": int(row["version"]),
                    "source_last": str(row["source_last"]),
                }
            )
        return out

    def query_events(self, since_iso: str = "", entity_type: str = "", limit: int = 500) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        query = (
            "SELECT event_time, entity_type, entity_key, event_type, payload_json, source_ref "
            "FROM entities_events"
        )
        where: list[str] = []
        params: list[Any] = []

        if since_iso:
            where.append("event_time >= ?")
            params.append(since_iso)
        if entity_type:
            where.append("entity_type = ?")
            params.append(entity_type)

        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))

        with self._conn() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        for row in rows:
            out.append(
                {
                    "event_time": str(row["event_time"]),
                    "entity_type": str(row["entity_type"]),
                    "entity_key": str(row["entity_key"]),
                    "event_type": str(row["event_type"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "source_ref": str(row["source_ref"]),
                }
            )
        return out

    def untrack_entity(self, entity_type: str, entity_key: str, source_ref: str = "ui") -> bool:
        et = str(entity_type).strip().lower()
        ek = str(entity_key).strip()
        if not et or not ek:
            return False

        # Keep manual untrack sticky so future scans do not recreate the entity.
        return self.suppress_entity(et, ek, reason="untracked_by_user", source_ref=source_ref)
