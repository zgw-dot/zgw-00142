from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional

from .enums import VersionStatus, AuditAction


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
