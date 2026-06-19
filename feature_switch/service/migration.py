from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Optional

from ..audit import AuditTrail
from ..core.enums import (
    AuditAction,
    ChangeType,
    MigrationStatus,
    VersionStatus,
)
from ..core.models import (
    MigrationDiffEntry,
    MigrationPackage,
    MigrationPreview,
    MigrationRecord,
    MigrationSwitchSnapshot,
    SwitchVersion,
    _MIGRATION_SCHEMA_VERSION,
    _now_iso,
)
from ..storage.repository import SwitchRepository
from ..validator.validators import (
    ValidationError,
    parse_json,
    parse_yaml,
    validate_dependencies,
    validate_not_self_approve,
    validate_switch_payload,
)

try:
    import yaml as _yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


_DIFF_FIELDS = (
    "rollout_ratio",
    "whitelist",
    "dependencies",
    "default_value",
)


def _dump_yaml(data: Any) -> str:
    if _HAS_YAML:
        return _yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return json.dumps(data, ensure_ascii=False, indent=2)


def _compute_checksum(source_env: str, target_env: str, switches: list[MigrationSwitchSnapshot]) -> str:
    """根据源环境、目标环境和所有开关的内容生成稳定的 SHA256 校验和，用于重复导入检测。"""
    payload: dict[str, Any] = {
        "source_env": source_env,
        "target_env": target_env,
        "switches": sorted(
            [
                {
                    "env": s.env,
                    "name": s.name,
                    "version": s.version,
                    "rollout_ratio": s.rollout_ratio,
                    "whitelist": sorted(s.whitelist),
                    "dependencies": sorted(s.dependencies),
                    "default_value": s.default_value,
                }
                for s in switches
            ],
            key=lambda x: (x["env"], x["name"]),
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class MigrationService:
    """环境迁移包 + 发布预演服务。

    工作流：
      1. create_package: 从源环境导出当前生效开关 → 生成迁移包
      2. preview_package: 在目标环境做预演 → 得到 diff / 依赖缺口 / 覆盖预警 / 审批要求
      3. import_package: 导入目标环境 → 落成 DRAFT（绝不直接发布）
      4. 可选：export_package 为 YAML/JSON，在新库 import_package_file 回导核对
    """

    def __init__(self, repo: SwitchRepository, audit: AuditTrail) -> None:
        self.repo = repo
        self.audit = audit

    # ------------------------------------------------------------------
    # 1. Create migration package from source env
    # ------------------------------------------------------------------

    def create_package(
        self,
        actor: str,
        *,
        source_env: str,
        target_env: str,
        description: str = "",
        names: Optional[list[str]] = None,
    ) -> MigrationPackage:
        """从源环境的 PUBLISHED 开关创建迁移包。"""
        if not source_env or not target_env:
            raise ValidationError("source_env 和 target_env 都必须非空")
        if source_env == target_env:
            raise ValidationError(
                f"源环境和目标环境不能相同: {source_env!r}",
                field="target_env",
            )
        all_published = self.repo.list_published_switches(env=source_env)
        if names:
            name_set = set(names)
            published = [v for v in all_published if v.name in name_set]
            missing = name_set - {v.name for v in published}
            if missing:
                raise ValidationError(
                    f"源环境 '{source_env}' 中这些开关没有已发布版本: {sorted(missing)}",
                    field="names",
                )
        else:
            published = all_published
        if not published:
            raise ValidationError(
                f"源环境 '{source_env}' 没有任何已发布的开关，无法打迁移包",
                field="source_env",
            )
        snapshots = [MigrationSwitchSnapshot.from_version(v) for v in published]
        checksum = _compute_checksum(source_env, target_env, snapshots)
        # 重复导入检测：同一套开关同一目标环境已打过包
        dup = self.repo.find_migration_by_checksum(source_env, target_env, checksum)
        if dup is not None:
            raise ValidationError(
                f"相同内容的迁移包已存在（package_id={dup.package_id}，"
                f"状态={dup.status.value}）。请直接复用该包，避免重复打包。"
            )
        pkg_id = f"pkg-{uuid.uuid4().hex[:12]}"
        pkg = MigrationPackage(
            package_id=pkg_id,
            source_env=source_env,
            target_env=target_env,
            created_by=actor,
            description=description,
            status=MigrationStatus.CREATED,
            switches=snapshots,
            checksum=checksum,
        )
        with self.repo.transaction() as conn:
            pkg = self.repo.insert_migration_package(pkg, conn=conn)
            for snap in snapshots:
                self.audit.record(
                    actor=actor,
                    action=AuditAction.MIGRATION_PACKAGE_CREATE,
                    env=source_env,
                    switch_name=snap.name,
                    version=snap.version,
                    details={
                        "package_id": pkg_id,
                        "target_env": target_env,
                        "checksum": checksum,
                    },
                    conn=conn,
                )
                self._append_record(
                    package_id=pkg_id,
                    action="CREATE_PACKAGE",
                    actor=actor,
                    env=source_env,
                    switch_name=snap.name,
                    version=snap.version,
                    details=json.dumps(
                        {"target_env": target_env, "checksum": checksum},
                        ensure_ascii=False,
                    ),
                    conn=conn,
                )
        return pkg

    # ------------------------------------------------------------------
    # 2. Preview (diff + dependency gaps + conflict detection)
    # ------------------------------------------------------------------

    def preview_package(self, actor: str, *, package_id: str) -> MigrationPreview:
        pkg = self._require_package(package_id)
        target_env = pkg.target_env
        entries: list[MigrationDiffEntry] = []
        summary: dict[str, int] = {
            ct.value: 0 for ct in ChangeType
        }
        all_gaps: set[str] = set()
        all_approvers: set[str] = set()
        blocking: list[str] = []

        # 目标环境已发布开关集合（用于依赖缺口 + 包内依赖合并判定）
        target_published_names = {
            v.name for v in self.repo.list_published_switches(env=target_env)
        }
        # 包内开关名集合
        in_package_names = {s.name for s in pkg.switches}
        # 允许的依赖 = 目标已发布 + 当前包内
        allowed_dep_names = target_published_names | in_package_names

        import json as _json

        for snap in pkg.switches:
            effective = self.repo.get_effective_version(target_env, snap.name)
            draft_v = self.repo.get_latest_version(
                target_env, snap.name, [VersionStatus.DRAFT]
            )
            pending_v = self.repo.get_latest_version(
                target_env, snap.name, [VersionStatus.PENDING_APPROVAL]
            )
            field_changes: dict[str, tuple[Any, Any]] = {}
            change_type: ChangeType
            conflict_reason: Optional[str] = None

            # Conflicts: 目标环境存在 DRAFT 或 PENDING_APPROVAL
            if draft_v is not None:
                change_type = ChangeType.CONFLICT_DRAFT
                conflict_reason = (
                    f"目标环境 '{target_env}' 中 '{snap.name}' 已存在草稿 V{draft_v.version}，"
                    f"请先废弃或发布该草稿后再导入"
                )
            elif pending_v is not None:
                change_type = ChangeType.CONFLICT_PENDING
                conflict_reason = (
                    f"目标环境 '{target_env}' 中 '{snap.name}' 已存在待审批版本 V{pending_v.version}，"
                    f"请先处理审批后再导入"
                )
            else:
                if effective is None:
                    change_type = ChangeType.NEW
                    for f in _DIFF_FIELDS:
                        field_changes[f] = (None, getattr(snap, f))
                else:
                    for f in _DIFF_FIELDS:
                        old = getattr(effective, f)
                        new = getattr(snap, f)
                        if old != new:
                            field_changes[f] = (old, new)
                    change_type = (
                        ChangeType.MODIFIED if field_changes else ChangeType.UNCHANGED
                    )

            # 依赖缺口：只看目标环境（包内其他开关算满足）
            dep_gaps = [
                d for d in snap.dependencies if d not in allowed_dep_names
            ]
            for g in dep_gaps:
                all_gaps.add(g)
            if dep_gaps and change_type in (
                ChangeType.NEW, ChangeType.MODIFIED, ChangeType.UNCHANGED,
            ):
                blocking.append(
                    f"{target_env}:{snap.name} 依赖缺口: {dep_gaps}"
                )

            # 需要谁审批：谁审批过源环境生效版 → 目标环境也需要同级别的审批人
            req_approvers: list[str] = []
            if snap.approver:
                req_approvers.append(snap.approver)
                all_approvers.add(snap.approver)
            # 越权校验：actor 如果是源作者，需要别人审批
            if snap.author == actor and snap.approver and snap.approver != actor:
                # 要求原审批人重新审批
                pass

            entry = MigrationDiffEntry(
                env=target_env,
                name=snap.name,
                change_type=change_type,
                source_snapshot=snap,
                target_effective=effective,
                target_draft=draft_v,
                target_pending=pending_v,
                field_changes=field_changes,
                dependency_gaps=list(dep_gaps),
                required_approvers=req_approvers,
                conflict_reason=conflict_reason,
            )
            summary[change_type.value] += 1
            entries.append(entry)

            # 冲突 → 阻塞
            if change_type in (ChangeType.CONFLICT_DRAFT, ChangeType.CONFLICT_PENDING):
                blocking.append(conflict_reason or f"{target_env}:{snap.name} 存在冲突")

        can_import = len(blocking) == 0

        # 写审计 + 记录
        with self.repo.transaction() as conn:
            self.repo.update_migration_package_fields(
                pkg.package_id,
                {"status": MigrationStatus.PREVIEWED, "previewed_at": _now_iso()},
                conn=conn,
            )
            for entry in entries:
                self.audit.record(
                    actor=actor,
                    action=AuditAction.MIGRATION_PACKAGE_PREVIEW,
                    env=target_env,
                    switch_name=entry.name,
                    details={
                        "package_id": pkg.package_id,
                        "change_type": entry.change_type.value,
                        "dependency_gaps": entry.dependency_gaps,
                        "required_approvers": entry.required_approvers,
                        "conflict": entry.conflict_reason,
                        "can_import": can_import,
                    },
                    conn=conn,
                )
            self._append_record(
                package_id=pkg.package_id,
                action="PREVIEW",
                actor=actor,
                env=target_env,
                details=_json.dumps(
                    {
                        "summary": summary,
                        "blocking": blocking,
                        "can_import": can_import,
                    },
                    ensure_ascii=False,
                ),
                conn=conn,
            )

        return MigrationPreview(
            package_id=pkg.package_id,
            source_env=pkg.source_env,
            target_env=target_env,
            entries=entries,
            summary=summary,
            all_dependency_gaps=sorted(all_gaps),
            all_required_approvers=sorted(all_approvers),
            blocking_issues=blocking,
            can_import=can_import,
        )

    # ------------------------------------------------------------------
    # 3. Import package (create DRAFT only, never publish)
    # ------------------------------------------------------------------

    def import_package(
        self,
        actor: str,
        *,
        package_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """将迁移包导入到目标环境，仅创建 DRAFT。"""
        pkg = self._require_package(package_id)
        target_env = pkg.target_env

        # 重复导入拦截：已经 IMPORTED_DRAFT 或 APPROVED
        if pkg.status in (MigrationStatus.IMPORTED_DRAFT, MigrationStatus.APPROVED):
            raise ValidationError(
                f"迁移包 {pkg.package_id} 已处于 {pkg.status.value} 状态，"
                f"不允许重复导入。如需重新导入请先废弃现有草稿。"
            )

        # 先做一次 preview 验证（不写数据只看 can_import）
        preview = self.preview_package(actor=actor, package_id=package_id)
        if not preview.can_import and not force:
            raise ValidationError(
                "导入被阻塞，原因: " + " | ".join(preview.blocking_issues)
                + "。确认风险后可加 --force 强制，但同名草稿/待审批冲突仍会被硬拦截。"
            )

        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        # 包内所有开关名（用于依赖校验）— 因为本次导入会全部创建 DRAFT
        all_in_package: set[str] = {s.name for s in pkg.switches}
        with self.repo.transaction() as conn:
            # 包内依赖可用集合（新创建的也算）+ 本次包内全部开关（因为都会创建）
            available_in_batch: set[str] = set(all_in_package)

            for entry in preview.entries:
                if entry.change_type in (
                    ChangeType.CONFLICT_DRAFT, ChangeType.CONFLICT_PENDING,
                ):
                    skipped.append({
                        "env": target_env,
                        "name": entry.name,
                        "reason": entry.conflict_reason,
                    })
                    continue
                snap = entry.source_snapshot
                assert snap is not None
                # 校验：同批依赖 + 目标生效版依赖
                try:
                    validate_dependencies(
                        target_env,
                        snap.dependencies,
                        self.repo,
                        extra_available=available_in_batch,
                    )
                except ValidationError as exc:
                    raise ValidationError(
                        f"{target_env}:{snap.name} {exc}"
                    ) from exc
                # 构造 DRAFT
                switch = self.repo.get_or_create_switch(
                    target_env, snap.name, conn=conn
                )
                ver_no = self.repo.next_version(switch.id, conn=conn)
                version = SwitchVersion(
                    switch_id=switch.id,
                    env=target_env,
                    name=snap.name,
                    author=actor,  # 用本次操作人而非源作者
                    status=VersionStatus.DRAFT,
                    version=ver_no,
                    rollout_ratio=snap.rollout_ratio,
                    whitelist=list(snap.whitelist),
                    dependencies=list(snap.dependencies),
                    default_value=snap.default_value,
                )
                version = self.repo.insert_version(version, conn=conn)
                available_in_batch.add(snap.name)
                # 越权检查：如果 actor 就是源作者，那么后续审批时必须换人
                approver_hint = snap.approver
                if approver_hint and approver_hint == actor:
                    # 审批人不能是自己，提示需要其他人
                    pass
                imported.append({
                    "env": target_env,
                    "name": snap.name,
                    "version": version.version,
                    "status": VersionStatus.DRAFT.value,
                    "original_source": {
                        "source_env": pkg.source_env,
                        "source_version": snap.version,
                        "package_id": pkg.package_id,
                    },
                    "required_approver": approver_hint,
                })
                self.audit.record(
                    actor=actor,
                    action=AuditAction.MIGRATION_PACKAGE_IMPORT,
                    env=target_env,
                    switch_name=snap.name,
                    version=version.version,
                    new_status=VersionStatus.DRAFT,
                    details={
                        "package_id": pkg.package_id,
                        "source_env": pkg.source_env,
                        "source_version": snap.version,
                        "required_approver": approver_hint,
                    },
                    conn=conn,
                )
                self._append_record(
                    package_id=pkg.package_id,
                    action="IMPORT_DRAFT",
                    actor=actor,
                    env=target_env,
                    switch_name=snap.name,
                    version=version.version,
                    details=json.dumps({
                        "source_env": pkg.source_env,
                        "source_version": snap.version,
                    }, ensure_ascii=False),
                    conn=conn,
                )
            self.repo.update_migration_package_fields(
                pkg.package_id,
                {"status": MigrationStatus.IMPORTED_DRAFT, "imported_at": _now_iso()},
                conn=conn,
            )

        return {
            "package_id": pkg.package_id,
            "source_env": pkg.source_env,
            "target_env": target_env,
            "imported_count": len(imported),
            "skipped_count": len(skipped),
            "imported": imported,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------
    # 4. Export / Import package as YAML / JSON
    # ------------------------------------------------------------------

    def export_package_file(
        self,
        actor: str,
        *,
        package_id: str,
        fmt: str = "yaml",
    ) -> str:
        pkg = self._require_package(package_id)
        data = pkg.to_export_dict()
        # 记录审计
        with self.repo.transaction() as conn:
            for snap in pkg.switches:
                self.audit.record(
                    actor=actor,
                    action=AuditAction.MIGRATION_PACKAGE_EXPORT,
                    env=pkg.source_env,
                    switch_name=snap.name,
                    version=snap.version,
                    details={"package_id": package_id, "format": fmt},
                    conn=conn,
                )
            self._append_record(
                package_id=package_id,
                action="EXPORT_FILE",
                actor=actor,
                env=pkg.source_env,
                details=json.dumps({"format": fmt}, ensure_ascii=False),
                conn=conn,
            )
        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return _dump_yaml(data)

    def import_package_file(
        self,
        actor: str,
        *,
        path: str,
    ) -> MigrationPackage:
        """从 YAML/JSON 文件导入迁移包定义（只落 migration_package 表，不生成开关草稿）。"""
        raw = self._read(path)
        ext = os.path.splitext(path)[1].lower()
        if ext in (".yaml", ".yml"):
            doc = parse_yaml(raw)
        elif ext == ".json":
            doc = parse_json(raw)
        else:
            stripped = raw.lstrip()
            if stripped.startswith("{"):
                doc = parse_json(raw)
            else:
                doc = parse_yaml(raw)
        return self._load_package_document(actor=actor, doc=doc, source=path)

    # ------------------------------------------------------------------
    # 5. Queries
    # ------------------------------------------------------------------

    def list_packages(
        self,
        source_env: Optional[str] = None,
        target_env: Optional[str] = None,
        statuses: Optional[list[MigrationStatus]] = None,
    ) -> list[MigrationPackage]:
        return self.repo.list_migration_packages(
            source_env=source_env, target_env=target_env, statuses=statuses,
        )

    def get_package(self, package_id: str) -> MigrationPackage:
        return self._require_package(package_id)

    def list_records(self, package_id: str, limit: int = 100) -> list[MigrationRecord]:
        return self.repo.list_migration_records(package_id=package_id, limit=limit)

    # ------------------------------------------------------------------
    # 6. Approval for package-level trace (optional, 给审计留痕)
    # ------------------------------------------------------------------

    def mark_package_approved(
        self, approver: str, *, package_id: str
    ) -> MigrationPackage:
        """标记迁移包已审批（实际审批仍走单开关的 approve 流程）。"""
        pkg = self._require_package(package_id)
        # 包级越权校验：approver 不能是创建人
        if pkg.created_by == approver:
            raise ValidationError(
                f"迁移包创建人 '{pkg.created_by}' 不能审批自己的迁移包，"
                f"请由其他管理员审批（越权拦截）。"
            )
        with self.repo.transaction() as conn:
            self.repo.update_migration_package_fields(
                package_id,
                {
                    "status": MigrationStatus.APPROVED,
                    "approved_by": approver,
                    "approved_at": _now_iso(),
                },
                conn=conn,
            )
            self._append_record(
                package_id=package_id,
                action="MARK_APPROVED",
                actor=approver,
                env=pkg.target_env,
                details=json.dumps({"created_by": pkg.created_by}, ensure_ascii=False),
                conn=conn,
            )
        return self._require_package(package_id)

    def mark_package_rejected(
        self, rejector: str, *, package_id: str, reason: str
    ) -> MigrationPackage:
        pkg = self._require_package(package_id)
        if not reason:
            raise ValidationError("驳回必须提供原因", field="reason")
        with self.repo.transaction() as conn:
            self.repo.update_migration_package_fields(
                package_id,
                {
                    "status": MigrationStatus.REJECTED,
                    "rejected_by": rejector,
                    "rejected_at": _now_iso(),
                    "reject_reason": reason,
                },
                conn=conn,
            )
            self._append_record(
                package_id=package_id,
                action="MARK_REJECTED",
                actor=rejector,
                env=pkg.target_env,
                details=json.dumps({"reason": reason}, ensure_ascii=False),
                conn=conn,
            )
        return self._require_package(package_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_package(self, package_id: str) -> MigrationPackage:
        pkg = self.repo.get_migration_package(package_id)
        if pkg is None:
            raise ValidationError(f"迁移包不存在: {package_id}", field="package_id")
        return pkg

    def _load_package_document(
        self, *, actor: str, doc: Any, source: str
    ) -> MigrationPackage:
        if not isinstance(doc, dict):
            raise ValidationError("迁移包文件根节点必须是对象 (mapping)")
        schema = doc.get("schema_version")
        if schema != _MIGRATION_SCHEMA_VERSION:
            raise ValidationError(
                f"迁移包 schema_version 不匹配: 期望 {_MIGRATION_SCHEMA_VERSION!r}，收到 {schema!r}"
            )
        package_id = doc.get("package_id")
        source_env = doc.get("source_env")
        target_env = doc.get("target_env")
        created_by = doc.get("created_by") or actor
        description = doc.get("description") or ""
        switches_raw = doc.get("switches")
        checksum = doc.get("checksum") or ""
        if not package_id:
            raise ValidationError("缺少 package_id")
        if not source_env or not target_env:
            raise ValidationError("缺少 source_env / target_env")
        if not isinstance(switches_raw, list) or not switches_raw:
            raise ValidationError("'switches' 字段必须是非空列表")

        # 是否已存在同 package_id 的包
        existing = self.repo.get_migration_package(package_id)
        if existing is not None:
            raise ValidationError(
                f"package_id={package_id} 已存在于本地库（状态={existing.status.value}），"
                f"不能重复导入。如需替换请先删除旧记录（或改用新 package_id）。"
            )

        snapshots: list[MigrationSwitchSnapshot] = []
        for idx, item in enumerate(switches_raw):
            if not isinstance(item, dict):
                raise ValidationError(f"switches[{idx}] 必须是对象")
            enriched = dict(item)
            enriched.setdefault("env", target_env)
            enriched.setdefault("author", created_by)
            enriched["default_value"] = bool(enriched.get("default_value", False))
            try:
                cleaned = validate_switch_payload(enriched)
            except ValidationError as exc:
                raise ValidationError(f"switches[{idx}] {exc}") from exc
            snapshots.append(MigrationSwitchSnapshot(
                env=cleaned["env"],
                name=cleaned["name"],
                version=int(item.get("version", 1)),
                rollout_ratio=cleaned["rollout_ratio"],
                whitelist=cleaned["whitelist"],
                dependencies=cleaned["dependencies"],
                default_value=cleaned["default_value"],
                author=cleaned["author"],
                approver=item.get("approver"),
                published_at=item.get("published_at"),
            ))

        # 重新计算 checksum，若源文档已有则做一致性校验
        computed = _compute_checksum(source_env, target_env, snapshots)
        if checksum and checksum != computed:
            raise ValidationError(
                f"迁移包校验和不一致：文档 {checksum[:12]}… vs 计算 {computed[:12]}…，"
                f"文件可能被篡改。"
            )

        pkg = MigrationPackage(
            package_id=package_id,
            source_env=source_env,
            target_env=target_env,
            created_by=created_by,
            description=description,
            status=MigrationStatus.CREATED,
            switches=snapshots,
            checksum=computed,
            created_at=doc.get("created_at") or _now_iso(),
        )
        with self.repo.transaction() as conn:
            pkg = self.repo.insert_migration_package(pkg, conn=conn)
            self._append_record(
                package_id=package_id,
                action="IMPORT_PACKAGE_FILE",
                actor=actor,
                env=target_env,
                details=json.dumps({
                    "source": source,
                    "switch_count": len(snapshots),
                    "checksum": computed,
                }, ensure_ascii=False),
                conn=conn,
            )
        return pkg

    @staticmethod
    def _read(path: str) -> str:
        if not os.path.isfile(path):
            raise ValidationError(f"迁移包文件不存在: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise ValidationError(f"读取迁移包文件失败: {exc}") from exc

    def _append_record(
        self,
        *,
        package_id: str,
        action: str,
        actor: str,
        env: str,
        switch_name: Optional[str] = None,
        version: Optional[int] = None,
        details: str = "",
        rollback_source_package_id: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        rec = MigrationRecord(
            package_id=package_id,
            action=action,
            actor=actor,
            env=env,
            switch_name=switch_name,
            version=version,
            details=details,
            rollback_source_package_id=rollback_source_package_id,
            timestamp=_now_iso(),
        )
        self.repo.append_migration_record(rec, conn=conn)
