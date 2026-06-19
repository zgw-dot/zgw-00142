from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Optional

from ..audit import AuditTrail
from ..core.enums import MigrationStatus, VersionStatus
from ..core.models import SwitchVersion
from ..service import ConfigExporter, ConfigImporter, MigrationService, SwitchService
from ..storage.repository import SwitchRepository
from ..validator.validators import ValidationError


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

    return parser


def cli(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # export subparser overrides --format
    if hasattr(args, "format_export"):
        args.format = args.format_export

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
