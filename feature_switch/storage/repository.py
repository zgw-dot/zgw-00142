from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ..core.enums import VersionStatus, AuditAction
from ..core.models import FeatureSwitch, SwitchVersion, AuditLog


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feature_switch (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    env        TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(env, name)
);

CREATE TABLE IF NOT EXISTS switch_version (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    switch_id        INTEGER NOT NULL,
    env              TEXT NOT NULL,
    name             TEXT NOT NULL,
    version          INTEGER NOT NULL,
    author           TEXT NOT NULL,
    approver         TEXT,
    status           TEXT NOT NULL,
    rollout_ratio    INTEGER NOT NULL,
    whitelist        TEXT NOT NULL DEFAULT '[]',
    dependencies     TEXT NOT NULL DEFAULT '[]',
    default_value    INTEGER NOT NULL DEFAULT 0,
    rollback_reason  TEXT,
    replace_reason   TEXT,
    reject_reason    TEXT,
    created_at       TEXT NOT NULL,
    submitted_at     TEXT,
    approved_at      TEXT,
    published_at     TEXT,
    rolled_back_at   TEXT,
    deprecated_at    TEXT,
    UNIQUE(switch_id, version),
    FOREIGN KEY (switch_id) REFERENCES feature_switch(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_switch_version_status
    ON switch_version(env, name, status);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    env        TEXT NOT NULL,
    switch_name TEXT NOT NULL,
    version    INTEGER,
    old_status TEXT,
    new_status TEXT,
    details    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_switch
    ON audit_log(env, switch_name, timestamp);
"""


class SwitchRepository:
    """SQLite-backed repository with transactional writes.

    All mutating methods run inside a single transaction: if any step
    raises, nothing is persisted (preventing half-written imports).
    """

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # ---------- schema / connection helpers ----------

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.executescript(SCHEMA_SQL)
            finally:
                cur.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside an explicit BEGIN/COMMIT/ROLLBACK transaction."""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ---------- FeatureSwitch ----------

    def get_or_create_switch(
        self, env: str, name: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> FeatureSwitch:
        c = conn or self._conn
        row = c.execute(
            "SELECT * FROM feature_switch WHERE env = ? AND name = ?",
            (env, name),
        ).fetchone()
        if row:
            return FeatureSwitch(
                id=row["id"], env=row["env"], name=row["name"], created_at=row["created_at"]
            )
        from ..core.models import _now_iso
        cur = c.execute(
            "INSERT INTO feature_switch(env, name, created_at) VALUES (?, ?, ?)",
            (env, name, _now_iso()),
        )
        return FeatureSwitch(
            id=cur.lastrowid, env=env, name=name, created_at=_now_iso()
        )

    def find_switch(self, env: str, name: str) -> Optional[FeatureSwitch]:
        row = self._conn.execute(
            "SELECT * FROM feature_switch WHERE env = ? AND name = ?", (env, name)
        ).fetchone()
        if not row:
            return None
        return FeatureSwitch(
            id=row["id"], env=row["env"], name=row["name"], created_at=row["created_at"]
        )

    def list_switches(self, env: Optional[str] = None) -> list[FeatureSwitch]:
        sql = "SELECT * FROM feature_switch"
        params: tuple = ()
        if env:
            sql += " WHERE env = ?"
            params = (env,)
        sql += " ORDER BY env, name"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            FeatureSwitch(id=r["id"], env=r["env"], name=r["name"], created_at=r["created_at"])
            for r in rows
        ]

    # ---------- SwitchVersion ----------

    def next_version(self, switch_id: int, *, conn: Optional[sqlite3.Connection] = None) -> int:
        c = conn or self._conn
        row = c.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM switch_version WHERE switch_id = ?",
            (switch_id,),
        ).fetchone()
        return (row["v"] or 0) + 1

    def insert_version(
        self, version: SwitchVersion, *, conn: Optional[sqlite3.Connection] = None
    ) -> SwitchVersion:
        c = conn or self._conn
        cur = c.execute(
            """
            INSERT INTO switch_version(
                switch_id, env, name, version, author, approver, status,
                rollout_ratio, whitelist, dependencies, default_value,
                rollback_reason, replace_reason, reject_reason,
                created_at, submitted_at, approved_at, published_at,
                rolled_back_at, deprecated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.switch_id,
                version.env,
                version.name,
                version.version,
                version.author,
                version.approver,
                version.status.value if isinstance(version.status, VersionStatus) else version.status,
                version.rollout_ratio,
                json.dumps(version.whitelist, ensure_ascii=False),
                json.dumps(version.dependencies, ensure_ascii=False),
                1 if version.default_value else 0,
                version.rollback_reason,
                version.replace_reason,
                version.reject_reason,
                version.created_at,
                version.submitted_at,
                version.approved_at,
                version.published_at,
                version.rolled_back_at,
                version.deprecated_at,
            ),
        )
        version.id = cur.lastrowid
        return version

    def update_version_fields(
        self,
        version_id: int,
        updates: dict[str, Any],
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        if not updates:
            return
        c = conn or self._conn
        # serialize list-valued fields
        for k in ("whitelist", "dependencies"):
            if k in updates and updates[k] is not None:
                updates[k] = json.dumps(updates[k], ensure_ascii=False)
        if "default_value" in updates:
            updates["default_value"] = 1 if updates["default_value"] else 0
        if "status" in updates and isinstance(updates["status"], VersionStatus):
            updates["status"] = updates["status"].value
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [version_id]
        c.execute(f"UPDATE switch_version SET {sets} WHERE id = ?", values)

    def get_version(self, version_id: int) -> Optional[SwitchVersion]:
        row = self._conn.execute(
            "SELECT * FROM switch_version WHERE id = ?", (version_id,)
        ).fetchone()
        return SwitchVersion.from_row(dict(row)) if row else None

    def get_latest_version(
        self, env: str, name: str, statuses: Optional[list[VersionStatus]] = None
    ) -> Optional[SwitchVersion]:
        sql = "SELECT * FROM switch_version WHERE env = ? AND name = ?"
        params: list[Any] = [env, name]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(s.value for s in statuses)
        sql += " ORDER BY version DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return SwitchVersion.from_row(dict(row)) if row else None

    def get_effective_version(self, env: str, name: str) -> Optional[SwitchVersion]:
        return self.get_latest_version(env, name, [VersionStatus.PUBLISHED])

    def get_draft_version(self, env: str, name: str) -> Optional[SwitchVersion]:
        return self.get_latest_version(
            env, name, [VersionStatus.DRAFT, VersionStatus.PENDING_APPROVAL]
        )

    def list_versions(
        self,
        env: Optional[str] = None,
        name: Optional[str] = None,
        statuses: Optional[list[VersionStatus]] = None,
    ) -> list[SwitchVersion]:
        sql = "SELECT * FROM switch_version WHERE 1=1"
        params: list[Any] = []
        if env:
            sql += " AND env = ?"
            params.append(env)
        if name:
            sql += " AND name = ?"
            params.append(name)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(s.value for s in statuses)
        sql += " ORDER BY env, name, version DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [SwitchVersion.from_row(dict(r)) for r in rows]

    def list_published_switches(self, env: Optional[str] = None) -> list[SwitchVersion]:
        """Return one (latest published) version per switch."""
        sql = """
        SELECT sv.* FROM switch_version sv
        INNER JOIN (
            SELECT switch_id, MAX(version) AS mv
            FROM switch_version WHERE status = 'PUBLISHED'
            GROUP BY switch_id
        ) agg ON sv.switch_id = agg.switch_id AND sv.version = agg.mv
        """
        params: list[Any] = []
        if env:
            sql += " WHERE sv.env = ?"
            params.append(env)
        sql += " ORDER BY sv.env, sv.name"
        rows = self._conn.execute(sql, params).fetchall()
        return [SwitchVersion.from_row(dict(r)) for r in rows]

    # ---------- AuditLog ----------

    def append_audit(
        self, log: AuditLog, *, conn: Optional[sqlite3.Connection] = None
    ) -> AuditLog:
        c = conn or self._conn
        action_value = log.action.value if isinstance(log.action, AuditAction) else log.action
        cur = c.execute(
            """
            INSERT INTO audit_log(
                timestamp, actor, action, env, switch_name,
                version, old_status, new_status, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log.timestamp,
                log.actor,
                action_value,
                log.env,
                log.switch_name,
                log.version,
                log.old_status,
                log.new_status,
                log.details,
            ),
        )
        log.id = cur.lastrowid
        return log

    def list_audit(
        self,
        env: Optional[str] = None,
        switch_name: Optional[str] = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if env:
            sql += " AND env = ?"
            params.append(env)
        if switch_name:
            sql += " AND switch_name = ?"
            params.append(switch_name)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [AuditLog.from_row(dict(r)) for r in rows]
