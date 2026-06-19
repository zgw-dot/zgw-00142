from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Optional

from ..audit import AuditTrail
from ..core.enums import VersionStatus
from ..core.models import SwitchVersion
from ..service import ConfigExporter, ConfigImporter, SwitchService
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
