"""发布窗口模板 + 临时放行单 综合验收测试。
覆盖：模板CRUD、窗口校验、放行单生命周期、导入导出、冲突拦截、权限控制、审计日志、重启持久性。
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile

os.environ["FSWITCH_ADMINS"] = "alice@local,bob@local"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 强制重新导入以确保环境变量生效
if "feature_switch.cli.main" in sys.modules:
    importlib.reload(sys.modules["feature_switch.cli.main"])
if "feature_switch.service.release_window" in sys.modules:
    importlib.reload(sys.modules["feature_switch.service.release_window"])

from feature_switch.cli.main import cli, DEFAULT_ADMINS  # noqa: E402


def run(*argv: str) -> tuple[int, str, str]:
    import io
    import contextlib
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


class Case:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = True
        self.msgs: list[str] = []

    def check(self, cond: bool, msg: str) -> None:
        if not cond:
            self.ok = False
            self.msgs.append(f"  [FAIL] {msg}")
        else:
            self.msgs.append(f"  [ OK ] {msg}")

    def report(self) -> bool:
        status = "PASS" if self.ok else "FAIL"
        print(f"\n[{status}] {self.name}")
        for m in self.msgs:
            print(m)
        return self.ok


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="fswitch_window_")
    db = os.path.join(tmpdir, "fswitch.db")
    db_restart = os.path.join(tmpdir, "fswitch_restart.db")
    db_import = os.path.join(tmpdir, "fswitch_import.db")
    win_yaml = os.path.join(tmpdir, "window_templates.yaml")
    win_json = os.path.join(tmpdir, "window_templates.json")
    pass_yaml = os.path.join(tmpdir, "release_passes.yaml")

    def f(*args: str) -> tuple[int, str, str]:
        return run("--db", db, *args)

    def fj(*args: str) -> tuple[int, dict]:
        code, out, _ = f(*args)
        data = {}
        if out:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                pass
        return code, data

    all_ok = True

    # ------------------------------------------------------------------
    # 1. 发布窗口模板 CRUD + 权限控制
    # ------------------------------------------------------------------
    c = Case("[1] 发布窗口模板 CRUD + 权限控制")
    # 1a. 普通开发不能创建模板
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "win-create", "--env", "prod",
                    "--time-range", "09:00:18:00:monday-friday",
                    "--approver", "bob@local")
    c.check(code == 2, "普通开发 charlie 不能创建窗口模板（权限拦截")

    # 1b. 管理员创建 prod 窗口模板
    code, data = fj("--as", "alice@local", "--format", "json",
                    "win-create", "--env", "prod",
                    "--time-range", "09:00:18:00:monday-friday",
                    "--freeze-day", "2025-12-25", "2026-01-01",
                    "--approver", "bob@local", "david@local",
                    "--description", "工作日工作时间发布")
    c.check(code == 0, f"管理员 alice 创建 prod 窗口模板成功 (code={code}, msg={data.get('message')})")
    c.check(data.get("ok") and data.get("template", {}).get("env") == "prod", "返回 env=prod")
    template = data.get("template", {})
    c.check(len(template.get("allowed_time_ranges", [])) == 1, "有 1 个允许时段")
    c.check(len(template.get("freeze_days", [])) == 2, "有 2 个冻结日")
    c.check(len(template.get("on_call_approvers", [])) == 2, "有 2 个值班审批人")
    c.check(len(template.get("checksum", "")) == 16, "checksum 长度 16")
    prod_checksum = template.get("checksum", "")

    # 1c. 重复创建相同内容 → 拦截
    code, data = fj("--as", "alice@local", "--format", "json",
                    "win-create", "--env", "prod",
                    "--time-range", "09:00:18:00:monday-friday",
                    "--freeze-day", "2025-12-25", "2026-01-01",
                    "--approver", "bob@local", "david@local")
    c.check(code == 2, "重复创建相同内容模板被拦截（checksum 去重）")

    # 1d. 管理员创建 staging 窗口模板
    code, data = fj("--as", "bob@local", "--format", "json",
                    "win-create", "--env", "staging",
                    "--time-range", "09:00:20:00",
                    "--approver", "alice@local")
    c.check(code == 0, f"管理员 bob 创建 staging 窗口模板成功 (code={code}, msg={data.get('message')})")
    staging_template = data.get("template", {})
    staging_checksum = staging_template.get("checksum", "")
    c.check(staging_checksum!= prod_checksum, "不同环境 checksum 不同")

    # 1e. 列出模板
    code, out, _ = f("--format", "json", "win-list")
    data = json.loads(out)
    c.check(code == 0, "win-list 成功")
    c.check(data["count"] == 2, "list 到 2 个模板")

    # 1f. win-show 查看详情
    code, out, _ = f("--format", "json", "win-show", "--env", "prod")
    data = json.loads(out)
    c.check(code == 0, "win-show prod 成功")
    c.check(data["template"]["default_description"] == "工作日工作时间发布", "默认说明正确")

    # 1g. 普通开发不能更新模板
    code, _ = fj("--as", "charlie@local", "--format", "json",
                  "win-update", "--env", "prod",
                  "--description", "已更新")
    c.check(code == 2, "普通开发 charlie 不能更新模板")

    # 1h. 管理员更新模板
    code, data = fj("--as", "alice@local", "--format", "json",
                    "win-update", "--env", "prod",
                    "--description", "更新后的说明")
    c.check(code == 0, "管理员更新模板成功")
    c.check(data["template"]["default_description"] == "更新后的说明", "说明已更新")
    c.check(data["template"]["checksum"]!= prod_checksum, "内容变更后 checksum 变化")
    prod_checksum_new = data["template"]["checksum"]

    # 1i. 普通开发不能删除模板
    code, _ = fj("--as", "charlie@local", "--format", "json",
                  "win-delete", "--env", "staging")
    c.check(code == 2, "普通开发 charlie 不能删除模板")

    all_ok = c.report() and all_ok

    # ------------------------------------------------------------------
    # 2. 窗口校验逻辑
    # ------------------------------------------------------------------
    c = Case("[2] 窗口校验逻辑（时段、冻结日、无模板）")
    # 2a. 工作日工作时间 → 在窗口内
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "win-check", "--env", "prod",
                    "--at", "2025-06-18T10:00:00")
    c.check(code == 0, "工作日 10:00 校验成功")
    c.check(data["result"] == "IN_WINDOW", "结果应为 IN_WINDOW")
    c.check(data["in_window"] == True, "in_window=True")

    # 2b. 工作日非工作时间 → 不在窗口
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "win-check", "--env", "prod",
                    "--at", "2025-06-18T20:00:00")
    c.check(code == 0, "工作日 20:00 校验成功")
    c.check(data["result"] == "OUT_OF_WINDOW", "结果应为 OUT_OF_WINDOW")
    c.check(data["in_window"] == False, "in_window=False")

    # 2c. 周末 → 不在窗口
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "win-check", "--env", "prod",
                    "--at", "2025-06-21T10:00:00")
    c.check(code == 0, "周末 10:00 校验成功")
    c.check(data["result"] == "OUT_OF_WINDOW", "周末不在窗口")

    # 2d. 冻结日 → 拦截
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "win-check", "--env", "prod",
                    "--at", "2025-12-25T10:00:00")
    c.check(code == 0, "冻结日 10:00 校验成功")
    c.check(data["result"] == "FREEZE_DAY", "冻结日拦截")
    c.check(data["in_window"] == False, "冻结日 in_window=False")

    # 2e. 无模板的环境（无配置 = 无限制，所以 in_window=True）
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "win-check", "--env", "uat",
                    "--at", "2025-06-18T10:00:00")
    c.check(code == 0, "uat 无模板校验成功")
    c.check(data.get("result") == "NO_TEMPLATE", "无模板")
    c.check(data.get("in_window") == True, "无模板时不限制发布，in_window=True")
    all_ok = c.report() and all_ok

    # ------------------------------------------------------------------
    # 3. 临时放行单生命周期 + 自审批拦截
    # ------------------------------------------------------------------
    c = Case("[3] 临时放行单生命周期 + 自审批拦截")
    # 3a. 创建放行单草稿（不在窗口时需要）
    valid_from = "2025-12-25T08:00:00"
    valid_until = "2025-12-25T23:59:59"
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-create", "--env", "prod",
                    "--reason", "紧急线上 bug 修复，需要在冻结日发布",
                    "--switch", "payment_flow", "user_profile",
                    "--valid-from", valid_from,
                    "--valid-until", valid_until,
                    "--approver", "alice@local",
                    "--description", "修复支付超时问题，影响范围可控")
    c.check(code == 0, f"charlie 创建放行单草稿成功 (code={code}, msg={data.get('message')})")
    c.check(data.get("pass", {}).get("status") == "DRAFT", f"状态为 DRAFT (actual={data.get('pass', {}).get('status')})")
    pass_id = data.get("pass", {}).get("pass_id", "")
    c.check(len(pass_id) == 17, f"pass_id 长度 17 (pass- + 12 hex chars, actual={pass_id}, len={len(pass_id)})")
    c.check(len(data.get("pass", {}).get("checksum", "")) == 16, f"checksum 长度 16 (actual={data.get('pass', {}).get('checksum')})")
    pass_checksum = data.get("pass", {}).get("checksum", "")

    # 3b. 重复创建相同内容 → 拦截
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-create", "--env", "prod",
                    "--reason", "紧急线上 bug 修复，需要在冻结日发布",
                    "--switch", "payment_flow", "user_profile",
                    "--valid-from", valid_from,
                    "--valid-until", valid_until,
                    "--approver", "alice@local",
                    "--description", "修复支付超时问题，影响范围可控")
    c.check(code == 2, "重复创建相同内容放行单被拦截")

    # 3c. 提交审批
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-submit", "--pass-id", pass_id)
    c.check(code == 0, "提交审批成功")
    c.check(data["status"] == "PENDING_APPROVAL", "状态变为 PENDING_APPROVAL")
    c.check(data["submitted_at"] is not None, "submitted_at 已记录")

    # 3d. 申请人自己审批 → 拦截
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-approve", "--pass-id", pass_id)
    c.check(code == 2, "申请人 charlie 不能审批自己的放行单（自审拦截")

    # 3e. 非审批人审批 → 拦截
    code, data = fj("--as", "david@local", "--format", "json",
                    "pass-approve", "--pass-id", pass_id)
    c.check(code == 2, "非指定审批人 david 不能审批")

    # 3f. 指定审批人 alice 审批通过
    code, data = fj("--as", "alice@local", "--format", "json",
                    "pass-approve", "--pass-id", pass_id)
    c.check(code == 0, "指定审批人 alice 审批通过")
    c.check(data["status"] == "APPROVED", "状态变为 APPROVED")
    c.check(data["approved_at"] is not None, "approved_at 已记录")

    # 3g. 再次校验窗口 → 应返回适用放行单
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "win-check", "--env", "prod",
                    "--at", "2025-12-25T10:00:00")
    c.check(code == 0, "冻结日校验（有放行单）成功")
    c.check(data.get("in_window") == True, f"有有效放行单时 in_window=True (实际={data.get('in_window')})")
    applicable_pass = data.get("applicable_pass")
    c.check(applicable_pass is not None, f"返回了适用放行单 (actual={applicable_pass})")
    c.check(applicable_pass.get("pass_id") == pass_id if applicable_pass else False, "放行单 ID 正确")

    # 3h. 时间冲突：创建重叠时间段的另一张 → 拦截
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-create", "--env", "prod",
                    "--reason", "另一个紧急修复",
                    "--valid-from", "2025-12-25T09:00:00",
                    "--valid-until", "2025-12-25T11:00:00",
                    "--approver", "bob@local")
    c.check(code == 2, "时间重叠的放行单被拦截（冲突检测")

    # 3i. 使用放行单（一次性）
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-use", "--pass-id", pass_id,
                    "--order-id", "REL-20251225-001",
                    "--at", "2025-12-25T10:30:00")
    c.check(code == 0, "使用放行单成功")
    c.check(data["status"] == "USED", "状态变为 USED")
    c.check(data["used_by"] == "charlie@local", "used_by 正确")
    c.check(data["used_for_order_id"] == "REL-20251225-001", "关联发布单正确")

    # 3j. 再次使用 → 拦截（一次性）
    code, _ = fj("--as", "charlie@local", "--format", "json",
                    "pass-use", "--pass-id", pass_id,
                    "--order-id", "REL-20251225-002")
    c.check(code == 2, "已使用的放行单不能重复使用")

    # 3k. pass-show 查看详情
    code, out, _ = f("--format", "json", "pass-show", "--pass-id", pass_id)
    data = json.loads(out)
    c.check(code == 0, "pass-show 成功")
    c.check(len(data["records"]) >= 4, "至少 4 条操作记录（创建/提交/审批/使用）")
    all_ok = c.report() and all_ok

    # ------------------------------------------------------------------
    # 4. 放行单查询分类（待审批/已使用/已过期/已撤销）
    # ------------------------------------------------------------------
    c = Case("[4] 放行单查询分类")
    # 4a. 创建另一张放行单（待审批状态）
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-create", "--env", "staging",
                    "--reason", "staging 紧急修复",
                    "--valid-from", "2025-06-18T08:00:00",
                    "--valid-until", "2025-06-18T23:59:59",
                    "--approver", "bob@local")
    c.check(code == 0, f"创建 staging 放行单成功 (code={code}, msg={data.get('message')})")
    pass_id_pending = data.get("pass", {}).get("pass_id", "")
    c.check(len(pass_id_pending) == 17, f"staging pass_id 有效 (actual={pass_id_pending})")
    if pass_id_pending:
        code, _ = fj("--as", "charlie@local", "--format", "json",
                      "pass-submit", "--pass-id", pass_id_pending)
        c.check(code == 0, f"提交 staging 放行单成功 (code={code})")

    # 4b. 创建一张已过期的放行单（先创建再模拟过期？不，我们创建一张有效期在过去的）
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-create", "--env", "prod",
                    "--reason", "过期测试",
                    "--valid-from", "2020-01-01T00:00:00",
                    "--valid-until", "2020-01-01T23:59:59",
                    "--approver", "alice@local")
    c.check(code == 0, f"创建过期放行单成功 (code={code}, msg={data.get('message')})")
    pass_id_expired = data.get("pass", {}).get("pass_id", "")
    c.check(len(pass_id_expired) == 17, f"expired pass_id 有效 (actual={pass_id_expired})")
    if pass_id_expired:
        code, _ = fj("--as", "charlie@local", "--format", "json",
                      "pass-submit", "--pass-id", pass_id_expired)
        code, _ = fj("--as", "alice@local", "--format", "json",
                      "pass-approve", "--pass-id", pass_id_expired)
        c.check(code == 0, f"过期放行单审批通过 (code={code})")

    # 4c. 创建一张撤销的放行单
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-create", "--env", "prod",
                    "--reason", "计划外发布，后来取消了",
                    "--valid-from", "2025-12-26T08:00:00",
                    "--valid-until", "2025-12-26T23:59:59",
                    "--approver", "alice@local")
    c.check(code == 0, f"创建撤销测试放行单成功 (code={code}, msg={data.get('message')})")
    pass_id_cancelled = data.get("pass", {}).get("pass_id", "")
    c.check(len(pass_id_cancelled) == 17, f"cancelled pass_id 有效 (actual={pass_id_cancelled})")
    if pass_id_cancelled:
        code, _ = fj("--as", "charlie@local", "--format", "json",
                      "pass-submit", "--pass-id", pass_id_cancelled)
        code, _ = fj("--as", "charlie@local", "--format", "json",
                      "pass-cancel", "--pass-id", pass_id_cancelled,
                      "--reason", "不需要了")
        c.check(code == 0, f"撤销放行单成功 (code={code})")

    # 4d. 按状态查询：待审批
    code, out, _ = f("--format", "json", "pass-list", "--status", "PENDING_APPROVAL")
    data = json.loads(out)
    c.check(code == 0, f"查询待审批放行单成功 (code={code})")
    c.check(data.get("count") == 1, f"有 1 张待审批 (actual={data.get('count')}, pass_id_pending={pass_id_pending})")
    passes_pending = data.get("passes", [])
    if passes_pending:
        c.check(passes_pending[0].get("pass_id") == pass_id_pending, f"待审批放行单 ID 正确 (actual={passes_pending[0].get('pass_id')}, expected={pass_id_pending})")

    # 4e. 按状态查询：已使用
    code, out, _ = f("--format", "json", "pass-list", "--status", "USED")
    data = json.loads(out)
    c.check(code == 0, f"查询已使用放行单成功 (code={code})")
    c.check(data.get("count") == 1, f"有 1 张已使用 (actual={data.get('count')})")

    # 4f. 按状态查询：已撤销
    code, out, _ = f("--format", "json", "pass-list", "--status", "CANCELLED")
    data = json.loads(out)
    c.check(code == 0, f"查询已撤销放行单成功 (code={code})")
    c.check(data.get("count") == 1, f"有 1 张已撤销 (actual={data.get('count')})")

    # 4g. 按状态查询：已过期（核心链路）
    code, out, _ = f("--format", "json", "pass-list", "--status", "EXPIRED")
    data = json.loads(out)
    c.check(code == 0, f"查询已过期放行单成功 (code={code})")
    c.check(data.get("count") == 1, f"有 1 张已过期 (actual={data.get('count')})")
    passes_expired = data.get("passes", [])
    if passes_expired:
        c.check(passes_expired[0].get("pass_id") == pass_id_expired, f"已过期放行单 ID 正确 (actual={passes_expired[0].get('pass_id')}, expected={pass_id_expired})")

    # 4h. 按状态查询：APPROVED 不含已过期单据
    code, out, _ = f("--format", "json", "pass-list", "--status", "APPROVED")
    data = json.loads(out)
    c.check(code == 0, f"查询 APPROVED 放行单成功 (code={code})")
    c.check(data.get("count") == 0, f"APPROVED 查询不含已过期单据 (actual={data.get('count')})")

    # 4i. 确认其他状态未回退：待审批
    code, out, _ = f("--format", "json", "pass-list", "--status", "PENDING_APPROVAL")
    data = json.loads(out)
    c.check(code == 0, f"过期链路后待审批仍正确 (code={code})")
    c.check(data.get("count") == 1, f"仍有 1 张待审批 (actual={data.get('count')})")

    # 4j. 确认其他状态未回退：已使用
    code, out, _ = f("--format", "json", "pass-list", "--status", "USED")
    data = json.loads(out)
    c.check(code == 0, f"过期链路后已使用仍正确 (code={code})")
    c.check(data.get("count") == 1, f"仍有 1 张已使用 (actual={data.get('count')})")

    # 4k. 确认其他状态未回退：已撤销
    code, out, _ = f("--format", "json", "pass-list", "--status", "CANCELLED")
    data = json.loads(out)
    c.check(code == 0, f"过期链路后已撤销仍正确 (code={code})")
    c.check(data.get("count") == 1, f"仍有 1 张已撤销 (actual={data.get('count')})")

    # 4l. 按环境查询
    code, out, _ = f("--format", "json", "pass-list", "--env", "prod")
    data = json.loads(out)
    c.check(code == 0, f"按环境查询成功 (code={code})")
    c.check(data.get("count") == 3, f"prod 环境有 3 张放行单 (actual={data.get('count')})")

    # 4m. 按申请人查询
    code, out, _ = f("--format", "json", "pass-list", "--created-by", "charlie@local")
    data = json.loads(out)
    c.check(code == 0, f"按申请人查询成功 (code={code})")
    c.check(data.get("count") == 4, f"charlie 创建了 4 张放行单 (actual={data.get('count')})")
    all_ok = c.report() and all_ok

    # ------------------------------------------------------------------
    # 5. 导入导出（YAML/JSON）+ 校验和验证
    # ------------------------------------------------------------------
    c = Case("[5] 导入导出（YAML/JSON）+ 校验和验证")
    # 5a. 导出窗口模板 YAML
    code, data = fj("--as", "alice@local", "--format", "json",
                    "win-export", "--format", "yaml", "-o", win_yaml)
    c.check(code == 0, "导出窗口模板 YAML 成功")
    c.check(os.path.exists(win_yaml), "YAML 文件已创建")

    # 5b. 导出窗口模板 JSON
    code, data = fj("--as", "alice@local", "--format", "json",
                    "win-export", "--format", "json", "-o", win_json)
    c.check(code == 0, "导出窗口模板 JSON 成功")

    # 5c. 导入窗口模板到新库（验证一致性）
    def f2(*args: str) -> tuple[int, str, str]:
        return run("--db", db_import, *args)
    def fj2(*args: str) -> tuple[int, dict]:
        code, out, _ = f2(*args)
        data = {}
        if out:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                pass
        return code, data

    # 先在新库导入
    code, data = fj2("--as", "alice@local", "--format", "json",
                   "win-import", "--file", win_yaml)
    c.check(code == 0, f"新库导入窗口模板 YAML 成功 (code={code}, msg={data.get('message')})")
    c.check(data.get("imported") == 2, f"导入了 2 个模板 (actual={data.get('imported')})")

    # 验证新库内容一致
    code, out, _ = f2("--format", "json", "win-show", "--env", "prod")
    data = json.loads(out)
    c.check(code == 0, f"新库 win-show prod 成功 (code={code})")
    c.check(data.get("template", {}).get("checksum") == prod_checksum_new, f"新库 prod checksum 一致 (actual={data.get('template', {}).get('checksum')}, expected={prod_checksum_new})")
    c.check(len(data.get("template", {}).get("allowed_time_ranges", [])) == 1, "时段数量一致")
    c.check(len(data.get("template", {}).get("freeze_days", [])) == 2, "冻结日数量一致")

    # 5d. 导出行单 YAML
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-export", "--format", "yaml", "-o", pass_yaml)
    c.check(code == 0, "导出行单 YAML 成功")

    # 5e. 篡改 checksum → 导入拦截
    import yaml
    c.check(os.path.exists(pass_yaml), f"pass-export 文件已创建 (path={pass_yaml}, exists={os.path.exists(pass_yaml)})")
    if os.path.exists(pass_yaml):
        with open(pass_yaml, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        # 篡改 passes 列表中第一个条目的 checksum
        if "passes" in doc and doc["passes"]:
            doc["passes"][0]["checksum"] = "deadbeefdeadbeef"
        else:
            doc["checksum"] = "deadbeefdeadbeef"
        with open(pass_yaml, "w", encoding="utf-8") as fh:
            yaml.dump(doc, fh, allow_unicode=True)
        code, data = fj2("--as", "charlie@local", "--format", "json",
                       "pass-import", "--file", pass_yaml)
        c.check(code == 2, f"篡改 checksum 被正确拦截 (code={code}, msg={data.get('message')})")
    all_ok = c.report() and all_ok

    # ------------------------------------------------------------------
    # 6. 重启持久性验证
    # ------------------------------------------------------------------
    c = Case("[6] 重启持久性验证")
    # 6a. 复制 DB 到重启库
    import shutil
    shutil.copy2(db, db_restart)

    def f_restart(*args: str) -> tuple[int, str, str]:
        return run("--db", db_restart, *args)

    # 6b. 重启后查询窗口模板
    code, out, _ = f_restart("--format", "json", "win-list")
    data = json.loads(out)
    c.check(code == 0, "重启后 win-list 成功")
    c.check(data["count"] == 2, "重启后仍有 2 个模板")

    # 6c. 重启后查放行单
    code, out, _ = f_restart("--format", "json", "pass-list", "--status", "USED")
    data = json.loads(out)
    c.check(code == 0, "重启后查询 USED 放行单成功")
    c.check(data["count"] == 1, "重启后仍有 1 张 USED")
    c.check(data["passes"][0]["pass_id"] == pass_id, "重启后 pass_id 一致")
    c.check(data["passes"][0]["used_for_order_id"] == "REL-20251225-001", "重启后关联发布单一致")

    # 6d. 重启后查审计日志
    code, out, _ = f_restart("--format", "json", "audit", "--limit", "50")
    data = json.loads(out)
    c.check(code == 0, "重启后查询审计日志成功")
    actions = {log["action"] for log in data["logs"]}
    required = {
        "RELEASE_WINDOW_CREATE", "RELEASE_WINDOW_UPDATE",
        "RELEASE_PASS_CREATE", "RELEASE_PASS_SUBMIT",
        "RELEASE_PASS_APPROVE", "RELEASE_PASS_USE",
        "RELEASE_PASS_CANCEL", "RELEASE_WINDOW_CHECK"
    }
    missing = required - actions
    c.check(len(missing) == 0, f"重启后审计日志包含所有关键动作: {sorted(actions)}")
    all_ok = c.report() and all_ok

    # ------------------------------------------------------------------
    # 7. 驳回 + 撤销流程
    # ------------------------------------------------------------------
    c = Case("[7] 驳回 + 撤销流程")
    # 7a. 创建并提交一张放行单
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-create", "--env", "staging",
                    "--reason", "测试驳回",
                    "--valid-from", "2025-06-20T08:00:00",
                    "--valid-until", "2025-06-20T23:59:59",
                    "--approver", "bob@local")
    c.check(code == 0, f"创建驳回测试放行单成功 (code={code}, msg={data.get('message')})")
    pass_id_reject = data.get("pass", {}).get("pass_id", "")
    c.check(len(pass_id_reject) == 17, f"reject pass_id 有效 (actual={pass_id_reject})")
    if pass_id_reject:
        code, _ = fj("--as", "charlie@local", "--format", "json",
                      "pass-submit", "--pass-id", pass_id_reject)

    # 7b. 审批人驳回
    if pass_id_reject:
        code, data = fj("--as", "bob@local", "--format", "json",
                        "pass-reject", "--pass-id", pass_id_reject,
                        "--reason", "风险太高，需要更详细的影响评估")
        c.check(code == 0, f"驳回成功 (code={code}, msg={data.get('message')})")
        c.check(data.get("status") == "REJECTED", f"状态为 REJECTED (actual={data.get('status')})")
        c.check(data.get("reject_reason") == "风险太高，需要更详细的影响评估", f"驳回原因正确 (actual={data.get('reject_reason')})")

    # 7c. 创建并提交一张放行单用于撤销
    code, data = fj("--as", "charlie@local", "--format", "json",
                    "pass-create", "--env", "staging",
                    "--reason", "测试撤销（未审批）",
                    "--valid-from", "2025-06-21T08:00:00",
                    "--valid-until", "2025-06-21T23:59:59",
                    "--approver", "bob@local")
    c.check(code == 0, f"创建撤销测试放行单成功 (code={code}, msg={data.get('message')})")
    pass_id_cancel2 = data.get("pass", {}).get("pass_id", "")
    c.check(len(pass_id_cancel2) == 17, f"cancel2 pass_id 有效 (actual={pass_id_cancel2})")
    if pass_id_cancel2:
        code, _ = fj("--as", "charlie@local", "--format", "json",
                      "pass-submit", "--pass-id", pass_id_cancel2)

    # 7d. 申请人撤销（未生效的放行单）
    if pass_id_cancel2:
        code, data = fj("--as", "charlie@local", "--format", "json",
                        "pass-cancel", "--pass-id", pass_id_cancel2,
                        "--reason", "不需要了，改到下周一")
        c.check(code == 0, f"撤销未生效放行单成功 (code={code}, msg={data.get('message')})")
        c.check(data.get("status") == "CANCELLED", f"状态为 CANCELLED (actual={data.get('status')})")
        c.check(data.get("cancel_reason") == "不需要了，改到下周一", f"撤销原因正确 (actual={data.get('cancel_reason')})")

    # 7e. 已审批的放行单不能撤销
    code, _ = fj("--as", "charlie@local", "--format", "json",
                  "pass-cancel", "--pass-id", pass_id,  # 这张已经 USED 了
                  "--reason", "试试撤销已使用的")
    c.check(code == 2, "已使用的放行单不能撤销")
    all_ok = c.report() and all_ok

    # ------------------------------------------------------------------
    # 8. pass-records 审计链
    # ------------------------------------------------------------------
    c = Case("[8] pass-records 审计链完整性")
    code, out, _ = f("--format", "json", "pass-records", "--pass-id", pass_id)
    data = json.loads(out)
    c.check(code == 0, "pass-records 查询成功")
    c.check(data["count"] >= 4, "至少 4 条记录")
    actions = [r["action"] for r in data["records"]]
    c.check("RELEASE_PASS_CREATE" in actions, "包含 CREATE 记录")
    c.check("RELEASE_PASS_SUBMIT" in actions, "包含 SUBMIT 记录")
    c.check("RELEASE_PASS_APPROVE" in actions, "包含 APPROVE 记录")
    c.check("RELEASE_PASS_USE" in actions, "包含 USE 记录")
    all_ok = c.report() and all_ok

    # ------------------------------------------------------------------
    # 总结
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    if all_ok:
        print("[OK] 全部 PASS")
    else:
        print("[FAIL] 有失败用例")
    print(f"临时 DB 目录: {tmpdir}")
    print("=" * 60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
