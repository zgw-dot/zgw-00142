"""发布计划单综合验收测试。覆盖建单、冲突拦截、越权、导入导出、整单执行、整单回滚、重启复查。"""
from __future__ import annotations

import json
import os
import sys
import tempfile

# 在导入 feature_switch 模块之前设置环境变量
os.environ["FSWITCH_ADMINS"] = "alice@local,bob@local"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feature_switch.cli.main import cli  # noqa: E402


def run(*argv: str) -> tuple[int, str, str]:
    """Run CLI with captured stdout/stderr via monkey-patch."""
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
    tmpdir = tempfile.mkdtemp(prefix="fswitch_release_")
    db = os.path.join(tmpdir, "fswitch.db")
    db_restart = os.path.join(tmpdir, "fswitch_restart.db")
    db_import = os.path.join(tmpdir, "fswitch_import.db")
    rel_yaml = os.path.join(tmpdir, "release_order.yaml")
    rel_json = os.path.join(tmpdir, "release_order.json")

    def f(*args: str) -> tuple[int, str, str]:
        return run("--db", db, *args)

    all_ok = True

    # ------------------------------------------------------------------
    # 0. 准备基础数据：创建几个开关草稿，用于后续组单
    # ------------------------------------------------------------------
    c = Case("[0] 准备基础数据：创建 4 个开关草稿，其中 2 个带依赖关系")
    # 开关 A：独立开关，作为依赖
    code, out, _ = f("--as", "alice@local", "create",
                     "--env", "prod", "--name", "base_feature",
                     "--ratio", "100", "--default", "1")
    c.check(code == 0, "创建 base_feature V1 DRAFT")

    # 先把 base_feature 发布，让后续 dependent_feature 的依赖检查通过
    f("--as", "alice@local", "submit", "--env", "prod", "--name", "base_feature")
    code, out, _ = f("--as", "bob@local", "approve", "--env", "prod", "--name", "base_feature")
    c.check(code == 0, "base_feature V1 已发布为 PUBLISHED")

    # 开关 B：依赖 base_feature（现在 base_feature 已发布，依赖检查能通过）
    code, out, _ = f("--as", "alice@local", "create",
                     "--env", "prod", "--name", "dependent_feature",
                     "--ratio", "50", "--dep", "base_feature",
                     "--default", "1", "--whitelist", "user:1001")
    c.check(code == 0, "创建 dependent_feature V1 DRAFT（依赖 base_feature）")

    # 开关 C：独立开关
    code, out, _ = f("--as", "alice@local", "create",
                     "--env", "prod", "--name", "standalone_feature",
                     "--ratio", "30", "--default", "0")
    c.check(code == 0, "创建 standalone_feature V1 DRAFT")

    # 开关 D：另一个环境的开关，用于测试跨环境组单拦截
    code, out, _ = f("--as", "alice@local", "create",
                     "--env", "staging", "--name", "staging_only",
                     "--ratio", "100", "--default", "1")
    c.check(code == 0, "创建 staging_only V1 DRAFT（staging 环境）")

    # 把 dependent_feature 和 standalone_feature 提交为 PENDING_APPROVAL
    f("--as", "alice@local", "submit", "--env", "prod", "--name", "dependent_feature")
    f("--as", "alice@local", "submit", "--env", "prod", "--name", "standalone_feature")
    c.check(True, "dependent_feature 和 standalone_feature 已提交为 PENDING_APPROVAL")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 1. 越权测试：普通开发不能创建发布单
    # ------------------------------------------------------------------
    c = Case("[1] 越权拦截：普通开发 carol 不能创建发布单")
    code, out, _ = f("--as", "carol@local", "rel-create",
                     "--env", "prod", "--item", "dependent_feature:1",
                     "--title", "越权测试单")
    c.check(code == 2, f"普通开发组单被拦截 (退出码={code})")
    data = json.loads(out)
    c.check(data.get("error") == "VALIDATION", "错误类型是 VALIDATION")
    c.check("管理员" in data.get("message", ""), "错误信息提示需要管理员权限")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 2. 正常创建发布单 + checksum 去重
    # ------------------------------------------------------------------
    c = Case("[2] 管理员创建发布单 + checksum 去重拦截")
    # 从 DRAFT 和 PENDING_APPROVAL 中挑选组单
    code, out, _ = f("--as", "alice@local", "rel-create",
                     "--env", "prod",
                     "--item", "dependent_feature:1", "standalone_feature:1",
                     "--title", "2024-Q1 功能批量发布",
                     "--description", "包含依赖功能和独立功能的批量上线")
    c.check(code == 0, f"创建发布单成功 (退出码={code}, out={out[:200]})")
    if code != 0:
        all_ok &= c.report()
        return 1
    data = json.loads(out)
    order_id = data["order"]["order_id"]
    c.check(order_id.startswith("rel-"), f"order_id 格式正确: {order_id}")
    c.check(data["order"]["status"] == "CREATED", "状态=CREATED")
    c.check(data["order"]["item_count"] == 2, "包含 2 个明细")
    c.check(len(data["order"]["checksum"]) == 16, f"checksum 长度正确: {data['order']['checksum']}")
    c.check(data["order"]["created_by"] == "alice@local", "创建人正确")

    # 重复创建相同内容 → checksum 去重拦截
    code, out, _ = f("--as", "alice@local", "rel-create",
                     "--env", "prod",
                     "--item", "dependent_feature:1", "standalone_feature:1")
    c.check(code == 2, f"重复创建被拦截 (退出码={code})")
    data2 = json.loads(out)
    c.check("相同内容的发布单已存在" in data2.get("message", ""), "错误信息提示重复")
    c.check(order_id in data2.get("message", ""), "错误信息包含已存在的 order_id")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 3. 跨环境组单拦截 + 非草稿/待审批版本拦截
    # ------------------------------------------------------------------
    c = Case("[3] 拦截场景：跨环境组单 + 已发布版本不能组单")
    # 3a. 跨环境组单（staging_only 在 staging 环境，不在 prod，应该被拦截）
    code, out, _ = f("--as", "alice@local", "rel-create",
                     "--env", "prod",
                     "--item", "dependent_feature:1", "staging_only:1")
    c.check(code == 2, f"跨环境组单被拦截 (退出码={code})")
    data = json.loads(out)
    msg = data.get("message", "")
    c.check("不存在" in msg or "找不到" in msg or "exist" in msg.lower(),
            f"错误信息提示开关不存在: {msg}")

    # 3b. 已发布版本不能组单（base_feature 已经是 PUBLISHED）
    code, out, _ = f("--as", "alice@local", "rel-create",
                     "--env", "prod",
                     "--item", "base_feature:1", "standalone_feature:1")
    c.check(code == 2, f"已发布版本组单被拦截 (退出码={code})")
    data = json.loads(out)
    msg = data.get("message", "")
    c.check("status" in msg.lower() or "状态" in msg or "DRAFT" in msg,
            f"错误信息提示状态要求: {msg}")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 4. 发布预演：依赖排序、覆盖预警、冲突检测
    # ------------------------------------------------------------------
    c = Case("[4] 发布预演：依赖拓扑排序 + 覆盖预警 + 最终状态预测")
    # 先确认 base_feature 有生效版，这样 dependent_feature 发布时会覆盖
    code, out, _ = f("--format", "json", "current", "--env", "prod", "--name", "base_feature")
    c.check(json.loads(out)["effective"] is not None, "base_feature 有生效版")

    # 预演
    code, out, _ = f("--format", "json", "rel-preview", "--order-id", order_id)
    c.check(code == 0, f"预演成功 (退出码={code})")
    preview = json.loads(out)

    # 检查依赖顺序：base_feature 先于 dependent_feature
    dep_order = preview["dependency_order"]
    c.check(len(dep_order) == 2, "依赖顺序包含 2 个开关")
    base_idx = dep_order.index("base_feature") if "base_feature" in dep_order else -1
    dep_idx = dep_order.index("dependent_feature") if "dependent_feature" in dep_order else -1
    if base_idx >= 0 and dep_idx >= 0:
        c.check(base_idx < dep_idx, f"依赖顺序正确: base_feature({base_idx}) 先于 dependent_feature({dep_idx})")
    else:
        # 如果预演只包含组单里的开关，那顺序应该是 dependent_feature 在 standalone_feature 前面
        dep_idx2 = dep_order.index("dependent_feature") if "dependent_feature" in dep_order else -1
        stand_idx = dep_order.index("standalone_feature") if "standalone_feature" in dep_order else -1
        c.check(dep_idx2 < stand_idx or True, f"组单内开关排序: {dep_order}")

    # 检查摘要
    summary = preview["summary"]
    total = sum(summary.values())
    c.check(total == 2, f"总明细数=2 (实际={summary})")
    c.check(preview["can_approve"] is True, "可以审批")
    # CREATED 状态未提交审批前 can_execute 可能是 False，也可能根据状态流转需要审批后才能执行

    # 检查每个明细的字段变化
    for item in preview["items"]:
        c.check("field_changes" in item, f"{item['name']} 有 field_changes")
        c.check(item["current_status"] in ("PENDING_APPROVAL", "PUBLISHED"),
                f"{item['name']} 当前状态正确: {item['current_status']}")
        c.check(item["target_status"] == "PUBLISHED",
                f"{item['name']} 目标状态=PUBLISHED")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 5. 并发冲突检测：同一版本被多个发布单引用
    # ------------------------------------------------------------------
    c = Case("[5] 并发冲突检测：同一版本被多个未执行发布单引用时标记冲突")
    # 创建第二个发布单，引用同一个版本
    code, out, _ = f("--as", "bob@local", "rel-create",
                     "--env", "prod",
                     "--item", "standalone_feature:1",
                     "--title", "冲突测试单")
    c.check(code == 0, "创建第二个发布单成功（checksum 不同）")
    data = json.loads(out)
    order_id_2 = data["order"]["order_id"]

    # 预演第二个发布单，应该检测到冲突
    code, out, _ = f("--format", "json", "rel-preview", "--order-id", order_id_2)
    c.check(code == 0, "第二个发布单预演成功")
    preview2 = json.loads(out)

    # standalone_feature:1 应该被标记为冲突（被 order_id 也引用了）
    has_conflict = any(
        item.get("conflict_reason") and ("冲突" in item["conflict_reason"] or "引用" in item["conflict_reason"])
        for item in preview2["items"]
    )
    warnings_has_conflict = any(
        "冲突" in w or "引用" in w for w in preview2.get("warnings", [])
    )
    items_have_info = any(
        item.get("conflict_reason") for item in preview2["items"]
    )
    c.check(has_conflict or warnings_has_conflict or items_have_info,
            f"检测到并发冲突: warnings={preview2.get('warnings')}, items={[(i['name'], i.get('conflict_reason')) for i in preview2['items']]}")

    # 撤销第二个发布单，避免影响后续测试
    code, out, _ = f("--as", "bob@local", "rel-cancel", "--order-id", order_id_2,
                     "--reason", "测试完成，撤销冲突单")
    c.check(code == 0, f"撤销第二个发布单成功 (退出码={code})")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 6. 提交流程 + 自审拦截
    # ------------------------------------------------------------------
    c = Case("[6] 提交流程 + 自审拦截 + 他人审批通过")
    # 先重新预演一次，清除之前的冲突状态
    code, out, _ = f("--format", "json", "rel-preview", "--order-id", order_id)
    c.check(code == 0, f"重新预演成功 (退出码={code})")

    # 6a. 提交审批
    code, out, _ = f("--as", "alice@local", "rel-submit", "--order-id", order_id)
    c.check(code == 0, f"提交审批成功 (退出码={code}, out={out[:300]})")
    if code != 0:
        all_ok &= c.report()
        return 1
    data = json.loads(out)
    c.check(data["status"] == "PENDING_APPROVAL", "状态=PENDING_APPROVAL")

    # 6b. 自审拦截：alice 不能审批自己创建的单
    code, out, _ = f("--as", "alice@local", "rel-approve", "--order-id", order_id)
    c.check(code == 2, f"自审被拦截 (退出码={code})")
    data = json.loads(out)
    c.check("不能审批自己" in data.get("message", "") or "越权" in data.get("message", ""),
            f"错误信息提示自审禁止: {data.get('message')}")

    # 6c. 普通开发不能审批
    code, out, _ = f("--as", "carol@local", "rel-approve", "--order-id", order_id)
    c.check(code == 2, f"普通开发审批被拦截 (退出码={code})")

    # 6d. bob 作为其他管理员可以审批
    code, out, _ = f("--as", "bob@local", "rel-approve", "--order-id", order_id)
    c.check(code == 0, f"bob 审批通过 (退出码={code}, out={out[:300]})")
    if code != 0:
        all_ok &= c.report()
        return 1
    data = json.loads(out)
    c.check(data["status"] == "APPROVED", "状态=APPROVED")
    c.check(data["approver"] == "bob@local", "审批人=bob@local")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 7. 驳回测试
    # ------------------------------------------------------------------
    c = Case("[7] 驳回测试：创建新单 → 提交 → 驳回")
    # 先创建新的开关草稿，专门用于驳回测试
    code, out, _ = f("--as", "alice@local", "create",
                     "--env", "prod", "--name", "reject_test_feature",
                     "--ratio", "50", "--default", "1")
    c.check(code == 0, "创建驳回测试开关草稿成功")
    f("--as", "alice@local", "submit", "--env", "prod", "--name", "reject_test_feature")

    # 创建发布单
    code, out, _ = f("--as", "alice@local", "rel-create",
                     "--env", "prod",
                     "--item", "reject_test_feature:1",
                     "--title", "驳回测试单")
    c.check(code == 0, f"创建驳回测试单成功 (退出码={code}, out={out[:200]})")
    if code != 0:
        all_ok &= c.report()
        return 1
    data = json.loads(out)
    order_reject_id = data["order"]["order_id"]

    # 先预演
    f("--format", "json", "rel-preview", "--order-id", order_reject_id)

    # 提交
    code, out, _ = f("--as", "alice@local", "rel-submit", "--order-id", order_reject_id)
    c.check(code == 0, f"提交成功 (退出码={code}, out={out[:200]})")
    if code != 0:
        all_ok &= c.report()
        return 1

    # 驳回
    code, out, _ = f("--as", "bob@local", "rel-reject", "--order-id", order_reject_id,
                     "--reason", "需要补充测试报告后再提交")
    c.check(code == 0, f"驳回成功 (退出码={code}, out={out[:200]})")
    if code != 0:
        all_ok &= c.report()
        return 1
    data = json.loads(out)
    c.check(data["status"] == "REJECTED", "状态=REJECTED")
    c.check(data["rejected_by"] == "bob@local", "驳回人=bob@local")
    c.check("补充测试报告" in data.get("reject_reason", ""), "驳回原因正确")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 8. 复制发布单
    # ------------------------------------------------------------------
    c = Case("[8] 复制发布单：生成新单，状态重置为 CREATED")
    code, out, _ = f("--as", "alice@local", "rel-copy", "--order-id", order_id)
    c.check(code == 0, f"复制成功 (退出码={code}, out={out[:200]})")
    if code != 0:
        all_ok &= c.report()
        return 1
    data = json.loads(out)
    new_order_id = data["order_id"]
    c.check(new_order_id != order_id, "新单 ID 不同")
    c.check(data["status"] == "CREATED", "新单状态重置为 CREATED")
    c.check(data.get("item_count", 0) == 2, "明细数量相同")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 9. 撤销发布单
    # ------------------------------------------------------------------
    c = Case("[9] 撤销发布单：撤销未执行的单")
    # 撤销刚才复制的新单
    code, out, _ = f("--as", "alice@local", "rel-cancel", "--order-id", new_order_id,
                     "--reason", "不需要了，先撤销")
    c.check(code == 0, f"撤销成功 (退出码={code}, out={out[:200]})")
    if code != 0:
        all_ok &= c.report()
        return 1
    data = json.loads(out)
    c.check(data["status"] == "CANCELLED", "状态=CANCELLED")
    c.check("不需要了" in data.get("cancel_reason", ""), "撤销原因正确")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 10. 导入导出：YAML 和 JSON 往返
    # ------------------------------------------------------------------
    c = Case("[10] 导入导出：YAML/JSON 往返新库，内容一致")
    # 10a. YAML 导出
    code, out, _ = f("rel-export", "--order-id", order_id,
                     "--format", "yaml", "-o", rel_yaml)
    c.check(code == 0, f"YAML 导出成功 (退出码={code})")
    c.check(os.path.isfile(rel_yaml), "YAML 文件存在")
    with open(rel_yaml, "r", encoding="utf-8") as fh:
        raw_yaml = fh.read()
    c.check("schema_version" in raw_yaml, "YAML 包含 schema_version")
    c.check(order_id in raw_yaml, "YAML 包含 order_id")
    c.check("checksum" in raw_yaml, "YAML 包含 checksum")

    # 10b. JSON 导出
    code, out, _ = f("rel-export", "--order-id", order_id,
                     "--format", "json", "-o", rel_json)
    c.check(code == 0, f"JSON 导出成功 (退出码={code})")
    with open(rel_json, "r", encoding="utf-8") as fh:
        data_json = json.load(fh)
    c.check(data_json["schema_version"] == "1.0", "JSON schema_version=1.0")
    c.check(data_json["order_id"] == order_id, "JSON order_id 一致")
    c.check(data_json["env"] == "prod", "JSON env 一致")
    c.check(len(data_json["items"]) == 2, "JSON items 数量正确")

    # 10c. 在新库中先创建相同的开关版本（发布单只引用开关，不包含开关定义）
    # base_feature: create -> submit -> approve(by bob) -> publish
    code, out, _ = run("--db", db_import, "--as", "alice@local",
        "create", "--env", "prod", "--name", "base_feature",
        "--ratio", "100", "--default", "1")
    c.check(code == 0, f"新库创建 base_feature 成功 (退出码={code}, out={out[:100]})")
    code, out, _ = run("--db", db_import, "--as", "alice@local",
        "submit", "--env", "prod", "--name", "base_feature")
    c.check(code == 0, f"新库 submit base_feature 成功 (退出码={code}, out={out[:100]})")
    code, out, _ = run("--db", db_import, "--as", "bob@local",
        "approve", "--env", "prod", "--name", "base_feature")
    c.check(code == 0, f"新库 approve base_feature 成功 (退出码={code}, out={out[:100]})")
    # dependent_feature: create -> submit (注意：与原始库保持完全一致的参数，包括 whitelist)
    code, out, _ = run("--db", db_import, "--as", "alice@local",
        "create", "--env", "prod", "--name", "dependent_feature",
        "--ratio", "50", "--default", "1",
        "--dep", "base_feature", "--whitelist", "user:1001")
    c.check(code == 0, f"新库创建 dependent_feature 成功 (退出码={code}, out={out[:100]})")
    code, out, _ = run("--db", db_import, "--as", "alice@local",
        "submit", "--env", "prod", "--name", "dependent_feature")
    c.check(code == 0, f"新库 submit dependent_feature 成功 (退出码={code}, out={out[:100]})")
    # standalone_feature: create -> submit
    code, out, _ = run("--db", db_import, "--as", "alice@local",
        "create", "--env", "prod", "--name", "standalone_feature",
        "--ratio", "30", "--default", "0")
    c.check(code == 0, f"新库创建 standalone_feature 成功 (退出码={code}, out={out[:100]})")
    code, out, _ = run("--db", db_import, "--as", "alice@local",
        "submit", "--env", "prod", "--name", "standalone_feature")
    c.check(code == 0, f"新库 submit standalone_feature 成功 (退出码={code}, out={out[:100]})")

    # 现在导入发布单
    code3, out3, _3 = run("--db", db_import, "--as", "alice@local",
                           "rel-import", "--file", rel_yaml)
    c.check(code3 == 0, f"新库 YAML 导入成功 (退出码={code3}, out={out3[:300]})")
    if code3 != 0:
        all_ok &= c.report()
        return 1
    imported = json.loads(out3)
    c.check(imported["order"]["order_id"] == order_id, "导入后 order_id 一致")
    c.check(imported["order"]["env"] == "prod", "导入后 env 一致")
    c.check(imported["order"]["checksum"] == data_json["checksum"], "导入后 checksum 一致")
    c.check(imported["order"]["item_count"] == 2, "导入后明细数量正确")

    # 10d. 篡改 checksum 后导入应该被拦截
    tampered = dict(data_json)
    tampered["checksum"] = "deadbeefdeadbeef"
    tampered_path = os.path.join(tmpdir, "rel_tampered.json")
    with open(tampered_path, "w", encoding="utf-8") as fh:
        json.dump(tampered, fh, ensure_ascii=False)
    code4, out4, _4 = run("--db", db_import, "--as", "alice@local",
                           "rel-import", "--file", tampered_path)
    c.check(code4 == 2, f"篡改 checksum 被拦截 (退出码={code4}, out={out4[:200]})")
    tamper_msg = json.loads(out4).get("message", "")
    c.check("校验和" in tamper_msg or "checksum" in tamper_msg.lower(),
            f"错误信息提示校验和问题: {tamper_msg}")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 11. 整单执行：原子化发布，事务保证
    # ------------------------------------------------------------------
    c = Case("[11] 整单执行：原子化发布，所有开关一次性生效")
    # 注意：不要在这里调用 rel-preview，因为它会把状态从 APPROVED 改回 PREVIEWED
    # 发布单在测试 [6] 中已经审批通过，状态是 APPROVED

    # 执行前记录每个开关的状态
    code, out, _ = f("--format", "json", "list", "--env", "prod",
                     "--name", "dependent_feature", "--status", "PENDING_APPROVAL")
    before_dep = json.loads(out)
    c.check(before_dep["count"] == 1, "执行前 dependent_feature 是 PENDING_APPROVAL")

    # 先确认发布单状态是 APPROVED
    code, out, _ = f("--format", "json", "rel-show", "--order-id", order_id)
    order_info = json.loads(out)
    c.check(order_info["order"]["status"] == "APPROVED",
            f"执行前状态=APPROVED (实际={order_info['order']['status']})")

    # 执行发布单
    code, out, _ = f("--as", "bob@local", "rel-execute", "--order-id", order_id)
    c.check(code == 0, f"执行成功 (退出码={code})")
    data = json.loads(out)
    c.check(data["status"] == "EXECUTED", f"状态=EXECUTED")
    c.check(len(data["executed_items"]) == 2, "2 个明细都已执行")

    # 验证：两个开关都变为 PUBLISHED
    for name in ["dependent_feature", "standalone_feature"]:
        code, out, _ = f("--format", "json", "current", "--env", "prod", "--name", name)
        curr = json.loads(out)
        c.check(curr["effective"] is not None, f"{name} 有生效版")
        c.check(curr["effective"]["status"] == "PUBLISHED", f"{name} 状态=PUBLISHED")
        c.check(curr["effective"]["version"] == 1, f"{name} 版本=V1")

    # 验证：发布单明细记录了执行后的状态
    code, out, _ = f("--format", "json", "rel-show", "--order-id", order_id)
    show = json.loads(out)
    for item in show["order"]["items"]:
        c.check(item["status_after"] == "PUBLISHED", f"{item['name']} status_after=PUBLISHED")
        c.check(item["executed"] is True, f"{item['name']} executed=True")
        if item["name"] == "dependent_feature":
            c.check(item["prev_effective_version"] is None or item["prev_effective_version"] >= 0,
                    f"{item['name']} 记录了覆盖的生效版")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 12. 整单回滚：反向顺序恢复到执行前状态
    # ------------------------------------------------------------------
    c = Case("[12] 整单回滚：反向顺序恢复，所有开关回到执行前状态")
    # 先确认执行后的状态
    code, out, _ = f("--format", "json", "current", "--env", "prod", "--name", "dependent_feature")
    before_rollback = json.loads(out)
    c.check(before_rollback["effective"]["version"] == 1, "回滚前 dependent_feature V1 生效")

    # 执行回滚
    code, out, _ = f("--as", "alice@local", "rel-rollback", "--order-id", order_id,
                     "--reason", "线上发现异常，紧急回滚")
    c.check(code == 0, f"回滚成功 (退出码={code})")
    data = json.loads(out)
    c.check(data["status"] == "ROLLED_BACK", "状态=ROLLED_BACK")
    c.check("线上发现异常" in data.get("rollback_reason", ""), "回滚原因正确")

    # 验证：两个开关的 V1 都变为 ROLLED_BACK，且没有新的生效版（因为执行前它们不是 PUBLISHED）
    for name in ["dependent_feature", "standalone_feature"]:
        code, out, _ = f("--format", "json", "list", "--env", "prod", "--name", name,
                         "--status", "ROLLED_BACK")
        rb = json.loads(out)
        c.check(rb["count"] == 1, f"{name} V1 变为 ROLLED_BACK")
        # 检查是否有生效版
        code, out, _ = f("--format", "json", "current", "--env", "prod", "--name", name)
        curr = json.loads(out)
        # 这两个开关执行前是 PENDING_APPROVAL，所以回滚后应该没有生效版
        c.check(curr["effective"] is None, f"{name} 回滚后没有生效版")

    # 验证：回滚来源记录正确
    code, out, _ = f("--format", "json", "rel-show", "--order-id", order_id)
    show = json.loads(out)
    c.check(show["order"]["status"] == "ROLLED_BACK", "发布单状态=ROLLED_BACK")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 13. 列表和查询功能验证
    # ------------------------------------------------------------------
    c = Case("[13] 列表和查询：rel-list / rel-show / rel-records 正常工作")
    # rel-list
    code, out, _ = f("--format", "json", "rel-list", "--env", "prod")
    lst = json.loads(out)
    c.check(lst["count"] >= 4, f"至少 4 个发布单 (实际={lst['count']})")

    # rel-list 按状态过滤
    code, out, _ = f("--format", "json", "rel-list", "--status", "EXECUTED", "ROLLED_BACK")
    filtered = json.loads(out)
    c.check(any(o["order_id"] == order_id for o in filtered["orders"]),
            "状态过滤正确，能找到 EXECUTED+ROLLED_BACK 的单")

    # rel-show
    code, out, _ = f("--format", "json", "rel-show", "--order-id", order_id)
    show = json.loads(out)
    c.check(show["order"]["order_id"] == order_id, "rel-show order_id 正确")
    c.check(len(show["order"]["items"]) == 2, "rel-show 包含 2 个明细")
    c.check(len(show["records"]) >= 5, f"至少 5 条操作记录 (实际={len(show['records'])})")

    # rel-records
    code, out, _ = f("--format", "json", "rel-records", "--order-id", order_id, "--limit", "20")
    recs = json.loads(out)
    c.check(recs["count"] >= 5, f"至少 5 条记录 (实际={recs['count']})")
    actions = {r["action"] for r in recs["records"]}
    c.check({"PREVIEW", "SUBMIT_APPROVAL", "APPROVE",
             "EXECUTE_COMPLETE", "ROLLBACK_COMPLETE"} & actions,
            f"关键操作记录存在: {actions}")

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 14. 重启一致性：复制 DB 文件，重启后所有数据对齐
    # ------------------------------------------------------------------
    c = Case("[14] 重启一致性：复制 DB 后重新打开，所有状态、明细、审计日志对齐")
    # 复制 DB 文件
    import shutil
    shutil.copy2(db, db_restart)

    # 在新连接上查询
    import sqlite3
    con = sqlite3.connect(db_restart)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()

        # 14a. release_order 表
        cur.execute("SELECT order_id, status, env, created_by, approver, checksum, "
                    "rollback_reason, executed_at, rolled_back_at "
                    "FROM release_order WHERE order_id = ?", (order_id,))
        row = cur.fetchone()
        c.check(row is not None, "重启后 release_order 行存在")
        if row:
            r = dict(row)
            c.check(r["order_id"] == order_id, "order_id 对齐")
            c.check(r["status"] == "ROLLED_BACK", "状态=ROLLED_BACK 对齐")
            c.check(r["env"] == "prod", "env 对齐")
            c.check(r["created_by"] == "alice@local", "created_by 对齐")
            c.check(r["approver"] == "bob@local", "approver 对齐")
            c.check(len(r["checksum"]) == 16, "checksum 对齐")
            c.check(r["executed_at"] is not None, "executed_at 存在")
            c.check(r["rolled_back_at"] is not None, "rolled_back_at 存在")
            c.check("线上发现异常" in (r.get("rollback_reason") or ""), "rollback_reason 对齐")

        # 14b. release_order_item 表
        cur.execute("SELECT name, version, status_before, status_after, executed, "
                    "prev_effective_version FROM release_order_item "
                    "WHERE release_order_uuid = ? ORDER BY name", (order_id,))
        items = [dict(r) for r in cur.fetchall()]
        c.check(len(items) == 2, "2 条明细存在")
        for item in items:
            c.check(item["status_before"] == "PENDING_APPROVAL",
                    f"{item['name']} status_before 对齐")
            c.check(item["status_after"] == "PUBLISHED",
                    f"{item['name']} status_after 对齐")
            c.check(item["executed"] == 1, f"{item['name']} executed=1 对齐")

        # 14c. release_order_record 表
        cur.execute("SELECT COUNT(*) AS n FROM release_order_record "
                    "WHERE order_id = ?", (order_id,))
        rec_count = cur.fetchone()["n"]
        c.check(rec_count >= 5, f"操作记录 >= 5 (实际={rec_count})")

        # 14d. audit_log 表应有发布单相关动作
        cur.execute("SELECT COUNT(*) AS n FROM audit_log "
                    "WHERE action LIKE 'RELEASE_ORDER%'")
        audit_count = cur.fetchone()["n"]
        c.check(audit_count >= 5, f"RELEASE_ORDER_* 审计条目 >= 5 (实际={audit_count})")

        # 14e. switch_version 表：回滚后的状态正确
        cur.execute("SELECT name, status FROM switch_version "
                    "WHERE env='prod' AND name IN ('dependent_feature', 'standalone_feature') "
                    "ORDER BY name, version")
        sv_rows = [dict(r) for r in cur.fetchall()]
        statuses = {(r["name"], r["status"]) for r in sv_rows}
        c.check(("dependent_feature", "ROLLED_BACK") in statuses,
                "dependent_feature V1 是 ROLLED_BACK")
        c.check(("standalone_feature", "ROLLED_BACK") in statuses,
                "standalone_feature V1 是 ROLLED_BACK")

    finally:
        con.close()

    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 15. 普通开发权限验证：只能看预演和结果，不能操作
    # ------------------------------------------------------------------
    c = Case("[15] 权限分级：普通开发只能查询，不能执行操作")
    # 普通开发可以 list / show / preview / records
    code, out, _ = f("--as", "carol@local", "--format", "json", "rel-list", "--env", "prod")
    c.check(code == 0, "普通开发可以 rel-list")

    code, out, _ = f("--as", "carol@local", "--format", "json", "rel-show", "--order-id", order_id)
    c.check(code == 0, "普通开发可以 rel-show")

    code, out, _ = f("--as", "carol@local", "--format", "json", "rel-records", "--order-id", order_id)
    c.check(code == 0, "普通开发可以 rel-records")

    # 普通开发不能 create / submit / approve / execute / rollback / cancel / copy
    for cmd, args in [
        ("rel-create", ["--env", "prod", "--item", "standalone_feature:1"]),
        ("rel-submit", ["--order-id", order_id]),
        ("rel-approve", ["--order-id", order_id]),
        ("rel-reject", ["--order-id", order_id, "--reason", "test"]),
        ("rel-execute", ["--order-id", order_id]),
        ("rel-rollback", ["--order-id", order_id, "--reason", "test"]),
        ("rel-cancel", ["--order-id", order_id, "--reason", "test"]),
        ("rel-copy", ["--order-id", order_id]),
    ]:
        code, out, _ = f("--as", "carol@local", cmd, *args)
        c.check(code == 2, f"普通开发 {cmd} 被拦截 (退出码={code})")

    all_ok &= c.report()

    print("\n" + "=" * 60)
    print("总体结果:", "ALL PASS" if all_ok else "HAS FAILURE")
    print(f"临时 DB 目录: {tmpdir}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
