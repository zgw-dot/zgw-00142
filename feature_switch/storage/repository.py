from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ..core.enums import VersionStatus, AuditAction, MigrationStatus
from ..core.models import (
    FeatureSwitch,
    SwitchVersion,
    AuditLog,
    MigrationPackage,
    MigrationSwitchSnapshot,
    MigrationRecord,
    _MIGRATION_SCHEMA_VERSION,
)


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

-- 迁移包：从源环境导出的一批开关快照
CREATE TABLE IF NOT EXISTS migration_package (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id              TEXT NOT NULL UNIQUE,
    source_env              TEXT NOT NULL,
    target_env              TEXT NOT NULL,
    created_by              TEXT NOT NULL,
    description             TEXT NOT NULL DEFAULT '',
    status                  TEXT NOT NULL DEFAULT 'CREATED',
    checksum                TEXT NOT NULL DEFAULT '',
    created_at              TEXT NOT NULL,
    previewed_at            TEXT,
    imported_at             TEXT,
    approved_by             TEXT,
    approved_at             TEXT,
    rejected_by             TEXT,
    rejected_at             TEXT,
    reject_reason           TEXT
);

CREATE INDEX IF NOT EXISTS idx_migration_package_env
    ON migration_package(source_env, target_env, status);

-- 迁移包里每个开关的快照
CREATE TABLE IF NOT EXISTS migration_switch (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id       INTEGER NOT NULL,
    package_uuid     TEXT NOT NULL,
    env              TEXT NOT NULL,
    name             TEXT NOT NULL,
    version          INTEGER NOT NULL,
    rollout_ratio    INTEGER NOT NULL,
    whitelist        TEXT NOT NULL DEFAULT '[]',
    dependencies     TEXT NOT NULL DEFAULT '[]',
    default_value    INTEGER NOT NULL DEFAULT 0,
    author           TEXT NOT NULL,
    approver         TEXT,
    published_at     TEXT,
    UNIQUE(package_id, env, name),
    FOREIGN KEY (package_id) REFERENCES migration_package(id) ON DELETE CASCADE
);

