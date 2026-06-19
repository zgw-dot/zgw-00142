from __future__ import annotations

from typing import Any, Optional

from ..audit import AuditTrail
from ..core.enums import AuditAction, VersionStatus
from ..core.models import SwitchVersion, VersionDiff, _now_iso
from ..storage.repository import SwitchRepository
from ..validator.validators import (
    ValidationError,
    validate_dependencies,
    validate_not_self_approve,
    validate_ratio,
    validate_switch_payload,
    validate_transition,
)


_PAYLOAD_FIELDS = (
    "rollout_ratio",
    "whitelist",
    "dependencies",
    "default_value",
)


class SwitchService:
    """Primary business orchestration layer.

    Every mutating call runs inside a repository transaction so that a
    failure at any point leaves the database untouched.
    """

    def __init__(self, repo: SwitchRepository, audit: AuditTrail) -> None:
        self.repo = repo
        self.audit = audit

    # ------------------------------------------------------------------
    # Draft lifecycle
    # ------------------------------------------------------------------

    def create_draft(
        self,
        actor: str,
        *,
        env: str,
        name: str,
        rollout_ratio: int,
        whitelist: Optional[list[str]] = None,
        dependencies: Optional[list[str]] = None,
        default_value: bool = False,
    ) -> SwitchVersion:
        """Create a brand-new DRAFT version. Increments version per switch."""
        payload = validate_switch_payload(
            {
                "env": env,
                "name": name,
                "author": actor,
                "rollout_ratio": rollout_ratio,
                "whitelist": whitelist or [],
                "dependencies": dependencies or [],
                "default_value": default_value,
            }
        )
        with self.repo.transaction() as conn:
            switch = self.repo.get_or_create_switch(
                payload["env"], payload["name"], conn=conn
            )
            validate_dependencies(
                switch.env, payload["dependencies"], self.repo
            )
            ver_no = self.repo.next_version(switch.id, conn=conn)
            version = SwitchVersion(
                switch_id=switch.id,
                env=switch.env,
                name=switch.name,
                version=ver_no,
                author=payload["author"],
                status=VersionStatus.DRAFT,
                rollout_ratio=payload["rollout_ratio"],
                whitelist=payload["whitelist"],
                dependencies=payload["dependencies"],
                default_value=payload["default_value"],
            )
            version = self.repo.insert_version(version, conn=conn)
            self.audit.record(
                actor=actor,
                action=AuditAction.CREATE_DRAFT,
                env=switch.env,
                switch_name=switch.name,
                version=version.version,
                new_status=VersionStatus.DRAFT,
                details={"version": version.version, "payload": payload},
                conn=conn,
            )
        return version

    def edit_draft(
        self,
        actor: str,
        *,
        env: str,
        name: str,
        version: Optional[int] = None,
        **updates: Any,
    ) -> SwitchVersion:
        """Edit a DRAFT in-place. Only DRAFTs may be edited."""
        target = self._resolve_version(env, name, version, [VersionStatus.DRAFT])
        unknown = set(updates) - set(_PAYLOAD_FIELDS)
        if unknown:
            raise ValidationError(f"不支持编辑的字段: {sorted(unknown)}")
        if "rollout_ratio" in updates:
            updates["rollout_ratio"] = validate_ratio(updates["rollout_ratio"])
        merged = target.to_dict()
        merged.update(updates)
        cleaned = validate_switch_payload(
            {
                "env": merged["env"],
                "name": merged["name"],
                "author": merged["author"],
                "rollout_ratio": merged["rollout_ratio"],
                "whitelist": merged.get("whitelist", []),
                "dependencies": merged.get("dependencies", []),
                "default_value": merged["default_value"],
            }
        )
        with self.repo.transaction() as conn:
            validate_dependencies(
                target.env, cleaned["dependencies"], self.repo, conn=conn
            )
            changes: dict[str, Any] = {}
            for f in _PAYLOAD_FIELDS:
                old = getattr(target, f)
                new = cleaned[f]
                if old != new:
                    changes[f] = (old, new)
            self.repo.update_version_fields(target.id, cleaned, conn=conn)
            self.audit.record(
                actor=actor,
                action=AuditAction.EDIT_DRAFT,
                env=target.env,
                switch_name=target.name,
                version=target.version,
                old_status=VersionStatus.DRAFT,
                new_status=VersionStatus.DRAFT,
                details={"changes": changes},
                conn=conn,
            )
        refreshed = self.repo.get_version(target.id)
        assert refreshed is not None
        return refreshed

    # ------------------------------------------------------------------
    # Approval + publishing
    # ------------------------------------------------------------------

    def submit_for_approval(
        self, actor: str, *, env: str, name: str, version: Optional[int] = None
    ) -> SwitchVersion:
        target = self._resolve_version(env, name, version, [VersionStatus.DRAFT])
        _ = actor  # anyone can submit their own draft
        with self.repo.transaction() as conn:
            validate_transition(target.status, VersionStatus.PENDING_APPROVAL)
            validate_dependencies(
                target.env, target.dependencies, self.repo
            )
            self.repo.update_version_fields(
                target.id,
                {"status": VersionStatus.PENDING_APPROVAL, "submitted_at": _now_iso()},
                conn=conn,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.SUBMIT_APPROVAL,
                env=target.env,
                switch_name=target.name,
                version=target.version,
                old_status=VersionStatus.DRAFT,
                new_status=VersionStatus.PENDING_APPROVAL,
                conn=conn,
            )
        refreshed = self.repo.get_version(target.id)
        assert refreshed is not None
        return refreshed

    def approve_and_publish(
        self,
        approver: str,
        *,
        env: str,
        name: str,
        version: Optional[int] = None,
        replace_reason: str = "",
    ) -> SwitchVersion:
        target = self._resolve_version(
            env, name, version, [VersionStatus.PENDING_APPROVAL]
        )
        validate_not_self_approve(target.author, approver)
        validate_transition(target.status, VersionStatus.PUBLISHED)

        prev_published = self.repo.get_effective_version(env, name)
        with self.repo.transaction() as conn:
            # Any previously PUBLISHED sibling is marked ROLLED_BACK
            # *only* if the new one is going to be PUBLISHED.
            if prev_published and prev_published.id != target.id:
                diff = _diff_versions(prev_published, target)
                self.repo.update_version_fields(
                    prev_published.id,
                    {
                        "status": VersionStatus.ROLLED_BACK,
                        "rolled_back_at": _now_iso(),
                        "replace_reason": replace_reason or diff.replace_reason,
                    },
                    conn=conn,
                )
                self.audit.record(
                    actor=approver,
                    action=AuditAction.ROLLBACK,
                    env=prev_published.env,
                    switch_name=prev_published.name,
                    version=prev_published.version,
                    old_status=VersionStatus.PUBLISHED,
                    new_status=VersionStatus.ROLLED_BACK,
                    details={
                        "rolled_back_by": approver,
                        "superseded_by_version": target.version,
                        "replace_reason": replace_reason or diff.replace_reason,
                        "changes": diff.field_changes,
                    },
                    conn=conn,
                )
            self.repo.update_version_fields(
                target.id,
                {
                    "status": VersionStatus.PUBLISHED,
                    "approver": approver,
                    "approved_at": _now_iso(),
                    "published_at": _now_iso(),
                },
                conn=conn,
            )
            self.audit.record(
                actor=approver,
                action=AuditAction.APPROVE_AND_PUBLISH,
                env=target.env,
                switch_name=target.name,
                version=target.version,
                old_status=VersionStatus.PENDING_APPROVAL,
                new_status=VersionStatus.PUBLISHED,
                details={
                    "approver": approver,
                    "supersedes_version": prev_published.version if prev_published else None,
                    "replace_reason": replace_reason,
                },
                conn=conn,
            )
        refreshed = self.repo.get_version(target.id)
        assert refreshed is not None
        return refreshed

    def reject_approval(
        self,
        approver: str,
        *,
        env: str,
        name: str,
        version: Optional[int] = None,
        reason: str = "",
    ) -> SwitchVersion:
        target = self._resolve_version(
            env, name, version, [VersionStatus.PENDING_APPROVAL]
        )
        validate_not_self_approve(target.author, approver)
        validate_transition(target.status, VersionStatus.DRAFT)
        with self.repo.transaction() as conn:
            self.repo.update_version_fields(
                target.id,
                {
                    "status": VersionStatus.DRAFT,
                    "reject_reason": reason,
                    "submitted_at": None,
                },
                conn=conn,
            )
            self.audit.record(
                actor=approver,
                action=AuditAction.REJECT_APPROVAL,
                env=target.env,
                switch_name=target.name,
                version=target.version,
                old_status=VersionStatus.PENDING_APPROVAL,
                new_status=VersionStatus.DRAFT,
                details={"approver": approver, "reject_reason": reason},
                conn=conn,
            )
        refreshed = self.repo.get_version(target.id)
        assert refreshed is not None
        return refreshed

    # ------------------------------------------------------------------
    # Rollback / deprecate
    # ------------------------------------------------------------------

    def rollback(
        self,
        actor: str,
        *,
        env: str,
        name: str,
        reason: str,
        target_version: Optional[int] = None,
    ) -> SwitchVersion:
        """Roll back the currently PUBLISHED version.

        If `target_version` is given, re-publish that historical version
        (a copy is made with a new version number). Otherwise simply mark
        the current one as ROLLED_BACK.
        """
        current = self.repo.get_effective_version(env, name)
        if current is None:
            raise ValidationError(
                f"环境 '{env}' 的开关 '{name}' 没有已发布的版本，无法回滚"
            )
        if not reason:
            raise ValidationError("回滚必须提供原因 (reason)", field="reason")

        with self.repo.transaction() as conn:
            # Mark current as rolled back
            self.repo.update_version_fields(
                current.id,
                {
                    "status": VersionStatus.ROLLED_BACK,
                    "rolled_back_at": _now_iso(),
                    "rollback_reason": reason,
                },
                conn=conn,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.ROLLBACK,
                env=current.env,
                switch_name=current.name,
                version=current.version,
                old_status=VersionStatus.PUBLISHED,
                new_status=VersionStatus.ROLLED_BACK,
                details={
                    "actor": actor,
                    "rollback_reason": reason,
                    "restored_from": target_version,
                },
                conn=conn,
            )
            restored: Optional[SwitchVersion] = None
            if target_version is not None:
                src = self._resolve_version(
                    env,
                    name,
                    target_version,
                    [VersionStatus.PUBLISHED, VersionStatus.ROLLED_BACK],
                )
                new_ver_no = self.repo.next_version(src.switch_id, conn=conn)
                restored = SwitchVersion(
                    switch_id=src.switch_id,
                    env=src.env,
                    name=src.name,
                    author=actor,
                    approver=None,
                    status=VersionStatus.PUBLISHED,
                    version=new_ver_no,
                    rollout_ratio=src.rollout_ratio,
                    whitelist=list(src.whitelist),
                    dependencies=list(src.dependencies),
                    default_value=src.default_value,
                    replace_reason=f"回滚自 V{src.version}，原因: {reason}",
                    published_at=_now_iso(),
                    approved_at=_now_iso(),
                )
                restored = self.repo.insert_version(restored, conn=conn)
                self.audit.record(
                    actor=actor,
                    action=AuditAction.APPROVE_AND_PUBLISH,
                    env=restored.env,
                    switch_name=restored.name,
                    version=restored.version,
                    new_status=VersionStatus.PUBLISHED,
                    details={
                        "restored_from_version": src.version,
                        "rollback_reason": reason,
                    },
                    conn=conn,
                )
        if restored:
            out = self.repo.get_version(restored.id)
        else:
            out = self.repo.get_version(current.id)
        assert out is not None
        return out

    def deprecate(
        self, actor: str, *, env: str, name: str, reason: str
    ) -> list[SwitchVersion]:
        """Mark *all* versions of a switch as DEPRECATED (soft-delete)."""
        versions = self.repo.list_versions(env=env, name=name)
        if not versions:
            raise ValidationError(f"开关 '{env}:{name}' 不存在")
        if not reason:
            raise ValidationError("废弃必须提供原因", field="reason")
        touched: list[SwitchVersion] = []
        with self.repo.transaction() as conn:
            for v in versions:
                if v.status == VersionStatus.DEPRECATED:
                    continue
                self.repo.update_version_fields(
                    v.id,
                    {
                        "status": VersionStatus.DEPRECATED,
                        "deprecated_at": _now_iso(),
                        "reject_reason": reason,
                    },
                    conn=conn,
                )
                self.audit.record(
                    actor=actor,
                    action=AuditAction.DEPRECATE,
                    env=v.env,
                    switch_name=v.name,
                    version=v.version,
                    old_status=v.status,
                    new_status=VersionStatus.DEPRECATED,
                    details={"reason": reason},
                    conn=conn,
                )
                refreshed = self.repo.get_version(v.id)
                if refreshed:
                    touched.append(refreshed)
        return touched

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        env: Optional[str] = None,
        name: Optional[str] = None,
        statuses: Optional[list[VersionStatus]] = None,
        include_deprecated: bool = False,
    ) -> list[SwitchVersion]:
        if not include_deprecated:
            blocked = {VersionStatus.DEPRECATED}
            if statuses is None:
                statuses = [s for s in VersionStatus if s not in blocked]
            else:
                statuses = [s for s in statuses if s not in blocked]
        return self.repo.list_versions(env=env, name=name, statuses=statuses)

    def get_current_and_draft(
        self, env: str, name: str
    ) -> dict[str, Optional[SwitchVersion]]:
        return {
            "effective": self.repo.get_effective_version(env, name),
            "draft": self.repo.get_draft_version(env, name),
            "latest_non_deprecated": self._latest_non_deprecated(env, name),
        }

    def history(self, env: str, name: str) -> list[VersionDiff]:
        versions = sorted(
            self.repo.list_versions(env=env, name=name),
            key=lambda v: v.version,
        )
        diffs: list[VersionDiff] = []
        prev: Optional[SwitchVersion] = None
        for v in versions:
            diffs.append(_diff_versions(prev, v))
            prev = v
        return diffs

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_version(
        self,
        env: str,
        name: str,
        version: Optional[int],
        allowed_statuses: list[VersionStatus],
    ) -> SwitchVersion:
        if version is not None:
            all_versions = self.repo.list_versions(env=env, name=name)
            matches = [v for v in all_versions if v.version == version]
            if not matches:
                raise ValidationError(
                    f"开关 '{env}:{name}' 不存在 V{version}"
                )
            target = matches[0]
            if target.status not in allowed_statuses:
                raise ValidationError(
                    f"操作只允许 {[s.value for s in allowed_statuses]} 状态，"
                    f"当前为 {target.status.value}"
                )
            return target
        candidate = self.repo.get_latest_version(env, name, allowed_statuses)
        if candidate is None:
            raise ValidationError(
                f"开关 '{env}:{name}' 没有处于 {[s.value for s in allowed_statuses]} 的版本"
            )
        return candidate

    def _latest_non_deprecated(
        self, env: str, name: str
    ) -> Optional[SwitchVersion]:
        all_v = self.repo.list_versions(env=env, name=name)
        non_dep = [v for v in all_v if v.status != VersionStatus.DEPRECATED]
        if not non_dep:
            return None
        return max(non_dep, key=lambda v: v.version)


def _diff_versions(
    prev: Optional[SwitchVersion], curr: SwitchVersion
) -> VersionDiff:
    changes: dict[str, tuple[Any, Any]] = {}
    for f in (
        "rollout_ratio",
        "whitelist",
        "dependencies",
        "default_value",
        "author",
    ):
        old = getattr(prev, f) if prev else None
        new = getattr(curr, f)
        if old != new:
            changes[f] = (old, new)
    return VersionDiff(
        prev_version=prev.version if prev else None,
        curr_version=curr.version,
        field_changes=changes,
        replace_reason=(
            (prev.replace_reason if prev else None)
            or curr.replace_reason
            or curr.rollback_reason
        ),
    )
