from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Optional

from ..audit import AuditTrail
from ..core.enums import MigrationStatus, ReleaseOrderStatus, ReleasePassStatus, VersionStatus, WindowCheckResult
from ..core.models import SwitchVersion
from ..service import (
    ConfigExporter,
    ConfigImporter,
    MigrationService,
    ReleaseOrderService,
    ReleaseWindowService,
    SwitchService,
)
from ..storage.repository import SwitchRepository
from ..validator.validators import ValidationError


DEFAULT_ADMINS = set(
    x.strip() for x in os.environ.get("FSWITCH_ADMINS", "").split(",") if x.strip()
)


DEFAULT_DB = os.environ.get(
    "FSWITCH_DB",
    os.path.join(os.getcwd(), "data", "fswitch.db"),
)
DEFAULT_ACTOR = os.environ.get("FSWITCH_ACTOR", "developer@local")


class AppContext:
    def __init__(self, db_path: str, actor: str) -> None:
        self.db_path = db_path
        self.actor = actor
        self.repo = SwitchRepository(db_path)
        self.audit = AuditTrail(self.repo)
        self.service = SwitchService(self.repo, self.audit)
        self.importer = ConfigImporter(self.repo, self.audit)
        self.exporter = ConfigExporter(self.repo, self.audit)
        self.migration = MigrationService(self.repo, self.audit)
        self.release = ReleaseOrderService(
            self.repo, self.audit, admin_emails=DEFAULT_ADMINS
        )
        self.window = ReleaseWindowService(
            self.repo, self.audit, admin_emails=DEFAULT_ADMINS
        )

    def close(self) -> None:
        self.repo.close()


def build_app(db_path: str = DEFAULT_DB, actor: str = DEFAULT_ACTOR) -> AppContext:
    return AppContext(db_path=db_path, actor=actor)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")


def _print_version_row(v: SwitchVersion, *, show_reason: bool = True) -> str:
    parts = [
        f"V{v.version:<3}",
        f"[{v.status.value:<18}]",
        f"{v.env}:{v.name}",
        f"ratio={v.rollout_ratio:>3}%",
        f"default={v.default_value}",
        f"author={v.author}",
    ]
    if v.approver:
        parts.append(f"approver={v.approver}")
    if v.whitelist:
        parts.append(f"wl={v.whitelist}")
    if v.dependencies:
        parts.append(f"deps={v.dependencies}")
    if show_reason:
        if v.rollback_reason:
            parts.append(f"[回滚原因: {v.rollback_reason}]")
        if v.replace_reason:
            parts.append(f"[替换原因: {v.replace_reason}]")
        if v.reject_reason and v.status == VersionStatus.DRAFT:
            parts.append(f"[驳回原因: {v.reject_reason}]")
    return " ".join(parts)


def _parse_statuses(values: Optional[list[str]]) -> Optional[list[VersionStatus]]:
    if not values:
        return None
    out: list[VersionStatus] = []
    for s in values:
        try:
            out.append(VersionStatus(s.upper()))
        except ValueError:
            raise ValidationError(
                f"未知状态 '{s}'，可选: {[x.value for x in VersionStatus]}"
            )
    return out


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

CmdFn = Callable[[argparse.Namespace, AppContext], Optional[int]]


