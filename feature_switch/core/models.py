from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional

from .enums import VersionStatus, AuditAction, MigrationStatus, ChangeType


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class FeatureSwitch:
    env: str
    name: str
    id: Optional[int] = None
    created_at: str = field(default_factory=_now_iso)

    def key(self) -> str:
        return f"{self.env}:{self.name}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SwitchVersion:
    switch_id: int
    env: str
    name: str
    author: str
    rollout_ratio: int
    whitelist: list[str]
    dependencies: list[str]
    default_value: bool
    status: VersionStatus = VersionStatus.DRAFT
    version: int = 1
    id: Optional[int] = None
    approver: Optional[str] = None
    rollback_reason: Optional[str] = None
    replace_reason: Optional[str] = None
    reject_reason: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    submitted_at: Optional[str] = None
    approved_at: Optional[str] = None
    published_at: Optional[str] = None
    rolled_back_at: Optional[str] = None
    deprecated_at: Optional[str] = None

    def effective_snapshot(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "name": self.name,
            "version": self.version,
            "rollout_ratio": self.rollout_ratio,
            "whitelist": list(self.whitelist),
            "dependencies": list(self.dependencies),
            "default_value": self.default_value,
            "author": self.author,
            "published_at": self.published_at,
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_row(cls, row: dict) -> "SwitchVersion":
        import json

        def _parse_list(raw: Any) -> list[str]:
            if raw is None:
                return []
            if isinstance(raw, list):
                return list(raw)
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    return list(parsed) if isinstance(parsed, list) else []
                except (json.JSONDecodeError, TypeError):
                    return []
            return []

        status = VersionStatus(row["status"]) if isinstance(row["status"], str) else row["status"]
        return cls(
            id=row.get("id"),
            switch_id=row["switch_id"],
            env=row["env"],
            name=row["name"],
            version=row["version"],
            author=row["author"],
            approver=row.get("approver"),
            status=status,
            rollout_ratio=row["rollout_ratio"],
            whitelist=_parse_list(row.get("whitelist")),
            dependencies=_parse_list(row.get("dependencies")),
            default_value=bool(row["default_value"]) if not isinstance(row["default_value"], bool) else row["default_value"],
            rollback_reason=row.get("rollback_reason"),
            replace_reason=row.get("replace_reason"),
            reject_reason=row.get("reject_reason"),
            created_at=row.get("created_at") or _now_iso(),
            submitted_at=row.get("submitted_at"),
            approved_at=row.get("approved_at"),
            published_at=row.get("published_at"),
            rolled_back_at=row.get("rolled_back_at"),
            deprecated_at=row.get("deprecated_at"),
        )


@dataclass
class AuditLog:
    actor: str
    action: AuditAction
    switch_name: str
    env: str
    id: Optional[int] = None
    version: Optional[int] = None
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    details: str = ""
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["action"] = self.action.value if isinstance(self.action, AuditAction) else self.action
        return data

    @classmethod
    def from_row(cls, row: dict) -> "AuditLog":
        action = AuditAction(row["action"]) if isinstance(row["action"], str) else row["action"]
        return cls(
            id=row.get("id"),
            actor=row["actor"],
            action=action,
            env=row["env"],
            switch_name=row["switch_name"],
            version=row.get("version"),
            old_status=row.get("old_status"),
            new_status=row.get("new_status"),
            details=row.get("details") or "",
            timestamp=row.get("timestamp") or _now_iso(),
        )


@dataclass
class VersionDiff:
    prev_version: Optional[int]
    curr_version: int
    field_changes: dict[str, tuple[Any, Any]]
    replace_reason: Optional[str]

    def format(self) -> str:
        lines = [f"V{self.prev_version or 'NEW'} -> V{self.curr_version}"]
        if self.replace_reason:
            lines.append(f"  替换原因: {self.replace_reason}")
        for field, (old, new) in self.field_changes.items():
            lines.append(f"  - {field}: {old!r} -> {new!r}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Migration package models
# ---------------------------------------------------------------------------

_MIGRATION_SCHEMA_VERSION = "2.0"


@dataclass
class MigrationSwitchSnapshot:
    """一个开关在迁移包里的快照（源环境生效版的内容）。"""
    env: str
    name: str
    version: int
    rollout_ratio: int
    whitelist: list[str]
    dependencies: list[str]
    default_value: bool
    author: str
    approver: Optional[str] = None
    published_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "MigrationSwitchSnapshot":
        import json

        def _pl(raw: Any) -> list[str]:
            if raw is None:
                return []
            if isinstance(raw, list):
                return list(raw)
            if isinstance(raw, str):
                try:
                    return list(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    return []
            return []

        return cls(
            env=row["env"],
            name=row["name"],
            version=row["version"],
            rollout_ratio=row["rollout_ratio"],
            whitelist=_pl(row.get("whitelist")),
            dependencies=_pl(row.get("dependencies")),
            default_value=bool(row["default_value"]) if not isinstance(row.get("default_value"), bool) else row["default_value"],
            author=row["author"],
            approver=row.get("approver"),
            published_at=row.get("published_at"),
        )

    @classmethod
    def from_version(cls, v: SwitchVersion) -> "MigrationSwitchSnapshot":
        return cls(
            env=v.env,
            name=v.name,
            version=v.version,
            rollout_ratio=v.rollout_ratio,
            whitelist=list(v.whitelist),
            dependencies=list(v.dependencies),
            default_value=v.default_value,
            author=v.author,
            approver=v.approver,
            published_at=v.published_at,
        )

    def effective_snapshot(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "name": self.name,
            "version": self.version,
            "rollout_ratio": self.rollout_ratio,
            "whitelist": list(self.whitelist),
            "dependencies": list(self.dependencies),
            "default_value": self.default_value,
            "author": self.author,
            "published_at": self.published_at,
        }


@dataclass
class MigrationPackage:
    """迁移包：从源环境导出的一批生效开关。"""
    package_id: str
    source_env: str
    target_env: str
    created_by: str
    description: str = ""
    status: MigrationStatus = MigrationStatus.CREATED
    switches: list[MigrationSwitchSnapshot] = field(default_factory=list)
    id: Optional[int] = None
    created_at: str = field(default_factory=_now_iso)
    previewed_at: Optional[str] = None
    imported_at: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    rejected_by: Optional[str] = None
    rejected_at: Optional[str] = None
    reject_reason: Optional[str] = None
    checksum: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value if isinstance(self.status, MigrationStatus) else self.status
        data["switches"] = [s.to_dict() for s in self.switches]
        return data

    def to_export_dict(self) -> dict:
        """用于导出到 YAML/JSON 的精简结构。"""
        return {
            "schema_version": _MIGRATION_SCHEMA_VERSION,
            "package_id": self.package_id,
            "source_env": self.source_env,
            "target_env": self.target_env,
            "created_by": self.created_by,
            "description": self.description,
            "created_at": self.created_at,
            "checksum": self.checksum,
            "switch_count": len(self.switches),
            "switches": [s.to_dict() for s in self.switches],
        }


@dataclass
class MigrationDiffEntry:
    """预演时单个开关的 diff 摘要。"""
    env: str
    name: str
    change_type: ChangeType
    source_snapshot: Optional[MigrationSwitchSnapshot]
    target_effective: Optional[SwitchVersion]
    target_draft: Optional[SwitchVersion]
    target_pending: Optional[SwitchVersion]
    field_changes: dict[str, tuple[Any, Any]]
    dependency_gaps: list[str]
    required_approvers: list[str]
    conflict_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "env": self.env,
            "name": self.name,
            "change_type": self.change_type.value if isinstance(self.change_type, ChangeType) else self.change_type,
            "source_version": self.source_snapshot.version if self.source_snapshot else None,
            "target_effective_version": self.target_effective.version if self.target_effective else None,
            "target_draft_version": self.target_draft.version if self.target_draft else None,
            "target_pending_version": self.target_pending.version if self.target_pending else None,
            "field_changes": {k: list(v) for k, v in self.field_changes.items()},
            "dependency_gaps": list(self.dependency_gaps),
            "required_approvers": list(self.required_approvers),
            "conflict_reason": self.conflict_reason,
        }


@dataclass
class MigrationPreview:
    """迁移预演结果汇总。"""
    package_id: str
    source_env: str
    target_env: str
    entries: list[MigrationDiffEntry]
    summary: dict[str, int]
    all_dependency_gaps: list[str]
    all_required_approvers: list[str]
    blocking_issues: list[str]
    can_import: bool

    def to_dict(self) -> dict:
        return {
            "package_id": self.package_id,
            "source_env": self.source_env,
            "target_env": self.target_env,
            "summary": dict(self.summary),
            "entries": [e.to_dict() for e in self.entries],
            "all_dependency_gaps": list(self.all_dependency_gaps),
            "all_required_approvers": list(self.all_required_approvers),
            "blocking_issues": list(self.blocking_issues),
            "can_import": self.can_import,
        }


@dataclass
class MigrationRecord:
    """迁移执行记录（每一次导入/审批/回滚都写一条）。"""
    package_id: str
    action: str
    actor: str
    env: str
    id: Optional[int] = None
    switch_name: Optional[str] = None
    version: Optional[int] = None
    details: str = ""
    rollback_source_package_id: Optional[str] = None
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        data = asdict(self)
        return data

    @classmethod
    def from_row(cls, row: dict) -> "MigrationRecord":
        return cls(
            id=row.get("id"),
            package_id=row["package_id"],
            action=row["action"],
            actor=row["actor"],
            env=row["env"],
            switch_name=row.get("switch_name"),
            version=row.get("version"),
            details=row.get("details") or "",
            rollback_source_package_id=row.get("rollback_source_package_id"),
            timestamp=row.get("timestamp") or _now_iso(),
        )
