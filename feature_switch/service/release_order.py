from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Optional

from ..audit import AuditTrail
from ..core.enums import (
    AuditAction,
    ReleaseOrderStatus,
    VersionStatus,
)
from ..core.models import (
    ReleaseOrder,
    ReleaseOrderItem,
    ReleaseOrderPreview,
    ReleaseOrderPreviewItem,
    ReleaseOrderRecord,
    SwitchVersion,
    _RELEASE_SCHEMA_VERSION,
    _now_iso,
)
from ..storage.repository import SwitchRepository
from ..validator.validators import (
    ValidationError,
    parse_json,
    parse_yaml,
    validate_dependencies,
    validate_not_self_approve,
    validate_release_admin_role,
    validate_release_items_not_empty,
    validate_release_no_duplicate_items,
    validate_release_not_self_approve,
    validate_release_payload,
    validate_release_transition,
    validate_release_version_status,
    validate_transition,
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


def _compute_release_checksum(env: str, items: list[ReleaseOrderItem]) -> str:
    payload: dict[str, Any] = {
        "env": env,
        "items": sorted(
            [
                {
                    "env": i.env,
                    "name": i.name,
                    "version": i.version,
                    "rollout_ratio": i.rollout_ratio,
                    "whitelist": sorted(i.whitelist),
                    "dependencies": sorted(i.dependencies),
                    "default_value": i.default_value,
                }
                for i in items
            ],
            key=lambda x: (x["env"], x["name"]),
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _topological_sort(items: list[ReleaseOrderItem]) -> list[str]:
    name_to_item = {i.name: i for i in items}
    in_degree: dict[str, int] = {i.name: 0 for i in items}
    adj: dict[str, list[str]] = {i.name: [] for i in items}

    item_names = set(name_to_item.keys())
    for item in items:
        for dep in item.dependencies:
            if dep in item_names:
                adj[dep].append(item.name)
                in_degree[item.name] += 1

    queue: list[str] = [name for name, deg in in_degree.items() if deg == 0]
    result: list[str] = []
    while queue:
        name = queue.pop(0)
        result.append(name)
        for neighbor in adj[name]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(items):
        cycle = [name for name, deg in in_degree.items() if deg > 0]
        raise ValidationError(f"发布单明细存在循环依赖: {cycle}")

    return result


class ReleaseOrderService:
    """发布计划单服务。

    工作流：
      1. create_order: 从 DRAFT / PENDING_APPROVAL 中挑选版本组单
      2. preview_order: 预演：依赖顺序、冲突项、会覆盖的生效版、执行后每条开关的状态
      3. submit_for_approval: 提交审批
      4. approve_order / reject_order: 审批（提单人不能自审）
      5. execute_order: 原子化执行，全部成功或全部回滚
      6. rollback_order: 整单回滚，恢复到执行前状态
      7. cancel_order: 撤销未执行的发布单
      8. copy_order: 复制发布单（生成新ID，状态重置为 CREATED）
      9. export_order / import_order: YAML/JSON 导入导出
    """

    def __init__(
        self,
        repo: SwitchRepository,
        audit: AuditTrail,
        *,
        admin_emails: Optional[set[str]] = None,
    ) -> None:
        self.repo = repo
        self.audit = audit
        self.admin_emails = admin_emails or set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_admin(self, actor: str) -> bool:
        return actor in self.admin_emails

    def _require_order(self, order_id: str) -> ReleaseOrder:
        order = self.repo.get_release_order(order_id)
        if order is None:
            raise ValidationError(f"发布单不存在: {order_id}", field="order_id")
        return order

    def _require_version(
        self, env: str, name: str, version: int
    ) -> SwitchVersion:
        all_versions = self.repo.list_versions(env=env, name=name)
        matches = [v for v in all_versions if v.version == version]
        if not matches:
            raise ValidationError(f"开关 '{env}:{name}' 不存在 V{version}")
        return matches[0]

    def _append_record(
        self,
        *,
        order_id: str,
        action: str,
        actor: str,
        env: str,
        switch_name: Optional[str] = None,
        version: Optional[int] = None,
        details: str = "",
        rollback_source_order_id: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        rec = ReleaseOrderRecord(
            order_id=order_id,
            action=action,
            actor=actor,
            env=env,
            switch_name=switch_name,
            version=version,
            details=details,
            rollback_source_order_id=rollback_source_order_id,
            timestamp=_now_iso(),
        )
        self.repo.append_release_order_record(rec, conn=conn)

    def _collect_rollback_snapshot(
        self, env: str, name: str, conn: Any = None
    ) -> dict[str, Any]:
        effective = self.repo.get_effective_version(env, name, conn=conn)
        if effective is None:
            return {"type": "no_effective", "env": env, "name": name}
        return {
            "type": "has_effective",
            "env": env,
            "name": name,
            "version": effective.version,
            "status": effective.status.value,
            "rollout_ratio": effective.rollout_ratio,
            "whitelist": list(effective.whitelist),
            "dependencies": list(effective.dependencies),
            "default_value": effective.default_value,
            "author": effective.author,
            "approver": effective.approver,
            "published_at": effective.published_at,
            "replace_reason": effective.replace_reason,
        }

    # ------------------------------------------------------------------
    # 1. Create release order (组单)
    # ------------------------------------------------------------------

    def create_order(
        self,
        actor: str,
        *,
        env: str,
        items: list[dict[str, Any]],
        title: str = "",
        description: str = "",
        skip_duplicate_check: bool = False,
        order_id: Optional[str] = None,
    ) -> ReleaseOrder:
        """从 DRAFT 或 PENDING_APPROVAL 版本中挑选，组建成发布单。

        仅管理员可以组单。
        """
        validate_release_admin_role(actor, self._is_admin(actor))

        payload = validate_release_payload({
            "env": env,
            "items": items,
            "title": title,
            "description": description,
        })

        env = payload["env"]
        allowed_statuses = [VersionStatus.DRAFT, VersionStatus.PENDING_APPROVAL]

        order_items: list[ReleaseOrderItem] = []
        for item_data in payload["items"]:
            name = item_data["name"]
            version = item_data["version"]
            v = self._require_version(env, name, version)
            validate_release_version_status(v, allowed_statuses)
            order_item = ReleaseOrderItem.from_version(v)
            order_items.append(order_item)

        validate_release_items_not_empty(order_items)
        validate_release_no_duplicate_items([i.to_dict() for i in order_items])

        checksum = _compute_release_checksum(env, order_items)

        if not skip_duplicate_check:
            dup = self.repo.find_release_order_by_checksum(env, checksum)
            if dup is not None and dup.status not in (
                ReleaseOrderStatus.CANCELLED,
                ReleaseOrderStatus.REJECTED,
            ):
                raise ValidationError(
                    f"相同内容的发布单已存在（order_id={dup.order_id}，"
                    f"状态={dup.status.value}）。请直接复用该单，避免重复组单。"
                )

        if order_id:
            existing = self.repo.get_release_order(order_id)
            if existing is not None:
                raise ValidationError(
                    f"发布单 order_id={order_id} 已存在，无法重复创建"
                )
        else:
            order_id = f"rel-{uuid.uuid4().hex[:12]}"

        order = ReleaseOrder(
            order_id=order_id,
            env=env,
            created_by=actor,
            title=payload["title"],
            description=payload["description"],
            status=ReleaseOrderStatus.CREATED,
            items=order_items,
            checksum=checksum,
        )

        with self.repo.transaction() as conn:
            order = self.repo.insert_release_order(order, conn=conn)
            for item in order_items:
                self.audit.record(
                    actor=actor,
                    action=AuditAction.RELEASE_ORDER_CREATE,
                    env=env,
                    switch_name=item.name,
                    version=item.version,
                    details={
                        "order_id": order_id,
                        "env": env,
                        "checksum": checksum,
                    },
                    conn=conn,
                )
                self._append_record(
                    order_id=order_id,
                    action="ADD_ITEM",
                    actor=actor,
                    env=env,
                    switch_name=item.name,
                    version=item.version,
                    details=json.dumps({
                        "status_before": item.status_before.value if item.status_before else None,
                        "checksum": checksum,
                    }, ensure_ascii=False),
                    conn=conn,
                )

        return order

    # ------------------------------------------------------------------
    # 2. Preview (预演)
    # ------------------------------------------------------------------

    def preview_order(
        self, actor: str, *, order_id: str
    ) -> ReleaseOrderPreview:
        """预演发布单，展示依赖顺序、冲突项、会覆盖的生效版、执行后状态。

        普通开发也可以看预演。
        """
        order = self._require_order(order_id)
        env = order.env
        items = order.items

        all_versions = self.repo.list_versions(env=env)
        version_map: dict[tuple[str, int], SwitchVersion] = {
            (v.name, v.version): v for v in all_versions
        }

        effective_map: dict[str, Optional[SwitchVersion]] = {}
        for item in items:
            effective_map[item.name] = self.repo.get_effective_version(env, item.name)

        other_drafts: set[tuple[str, int]] = set()
        other_pending: set[tuple[str, int]] = set()
        for v in all_versions:
            key = (v.name, v.version)
            in_this_order = any(
                i.name == v.name and i.version == v.version for i in items
            )
            if not in_this_order:
                if v.status == VersionStatus.DRAFT:
                    other_drafts.add(key)
                elif v.status == VersionStatus.PENDING_APPROVAL:
                    other_pending.add(key)

        active_order_versions: set[tuple[str, int]] = set()
        active_orders = self.repo.list_release_orders(
            env=env,
            statuses=[
                ReleaseOrderStatus.CREATED,
                ReleaseOrderStatus.PREVIEWED,
                ReleaseOrderStatus.PENDING_APPROVAL,
                ReleaseOrderStatus.APPROVED,
            ],
        )
        for other in active_orders:
            if other.order_id == order_id:
                continue
            for oi in other.items:
                active_order_versions.add((oi.name, oi.version))

        dep_order = _topological_sort(items)
        dep_order_index = {name: idx for idx, name in enumerate(dep_order)}

        in_order_names = {i.name for i in items}
        published_names = {
            v.name for v in self.repo.list_published_switches(env=env)
        }
        allowed_dep_names = published_names | in_order_names

        preview_items: list[ReleaseOrderPreviewItem] = []
        summary: dict[str, int] = {
            "DRAFT": 0,
            "PENDING_APPROVAL": 0,
            "WILL_OVERRIDE": 0,
            "CONFLICT": 0,
            "DEP_GAP": 0,
        }
        all_gaps: set[str] = set()
        blocking: list[str] = []
        warnings: list[str] = []

        for item in items:
            v = version_map.get((item.name, item.version))
            if v is None:
                blocking.append(f"{env}:{item.name} V{item.version} 不存在")
                continue

            current_status = v.status
            if current_status == VersionStatus.DRAFT:
                summary["DRAFT"] += 1
            elif current_status == VersionStatus.PENDING_APPROVAL:
                summary["PENDING_APPROVAL"] += 1

            target_status = VersionStatus.PUBLISHED

            prev_effective = effective_map.get(item.name)
            will_override = prev_effective is not None and prev_effective.id != v.id
            if will_override:
                summary["WILL_OVERRIDE"] += 1

            field_changes: dict[str, tuple[Any, Any]] = {}
            if prev_effective:
                for f in _DIFF_FIELDS:
                    old = getattr(prev_effective, f)
                    new = getattr(v, f)
                    if old != new:
                        field_changes[f] = (old, new)

            dep_gaps = [d for d in v.dependencies if d not in allowed_dep_names]
            for g in dep_gaps:
                all_gaps.add(g)

            conflict_reason: Optional[str] = None
            item_warnings: list[str] = []

            if (v.name, v.version) in active_order_versions:
                summary["CONFLICT"] += 1
                conflict_reason = (
                    f"该版本已被其他未执行的发布单引用，存在并发冲突"
                )
                blocking.append(
                    f"{env}:{item.name} V{v.version} 已被其他发布单引用"
                )

            if (v.name, v.version) in other_drafts:
                item_warnings.append(
                    f"该开关存在其他草稿版本未处理"
                )
            if (v.name, v.version) in other_pending:
                item_warnings.append(
                    f"该开关存在其他待审批版本未处理"
                )

            if dep_gaps:
                summary["DEP_GAP"] += 1
                blocking.append(
                    f"{env}:{item.name} 依赖缺口: {dep_gaps}"
                )

            try:
                validate_transition(current_status, target_status)
            except ValidationError as exc:
                blocking.append(f"{env}:{item.name} {exc.message}")

            preview_item = ReleaseOrderPreviewItem(
                env=env,
                name=item.name,
                version=v.version,
                current_status=current_status,
                target_status=target_status,
                prev_effective_version=prev_effective.version if prev_effective else None,
                prev_effective_snapshot=(
                    prev_effective.effective_snapshot() if prev_effective else None
                ),
                will_override_effective=will_override,
                dependency_order=dep_order_index.get(item.name, 0),
                field_changes=field_changes,
                dependency_gaps=dep_gaps,
                conflict_reason=conflict_reason,
                warnings=item_warnings,
            )
            preview_items.append(preview_item)
            warnings.extend(
                f"{env}:{item.name} V{v.version}: {w}" for w in item_warnings
            )

        can_approve = len(blocking) == 0
        can_execute = can_approve

        ordered_preview_items = sorted(
            preview_items, key=lambda x: x.dependency_order
        )

        with self.repo.transaction() as conn:
            self.repo.update_release_order_fields(
                order_id,
                {"status": ReleaseOrderStatus.PREVIEWED, "previewed_at": _now_iso()},
                conn=conn,
            )
            for pi in ordered_preview_items:
                self.audit.record(
                    actor=actor,
                    action=AuditAction.RELEASE_ORDER_PREVIEW,
                    env=env,
                    switch_name=pi.name,
                    version=pi.version,
                    details={
                        "order_id": order_id,
                        "current_status": pi.current_status.value,
                        "target_status": pi.target_status.value,
                        "will_override": pi.will_override_effective,
                        "dependency_order": pi.dependency_order,
                        "conflict": pi.conflict_reason,
                        "dep_gaps": pi.dependency_gaps,
                        "can_approve": can_approve,
                    },
                    conn=conn,
                )
            self._append_record(
                order_id=order_id,
                action="PREVIEW",
                actor=actor,
                env=env,
                details=json.dumps({
                    "summary": summary,
                    "blocking": blocking,
                    "warnings": warnings,
                    "can_approve": can_approve,
                    "dependency_order": dep_order,
                }, ensure_ascii=False),
                conn=conn,
            )

        return ReleaseOrderPreview(
            order_id=order_id,
            env=env,
            items=ordered_preview_items,
            summary=summary,
            dependency_order=dep_order,
            all_dependency_gaps=sorted(all_gaps),
            blocking_issues=blocking,
            warnings=warnings,
            can_approve=can_approve,
            can_execute=can_execute,
        )

    # ------------------------------------------------------------------
    # 3. Submit for approval (提交审批)
    # ------------------------------------------------------------------

    def submit_for_approval(
        self, actor: str, *, order_id: str
    ) -> ReleaseOrder:
        """提交发布单审批。管理员操作。"""
        order = self._require_order(order_id)
        validate_release_admin_role(actor, self._is_admin(actor))
        validate_release_transition(order.status, ReleaseOrderStatus.PENDING_APPROVAL)

        preview = self.preview_order(actor=actor, order_id=order_id)
        if not preview.can_approve:
            raise ValidationError(
                "发布单存在阻塞问题，无法提交审批："
                + " | ".join(preview.blocking_issues)
            )

        with self.repo.transaction() as conn:
            self.repo.update_release_order_fields(
                order_id,
                {
                    "status": ReleaseOrderStatus.PENDING_APPROVAL,
                    "submitted_at": _now_iso(),
                },
                conn=conn,
            )
            for item in order.items:
                self.audit.record(
                    actor=actor,
                    action=AuditAction.RELEASE_ORDER_SUBMIT,
                    env=order.env,
                    switch_name=item.name,
                    version=item.version,
                    old_status=item.status_before.value if item.status_before else None,
                    new_status=VersionStatus.PENDING_APPROVAL.value,
                    details={"order_id": order_id},
                    conn=conn,
                )
            self._append_record(
                order_id=order_id,
                action="SUBMIT_APPROVAL",
                actor=actor,
                env=order.env,
                details=json.dumps({
                    "created_by": order.created_by,
                    "item_count": len(order.items),
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_order(order_id)

    # ------------------------------------------------------------------
    # 4. Approve / Reject
    # ------------------------------------------------------------------

    def approve_order(
        self, approver: str, *, order_id: str
    ) -> ReleaseOrder:
        """审批发布单。提单人不能审批自己的单。"""
        order = self._require_order(order_id)
        validate_release_admin_role(approver, self._is_admin(approver))
        validate_release_transition(order.status, ReleaseOrderStatus.APPROVED)
        validate_release_not_self_approve(order.created_by, approver)
        validate_not_self_approve(order.created_by, approver)

        for item in order.items:
            v = self._require_version(order.env, item.name, item.version)
            if v.status == VersionStatus.PENDING_APPROVAL:
                validate_not_self_approve(v.author, approver)

        with self.repo.transaction() as conn:
            self.repo.update_release_order_fields(
                order_id,
                {
                    "status": ReleaseOrderStatus.APPROVED,
                    "approver": approver,
                    "approved_at": _now_iso(),
                },
                conn=conn,
            )
            for item in order.items:
                self.audit.record(
                    actor=approver,
                    action=AuditAction.RELEASE_ORDER_APPROVE,
                    env=order.env,
                    switch_name=item.name,
                    version=item.version,
                    details={
                        "order_id": order_id,
                        "approver": approver,
                    },
                    conn=conn,
                )
            self._append_record(
                order_id=order_id,
                action="APPROVE",
                actor=approver,
                env=order.env,
                details=json.dumps({
                    "created_by": order.created_by,
                    "item_count": len(order.items),
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_order(order_id)

    def reject_order(
        self, rejector: str, *, order_id: str, reason: str
    ) -> ReleaseOrder:
        """驳回发布单。"""
        order = self._require_order(order_id)
        validate_release_admin_role(rejector, self._is_admin(rejector))
        validate_release_transition(order.status, ReleaseOrderStatus.REJECTED)
        if not reason:
            raise ValidationError("驳回必须提供原因", field="reason")

        with self.repo.transaction() as conn:
            self.repo.update_release_order_fields(
                order_id,
                {
                    "status": ReleaseOrderStatus.REJECTED,
                    "rejected_by": rejector,
                    "reject_reason": reason,
                    "rejected_at": _now_iso(),
                },
                conn=conn,
            )
            for item in order.items:
                self.audit.record(
                    actor=rejector,
                    action=AuditAction.RELEASE_ORDER_REJECT,
                    env=order.env,
                    switch_name=item.name,
                    version=item.version,
                    details={
                        "order_id": order_id,
                        "reason": reason,
                    },
                    conn=conn,
                )
            self._append_record(
                order_id=order_id,
                action="REJECT",
                actor=rejector,
                env=order.env,
                details=json.dumps({
                    "reason": reason,
                    "item_count": len(order.items),
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_order(order_id)

    # ------------------------------------------------------------------
    # 5. Execute (原子化执行)
    # ------------------------------------------------------------------

    def execute_order(
        self, actor: str, *, order_id: str
    ) -> ReleaseOrder:
        """原子化执行发布单。

        所有明细在一个事务中执行。如果任何一步失败，整个事务回滚，
        发布单状态标记为 EXECUTE_FAILED，不留下任何半截状态。
        """
        order = self._require_order(order_id)
        validate_release_admin_role(actor, self._is_admin(actor))
        validate_release_transition(order.status, ReleaseOrderStatus.EXECUTING)

        dep_order = _topological_sort(order.items)
        item_by_name = {i.name: i for i in order.items}

        try:
            with self.repo.transaction() as conn:
                self.repo.update_release_order_fields(
                    order_id,
                    {"status": ReleaseOrderStatus.EXECUTING},
                    conn=conn,
                )

                for name in dep_order:
                    item = item_by_name[name]
                    v = self._require_version(order.env, name, item.version)
                    prev_effective = self.repo.get_effective_version(
                        order.env, name, conn=conn
                    )

                    rollback_snapshot = self._collect_rollback_snapshot(
                        order.env, name, conn=conn
                    )

                    validate_transition(v.status, VersionStatus.PUBLISHED)

                    if prev_effective and prev_effective.id != v.id:
                        self.repo.update_version_fields(
                            prev_effective.id,
                            {
                                "status": VersionStatus.ROLLED_BACK,
                                "rolled_back_at": _now_iso(),
                                "replace_reason": (
                                    f"被发布单 {order_id} 发布的 V{v.version} 替换"
                                ),
                            },
                            conn=conn,
                        )
                        self.audit.record(
                            actor=actor,
                            action=AuditAction.ROLLBACK,
                            env=order.env,
                            switch_name=name,
                            version=prev_effective.version,
                            old_status=VersionStatus.PUBLISHED,
                            new_status=VersionStatus.ROLLED_BACK,
                            details={
                                "order_id": order_id,
                                "superseded_by_version": v.version,
                                "replace_reason": (
                                    f"被发布单 {order_id} 发布的 V{v.version} 替换"
                                ),
                            },
                            conn=conn,
                        )

                    self.repo.update_version_fields(
                        v.id,
                        {
                            "status": VersionStatus.PUBLISHED,
                            "approver": actor,
                            "approved_at": _now_iso(),
                            "published_at": _now_iso(),
                        },
                        conn=conn,
                    )
                    self.audit.record(
                        actor=actor,
                        action=AuditAction.RELEASE_ORDER_EXECUTE,
                        env=order.env,
                        switch_name=name,
                        version=v.version,
                        old_status=v.status,
                        new_status=VersionStatus.PUBLISHED,
                        details={
                            "order_id": order_id,
                            "supersedes_version": prev_effective.version if prev_effective else None,
                        },
                        conn=conn,
                    )

                    assert item.id is not None
                    self.repo.update_release_order_item_fields(
                        item.id,
                        {
                            "status_after": VersionStatus.PUBLISHED,
                            "prev_effective_version": (
                                prev_effective.version if prev_effective else None
                            ),
                            "executed": True,
                            "rollback_snapshot": rollback_snapshot,
                        },
                        conn=conn,
                    )
                    self._append_record(
                        order_id=order_id,
                        action="EXECUTE_ITEM",
                        actor=actor,
                        env=order.env,
                        switch_name=name,
                        version=v.version,
                        details=json.dumps({
                            "prev_effective_version": prev_effective.version if prev_effective else None,
                            "new_status": VersionStatus.PUBLISHED.value,
                        }, ensure_ascii=False),
                        conn=conn,
                    )

                self.repo.update_release_order_fields(
                    order_id,
                    {
                        "status": ReleaseOrderStatus.EXECUTED,
                        "executed_at": _now_iso(),
                    },
                    conn=conn,
                )
                self._append_record(
                    order_id=order_id,
                    action="EXECUTE_COMPLETE",
                    actor=actor,
                    env=order.env,
                    details=json.dumps({
                        "item_count": len(order.items),
                        "dependency_order": dep_order,
                    }, ensure_ascii=False),
                    conn=conn,
                )

        except Exception as exc:
            self.repo.update_release_order_fields(
                order_id,
                {
                    "status": ReleaseOrderStatus.EXECUTE_FAILED,
                    "error_message": str(exc),
                }
            )
            self._append_record(
                order_id=order_id,
                action="EXECUTE_FAILED",
                actor=actor,
                env=order.env,
                details=json.dumps({
                    "error": str(exc),
                    "item_count": len(order.items),
                }, ensure_ascii=False),
            )
            raise

        return self._require_order(order_id)

    # ------------------------------------------------------------------
    # 6. Rollback (整单回滚)
    # ------------------------------------------------------------------

    def rollback_order(
        self, actor: str, *, order_id: str, reason: str
    ) -> ReleaseOrder:
        """整单回滚。按依赖顺序的反向依次恢复到执行前状态。

        回滚也是原子化的：要么全部回滚成功，要么全部失败。
        """
        order = self._require_order(order_id)
        validate_release_admin_role(actor, self._is_admin(actor))
        validate_release_transition(order.status, ReleaseOrderStatus.ROLLING_BACK)
        if not reason:
            raise ValidationError("回滚必须提供原因", field="reason")

        dep_order = _topological_sort(order.items)
        reverse_order = list(reversed(dep_order))
        item_by_name = {i.name: i for i in order.items}

        try:
            with self.repo.transaction() as conn:
                self.repo.update_release_order_fields(
                    order_id,
                    {"status": ReleaseOrderStatus.ROLLING_BACK},
                    conn=conn,
                )

                for name in reverse_order:
                    item = item_by_name[name]
                    if not item.executed:
                        continue

                    current = self.repo.get_effective_version(
                        order.env, name, conn=conn
                    )
                    if current and current.version == item.version:
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
                            action=AuditAction.RELEASE_ORDER_ROLLBACK,
                            env=order.env,
                            switch_name=name,
                            version=current.version,
                            old_status=VersionStatus.PUBLISHED,
                            new_status=VersionStatus.ROLLED_BACK,
                            details={
                                "order_id": order_id,
                                "rollback_reason": reason,
                            },
                            conn=conn,
                        )

                    snap = item.rollback_snapshot
                    if snap and snap.get("type") == "has_effective":
                        snap_version = snap["version"]
                        prev = self._require_version(order.env, name, snap_version)
                        if prev.status == VersionStatus.ROLLED_BACK:
                            new_ver_no = self.repo.next_version(prev.switch_id, conn=conn)
                            restored = SwitchVersion(
                                switch_id=prev.switch_id,
                                env=prev.env,
                                name=prev.name,
                                author=actor,
                                approver=actor,
                                status=VersionStatus.PUBLISHED,
                                version=new_ver_no,
                                rollout_ratio=snap["rollout_ratio"],
                                whitelist=list(snap["whitelist"]),
                                dependencies=list(snap["dependencies"]),
                                default_value=snap["default_value"],
                                replace_reason=(
                                    f"发布单 {order_id} 回滚恢复，原因为: {reason}"
                                ),
                                published_at=_now_iso(),
                                approved_at=_now_iso(),
                            )
                            restored = self.repo.insert_version(restored, conn=conn)
                            self.audit.record(
                                actor=actor,
                                action=AuditAction.RELEASE_ORDER_ROLLBACK,
                                env=order.env,
                                switch_name=name,
                                version=restored.version,
                                new_status=VersionStatus.PUBLISHED,
                                details={
                                    "order_id": order_id,
                                    "restored_from_version": snap_version,
                                    "rollback_reason": reason,
                                },
                                conn=conn,
                            )

                    self._append_record(
                        order_id=order_id,
                        action="ROLLBACK_ITEM",
                        actor=actor,
                        env=order.env,
                        switch_name=name,
                        version=item.version,
                        details=json.dumps({
                            "rollback_reason": reason,
                            "snapshot_type": snap.get("type") if snap else None,
                        }, ensure_ascii=False),
                        rollback_source_order_id=order_id,
                        conn=conn,
                    )

                self.repo.update_release_order_fields(
                    order_id,
                    {
                        "status": ReleaseOrderStatus.ROLLED_BACK,
                        "rollback_reason": reason,
                        "rolled_back_at": _now_iso(),
                    },
                    conn=conn,
                )
                self._append_record(
                    order_id=order_id,
                    action="ROLLBACK_COMPLETE",
                    actor=actor,
                    env=order.env,
                    details=json.dumps({
                        "reason": reason,
                        "item_count": len(order.items),
                        "reverse_order": reverse_order,
                    }, ensure_ascii=False),
                    rollback_source_order_id=order_id,
                    conn=conn,
                )

        except Exception as exc:
            self.repo.update_release_order_fields(
                order_id,
                {
                    "status": ReleaseOrderStatus.ROLLBACK_FAILED,
                    "error_message": str(exc),
                }
            )
            self._append_record(
                order_id=order_id,
                action="ROLLBACK_FAILED",
                actor=actor,
                env=order.env,
                details=json.dumps({
                    "error": str(exc),
                    "reason": reason,
                }, ensure_ascii=False),
            )
            raise

        return self._require_order(order_id)

    # ------------------------------------------------------------------
    # 7. Cancel (撤销)
    # ------------------------------------------------------------------

    def cancel_order(
        self, actor: str, *, order_id: str, reason: str
    ) -> ReleaseOrder:
        """撤销发布单。仅未执行的发布单可以撤销。"""
        order = self._require_order(order_id)
        validate_release_transition(order.status, ReleaseOrderStatus.CANCELLED)
        if not reason:
            raise ValidationError("撤销必须提供原因", field="reason")

        with self.repo.transaction() as conn:
            self.repo.update_release_order_fields(
                order_id,
                {
                    "status": ReleaseOrderStatus.CANCELLED,
                    "cancel_reason": reason,
                    "cancelled_at": _now_iso(),
                },
                conn=conn,
            )
            for item in order.items:
                self.audit.record(
                    actor=actor,
                    action=AuditAction.RELEASE_ORDER_CANCEL,
                    env=order.env,
                    switch_name=item.name,
                    version=item.version,
                    details={
                        "order_id": order_id,
                        "reason": reason,
                    },
                    conn=conn,
                )
            self._append_record(
                order_id=order_id,
                action="CANCEL",
                actor=actor,
                env=order.env,
                details=json.dumps({
                    "reason": reason,
                    "previous_status": order.status.value,
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_order(order_id)

    # ------------------------------------------------------------------
    # 8. Copy (复制)
    # ------------------------------------------------------------------

    def copy_order(
        self, actor: str, *, order_id: str
    ) -> ReleaseOrder:
        """复制发布单。生成新的 order_id，状态重置为 CREATED。"""
        order = self._require_order(order_id)
        validate_release_admin_role(actor, self._is_admin(actor))

        items_data = [
            {"name": i.name, "version": i.version} for i in order.items
        ]

        new_order = self.create_order(
            actor,
            env=order.env,
            items=items_data,
            title=f"{order.title} (副本)" if order.title else f"{order.order_id} 副本",
            description=(
                f"{order.description}\n\n复制自: {order_id}，创建人: {order.created_by}"
                if order.description
                else f"复制自: {order_id}，创建人: {order.created_by}"
            ),
            skip_duplicate_check=True,
        )

        with self.repo.transaction() as conn:
            self._append_record(
                order_id=new_order.order_id,
                action="COPY_FROM",
                actor=actor,
                env=order.env,
                details=json.dumps({
                    "source_order_id": order_id,
                    "source_created_by": order.created_by,
                }, ensure_ascii=False),
                conn=conn,
            )
            self._append_record(
                order_id=order_id,
                action="COPIED_TO",
                actor=actor,
                env=order.env,
                details=json.dumps({
                    "new_order_id": new_order.order_id,
                }, ensure_ascii=False),
                conn=conn,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_ORDER_COPY,
                env=order.env,
                switch_name="__order__",
                details={
                    "source_order_id": order_id,
                    "new_order_id": new_order.order_id,
                },
                conn=conn,
            )

        return new_order

    # ------------------------------------------------------------------
    # 9. Export / Import
    # ------------------------------------------------------------------

    def export_order(
        self,
        actor: str,
        *,
        order_id: str,
        fmt: str = "yaml",
    ) -> str:
        """导出发布单为 YAML 或 JSON。"""
        order = self._require_order(order_id)
        data = order.to_export_dict()

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_ORDER_EXPORT,
                env=order.env,
                switch_name="__order__",
                details={
                    "order_id": order_id,
                    "format": fmt,
                    "item_count": len(order.items),
                },
                conn=conn,
            )
            self._append_record(
                order_id=order_id,
                action="EXPORT",
                actor=actor,
                env=order.env,
                details=json.dumps({"format": fmt}, ensure_ascii=False),
                conn=conn,
            )

        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return _dump_yaml(data)

    def import_order_file(
        self,
        actor: str,
        *,
        path: str,
    ) -> ReleaseOrder:
        """从 YAML/JSON 文件导入发布单。"""
        validate_release_admin_role(actor, self._is_admin(actor))

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

        return self._load_order_document(actor=actor, doc=doc, source=path)

    def _load_order_document(
        self, *, actor: str, doc: Any, source: str
    ) -> ReleaseOrder:
        if not isinstance(doc, dict):
            raise ValidationError("发布单文件根节点必须是对象 (mapping)")

        schema = doc.get("schema_version")
        if schema != _RELEASE_SCHEMA_VERSION:
            raise ValidationError(
                f"发布单 schema_version 不匹配: 期望 {_RELEASE_SCHEMA_VERSION!r}，收到 {schema!r}"
            )

        original_order_id = doc.get("order_id")
        env = doc.get("env")
        items_raw = doc.get("items")
        title = doc.get("title", "")
        description = doc.get("description", "")
        checksum = doc.get("checksum", "")

        if not env:
            raise ValidationError("缺少 env")
        if not isinstance(items_raw, list) or not items_raw:
            raise ValidationError("'items' 字段必须是非空列表")

        items_data: list[dict[str, Any]] = []
        for idx, item in enumerate(items_raw):
            if not isinstance(item, dict):
                raise ValidationError(f"items[{idx}] 必须是对象")
            name = item.get("name")
            version = item.get("version")
            if not name:
                raise ValidationError(f"items[{idx}] 缺少 name")
            if not isinstance(version, int):
                raise ValidationError(f"items[{idx}].version 必须是整数")
            items_data.append({"name": name, "version": version})

        # 第一步：验证开关版本存在且状态合法，构建 order_items 用于计算 checksum
        allowed_statuses = [VersionStatus.DRAFT, VersionStatus.PENDING_APPROVAL]
        order_items: list[ReleaseOrderItem] = []
        for item_data in items_data:
            name = item_data["name"]
            version = item_data["version"]
            v = self._require_version(env, name, version)
            validate_release_version_status(v, allowed_statuses)
            order_item = ReleaseOrderItem.from_version(v)
            order_items.append(order_item)

        # 第二步：先验证 checksum，不匹配直接报错
        computed_checksum = _compute_release_checksum(env, order_items)
        if checksum and checksum != computed_checksum:
            raise ValidationError(
                f"发布单校验和不匹配：文件中 checksum={checksum}，"
                f"实际内容计算 checksum={computed_checksum}。"
                "文件可能已被篡改。"
            )

        # 第三步：检查是否已存在相同 order_id 的发布单
        if original_order_id:
            existing = self.repo.get_release_order(original_order_id)
            if existing is not None:
                return existing

        # 第四步：创建发布单，使用原始 order_id，跳过去重检查
        order = self.create_order(
            actor,
            env=env,
            items=items_data,
            title=title,
            description=f"{description}\n\n导入自: {source}" if description else f"导入自: {source}",
            skip_duplicate_check=True,
            order_id=original_order_id,
        )

        with self.repo.transaction() as conn:
            self._append_record(
                order_id=order.order_id,
                action="IMPORT_FILE",
                actor=actor,
                env=env,
                details=json.dumps({
                    "source": source,
                    "switch_count": len(order.items),
                    "checksum": computed_checksum,
                    "original_checksum": checksum,
                    "original_order_id": original_order_id,
                }, ensure_ascii=False),
                conn=conn,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_ORDER_IMPORT,
                env=env,
                switch_name="__order__",
                details={
                    "order_id": order.order_id,
                    "source": source,
                    "checksum": computed_checksum,
                },
                conn=conn,
            )

        return order

    @staticmethod
    def _read(path: str) -> str:
        if not os.path.isfile(path):
            raise ValidationError(f"发布单文件不存在: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise ValidationError(f"读取发布单文件失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_orders(
        self,
        env: Optional[str] = None,
        statuses: Optional[list[ReleaseOrderStatus]] = None,
    ) -> list[ReleaseOrder]:
        return self.repo.list_release_orders(env=env, statuses=statuses)

    def get_order(self, order_id: str) -> ReleaseOrder:
        return self._require_order(order_id)

    def list_records(
        self, order_id: str, limit: int = 100
    ) -> list[ReleaseOrderRecord]:
        return self.repo.list_release_order_records(order_id=order_id, limit=limit)