def cmd_create(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    ver = app.service.create_draft(
        actor=app.actor,
        env=args.env,
        name=args.name,
        rollout_ratio=args.ratio,
        whitelist=args.whitelist or [],
        dependencies=args.dep or [],
        default_value=bool(args.default),
    )
    _print_json({
        "ok": True,
        "version": ver.to_dict(),
    })
    return 0


def cmd_edit(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    updates: dict[str, Any] = {}
    if args.ratio is not None:
        updates["rollout_ratio"] = args.ratio
    if args.whitelist is not None:
        updates["whitelist"] = args.whitelist
    if args.dep is not None:
        updates["dependencies"] = args.dep
    if args.default is not None:
        updates["default_value"] = bool(args.default)
    if not updates:
        raise ValidationError("edit 需要至少指定一个要修改的字段")
    ver = app.service.edit_draft(
        actor=app.actor,
        env=args.env,
        name=args.name,
        version=args.version,
        **updates,
    )
    _print_json({"ok": True, "version": ver.to_dict()})
    return 0


def cmd_submit(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    ver = app.service.submit_for_approval(
        actor=app.actor, env=args.env, name=args.name, version=args.version
    )
    _print_json({"ok": True, "version": ver.to_dict()})
    return 0


def cmd_approve(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    ver = app.service.approve_and_publish(
        approver=app.actor,
        env=args.env,
        name=args.name,
        version=args.version,
        replace_reason=args.reason or "",
    )
    _print_json({"ok": True, "version": ver.to_dict()})
    return 0


def cmd_reject(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    ver = app.service.reject_approval(
        approver=app.actor,
        env=args.env,
        name=args.name,
        version=args.version,
        reason=args.reason or "",
    )
    _print_json({"ok": True, "version": ver.to_dict()})
    return 0


def cmd_rollback(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    ver = app.service.rollback(
        actor=app.actor,
        env=args.env,
        name=args.name,
        reason=args.reason,
        target_version=args.restore,
    )
    _print_json({"ok": True, "version": ver.to_dict()})
    return 0


def cmd_deprecate(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    touched = app.service.deprecate(
        actor=app.actor, env=args.env, name=args.name, reason=args.reason
    )
    _print_json({"ok": True, "count": len(touched), "versions": [v.to_dict() for v in touched]})
    return 0


def cmd_list(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    statuses = _parse_statuses(args.status)
    rows = app.service.query(
        env=args.env,
        name=args.name,
        statuses=statuses,
        include_deprecated=bool(args.include_deprecated),
    )
    if args.format == "json":
        _print_json({"count": len(rows), "versions": [r.to_dict() for r in rows]})
    else:
        for r in rows:
            print(_print_version_row(r))
    return 0


def cmd_current(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    pair = app.service.get_current_and_draft(args.env, args.name)
    if args.format == "json":
        _print_json({
            "effective": pair["effective"].to_dict() if pair["effective"] else None,
            "draft": pair["draft"].to_dict() if pair["draft"] else None,
            "latest_non_deprecated": (
                pair["latest_non_deprecated"].to_dict()
                if pair["latest_non_deprecated"] else None
            ),
        })
    else:
        print(f"=== {args.env}:{args.name} ===")
        print("生效版本 (PUBLISHED):")
        if pair["effective"]:
            print("  " + _print_version_row(pair["effective"]))
        else:
            print("  (无)")
        print("草稿 / 待审批:")
        if pair["draft"]:
            print("  " + _print_version_row(pair["draft"]))
        else:
            print("  (无)")
    return 0


def cmd_history(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    diffs = app.service.history(args.env, args.name)
    if args.format == "json":
        _print_json({
            "count": len(diffs),
            "changes": [
                {
                    "from": d.prev_version,
                    "to": d.curr_version,
                    "changes": d.field_changes,
                    "replace_reason": d.replace_reason,
                }
                for d in diffs
            ],
        })
    else:
        print(f"=== {args.env}:{args.name} 变更历史 ===")
        for d in diffs:
            print(d.format())
    return 0


def cmd_audit(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    rows = app.repo.list_audit(
        env=args.env, switch_name=args.name, limit=args.limit
    )
    if args.format == "json":
        _print_json({"count": len(rows), "logs": [r.to_dict() for r in rows]})
    else:
        for r in rows:
            details = r.details[:80]
            print(
                f"{r.timestamp} {r.actor:<20} {r.action.value:<22} "
                f"{r.env}:{r.switch_name} "
                f"V{r.version if r.version else '-':<3} "
                f"{r.old_status or '-':>18} -> {r.new_status or '-':<18} "
                f"{details}"
            )
    return 0


def cmd_export(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    fmt = args.format
    if fmt == "json":
        text = app.exporter.export_effective_json(actor=app.actor, env=args.env)
    else:
        text = app.exporter.export_effective_yaml(actor=app.actor, env=args.env)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
        _print_json({"ok": True, "output": os.path.abspath(args.output), "format": fmt})
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_import(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    result = app.importer.import_file(actor=app.actor, path=args.file)
    _print_json({"ok": True, **result})
    return 0


# ---------------------------------------------------------------------------
# Migration package command handlers
# ---------------------------------------------------------------------------

def cmd_pkg_create(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    pkg = app.migration.create_package(
        actor=app.actor,
        source_env=args.source_env,
        target_env=args.target_env,
        description=args.description or "",
        names=args.name or None,
    )
    _print_json({
        "ok": True,
        "package": {
            "package_id": pkg.package_id,
            "source_env": pkg.source_env,
            "target_env": pkg.target_env,
            "status": pkg.status.value if isinstance(pkg.status, MigrationStatus) else pkg.status,
            "created_by": pkg.created_by,
            "checksum": pkg.checksum,
            "switch_count": len(pkg.switches),
            "description": pkg.description,
            "created_at": pkg.created_at,
        },
    })
    return 0


def cmd_pkg_preview(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    preview = app.migration.preview_package(actor=app.actor, package_id=args.package_id)
    if args.format == "json":
        _print_json({"ok": True, **preview.to_dict()})
    else:
        print(f"=== 迁移预演报告 {args.package_id} ===")
        print(f"  源环境: {preview.source_env}  →  目标环境: {preview.target_env}")
        print(f"  摘要:")
        for k, v in preview.summary.items():
            if v > 0:
                print(f"    {k:<20}: {v}")
        print(f"  依赖缺口: {preview.all_dependency_gaps or '(无)'}")
        print(f"  需要审批: {preview.all_required_approvers or '(无)'}")
        print(f"  阻塞问题: {len(preview.blocking_issues)} 项")
        for b in preview.blocking_issues:
            print(f"    ⚠️  {b}")
        print(f"  可否导入: {'✅ 可以' if preview.can_import else '❌ 被阻塞'}")
        print(f"  逐项详情:")
        for e in preview.entries:
            ct = e.change_type.value if hasattr(e.change_type, "value") else e.change_type
            print(f"    [{ct:<18}] {e.env}:{e.name}")
            if e.dependency_gaps:
                print(f"      依赖缺口: {e.dependency_gaps}")
            if e.required_approvers:
                print(f"      需要审批: {e.required_approvers}")
            if e.conflict_reason:
                print(f"      冲突原因: {e.conflict_reason}")
            for f, (old, new) in e.field_changes.items():
                print(f"      · {f}: {old!r} → {new!r}")
    return 0


def cmd_pkg_import(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    result = app.migration.import_package(
        actor=app.actor,
        package_id=args.package_id,
        force=bool(args.force),
    )
    _print_json({"ok": True, **result})
    return 0


def cmd_pkg_export(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    fmt = args.format_pkg
    text = app.migration.export_package_file(
        actor=app.actor,
        package_id=args.package_id,
        fmt=fmt,
    )
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
        _print_json({
            "ok": True,
            "package_id": args.package_id,
            "output": os.path.abspath(args.output),
            "format": fmt,
        })
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_pkg_import_file(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    pkg = app.migration.import_package_file(actor=app.actor, path=args.file)
    _print_json({
        "ok": True,
        "package": {
            "package_id": pkg.package_id,
            "source_env": pkg.source_env,
            "target_env": pkg.target_env,
            "status": pkg.status.value if isinstance(pkg.status, MigrationStatus) else pkg.status,
            "created_by": pkg.created_by,
            "checksum": pkg.checksum,
            "switch_count": len(pkg.switches),
        },
    })
    return 0


def cmd_pkg_list(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    statuses: Optional[list[MigrationStatus]] = None
    if args.status:
        statuses = []
        for s in args.status:
            try:
                statuses.append(MigrationStatus(s.upper()))
            except ValueError:
                raise ValidationError(
                    f"未知迁移包状态 '{s}'，可选: {[x.value for x in MigrationStatus]}"
                )
    pkgs = app.migration.list_packages(
        source_env=args.source_env,
        target_env=args.target_env,
        statuses=statuses,
    )
    if args.format == "json":
        _print_json({
            "count": len(pkgs),
            "packages": [
                {
                    "package_id": p.package_id,
                    "source_env": p.source_env,
                    "target_env": p.target_env,
                    "status": p.status.value if isinstance(p.status, MigrationStatus) else p.status,
                    "created_by": p.created_by,
                    "checksum": p.checksum,
                    "switch_count": len(p.switches),
                    "description": p.description,
                    "created_at": p.created_at,
                    "previewed_at": p.previewed_at,
                    "imported_at": p.imported_at,
                    "approved_by": p.approved_by,
                    "rejected_by": p.rejected_by,
                }
                for p in pkgs
            ],
        })
    else:
        for p in pkgs:
            st = p.status.value if isinstance(p.status, MigrationStatus) else p.status
            print(
                f"{p.package_id:<18} {st:<18} "
                f"{p.source_env}→{p.target_env:<16} "
                f"switches={len(p.switches):<3} "
                f"by={p.created_by} "
                f"at={p.created_at}"
            )
    return 0


def cmd_pkg_show(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    pkg = app.migration.get_package(args.package_id)
    records = app.migration.list_records(args.package_id)
    st = pkg.status.value if isinstance(pkg.status, MigrationStatus) else pkg.status
    if args.format == "json":
        _print_json({
            "ok": True,
            "package": {
                "package_id": pkg.package_id,
                "source_env": pkg.source_env,
                "target_env": pkg.target_env,
                "status": st,
                "created_by": pkg.created_by,
                "checksum": pkg.checksum,
                "description": pkg.description,
                "created_at": pkg.created_at,
                "previewed_at": pkg.previewed_at,
                "imported_at": pkg.imported_at,
                "approved_by": pkg.approved_by,
                "approved_at": pkg.approved_at,
                "rejected_by": pkg.rejected_by,
                "rejected_at": pkg.rejected_at,
                "reject_reason": pkg.reject_reason,
                "switches": [s.to_dict() for s in pkg.switches],
            },
            "records": [r.to_dict() for r in records],
        })
    else:
        print(f"=== 迁移包 {pkg.package_id} ===")
        print(f"  状态      : {st}")
        print(f"  源 → 目标 : {pkg.source_env} → {pkg.target_env}")
        print(f"  创建人    : {pkg.created_by}")
        print(f"  描述      : {pkg.description or '(无)'}")
        print(f"  校验和    : {pkg.checksum}")
        print(f"  创建时间  : {pkg.created_at}")
        if pkg.previewed_at:
            print(f"  预演时间  : {pkg.previewed_at}")
        if pkg.imported_at:
            print(f"  导入时间  : {pkg.imported_at}")
        if pkg.approved_by:
            print(f"  包级审批  : {pkg.approved_by} @ {pkg.approved_at}")
        if pkg.rejected_by:
            print(f"  包级驳回  : {pkg.rejected_by} @ {pkg.rejected_at} 原因={pkg.reject_reason}")
        print(f"  包含开关 ({len(pkg.switches)}):")
        for s in pkg.switches:
            print(f"    - {s.env}:{s.name} V{s.version} ratio={s.rollout_ratio}% deps={s.dependencies}")
        print(f"  迁移记录 ({len(records)}):")
        for r in records:
            v = f" V{r.version}" if r.version else ""
            n = f" {r.switch_name}" if r.switch_name else ""
            print(f"    {r.timestamp} {r.action:<20} {r.actor:<20} {r.env}{n}{v}")
    return 0


def cmd_pkg_approve(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    pkg = app.migration.mark_package_approved(
        approver=app.actor, package_id=args.package_id
    )
    _print_json({
        "ok": True,
        "package_id": pkg.package_id,
        "status": pkg.status.value if isinstance(pkg.status, MigrationStatus) else pkg.status,
        "approved_by": pkg.approved_by,
        "approved_at": pkg.approved_at,
    })
    return 0


def cmd_pkg_reject(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    pkg = app.migration.mark_package_rejected(
        rejector=app.actor, package_id=args.package_id, reason=args.reason
    )
    _print_json({
        "ok": True,
        "package_id": pkg.package_id,
        "status": pkg.status.value if isinstance(pkg.status, MigrationStatus) else pkg.status,
        "rejected_by": pkg.rejected_by,
        "rejected_at": pkg.rejected_at,
        "reject_reason": pkg.reject_reason,
    })
    return 0


def cmd_pkg_records(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    records = app.migration.list_records(args.package_id, limit=args.limit)
    if args.format == "json":
        _print_json({
            "count": len(records),
            "records": [r.to_dict() for r in records],
        })
    else:
        for r in records:
            v = f"V{r.version:<4}" if r.version else "----"
            n = r.switch_name or "-"
            print(
                f"{r.timestamp} {r.action:<20} {r.actor:<20} "
                f"{r.env}:{n:<24} {v} {r.details[:80]}"
            )
    return 0


# ---------------------------------------------------------------------------
# Release Order command handlers
# ---------------------------------------------------------------------------

def cmd_rel_create(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    items_data: list[dict[str, Any]] = []
    names_versions = args.item or []
    for nv in names_versions:
        if ":" not in nv:
            raise ValidationError(f"item 格式错误，应为 name:version，收到 {nv!r}")
        name, ver_str = nv.split(":", 1)
        try:
            version = int(ver_str)
        except ValueError:
            raise ValidationError(f"版本号必须是整数，收到 {ver_str!r}")
        items_data.append({"name": name, "version": version})

    order = app.release.create_order(
        actor=app.actor,
        env=args.env,
        items=items_data,
        title=args.title or "",
        description=args.description or "",
    )
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order": {
            "order_id": order.order_id,
            "env": order.env,
            "status": st,
            "created_by": order.created_by,
            "title": order.title,
            "checksum": order.checksum,
            "item_count": len(order.items),
            "items": [
                {
                    "name": i.name,
                    "version": i.version,
                    "status_before": i.status_before.value if i.status_before else None,
                }
                for i in order.items
            ],
        },
    })
    return 0


def cmd_rel_preview(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    preview = app.release.preview_order(actor=app.actor, order_id=args.order_id)
    if args.format == "json":
        _print_json({"ok": True, **preview.to_dict()})
    else:
        print(f"=== 发布预演报告 {args.order_id} ===")
        print(f"  环境: {preview.env}")
        print(f"  摘要:")
        for k, v in preview.summary.items():
            if v > 0:
                print(f"    {k:<20}: {v}")
        print(f"  依赖顺序: {preview.dependency_order}")
        print(f"  依赖缺口: {preview.all_dependency_gaps or '(无)'}")
        print(f"  阻塞问题: {len(preview.blocking_issues)} 项")
        for b in preview.blocking_issues:
            print(f"    ⚠️  {b}")
        print(f"  警告: {len(preview.warnings)} 项")
        for w in preview.warnings:
            print(f"    ⚪  {w}")
        print(f"  可否审批: {'✅ 可以' if preview.can_approve else '❌ 被阻塞'}")
        print(f"  可否执行: {'✅ 可以' if preview.can_execute else '❌ 被阻塞'}")
        print(f"  逐项详情:")
        for e in preview.items:
            cs = e.current_status.value
            ts = e.target_status.value
            print(f"    [dep#{e.dependency_order:<2}] {e.env}:{e.name} V{e.version}")
            print(f"      状态: {cs} → {ts}")
            if e.will_override_effective:
                print(f"      ⚠️  将覆盖当前生效版 V{e.prev_effective_version}")
            if e.dependency_gaps:
                print(f"      依赖缺口: {e.dependency_gaps}")
            if e.conflict_reason:
                print(f"      冲突: {e.conflict_reason}")
            if e.warnings:
                for w in e.warnings:
                    print(f"      警告: {w}")
            for f, (old, new) in e.field_changes.items():
                print(f"      · {f}: {old!r} → {new!r}")
    return 0


def cmd_rel_submit(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.submit_for_approval(actor=app.actor, order_id=args.order_id)
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order_id": order.order_id,
        "status": st,
        "submitted_at": order.submitted_at,
    })
    return 0


def cmd_rel_approve(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.approve_order(approver=app.actor, order_id=args.order_id)
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order_id": order.order_id,
        "status": st,
        "approver": order.approver,
        "approved_at": order.approved_at,
    })
    return 0


def cmd_rel_reject(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.reject_order(
        rejector=app.actor, order_id=args.order_id, reason=args.reason
    )
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order_id": order.order_id,
        "status": st,
        "rejected_by": order.rejected_by,
        "reject_reason": order.reject_reason,
    })
    return 0


def cmd_rel_execute(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.execute_order(actor=app.actor, order_id=args.order_id)
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order_id": order.order_id,
        "status": st,
        "executed_at": order.executed_at,
        "executed_items": [
            {"name": i.name, "version": i.version, "status_after": i.status_after.value if i.status_after else None}
            for i in order.items if i.executed
        ],
    })
    return 0


def cmd_rel_rollback(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.rollback_order(
        actor=app.actor, order_id=args.order_id, reason=args.reason
    )
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order_id": order.order_id,
        "status": st,
        "rollback_reason": order.rollback_reason,
        "rolled_back_at": order.rolled_back_at,
    })
    return 0


def cmd_rel_cancel(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.cancel_order(
        actor=app.actor, order_id=args.order_id, reason=args.reason
    )
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order_id": order.order_id,
        "status": st,
        "cancel_reason": order.cancel_reason,
        "cancelled_at": order.cancelled_at,
    })
    return 0


def cmd_rel_copy(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.copy_order(actor=app.actor, order_id=args.order_id)
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order_id": order.order_id,
        "status": st,
        "created_by": order.created_by,
        "item_count": len(order.items),
        "copied_from": args.order_id,
    })
    return 0


def cmd_rel_export(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    fmt = args.format_rel
    text = app.release.export_order(
        actor=app.actor, order_id=args.order_id, fmt=fmt
    )
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
        _print_json({
            "ok": True,
            "order_id": args.order_id,
            "output": os.path.abspath(args.output),
            "format": fmt,
        })
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_rel_import(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.import_order_file(actor=app.actor, path=args.file)
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    _print_json({
        "ok": True,
        "order": {
            "order_id": order.order_id,
            "env": order.env,
            "status": st,
            "created_by": order.created_by,
            "checksum": order.checksum,
            "item_count": len(order.items),
        },
    })
    return 0


def cmd_rel_list(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    statuses: Optional[list[ReleaseOrderStatus]] = None
    if args.status:
        statuses = []
        for s in args.status:
            try:
                statuses.append(ReleaseOrderStatus(s.upper()))
            except ValueError:
                raise ValidationError(
                    f"未知发布单状态 '{s}'，可选: {[x.value for x in ReleaseOrderStatus]}"
                )
    orders = app.release.list_orders(env=args.env, statuses=statuses)
    if args.format == "json":
        _print_json({
            "count": len(orders),
            "orders": [
                {
                    "order_id": o.order_id,
                    "env": o.env,
                    "status": o.status.value if isinstance(o.status, ReleaseOrderStatus) else o.status,
                    "created_by": o.created_by,
                    "approver": o.approver,
                    "title": o.title,
                    "checksum": o.checksum,
                    "item_count": len(o.items),
                    "created_at": o.created_at,
                    "submitted_at": o.submitted_at,
                    "approved_at": o.approved_at,
                    "executed_at": o.executed_at,
                    "rolled_back_at": o.rolled_back_at,
                    "rollback_reason": o.rollback_reason,
                }
                for o in orders
            ],
        })
    else:
        for o in orders:
            st = o.status.value if isinstance(o.status, ReleaseOrderStatus) else o.status
            print(
                f"{o.order_id:<18} {st:<22} {o.env:<10} "
                f"items={len(o.items):<3} by={o.created_by:<20} "
                f"at={o.created_at}"
            )
    return 0


def cmd_rel_show(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    order = app.release.get_order(args.order_id)
    records = app.release.list_records(args.order_id)
    st = order.status.value if isinstance(order.status, ReleaseOrderStatus) else order.status
    if args.format == "json":
        _print_json({
            "ok": True,
            "order": {
                "order_id": order.order_id,
                "env": order.env,
                "status": st,
                "created_by": order.created_by,
                "approver": order.approver,
                "title": order.title,
                "description": order.description,
                "checksum": order.checksum,
                "error_message": order.error_message,
                "created_at": order.created_at,
                "previewed_at": order.previewed_at,
                "submitted_at": order.submitted_at,
                "approved_at": order.approved_at,
                "rejected_at": order.rejected_at,
                "reject_reason": order.reject_reason,
                "executed_at": order.executed_at,
                "rolled_back_at": order.rolled_back_at,
                "rollback_reason": order.rollback_reason,
                "rollback_source_order_id": order.rollback_source_order_id,
                "cancelled_at": order.cancelled_at,
                "cancel_reason": order.cancel_reason,
                "items": [i.to_dict() for i in order.items],
            },
            "records": [r.to_dict() for r in records],
        })
    else:
        print(f"=== 发布单 {order.order_id} ===")
        print(f"  状态        : {st}")
        print(f"  环境        : {order.env}")
        print(f"  创建人      : {order.created_by}")
        if order.approver:
            print(f"  审批人      : {order.approver}")
        print(f"  标题        : {order.title or '(无)'}")
        print(f"  描述        : {order.description or '(无)'}")
        print(f"  校验和      : {order.checksum}")
        print(f"  创建时间    : {order.created_at}")
        if order.previewed_at:
            print(f"  预演时间    : {order.previewed_at}")
        if order.submitted_at:
            print(f"  提交时间    : {order.submitted_at}")
        if order.approved_at:
            print(f"  审批时间    : {order.approved_at}")
        if order.rejected_at:
            print(f"  驳回时间    : {order.rejected_at} 原因={order.reject_reason}")
        if order.executed_at:
            print(f"  执行时间    : {order.executed_at}")
        if order.rolled_back_at:
            print(f"  回滚时间    : {order.rolled_back_at} 原因={order.rollback_reason}")
        if order.cancelled_at:
            print(f"  撤销时间    : {order.cancelled_at} 原因={order.cancel_reason}")
        if order.error_message:
            print(f"  错误信息    : {order.error_message}")
        print(f"  包含明细 ({len(order.items)}):")
        for i in order.items:
            sb = i.status_before.value if i.status_before else "-"
            sa = i.status_after.value if i.status_after else "-"
            exec_flag = "✅" if i.executed else "⬜"
            print(f"    {exec_flag} {i.env}:{i.name} V{i.version}  {sb} → {sa}")
            if i.prev_effective_version:
                print(f"       覆盖生效版 V{i.prev_effective_version}")
        print(f"  操作记录 ({len(records)}):")
        for r in records:
            v = f" V{r.version}" if r.version else ""
            n = f" {r.switch_name}" if r.switch_name else ""
            print(f"    {r.timestamp} {r.action:<20} {r.actor:<20} {r.env}{n}{v}")
    return 0


def cmd_rel_records(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    records = app.release.list_records(args.order_id, limit=args.limit)
    if args.format == "json":
        _print_json({
            "count": len(records),
            "records": [r.to_dict() for r in records],
        })
    else:
        for r in records:
            v = f"V{r.version:<4}" if r.version else "----"
            n = r.switch_name or "-"
            src = f" from={r.rollback_source_order_id}" if r.rollback_source_order_id else ""
            print(
                f"{r.timestamp} {r.action:<20} {r.actor:<20} "
                f"{r.env}:{n:<24} {v}{src} {r.details[:60]}"
            )
    return 0


# ---------------------------------------------------------------------------
# Release Window command handlers
# ---------------------------------------------------------------------------

def _parse_time_ranges(raw: Optional[list[str]]) -> list[dict[str, str]]:
    if not raw:
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        parts = item.split(":")
        if len(parts) < 4:
            raise ValidationError(
                f"时间范围格式错误，应为 HH:MM:HH:MM[:days]，如 09:00:18:00 或 09:00:18:00:monday-friday，收到 {item!r}"
            )
        start = f"{parts[0].strip()}:{parts[1].strip()}"
        end = f"{parts[2].strip()}:{parts[3].strip()}"
        tr: dict[str, str] = {"start": start, "end": end}
        if len(parts) >= 5 and parts[4].strip():
            tr["days"] = parts[4].strip()
        out.append(tr)
    return out


def cmd_win_create(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    allowed_time_ranges = _parse_time_ranges(args.time_range)
    tpl = app.window.create_window_template(
        actor=app.actor,
        env=args.env,
        allowed_time_ranges=allowed_time_ranges,
        freeze_days=args.freeze_day or [],
        on_call_approvers=args.approver or [],
        default_description=args.description or "",
    )
    _print_json({"ok": True, "template": tpl.to_export_dict()})
    return 0


def cmd_win_update(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    updates: dict[str, Any] = {}
    if args.time_range is not None:
        updates["allowed_time_ranges"] = _parse_time_ranges(args.time_range)
    if args.freeze_day is not None:
        updates["freeze_days"] = args.freeze_day
    if args.approver is not None:
        updates["on_call_approvers"] = args.approver
    if args.description is not None:
        updates["default_description"] = args.description
    if not updates:
        raise ValidationError("win-update 需要至少指定一个要修改的字段")
    tpl = app.window.update_window_template(actor=app.actor, env=args.env, **updates)
    _print_json({"ok": True, "template": tpl.to_export_dict()})
    return 0


def cmd_win_delete(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    app.window.delete_window_template(actor=app.actor, env=args.env)
    _print_json({"ok": True, "env": args.env})
    return 0


def cmd_win_list(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    tpls = app.window.list_window_templates()
    if args.format == "json":
        _print_json({
            "count": len(tpls),
            "templates": [t.to_export_dict() for t in tpls],
        })
    else:
        for t in tpls:
            ranges_str = "; ".join(
                f"{r['start']}-{r['end']}{'(' + r['days'] + ')' if 'days' in r else ''}"
                for r in t.allowed_time_ranges
            ) or "(无)"
            freeze_str = ", ".join(t.freeze_days) or "(无)"
            approvers_str = ", ".join(t.on_call_approvers) or "(无)"
            print(
                f"{t.env:<12} 时段={ranges_str} 冻结日={freeze_str} "
                f"审批人={approvers_str} checksum={t.checksum}"
            )
    return 0


def cmd_win_show(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    tpl = app.window.get_window_template(env=args.env)
    if args.format == "json":
        _print_json({"ok": True, "template": tpl.to_export_dict()})
    else:
        print(f"=== 发布窗口模板 {tpl.env} ===")
        print(f"  环境          : {tpl.env}")
        print(f"  创建人        : {tpl.created_by}")
        if tpl.updated_by:
            print(f"  更新人        : {tpl.updated_by}")
        print(f"  创建时间      : {tpl.created_at}")
        if tpl.updated_at:
            print(f"  更新时间      : {tpl.updated_at}")
        print(f"  校验和        : {tpl.checksum}")
        print(f"  允许时段 ({len(tpl.allowed_time_ranges)}):")
        for r in tpl.allowed_time_ranges:
            days = f" [{r['days']}]" if "days" in r else ""
            print(f"    - {r['start']} ~ {r['end']}{days}")
        print(f"  冻结日 ({len(tpl.freeze_days)}): {', '.join(tpl.freeze_days) or '(无)'}")
        print(f"  值班审批人 ({len(tpl.on_call_approvers)}): {', '.join(tpl.on_call_approvers) or '(无)'}")
        if tpl.default_description:
            print(f"  默认说明      : {tpl.default_description}")
    return 0


def cmd_win_check(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    resp = app.window.check_window(
        actor=app.actor,
        env=args.env,
        current_time=args.at,
    )
    if args.format == "json":
        _print_json({
            "ok": True,
            "env": resp.env,
            "result": resp.result.value,
            "in_window": resp.in_window,
            "message": resp.message,
            "current_time": resp.current_time,
            "template": resp.template.to_export_dict() if resp.template else None,
            "applicable_pass": resp.applicable_pass.to_dict() if resp.applicable_pass else None,
        })
    else:
        status_icon = "✅" if resp.in_window else "❌"
        print(f"=== 窗口校验 {resp.env} ===")
        print(f"  结果: {status_icon} {resp.result.value} - {resp.message}")
        print(f"  当前时间: {resp.current_time}")
        if resp.applicable_pass:
            print(f"  可用放行单: {resp.applicable_pass.pass_id} "
                  f"(有效期 {resp.applicable_pass.valid_from} ~ {resp.applicable_pass.valid_until})")
    return 0


def cmd_win_export(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    fmt = args.format_win
    text = app.window.export_window_templates(actor=app.actor, fmt=fmt)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
        _print_json({"ok": True, "output": os.path.abspath(args.output), "format": fmt})
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_win_import(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    result = app.window.import_window_templates(actor=app.actor, path=args.file)
    _print_json({"ok": True, **result})
    return 0


# ---------------------------------------------------------------------------
# Release Pass command handlers
# ---------------------------------------------------------------------------

def cmd_pass_create(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    rp = app.window.create_pass(
        actor=app.actor,
        env=args.env,
        reason=args.reason,
        affected_switches=args.switch or [],
        valid_from=args.valid_from,
        valid_until=args.valid_until,
        approver=args.approver,
        description=args.description or "",
    )
    _print_json({"ok": True, "pass": rp.to_dict()})
    return 0


def cmd_pass_submit(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    rp = app.window.submit_pass_for_approval(actor=app.actor, pass_id=args.pass_id)
    _print_json({
        "ok": True,
        "pass_id": rp.pass_id,
        "status": rp.status.value if isinstance(rp.status, ReleasePassStatus) else rp.status,
        "submitted_at": rp.submitted_at,
    })
    return 0


def cmd_pass_approve(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    rp = app.window.approve_pass(approver=app.actor, pass_id=args.pass_id)
    _print_json({
        "ok": True,
        "pass_id": rp.pass_id,
        "status": rp.status.value if isinstance(rp.status, ReleasePassStatus) else rp.status,
        "approver": rp.approver,
        "approved_at": rp.approved_at,
    })
    return 0


def cmd_pass_reject(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    rp = app.window.reject_pass(
        rejector=app.actor,
        pass_id=args.pass_id,
        reason=args.reason,
    )
    _print_json({
        "ok": True,
        "pass_id": rp.pass_id,
        "status": rp.status.value if isinstance(rp.status, ReleasePassStatus) else rp.status,
        "rejected_by": rp.rejected_by,
        "reject_reason": rp.reject_reason,
    })
    return 0


def cmd_pass_use(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    rp = app.window.use_pass(
        actor=app.actor,
        pass_id=args.pass_id,
        order_id=args.order_id,
        current_time=args.at,
    )
    _print_json({
        "ok": True,
        "pass_id": rp.pass_id,
        "status": rp.status.value if isinstance(rp.status, ReleasePassStatus) else rp.status,
        "used_at": rp.used_at,
        "used_by": rp.used_by,
        "used_for_order_id": rp.used_for_order_id,
    })
    return 0


def cmd_pass_cancel(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    rp = app.window.cancel_pass(
        actor=app.actor,
        pass_id=args.pass_id,
        reason=args.reason,
    )
    _print_json({
        "ok": True,
        "pass_id": rp.pass_id,
        "status": rp.status.value if isinstance(rp.status, ReleasePassStatus) else rp.status,
        "cancel_reason": rp.cancel_reason,
        "cancelled_at": rp.cancelled_at,
    })
    return 0


def cmd_pass_list(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    statuses: Optional[list[ReleasePassStatus]] = None
    if args.status:
        statuses = []
        for s in args.status:
            try:
                statuses.append(ReleasePassStatus(s.upper()))
            except ValueError:
                raise ValidationError(
                    f"未知放行单状态 '{s}'，可选: {[x.value for x in ReleasePassStatus]}"
                )
    passes = app.window.list_passes(
        env=args.env,
        statuses=statuses,
        created_by=args.created_by,
        approver=args.approver,
    )
    if args.format == "json":
        _print_json({
            "count": len(passes),
            "passes": [p.to_dict() for p in passes],
        })
    else:
        for p in passes:
            st = p.status.value if isinstance(p.status, ReleasePassStatus) else p.status
            switches = ", ".join(p.affected_switches) or "(全部)"
            print(
                f"{p.pass_id:<18} {st:<18} {p.env:<10} "
                f"申请人={p.created_by:<20} 审批人={p.approver:<20} "
                f"有效期={p.valid_from}~{p.valid_until} "
                f"影响开关={switches}"
            )
    return 0


def cmd_pass_show(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    rp = app.window.get_pass(pass_id=args.pass_id)
    records = app.window.list_pass_records(pass_id=args.pass_id)
    st = rp.status.value if isinstance(rp.status, ReleasePassStatus) else rp.status
    if args.format == "json":
        _print_json({
            "ok": True,
            "pass": rp.to_dict(),
            "records": [r.to_dict() for r in records],
        })
    else:
        print(f"=== 临时放行单 {rp.pass_id} ===")
        print(f"  状态        : {st}")
        print(f"  环境        : {rp.env}")
        print(f"  申请人      : {rp.created_by}")
        print(f"  审批人      : {rp.approver}")
        print(f"  创建时间    : {rp.created_at}")
        if rp.submitted_at:
            print(f"  提交时间    : {rp.submitted_at}")
        if rp.approved_at:
            print(f"  审批通过时间: {rp.approved_at}")
        if rp.rejected_at:
            print(f"  驳回时间    : {rp.rejected_at} 原因={rp.reject_reason}")
        if rp.cancelled_at:
            print(f"  撤销时间    : {rp.cancelled_at} 原因={rp.cancel_reason}")
        if rp.used_at:
            print(f"  使用时间    : {rp.used_at} 执行人={rp.used_by} 发布单={rp.used_for_order_id}")
        print(f"  有效期      : {rp.valid_from} ~ {rp.valid_until}")
        print(f"  申请原因    : {rp.reason}")
        if rp.description:
            print(f"  详细说明    : {rp.description}")
        print(f"  影响开关 ({len(rp.affected_switches)}): {', '.join(rp.affected_switches) or '(全部)'}")
        print(f"  校验和      : {rp.checksum}")
        print(f"  操作记录 ({len(records)}):")
        for r in records:
            print(f"    {r.timestamp} {r.action:<22} {r.actor:<20} {r.details[:80]}")
    return 0


def cmd_pass_records(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    records = app.window.list_pass_records(pass_id=args.pass_id, limit=args.limit)
    if args.format == "json":
        _print_json({
            "count": len(records),
            "records": [r.to_dict() for r in records],
        })
    else:
        for r in records:
            print(
                f"{r.timestamp} {r.action:<22} {r.actor:<20} "
                f"env={r.env:<10} {r.details[:80]}"
            )
    return 0


def cmd_pass_export(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    fmt = args.format_pass
    text = app.window.export_passes(
        actor=app.actor,
        pass_ids=[args.pass_id] if args.pass_id else None,
        fmt=fmt,
    )
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
        _print_json({
            "ok": True,
            "output": os.path.abspath(args.output),
            "format": fmt,
        })
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_pass_import(args: argparse.Namespace, app: AppContext) -> Optional[int]:
    result = app.window.import_passes(actor=app.actor, path=args.file)
    _print_json({"ok": True, **result})
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def _add_env_name(p: argparse.ArgumentParser, required_name: bool = True) -> None:
    p.add_argument("--env", required=True, help="环境 (如 prod / staging / dev)")
    p.add_argument("--name", required=required_name, help="开关名")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fswitch",
        description="本地功能开关灰度台 (Feature Switch Gray Console)",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 路径 (默认: {DEFAULT_DB})")
    parser.add_argument("--as", default=DEFAULT_ACTOR, dest="actor",
                        help=f"操作人标识 (默认: {DEFAULT_ACTOR})")
    parser.add_argument("--format", choices=["table", "json"], default="table",
                        help="输出格式 (默认 table)")

    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p = sub.add_parser("create", help="创建新开关（草稿）")
    _add_env_name(p)
    p.add_argument("--ratio", type=int, required=True, help="灰度比例 0-100")
    p.add_argument("--default", type=int, choices=[0, 1], default=0,
                   help="默认值: 0=False 1=True")
    p.add_argument("--whitelist", nargs="*", default=None, help="白名单用户")
    p.add_argument("--dep", nargs="*", default=None, help="依赖开关名（同环境）")
    p.set_defaults(func=cmd_create)

    # edit
    p = sub.add_parser("edit", help="编辑草稿")
    _add_env_name(p)
    p.add_argument("--version", type=int, default=None, help="指定版本号 (默认最新草稿)")
    p.add_argument("--ratio", type=int, default=None, help="修改灰度比例")
    p.add_argument("--default", type=int, choices=[0, 1], default=None,
                   help="修改默认值")
    p.add_argument("--whitelist", nargs="*", default=None, help="覆盖白名单")
    p.add_argument("--dep", nargs="*", default=None, help="覆盖依赖开关")
    p.set_defaults(func=cmd_edit)

    # submit
    p = sub.add_parser("submit", help="提交审批: DRAFT -> PENDING_APPROVAL")
    _add_env_name(p)
    p.add_argument("--version", type=int, default=None, help="指定版本号")
    p.set_defaults(func=cmd_submit)

    # approve
    p = sub.add_parser("approve", help="审批通过并发布: PENDING_APPROVAL -> PUBLISHED")
    _add_env_name(p)
    p.add_argument("--version", type=int, default=None, help="指定版本号")
    p.add_argument("--reason", default="", help="替换上一版的原因 (可选)")
    p.set_defaults(func=cmd_approve)

    # reject
    p = sub.add_parser("reject", help="驳回审批: PENDING_APPROVAL -> DRAFT")
    _add_env_name(p)
    p.add_argument("--version", type=int, default=None)
    p.add_argument("--reason", default="", help="驳回原因")
    p.set_defaults(func=cmd_reject)

    # rollback
    p = sub.add_parser("rollback", help="回滚当前生效版本 (可指定恢复到某历史版)")
    _add_env_name(p)
    p.add_argument("--reason", required=True, help="回滚原因 (必填)")
    p.add_argument("--restore", type=int, default=None,
                   help="恢复到指定的历史版本号 (可选, 会复制为新版本发布)")
    p.set_defaults(func=cmd_rollback)

    # deprecate
    p = sub.add_parser("deprecate", help="废弃开关 (所有版本标记 DEPRECATED)")
    _add_env_name(p)
    p.add_argument("--reason", required=True, help="废弃原因 (必填)")
    p.set_defaults(func=cmd_deprecate)

    # list
    p = sub.add_parser("list", help="查询开关列表")
    p.add_argument("--env", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--status", nargs="*", default=None,
                   help="过滤状态, 如 DRAFT PUBLISHED ROLLED_BACK")
    p.add_argument("--include-deprecated", action="store_true",
                   help="包含已废弃的版本")
    p.set_defaults(func=cmd_list)

    # current
    p = sub.add_parser("current", help="查看某开关的生效版本和草稿版本")
    _add_env_name(p)
    p.set_defaults(func=cmd_current)

    # history
    p = sub.add_parser("history", help="某开关的版本变更历史 (含替换原因)")
    _add_env_name(p)
    p.set_defaults(func=cmd_history)

    # audit
    p = sub.add_parser("audit", help="审计日志")
    p.add_argument("--env", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_audit)

    # export
    p = sub.add_parser("export", help="导出生效配置为 YAML / JSON")
    p.add_argument("--env", default=None, help="只导出指定环境")
    p.add_argument("--format", choices=["yaml", "json"], default="yaml",
                   dest="format_export")
    p.add_argument("-o", "--output", default=None, help="输出文件路径")
    p.set_defaults(func=cmd_export)

    # import
    p = sub.add_parser("import", help="从 YAML/JSON 导入 (创建草稿)")
    p.add_argument("--file", required=True, help="导入文件路径")
    p.set_defaults(func=cmd_import)

    # pkg-create
    p = sub.add_parser("pkg-create", help="从源环境创建迁移包 (取当前生效版)")
    p.add_argument("--source-env", required=True, dest="source_env", help="源环境 (如 staging)")
    p.add_argument("--target-env", required=True, dest="target_env", help="目标环境 (如 prod)")
    p.add_argument("--name", nargs="*", default=None, help="只打包指定开关名 (默认所有生效版)")
    p.add_argument("--description", default="", help="迁移包描述")
    p.set_defaults(func=cmd_pkg_create)

    # pkg-preview
    p = sub.add_parser("pkg-preview", help="发布预演: 查看 diff/依赖缺口/覆盖预警/审批要求")
    p.add_argument("--package-id", required=True, dest="package_id", help="迁移包 ID")
    p.set_defaults(func=cmd_pkg_preview)

    # pkg-import
    p = sub.add_parser("pkg-import", help="导入迁移包到目标环境 (只落成 DRAFT, 不直接发布)")
    p.add_argument("--package-id", required=True, dest="package_id", help="迁移包 ID")
    p.add_argument("--force", action="store_true", help="强制导入 (绕过非冲突类阻塞, 同名草稿冲突仍拦截)")
    p.set_defaults(func=cmd_pkg_import)

    # pkg-export
    p = sub.add_parser("pkg-export", help="导出迁移包为 YAML/JSON (便于回导到新库核对)")
    p.add_argument("--package-id", required=True, dest="package_id", help="迁移包 ID")
    p.add_argument("--format", choices=["yaml", "json"], default="yaml", dest="format_pkg",
                   help="导出格式 (默认 yaml)")
    p.add_argument("-o", "--output", default=None, help="输出文件路径")
    p.set_defaults(func=cmd_pkg_export)

    # pkg-import-file
    p = sub.add_parser("pkg-import-file", help="从 YAML/JSON 文件回导迁移包定义 (不生成开关草稿)")
    p.add_argument("--file", required=True, help="迁移包文件路径")
    p.set_defaults(func=cmd_pkg_import_file)

    # pkg-list
    p = sub.add_parser("pkg-list", help="列出所有迁移包")
    p.add_argument("--source-env", default=None, dest="source_env", help="按源环境过滤")
    p.add_argument("--target-env", default=None, dest="target_env", help="按目标环境过滤")
    p.add_argument("--status", nargs="*", default=None,
                   help="按状态过滤, 如 CREATED PREVIEWED IMPORTED_DRAFT APPROVED REJECTED")
    p.set_defaults(func=cmd_pkg_list)

    # pkg-show
    p = sub.add_parser("pkg-show", help="查看迁移包详情 + 迁移记录链")
    p.add_argument("--package-id", required=True, dest="package_id", help="迁移包 ID")
    p.set_defaults(func=cmd_pkg_show)

    # pkg-approve
    p = sub.add_parser("pkg-approve", help="包级标记审批 (越权拦截: 不能审批自己创建的包)")
    p.add_argument("--package-id", required=True, dest="package_id", help="迁移包 ID")
    p.set_defaults(func=cmd_pkg_approve)

    # pkg-reject
    p = sub.add_parser("pkg-reject", help="包级标记驳回 (需填原因)")
    p.add_argument("--package-id", required=True, dest="package_id", help="迁移包 ID")
    p.add_argument("--reason", required=True, help="驳回原因")
    p.set_defaults(func=cmd_pkg_reject)

    # pkg-records
    p = sub.add_parser("pkg-records", help="查看某迁移包的完整迁移记录链 (审计)")
    p.add_argument("--package-id", required=True, dest="package_id", help="迁移包 ID")
    p.add_argument("--limit", type=int, default=100, help="返回条目上限")
    p.set_defaults(func=cmd_pkg_records)

    # rel-create
    p = sub.add_parser("rel-create", help="创建发布单（从 DRAFT/PENDING_APPROVAL 版本组单）")
    p.add_argument("--env", required=True, help="环境（所有开关必须在同一环境）")
    p.add_argument("--item", nargs="+", required=True, help="开关名:版本号，如 feature_a:1 feature_b:2")
    p.add_argument("--title", default="", help="发布单标题")
    p.add_argument("--description", default="", help="发布单描述")
    p.set_defaults(func=cmd_rel_create)

    # rel-preview
    p = sub.add_parser("rel-preview", help="发布预演：查看依赖顺序/冲突/覆盖预警/最终状态")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.set_defaults(func=cmd_rel_preview)

    # rel-submit
    p = sub.add_parser("rel-submit", help="提交审批：CREATED -> PENDING_APPROVAL")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.set_defaults(func=cmd_rel_submit)

    # rel-approve
    p = sub.add_parser("rel-approve", help="审批通过：PENDING_APPROVAL -> APPROVED（自审拦截）")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.set_defaults(func=cmd_rel_approve)

    # rel-reject
    p = sub.add_parser("rel-reject", help="驳回：PENDING_APPROVAL -> REJECTED")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.add_argument("--reason", required=True, help="驳回原因")
    p.set_defaults(func=cmd_rel_reject)

    # rel-execute
    p = sub.add_parser("rel-execute", help="执行发布单：APPROVED -> EXECUTED（原子化，事务保证）")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.set_defaults(func=cmd_rel_execute)

    # rel-rollback
    p = sub.add_parser("rel-rollback", help="整单回滚：EXECUTED -> ROLLED_BACK（反向顺序+快照恢复）")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.add_argument("--reason", required=True, help="回滚原因")
    p.set_defaults(func=cmd_rel_rollback)

    # rel-cancel
    p = sub.add_parser("rel-cancel", help="撤销发布单（未执行的单）")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.add_argument("--reason", required=True, help="撤销原因")
    p.set_defaults(func=cmd_rel_cancel)

    # rel-copy
    p = sub.add_parser("rel-copy", help="复制发布单（生成新单，状态重置为 CREATED）")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.set_defaults(func=cmd_rel_copy)

    # rel-export
    p = sub.add_parser("rel-export", help="导出发布单为 YAML/JSON（含校验和）")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.add_argument("--format", choices=["yaml", "json"], default="yaml", dest="format_rel",
                   help="导出格式 (默认 yaml)")
    p.add_argument("-o", "--output", default=None, help="输出文件路径")
    p.set_defaults(func=cmd_rel_export)

    # rel-import
    p = sub.add_parser("rel-import", help="从 YAML/JSON 导入发布单（校验 schema 和校验和）")
    p.add_argument("--file", required=True, help="发布单文件路径")
    p.set_defaults(func=cmd_rel_import)

    # rel-list
    p = sub.add_parser("rel-list", help="列出所有发布单")
    p.add_argument("--env", default=None, help="按环境过滤")
    p.add_argument("--status", nargs="*", default=None,
                   help="按状态过滤，如 CREATED PENDING_APPROVAL APPROVED EXECUTED ROLLED_BACK")
    p.set_defaults(func=cmd_rel_list)

    # rel-show
    p = sub.add_parser("rel-show", help="查看发布单详情 + 操作记录链")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.set_defaults(func=cmd_rel_show)

    # rel-records
    p = sub.add_parser("rel-records", help="查看某发布单的完整操作记录链（审计）")
    p.add_argument("--order-id", required=True, dest="order_id", help="发布单 ID")
    p.add_argument("--limit", type=int, default=100, help="返回条目上限")
    p.set_defaults(func=cmd_rel_records)

    # win-create
    p = sub.add_parser("win-create", help="创建发布窗口模板（仅管理员）")
    p.add_argument("--env", required=True, help="环境 (如 prod / staging)")
    p.add_argument("--time-range", nargs="+", required=True, dest="time_range",
                   help="允许时段，格式 HH:MM:HH:MM[:days]，如 09:00:18:00 或 09:00:18:00:monday-friday")
    p.add_argument("--freeze-day", nargs="*", default=None, dest="freeze_day",
                   help="冻结日，格式 YYYY-MM-DD")
    p.add_argument("--approver", nargs="*", default=None, dest="approver",
                   help="值班审批人邮箱")
    p.add_argument("--description", default=None, help="默认说明")
    p.set_defaults(func=cmd_win_create)

    # win-update
    p = sub.add_parser("win-update", help="更新发布窗口模板（仅管理员）")
    p.add_argument("--env", required=True, help="环境")
    p.add_argument("--time-range", nargs="*", default=None, dest="time_range",
                   help="覆盖允许时段，格式 HH:MM:HH:MM[:days]")
    p.add_argument("--freeze-day", nargs="*", default=None, dest="freeze_day",
                   help="覆盖冻结日列表")
    p.add_argument("--approver", nargs="*", default=None, dest="approver",
                   help="覆盖值班审批人列表")
    p.add_argument("--description", default=None, help="更新默认说明")
    p.set_defaults(func=cmd_win_update)

    # win-delete
    p = sub.add_parser("win-delete", help="删除发布窗口模板（仅管理员）")
    p.add_argument("--env", required=True, help="环境")
    p.set_defaults(func=cmd_win_delete)

    # win-list
    p = sub.add_parser("win-list", help="列出所有发布窗口模板")
    p.set_defaults(func=cmd_win_list)

    # win-show
    p = sub.add_parser("win-show", help="查看某环境的发布窗口模板详情")
    p.add_argument("--env", required=True, help="环境")
    p.set_defaults(func=cmd_win_show)

    # win-check
    p = sub.add_parser("win-check", help="校验当前时间是否在发布窗口内")
    p.add_argument("--env", required=True, help="环境")
    p.add_argument("--at", default=None, help="指定校验时间 (ISO 格式，默认当前时间)")
    p.set_defaults(func=cmd_win_check)

    # win-export
    p = sub.add_parser("win-export", help="导出所有窗口模板为 YAML/JSON")
    p.add_argument("--format", choices=["yaml", "json"], default="yaml", dest="format_win",
                   help="导出格式 (默认 yaml)")
    p.add_argument("-o", "--output", default=None, help="输出文件路径")
    p.set_defaults(func=cmd_win_export)

    # win-import
    p = sub.add_parser("win-import", help="从 YAML/JSON 导入窗口模板（仅管理员）")
    p.add_argument("--file", required=True, help="导入文件路径")
    p.set_defaults(func=cmd_win_import)

    # pass-create
    p = sub.add_parser("pass-create", help="创建临时放行单（草稿）")
    p.add_argument("--env", required=True, help="环境")
    p.add_argument("--reason", required=True, help="放行原因")
    p.add_argument("--switch", nargs="*", default=None, help="影响的开关名（不指定则为全部）")
    p.add_argument("--valid-from", required=True, dest="valid_from",
                   help="生效开始时间 (ISO 格式)")
    p.add_argument("--valid-until", required=True, dest="valid_until",
                   help="生效截止时间 (ISO 格式)")
    p.add_argument("--approver", required=True, help="审批人邮箱")
    p.add_argument("--description", default=None, help="详细说明")
    p.set_defaults(func=cmd_pass_create)

    # pass-submit
    p = sub.add_parser("pass-submit", help="提交放行单审批：DRAFT -> PENDING_APPROVAL")
    p.add_argument("--pass-id", required=True, dest="pass_id", help="放行单 ID")
    p.set_defaults(func=cmd_pass_submit)

    # pass-approve
    p = sub.add_parser("pass-approve", help="审批通过放行单（自审拦截）")
    p.add_argument("--pass-id", required=True, dest="pass_id", help="放行单 ID")
    p.set_defaults(func=cmd_pass_approve)

    # pass-reject
    p = sub.add_parser("pass-reject", help="驳回放行单")
    p.add_argument("--pass-id", required=True, dest="pass_id", help="放行单 ID")
    p.add_argument("--reason", required=True, help="驳回原因")
    p.set_defaults(func=cmd_pass_reject)

    # pass-use
    p = sub.add_parser("pass-use", help="使用放行单（一次性，状态变 USED）")
    p.add_argument("--pass-id", required=True, dest="pass_id", help="放行单 ID")
    p.add_argument("--order-id", required=True, dest="order_id", help="关联的发布单 ID")
    p.add_argument("--at", default=None, help="指定使用时间 (ISO 格式，默认当前时间)")
    p.set_defaults(func=cmd_pass_use)

    # pass-cancel
    p = sub.add_parser("pass-cancel", help="撤销未生效的放行单")
    p.add_argument("--pass-id", required=True, dest="pass_id", help="放行单 ID")
    p.add_argument("--reason", required=True, help="撤销原因")
    p.set_defaults(func=cmd_pass_cancel)

    # pass-list
    p = sub.add_parser("pass-list", help="列出放行单（可按状态/环境/申请人/审批人过滤）")
    p.add_argument("--env", default=None, help="按环境过滤")
    p.add_argument("--status", nargs="*", default=None,
                   help="按状态过滤，如 DRAFT PENDING_APPROVAL APPROVED USED REJECTED CANCELLED EXPIRED")
    p.add_argument("--created-by", default=None, dest="created_by", help="按申请人过滤")
    p.add_argument("--approver", default=None, help="按审批人过滤")
    p.set_defaults(func=cmd_pass_list)

    # pass-show
    p = sub.add_parser("pass-show", help="查看放行单详情 + 操作记录链")
    p.add_argument("--pass-id", required=True, dest="pass_id", help="放行单 ID")
    p.set_defaults(func=cmd_pass_show)

    # pass-records
    p = sub.add_parser("pass-records", help="查看某放行单的完整操作记录链（审计）")
    p.add_argument("--pass-id", required=True, dest="pass_id", help="放行单 ID")
    p.add_argument("--limit", type=int, default=100, help="返回条目上限")
    p.set_defaults(func=cmd_pass_records)

    # pass-export
    p = sub.add_parser("pass-export", help="导出行单为 YAML/JSON")
    p.add_argument("--pass-id", default=None, dest="pass_id", help="指定放行单 ID（不指定则导出全部）")
    p.add_argument("--format", choices=["yaml", "json"], default="yaml", dest="format_pass",
                   help="导出格式 (默认 yaml)")
    p.add_argument("-o", "--output", default=None, help="输出文件路径")
    p.set_defaults(func=cmd_pass_export)

    # pass-import
    p = sub.add_parser("pass-import", help="从 YAML/JSON 导出行单")
    p.add_argument("--file", required=True, help="导入文件路径")
    p.set_defaults(func=cmd_pass_import)

    return parser


def cli(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # export subparser overrides --format
    if hasattr(args, "format_export"):
        args.format = args.format_export
    if hasattr(args, "format_pkg"):
        args.format = args.format_pkg
    if hasattr(args, "format_rel"):
        args.format = args.format_rel
    if hasattr(args, "format_win"):
        args.format = args.format_win
    if hasattr(args, "format_pass"):
        args.format = args.format_pass

    app = build_app(db_path=args.db, actor=args.actor)
    try:
        code = args.func(args, app)
        return code if code is not None else 0
    except ValidationError as exc:
        _print_json({"ok": False, "error": "VALIDATION", "message": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001
        _print_json({
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        })
        return 1
    finally:
        app.close()


def _main_entry() -> None:  # for console_scripts
    raise SystemExit(cli())


if __name__ == "__main__":
    _main_entry()