-- 迁移执行记录（审计链）
CREATE TABLE IF NOT EXISTS migration_record (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id                 TEXT NOT NULL,
    action                     TEXT NOT NULL,
    actor                      TEXT NOT NULL,
    env                        TEXT NOT NULL,
    switch_name                TEXT,
    version                    INTEGER,
    details                    TEXT NOT NULL DEFAULT '',
    rollback_source_package_id TEXT,
    timestamp                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_migration_record_pkg
    ON migration_record(package_id, timestamp);
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

    # ---------- MigrationPackage ----------

    def insert_migration_package(
        self, pkg: MigrationPackage, *, conn: Optional[sqlite3.Connection] = None
    ) -> MigrationPackage:
        c = conn or self._conn
        status_val = pkg.status.value if isinstance(pkg.status, MigrationStatus) else pkg.status
        cur = c.execute(
            """
            INSERT INTO migration_package(
                package_id, source_env, target_env, created_by, description,
                status, checksum, created_at, previewed_at, imported_at,
                approved_by, approved_at, rejected_by, rejected_at, reject_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pkg.package_id, pkg.source_env, pkg.target_env, pkg.created_by,
                pkg.description, status_val, pkg.checksum, pkg.created_at,
                pkg.previewed_at, pkg.imported_at, pkg.approved_by, pkg.approved_at,
                pkg.rejected_by, pkg.rejected_at, pkg.reject_reason,
            ),
        )
        pkg.id = cur.lastrowid
        # Insert switches
        for snap in pkg.switches:
            c.execute(
                """
                INSERT INTO migration_switch(
                    package_id, package_uuid, env, name, version,
                    rollout_ratio, whitelist, dependencies, default_value,
                    author, approver, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pkg.id, pkg.package_id, snap.env, snap.name, snap.version,
                    snap.rollout_ratio,
                    json.dumps(snap.whitelist, ensure_ascii=False),
                    json.dumps(snap.dependencies, ensure_ascii=False),
                    1 if snap.default_value else 0,
                    snap.author, snap.approver, snap.published_at,
                ),
            )
        return pkg

    def update_migration_package_fields(
        self,
        package_id: str,
        updates: dict[str, Any],
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        if not updates:
            return
        c = conn or self._conn
        if "status" in updates and isinstance(updates["status"], MigrationStatus):
            updates["status"] = updates["status"].value
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [package_id]
        c.execute(f"UPDATE migration_package SET {sets} WHERE package_id = ?", values)

    def get_migration_package(
        self, package_id: str
    ) -> Optional[MigrationPackage]:
        row = self._conn.execute(
            "SELECT * FROM migration_package WHERE package_id = ?", (package_id,)
        ).fetchone()
        if not row:
            return None
        switches = self._list_migration_switches(row["id"])
        d = dict(row)
        return MigrationPackage(
            id=d["id"],
            package_id=d["package_id"],
            source_env=d["source_env"],
            target_env=d["target_env"],
            created_by=d["created_by"],
            description=d.get("description") or "",
            status=MigrationStatus(d["status"]),
            checksum=d.get("checksum") or "",
            created_at=d["created_at"],
            previewed_at=d.get("previewed_at"),
            imported_at=d.get("imported_at"),
            approved_by=d.get("approved_by"),
            approved_at=d.get("approved_at"),
            rejected_by=d.get("rejected_by"),
            rejected_at=d.get("rejected_at"),
            reject_reason=d.get("reject_reason"),
            switches=switches,
        )

    def find_migration_by_checksum(
        self, source_env: str, target_env: str, checksum: str
    ) -> Optional[MigrationPackage]:
        row = self._conn.execute(
            """
            SELECT * FROM migration_package
            WHERE source_env = ? AND target_env = ? AND checksum = ?
            ORDER BY id DESC LIMIT 1
            """,
            (source_env, target_env, checksum),
        ).fetchone()
        if not row:
            return None
        return self.get_migration_package(row["package_id"])

    def list_migration_packages(
        self,
        source_env: Optional[str] = None,
        target_env: Optional[str] = None,
        statuses: Optional[list[MigrationStatus]] = None,
    ) -> list[MigrationPackage]:
        sql = "SELECT * FROM migration_package WHERE 1=1"
        params: list[Any] = []
        if source_env:
            sql += " AND source_env = ?"
            params.append(source_env)
        if target_env:
            sql += " AND target_env = ?"
            params.append(target_env)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(s.value for s in statuses)
        sql += " ORDER BY id DESC"
        rows = self._conn.execute(sql, params).fetchall()
        result: list[MigrationPackage] = []
        for row in rows:
            result.append(self.get_migration_package(row["package_id"]))  # type: ignore[arg-type]
        return result

    def _list_migration_switches(self, pkg_db_id: int) -> list[MigrationSwitchSnapshot]:
        rows = self._conn.execute(
            "SELECT * FROM migration_switch WHERE package_id = ? ORDER BY env, name",
            (pkg_db_id,),
        ).fetchall()
        return [MigrationSwitchSnapshot.from_row(dict(r)) for r in rows]

    # ---------- MigrationRecord ----------

    def append_migration_record(
        self, rec: MigrationRecord, *, conn: Optional[sqlite3.Connection] = None
    ) -> MigrationRecord:
        c = conn or self._conn
        cur = c.execute(
            """
            INSERT INTO migration_record(
                package_id, action, actor, env, switch_name, version,
                details, rollback_source_package_id, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.package_id, rec.action, rec.actor, rec.env, rec.switch_name,
                rec.version, rec.details, rec.rollback_source_package_id, rec.timestamp,
            ),
        )
        rec.id = cur.lastrowid
        return rec

    def list_migration_records(
        self, package_id: Optional[str] = None, limit: int = 100
    ) -> list[MigrationRecord]:
        sql = "SELECT * FROM migration_record WHERE 1=1"
        params: list[Any] = []
        if package_id:
            sql += " AND package_id = ?"
            params.append(package_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [MigrationRecord.from_row(dict(r)) for r in rows]
