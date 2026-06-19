from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any, Optional

from ..audit import AuditTrail
from ..core.enums import (
    AuditAction,
    ReleasePassStatus,
    WindowCheckResult,
)
from ..core.models import (
    ReleaseWindowTemplate,
    ReleasePass,
    ReleasePassRecord,
    WindowCheckResponse,
    _RELEASE_WINDOW_SCHEMA_VERSION,
    _RELEASE_PASS_SCHEMA_VERSION,
    _now_iso,
)
from ..storage.repository import SwitchRepository
from ..validator.validators import (
    ValidationError,
    parse_json,
    parse_yaml,
    validate_release_admin_role,
    validate_release_not_self_approve,
    validate_release_pass_not_self_approve,
    validate_release_pass_payload,
    validate_release_pass_transition,
    validate_release_window_payload,
)

try:
    import yaml as _yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


def _dump_yaml(data: Any) -> str:
    if _HAS_YAML:
        return _yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return json.dumps(data, ensure_ascii=False, indent=2)


def _compute_window_checksum(
    env: str,
    allowed_time_ranges: list[dict[str, str]],
    freeze_days: list[str],
    on_call_approvers: list[str],
    default_description: str,
) -> str:
    payload: dict[str, Any] = {
        "env": env,
        "allowed_time_ranges": sorted(
            [dict(r) for r in allowed_time_ranges],
            key=lambda x: (x.get("start", ""), x.get("end", "")),
        ),
        "freeze_days": sorted(freeze_days),
        "on_call_approvers": sorted(on_call_approvers),
        "default_description": default_description,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _compute_pass_checksum(
    env: str,
    reason: str,
    affected_switches: list[str],
    valid_from: str,
    valid_until: str,
    approver: str,
    description: str,
) -> str:
    payload: dict[str, Any] = {
        "env": env,
        "reason": reason,
        "affected_switches": sorted(affected_switches),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "approver": approver,
        "description": description,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _parse_iso_datetime(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_days(days_str: str) -> set[str]:
    """解析 days 字符串，支持格式：'monday', 'monday-friday', 'monday,wednesday', 'all'"""
    days_str = days_str.lower().strip()
    if not days_str or days_str == "all":
        return set(_WEEKDAYS)

    result: set[str] = set()
    for part in days_str.split(","):
        part = part.strip()
        if "-" in part and part.count("-") == 1:
            start_day, end_day = part.split("-", 1)
            start_day = start_day.strip()
            end_day = end_day.strip()
            if start_day in _WEEKDAYS and end_day in _WEEKDAYS:
                start_idx = _WEEKDAYS.index(start_day)
                end_idx = _WEEKDAYS.index(end_day)
                if start_idx <= end_idx:
                    result.update(_WEEKDAYS[start_idx : end_idx + 1])
                else:
                    result.update(_WEEKDAYS[start_idx:])
                    result.update(_WEEKDAYS[: end_idx + 1])
        elif part in _WEEKDAYS:
            result.add(part)
    return result


def _is_time_in_ranges(
    current_time: datetime,
    allowed_time_ranges: list[dict[str, str]],
) -> bool:
    current_weekday = current_time.strftime("%A").lower()
    current_hm = current_time.strftime("%H:%M")

    for tr in allowed_time_ranges:
        days = tr.get("days", "")
        allowed_days = _parse_days(days)
        if current_weekday not in allowed_days:
            continue
        start = tr.get("start", "00:00")
        end = tr.get("end", "23:59")
        if start <= current_hm <= end:
            return True
    return False


def _is_freeze_day(current_time: datetime, freeze_days: list[str]) -> bool:
    current_date = current_time.strftime("%Y-%m-%d")
    return current_date in freeze_days


class ReleaseWindowService:
    """发布窗口 + 临时放行单服务。

    工作流：
      1. 管理员配置发布窗口模板（可发布时间段、冻结日、值班审批人）
      2. 开发提发布前先调用 check_window 校验是否在窗口内
      3. 不在窗口内只能提放行单，经审批后才能执行一次
      4. 支持查询：环境模板、待审批、已使用、已过期
      5. 支持 YAML/JSON 导入导出、重复申请拦截、撤销、审计日志
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

    @staticmethod
    def _resolve_pass_status(
        pass_obj: ReleasePass,
        current_time: str,
    ) -> ReleasePassStatus:
        if pass_obj.status == ReleasePassStatus.APPROVED and pass_obj.valid_until < current_time:
            return ReleasePassStatus.EXPIRED
        return pass_obj.status

    def _require_window_template(self, env: str) -> ReleaseWindowTemplate:
        template = self.repo.get_release_window_template(env)
        if template is None:
            raise ValidationError(
                f"环境 '{env}' 没有配置发布窗口模板", field="env")
        return template

    def _require_pass(self, pass_id: str) -> ReleasePass:
        pass_obj = self.repo.get_release_pass(pass_id)
        if pass_obj is None:
            raise ValidationError(f"放行单不存在: {pass_id}", field="pass_id")
        return pass_obj

    def _append_pass_record(
        self,
        *,
        pass_id: str,
        action: str,
        actor: str,
        env: str,
        details: str = "",
        conn: Any = None,
    ) -> None:
        rec = ReleasePassRecord(
            pass_id=pass_id,
            action=action,
            actor=actor,
            env=env,
            details=details,
            timestamp=_now_iso(),
        )
        self.repo.append_release_pass_record(rec, conn=conn)

    # ------------------------------------------------------------------
    # 1. Release Window Template (发布窗口模板)
    # ------------------------------------------------------------------

    def create_window_template(
        self,
        actor: str,
        *,
        env: str,
        allowed_time_ranges: list[dict[str, str]],
        freeze_days: list[str],
        on_call_approvers: list[str],
        default_description: str = "",
    ) -> ReleaseWindowTemplate:
        """创建发布窗口模板。仅管理员可操作。"""
        validate_release_admin_role(actor, self._is_admin(actor))

        payload = validate_release_window_payload({
            "env": env,
            "allowed_time_ranges": allowed_time_ranges,
            "freeze_days": freeze_days,
            "on_call_approvers": on_call_approvers,
            "default_description": default_description,
        })

        existing = self.repo.get_release_window_template(payload["env"])
        if existing is not None:
            raise ValidationError(
                f"环境 '{payload['env']}' 已存在发布窗口模板，"
                f"请使用 update 命令修改"
            )

        checksum = _compute_window_checksum(
            payload["env"],
            payload["allowed_time_ranges"],
            payload["freeze_days"],
            payload["on_call_approvers"],
            payload["default_description"],
        )

        template = ReleaseWindowTemplate(
            env=payload["env"],
            allowed_time_ranges=payload["allowed_time_ranges"],
            freeze_days=payload["freeze_days"],
            on_call_approvers=payload["on_call_approvers"],
            default_description=payload["default_description"],
            created_by=actor,
            checksum=checksum,
        )

        with self.repo.transaction() as conn:
            template = self.repo.insert_release_window_template(template, conn=conn)
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_CREATE,
                env=template.env,
                switch_name="__window__",
                details={
                    "env": template.env,
                    "allowed_time_ranges": template.allowed_time_ranges,
                    "freeze_days": template.freeze_days,
                    "on_call_approvers": template.on_call_approvers,
                    "checksum": checksum,
                },
                conn=conn,
            )

        return template

    def update_window_template(
        self,
        actor: str,
        *,
        env: str,
        allowed_time_ranges: Optional[list[dict[str, str]]] = None,
        freeze_days: Optional[list[str]] = None,
        on_call_approvers: Optional[list[str]] = None,
        default_description: Optional[str] = None,
    ) -> ReleaseWindowTemplate:
        """更新发布窗口模板。仅管理员可操作。"""
        validate_release_admin_role(actor, self._is_admin(actor))

        template = self._require_window_template(env)

        updates: dict[str, Any] = {}
        new_allowed = allowed_time_ranges if allowed_time_ranges is not None else template.allowed_time_ranges
        new_freeze = freeze_days if freeze_days is not None else template.freeze_days
        new_approvers = on_call_approvers if on_call_approvers is not None else template.on_call_approvers
        new_desc = default_description if default_description is not None else template.default_description

        if allowed_time_ranges is not None:
            for tr in allowed_time_ranges:
                from ..validator.validators import validate_time_range_format
                validate_time_range_format(tr)
            updates["allowed_time_ranges"] = allowed_time_ranges

        if freeze_days is not None:
            for day in freeze_days:
                from ..validator.validators import validate_freeze_day_format
                validate_freeze_day_format(day)
            updates["freeze_days"] = freeze_days

        if on_call_approvers is not None:
            if not on_call_approvers:
                raise ValidationError("on_call_approvers 不能为空", field="on_call_approvers")
            updates["on_call_approvers"] = on_call_approvers

        if default_description is not None:
            updates["default_description"] = default_description

        if not updates:
            return template

        new_checksum = _compute_window_checksum(
            env, new_allowed, new_freeze, new_approvers, new_desc
        )
        updates["checksum"] = new_checksum
        updates["updated_by"] = actor
        updates["updated_at"] = _now_iso()

        with self.repo.transaction() as conn:
            self.repo.update_release_window_template(env, updates, conn=conn)
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_UPDATE,
                env=env,
                switch_name="__window__",
                details={
                    "updates": updates,
                    "old_checksum": template.checksum,
                    "new_checksum": new_checksum,
                },
                conn=conn,
            )

        return self._require_window_template(env)

    def delete_window_template(
        self,
        actor: str,
        *,
        env: str,
    ) -> None:
        """删除发布窗口模板。仅管理员可操作。"""
        validate_release_admin_role(actor, self._is_admin(actor))

        template = self._require_window_template(env)

        with self.repo.transaction() as conn:
            self.repo.delete_release_window_template(env, conn=conn)
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_DELETE,
                env=env,
                switch_name="__window__",
                details={
                    "env": env,
                    "old_checksum": template.checksum,
                },
                conn=conn,
            )

    def get_window_template(self, env: str) -> Optional[ReleaseWindowTemplate]:
        """获取环境的发布窗口模板。"""
        return self.repo.get_release_window_template(env)

    def list_window_templates(self) -> list[ReleaseWindowTemplate]:
        """列出所有发布窗口模板。"""
        return self.repo.list_release_window_templates()

    def check_window(
        self,
        actor: str,
        *,
        env: str,
        current_time: Optional[str] = None,
    ) -> WindowCheckResponse:
        """检查当前时间是否在发布窗口内。

        返回 WindowCheckResponse，包含：
        - result: IN_WINDOW / OUT_OF_WINDOW / FREEZE_DAY / NO_TEMPLATE
        - in_window: 是否在窗口内（含有效放行单）
        - message: 人类可读信息
        - applicable_pass: 如存在有效放行单则返回
        """
        if current_time is None:
            current_time = _now_iso()

        template = self.repo.get_release_window_template(env)
        if template is None:
            result = WindowCheckResponse(
                env=env,
                result=WindowCheckResult.NO_TEMPLATE,
                in_window=True,
                message=f"环境 '{env}' 未配置发布窗口模板，不限制发布",
                current_time=current_time,
                template=None,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_CHECK,
                env=env,
                switch_name="__window__",
                details={
                    "result": result.result.value,
                    "current_time": current_time,
                    "in_window": result.in_window,
                },
            )
            return result

        try:
            dt = _parse_iso_datetime(current_time)
        except ValueError as exc:
            raise ValidationError(f"current_time 格式错误: {exc}", field="current_time")

        # 优先检查是否有有效放行单（放行单可以覆盖冻结日和时间限制）
        active_pass = self.repo.get_active_approved_pass(env, current_time)
        if active_pass is not None:
            result = WindowCheckResponse(
                env=env,
                result=WindowCheckResult.IN_WINDOW,
                in_window=True,
                message=(
                    f"当前时间通过有效放行单 {active_pass.pass_id} 允许发布"
                    f"（审批人: {active_pass.approver}，"
                    f"有效期: {active_pass.valid_from} ~ {active_pass.valid_until}）"
                ),
                current_time=current_time,
                template=template,
                applicable_pass=active_pass,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_CHECK,
                env=env,
                switch_name="__window__",
                details={
                    "result": result.result.value,
                    "current_time": current_time,
                    "in_window": result.in_window,
                    "applicable_pass": active_pass.pass_id,
                },
            )
            return result

        # 没有放行单时，检查冻结日
        if _is_freeze_day(dt, template.freeze_days):
            result = WindowCheckResponse(
                env=env,
                result=WindowCheckResult.FREEZE_DAY,
                in_window=False,
                message=f"当前日期 {dt.strftime('%Y-%m-%d')} 是冻结日，禁止发布。请申请临时放行单。",
                current_time=current_time,
                template=template,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_CHECK,
                env=env,
                switch_name="__window__",
                details={
                    "result": result.result.value,
                    "current_time": current_time,
                    "in_window": result.in_window,
                },
            )
            return result

        # 检查是否在允许的时间范围内
        if _is_time_in_ranges(dt, template.allowed_time_ranges):
            result = WindowCheckResponse(
                env=env,
                result=WindowCheckResult.IN_WINDOW,
                in_window=True,
                message=f"当前时间在允许的发布窗口内",
                current_time=current_time,
                template=template,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_CHECK,
                env=env,
                switch_name="__window__",
                details={
                    "result": result.result.value,
                    "current_time": current_time,
                    "in_window": result.in_window,
                },
            )
            return result

        # 不在时间范围内且无放行单
        result = WindowCheckResponse(
            env=env,
            result=WindowCheckResult.OUT_OF_WINDOW,
            in_window=False,
            message=(
                f"当前时间不在发布窗口内，且无有效放行单。"
                f"请申请临时放行单或在窗口时间内发布。"
            ),
            current_time=current_time,
            template=template,
        )
        self.audit.record(
            actor=actor,
            action=AuditAction.RELEASE_WINDOW_CHECK,
            env=env,
            switch_name="__window__",
            details={
                "result": result.result.value,
                "current_time": current_time,
                "in_window": result.in_window,
            },
        )
        return result

    # ------------------------------------------------------------------
    # 2. Release Pass (临时放行单)
    # ------------------------------------------------------------------

    def create_pass(
        self,
        actor: str,
        *,
        env: str,
        reason: str,
        affected_switches: list[str],
        valid_from: str,
        valid_until: str,
        approver: str,
        description: str = "",
        skip_duplicate_check: bool = False,
        pass_id: Optional[str] = None,
    ) -> ReleasePass:
        """创建临时放行单。任何开发都可以创建。"""
        payload = validate_release_pass_payload({
            "env": env,
            "reason": reason,
            "affected_switches": affected_switches,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "approver": approver,
            "description": description,
        })

        checksum = _compute_pass_checksum(
            payload["env"],
            payload["reason"],
            payload["affected_switches"],
            payload["valid_from"],
            payload["valid_until"],
            payload["approver"],
            payload["description"],
        )

        if not skip_duplicate_check:
            dup = self.repo.find_release_pass_by_checksum(payload["env"], checksum)
            if dup is not None and dup.status not in (
                ReleasePassStatus.CANCELLED,
                ReleasePassStatus.REJECTED,
                ReleasePassStatus.USED,
                ReleasePassStatus.EXPIRED,
            ):
                raise ValidationError(
                    f"相同内容的放行单已存在（pass_id={dup.pass_id}，"
                    f"状态={dup.status.value}）。请直接复用该单。"
                )

        if pass_id:
            existing = self.repo.get_release_pass(pass_id)
            if existing is not None:
                raise ValidationError(
                    f"放行单 pass_id={pass_id} 已存在，无法重复创建"
                )
        else:
            pass_id = f"pass-{uuid.uuid4().hex[:12]}"

        # 检查时间冲突：同一环境、同一时间段内不能有重叠的 APPROVED 放行单
        existing_approved = self.repo.list_release_passes(
            env=payload["env"],
            statuses=[ReleasePassStatus.APPROVED],
        )
        for ep in existing_approved:
            if not (ep.valid_until < payload["valid_from"] or ep.valid_from > payload["valid_until"]):
                raise ValidationError(
                    f"时间冲突：环境 '{payload['env']}' 在 {payload['valid_from']} "
                    f"至 {payload['valid_until']} 期间已有生效的放行单 "
                    f"{ep.pass_id}（审批人: {ep.approver}）"
                )

        pass_obj = ReleasePass(
            pass_id=pass_id,
            env=payload["env"],
            created_by=actor,
            reason=payload["reason"],
            affected_switches=payload["affected_switches"],
            valid_from=payload["valid_from"],
            valid_until=payload["valid_until"],
            approver=payload["approver"],
            status=ReleasePassStatus.DRAFT,
            description=payload["description"],
            checksum=checksum,
        )

        with self.repo.transaction() as conn:
            pass_obj = self.repo.insert_release_pass(pass_obj, conn=conn)
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_PASS_CREATE,
                env=pass_obj.env,
                switch_name="__pass__",
                details={
                    "pass_id": pass_id,
                    "env": pass_obj.env,
                    "reason": pass_obj.reason,
                    "affected_switches": pass_obj.affected_switches,
                    "valid_from": pass_obj.valid_from,
                    "valid_until": pass_obj.valid_until,
                    "approver": pass_obj.approver,
                    "checksum": checksum,
                },
                conn=conn,
            )
            self._append_pass_record(
                pass_id=pass_id,
                action=AuditAction.RELEASE_PASS_CREATE.value,
                actor=actor,
                env=pass_obj.env,
                details=json.dumps({
                    "reason": pass_obj.reason,
                    "affected_switches": pass_obj.affected_switches,
                    "valid_from": pass_obj.valid_from,
                    "valid_until": pass_obj.valid_until,
                    "approver": pass_obj.approver,
                    "checksum": checksum,
                }, ensure_ascii=False),
                conn=conn,
            )

        return pass_obj

    def submit_pass_for_approval(
        self,
        actor: str,
        *,
        pass_id: str,
    ) -> ReleasePass:
        """提交放行单审批。创建人提交。"""
        pass_obj = self._require_pass(pass_id)

        if pass_obj.created_by != actor:
            raise ValidationError(
                f"只有创建人 '{pass_obj.created_by}' 可以提交审批",
                field="actor",
            )

        validate_release_pass_transition(pass_obj.status, ReleasePassStatus.PENDING_APPROVAL)

        with self.repo.transaction() as conn:
            self.repo.update_release_pass_fields(
                pass_id,
                {
                    "status": ReleasePassStatus.PENDING_APPROVAL,
                    "submitted_at": _now_iso(),
                },
                conn=conn,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_PASS_SUBMIT,
                env=pass_obj.env,
                switch_name="__pass__",
                details={
                    "pass_id": pass_id,
                    "approver": pass_obj.approver,
                },
                conn=conn,
            )
            self._append_pass_record(
                pass_id=pass_id,
                action=AuditAction.RELEASE_PASS_SUBMIT.value,
                actor=actor,
                env=pass_obj.env,
                details=json.dumps({
                    "approver": pass_obj.approver,
                    "previous_status": pass_obj.status.value,
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_pass(pass_id)

    def approve_pass(
        self,
        approver: str,
        *,
        pass_id: str,
    ) -> ReleasePass:
        """审批放行单。不能审批自己创建的单。"""
        pass_obj = self._require_pass(pass_id)

        validate_release_pass_transition(pass_obj.status, ReleasePassStatus.APPROVED)

        if pass_obj.approver != approver:
            raise ValidationError(
                f"此放行单指定审批人是 '{pass_obj.approver}'，"
                f"'{approver}' 无权审批",
                field="approver",
            )

        validate_release_pass_not_self_approve(pass_obj.created_by, approver)
        validate_release_not_self_approve(pass_obj.created_by, approver)

        with self.repo.transaction() as conn:
            self.repo.update_release_pass_fields(
                pass_id,
                {
                    "status": ReleasePassStatus.APPROVED,
                    "approved_at": _now_iso(),
                },
                conn=conn,
            )
            self.audit.record(
                actor=approver,
                action=AuditAction.RELEASE_PASS_APPROVE,
                env=pass_obj.env,
                switch_name="__pass__",
                details={
                    "pass_id": pass_id,
                    "created_by": pass_obj.created_by,
                    "affected_switches": pass_obj.affected_switches,
                },
                conn=conn,
            )
            self._append_pass_record(
                pass_id=pass_id,
                action=AuditAction.RELEASE_PASS_APPROVE.value,
                actor=approver,
                env=pass_obj.env,
                details=json.dumps({
                    "created_by": pass_obj.created_by,
                    "valid_from": pass_obj.valid_from,
                    "valid_until": pass_obj.valid_until,
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_pass(pass_id)

    def reject_pass(
        self,
        rejector: str,
        *,
        pass_id: str,
        reason: str,
    ) -> ReleasePass:
        """驳回放行单。"""
        pass_obj = self._require_pass(pass_id)

        validate_release_pass_transition(pass_obj.status, ReleasePassStatus.REJECTED)

        if pass_obj.approver != rejector:
            raise ValidationError(
                f"此放行单指定审批人是 '{pass_obj.approver}'，"
                f"'{rejector}' 无权驳回",
                field="rejector",
            )

        if not reason:
            raise ValidationError("驳回必须提供原因", field="reason")

        with self.repo.transaction() as conn:
            self.repo.update_release_pass_fields(
                pass_id,
                {
                    "status": ReleasePassStatus.REJECTED,
                    "rejected_by": rejector,
                    "reject_reason": reason,
                    "rejected_at": _now_iso(),
                },
                conn=conn,
            )
            self.audit.record(
                actor=rejector,
                action=AuditAction.RELEASE_PASS_REJECT,
                env=pass_obj.env,
                switch_name="__pass__",
                details={
                    "pass_id": pass_id,
                    "reason": reason,
                    "created_by": pass_obj.created_by,
                },
                conn=conn,
            )
            self._append_pass_record(
                pass_id=pass_id,
                action=AuditAction.RELEASE_PASS_REJECT.value,
                actor=rejector,
                env=pass_obj.env,
                details=json.dumps({
                    "reason": reason,
                    "created_by": pass_obj.created_by,
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_pass(pass_id)

    def use_pass(
        self,
        actor: str,
        *,
        pass_id: str,
        order_id: str,
        current_time: Optional[str] = None,
    ) -> ReleasePass:
        """使用放行单（标记为已使用）。执行发布单时调用。"""
        pass_obj = self._require_pass(pass_id)

        validate_release_pass_transition(pass_obj.status, ReleasePassStatus.USED)

        if current_time is None:
            current_time = _now_iso()

        if pass_obj.valid_from > current_time or pass_obj.valid_until < current_time:
            raise ValidationError(
                f"放行单已过期或未生效。有效期: {pass_obj.valid_from} "
                f"至 {pass_obj.valid_until}，当前时间: {current_time}"
            )

        with self.repo.transaction() as conn:
            self.repo.update_release_pass_fields(
                pass_id,
                {
                    "status": ReleasePassStatus.USED,
                    "used_at": current_time,
                    "used_by": actor,
                    "used_for_order_id": order_id,
                },
                conn=conn,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_PASS_USE,
                env=pass_obj.env,
                switch_name="__pass__",
                details={
                    "pass_id": pass_id,
                    "order_id": order_id,
                    "affected_switches": pass_obj.affected_switches,
                },
                conn=conn,
            )
            self._append_pass_record(
                pass_id=pass_id,
                action=AuditAction.RELEASE_PASS_USE.value,
                actor=actor,
                env=pass_obj.env,
                details=json.dumps({
                    "order_id": order_id,
                    "current_time": current_time,
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_pass(pass_id)

    def cancel_pass(
        self,
        actor: str,
        *,
        pass_id: str,
        reason: str,
    ) -> ReleasePass:
        """撤销放行单（未生效的可以撤销）。"""
        pass_obj = self._require_pass(pass_id)

        validate_release_pass_transition(pass_obj.status, ReleasePassStatus.CANCELLED)

        if not reason:
            raise ValidationError("撤销必须提供原因", field="reason")

        if pass_obj.created_by != actor and not self._is_admin(actor):
            raise ValidationError(
                f"只有创建人 '{pass_obj.created_by}' 或管理员可以撤销",
                field="actor",
            )

        with self.repo.transaction() as conn:
            self.repo.update_release_pass_fields(
                pass_id,
                {
                    "status": ReleasePassStatus.CANCELLED,
                    "cancel_reason": reason,
                    "cancelled_at": _now_iso(),
                },
                conn=conn,
            )
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_PASS_CANCEL,
                env=pass_obj.env,
                switch_name="__pass__",
                details={
                    "pass_id": pass_id,
                    "reason": reason,
                    "previous_status": pass_obj.status.value,
                },
                conn=conn,
            )
            self._append_pass_record(
                pass_id=pass_id,
                action=AuditAction.RELEASE_PASS_CANCEL.value,
                actor=actor,
                env=pass_obj.env,
                details=json.dumps({
                    "reason": reason,
                    "previous_status": pass_obj.status.value,
                }, ensure_ascii=False),
                conn=conn,
            )

        return self._require_pass(pass_id)

    def mark_expired_passes(
        self,
        current_time: Optional[str] = None,
    ) -> int:
        """标记过期的放行单。定期清理用。"""
        if current_time is None:
            current_time = _now_iso()

        approved_passes = self.repo.list_release_passes(
            statuses=[ReleasePassStatus.APPROVED],
        )
        count = 0
        for pass_obj in approved_passes:
            if self._resolve_pass_status(pass_obj, current_time) == ReleasePassStatus.EXPIRED:
                self.repo.update_release_pass_fields(
                    pass_obj.pass_id,
                    {"status": ReleasePassStatus.EXPIRED},
                )
                count += 1
        return count

    # ------------------------------------------------------------------
    # 3. Import / Export
    # ------------------------------------------------------------------

    def export_window_template(
        self,
        actor: str,
        *,
        env: str,
        fmt: str = "yaml",
    ) -> str:
        """导出发布窗口模板为 YAML 或 JSON。"""
        template = self._require_window_template(env)
        data = template.to_export_dict()

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_EXPORT,
                env=env,
                switch_name="__window__",
                details={"env": env, "format": fmt},
                conn=conn,
            )

        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return _dump_yaml(data)

    def import_window_template(
        self,
        actor: str,
        *,
        path: str,
    ) -> ReleaseWindowTemplate:
        """从 YAML/JSON 文件导入发布窗口模板。"""
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

        return self._load_window_document(actor=actor, doc=doc, source=path)

    def _load_window_document(
        self, *, actor: str, doc: Any, source: str
    ) -> ReleaseWindowTemplate:
        if not isinstance(doc, dict):
            raise ValidationError("窗口模板文件根节点必须是对象 (mapping)")

        schema = doc.get("schema_version")
        if schema != _RELEASE_WINDOW_SCHEMA_VERSION:
            raise ValidationError(
                f"窗口模板 schema_version 不匹配: 期望 {_RELEASE_WINDOW_SCHEMA_VERSION!r}，收到 {schema!r}"
            )

        env = doc.get("env")
        allowed_time_ranges = doc.get("allowed_time_ranges", [])
        freeze_days = doc.get("freeze_days", [])
        on_call_approvers = doc.get("on_call_approvers", [])
        default_description = doc.get("default_description", "")
        checksum = doc.get("checksum", "")

        existing = self.repo.get_release_window_template(env)
        if existing is not None:
            return self.update_window_template(
                actor,
                env=env,
                allowed_time_ranges=allowed_time_ranges,
                freeze_days=freeze_days,
                on_call_approvers=on_call_approvers,
                default_description=default_description,
            )

        template = self.create_window_template(
            actor,
            env=env,
            allowed_time_ranges=allowed_time_ranges,
            freeze_days=freeze_days,
            on_call_approvers=on_call_approvers,
            default_description=default_description,
        )

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_IMPORT,
                env=env,
                switch_name="__window__",
                details={
                    "source": source,
                    "checksum": checksum,
                    "computed_checksum": template.checksum,
                },
                conn=conn,
            )

        return template

    def export_window_templates(
        self,
        actor: str,
        *,
        fmt: str = "yaml",
    ) -> str:
        """导出所有发布窗口模板为 YAML 或 JSON。"""
        templates = self.list_window_templates()
        data = {
            "schema_version": _RELEASE_WINDOW_SCHEMA_VERSION,
            "templates": [t.to_export_dict() for t in templates],
        }

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_EXPORT,
                env="__all__",
                switch_name="__window__",
                details={"format": fmt, "count": len(templates)},
                conn=conn,
            )

        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return _dump_yaml(data)

    def import_window_templates(
        self,
        actor: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        """从 YAML/JSON 文件导入多个发布窗口模板。"""
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

        if not isinstance(doc, dict):
            raise ValidationError("导入文件根节点必须是对象 (mapping)")

        templates_list = doc.get("templates", [])
        if not isinstance(templates_list, list):
            raise ValidationError("templates 必须是列表")

        imported = 0
        skipped = 0
        for tpl_doc in templates_list:
            try:
                self._load_window_document(actor=actor, doc=tpl_doc, source=path)
                imported += 1
            except ValidationError as exc:
                if "已存在发布窗口模板" in str(exc):
                    skipped += 1
                elif "checksum 不匹配" in str(exc) or "校验和不匹配" in str(exc):
                    raise  # checksum 不匹配是严重错误，直接失败
                else:
                    raise

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_WINDOW_IMPORT,
                env="__all__",
                switch_name="__window__",
                details={
                    "source": path,
                    "imported": imported,
                    "skipped": skipped,
                },
                conn=conn,
            )

        return {"imported": imported, "skipped": skipped, "total": len(templates_list)}

    def export_pass(
        self,
        actor: str,
        *,
        pass_id: str,
        fmt: str = "yaml",
    ) -> str:
        """导出放行单为 YAML 或 JSON。"""
        pass_obj = self._require_pass(pass_id)
        data = pass_obj.to_export_dict()

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_PASS_EXPORT,
                env=pass_obj.env,
                switch_name="__pass__",
                details={"pass_id": pass_id, "format": fmt},
                conn=conn,
            )

        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return _dump_yaml(data)

    def import_pass(
        self,
        actor: str,
        *,
        path: str,
    ) -> ReleasePass:
        """从 YAML/JSON 文件导入放行单。"""
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

        return self._load_pass_document(actor=actor, doc=doc, source=path)

    def export_passes(
        self,
        actor: str,
        *,
        pass_ids: Optional[list[str]] = None,
        fmt: str = "yaml",
    ) -> str:
        """导出放行单为 YAML 或 JSON。"""
        if pass_ids:
            passes = []
            for pid in pass_ids:
                pass_obj = self._require_pass(pid)
                passes.append(pass_obj)
        else:
            passes = self.list_passes()

        data = {
            "schema_version": _RELEASE_PASS_SCHEMA_VERSION,
            "passes": [p.to_export_dict() for p in passes],
        }

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_PASS_EXPORT,
                env="__all__",
                switch_name="__pass__",
                details={"format": fmt, "count": len(passes)},
                conn=conn,
            )

        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return _dump_yaml(data)

    def import_passes(
        self,
        actor: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        """从 YAML/JSON 文件导入多个放行单。"""
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

        if not isinstance(doc, dict):
            raise ValidationError("导入文件根节点必须是对象 (mapping)")

        passes_list = doc.get("passes", [])
        if not isinstance(passes_list, list):
            raise ValidationError("passes 必须是列表")

        imported = 0
        skipped = 0
        for pass_doc in passes_list:
            try:
                self._load_pass_document(actor=actor, doc=pass_doc, source=path)
                imported += 1
            except ValidationError as exc:
                if "已存在" in str(exc) and "校验和不匹配" not in str(exc):
                    skipped += 1
                elif "校验和不匹配" in str(exc):
                    raise  # checksum 不匹配是严重错误，直接失败
                else:
                    raise

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_PASS_IMPORT,
                env="__all__",
                switch_name="__pass__",
                details={
                    "source": path,
                    "imported": imported,
                    "skipped": skipped,
                },
                conn=conn,
            )

        return {"imported": imported, "skipped": skipped, "total": len(passes_list)}

    def _load_pass_document(
        self, *, actor: str, doc: Any, source: str
    ) -> ReleasePass:
        if not isinstance(doc, dict):
            raise ValidationError("放行单文件根节点必须是对象 (mapping)")

        schema = doc.get("schema_version")
        if schema != _RELEASE_PASS_SCHEMA_VERSION:
            raise ValidationError(
                f"放行单 schema_version 不匹配: 期望 {_RELEASE_PASS_SCHEMA_VERSION!r}，收到 {schema!r}"
            )

        original_pass_id = doc.get("pass_id")
        env = doc.get("env")
        reason = doc.get("reason", "")
        affected_switches = doc.get("affected_switches", [])
        valid_from = doc.get("valid_from", "")
        valid_until = doc.get("valid_until", "")
        approver = doc.get("approver", "")
        description = doc.get("description", "")
        checksum = doc.get("checksum", "")

        computed_checksum = _compute_pass_checksum(
            env, reason, affected_switches, valid_from, valid_until, approver, description
        )
        if checksum and checksum != computed_checksum:
            raise ValidationError(
                f"放行单校验和不匹配：文件中 checksum={checksum}，"
                f"实际内容计算 checksum={computed_checksum}。"
                "文件可能已被篡改。"
            )

        if original_pass_id:
            existing = self.repo.get_release_pass(original_pass_id)
            if existing is not None:
                return existing

        pass_obj = self.create_pass(
            actor,
            env=env,
            reason=reason,
            affected_switches=affected_switches,
            valid_from=valid_from,
            valid_until=valid_until,
            approver=approver,
            description=f"{description}\n\n导入自: {source}" if description else f"导入自: {source}",
            skip_duplicate_check=True,
            pass_id=original_pass_id,
        )

        with self.repo.transaction() as conn:
            self.audit.record(
                actor=actor,
                action=AuditAction.RELEASE_PASS_IMPORT,
                env=env,
                switch_name="__pass__",
                details={
                    "source": source,
                    "pass_id": pass_obj.pass_id,
                    "original_pass_id": original_pass_id,
                    "checksum": computed_checksum,
                },
                conn=conn,
            )

        return pass_obj

    @staticmethod
    def _read(path: str) -> str:
        if not os.path.isfile(path):
            raise ValidationError(f"文件不存在: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise ValidationError(f"读取文件失败: {exc}") from exc

    # ------------------------------------------------------------------
    # 4. Queries
    # ------------------------------------------------------------------

    @staticmethod
    def _expand_query_statuses(
        statuses: Optional[list[ReleasePassStatus]],
    ) -> Optional[list[ReleasePassStatus]]:
        if statuses is None:
            return None
        expanded = set(statuses)
        if ReleasePassStatus.EXPIRED in expanded:
            expanded.add(ReleasePassStatus.APPROVED)
        return list(expanded) if expanded else None

    def list_passes(
        self,
        *,
        env: Optional[str] = None,
        statuses: Optional[list[ReleasePassStatus]] = None,
        approver: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> list[ReleasePass]:
        current_time = _now_iso()
        query_statuses = self._expand_query_statuses(statuses)
        raw_passes = self.repo.list_release_passes(
            env=env,
            statuses=query_statuses,
            approver=approver,
            created_by=created_by,
        )
        result: list[ReleasePass] = []
        for p in raw_passes:
            effective = self._resolve_pass_status(p, current_time)
            if statuses is None or effective in statuses:
                if effective != p.status:
                    p.status = effective
                result.append(p)
        return result

    def get_pass(self, pass_id: str) -> ReleasePass:
        """获取放行单详情。"""
        return self._require_pass(pass_id)

    def list_pass_records(
        self, pass_id: str, limit: int = 100
    ) -> list[ReleasePassRecord]:
        """获取放行单操作记录。"""
        return self.repo.list_release_pass_records(pass_id=pass_id, limit=limit)
