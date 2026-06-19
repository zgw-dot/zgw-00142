"""Automated acceptance tests. Prints PASS/FAIL per step."""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feature_switch.cli.main import build_app, cli  # noqa: E402
from feature_switch.core.enums import VersionStatus  # noqa: E402


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
            self.msgs.append(f"  ❌ {msg}")
        else:
            self.msgs.append(f"  ✅ {msg}")

    def report(self) -> bool:
        status = "PASS" if self.ok else "FAIL"
        print(f"\n[{status}] {self.name}")
        for m in self.msgs:
            print(m)
        return self.ok


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="fswitch_accept_")
    db = os.path.join(tmpdir, "fswitch.db")
    db2 = os.path.join(tmpdir, "fswitch_roundtrip.db")
    export_yaml = os.path.join(tmpdir, "exported.yaml")

    def f(*args: str) -> tuple[int, str, str]:
        return run("--db", db, *args)

    all_ok = True

    # ------------------------------------------------------------------
    # 1. 依赖开关创建 + 审批 + 发布
    # ------------------------------------------------------------------
    c = Case("[1] 创建依赖开关 unified_login_v2 并发布 (V1 PUBLISHED)")
    code, out, _ = f("--as", "alice@local", "create", "--env", "prod", "--name", "unified_login_v2",
                     "--ratio", "100", "--default", "1")
    c.check(code == 0, f"create 返回码 {code}")
    code, out, _ = f("--as", "alice@local", "submit", "--env", "prod", "--name", "unified_login_v2")
    c.check(code == 0, f"submit 返回码 {code}")
    # 自己审批自己 → 应 fail
    code, out, _ = f("--as", "alice@local", "approve", "--env", "prod", "--name", "unified_login_v2")
    c.check(code == 2, f"自审批被拦截 (退出码={code})")
    data = json.loads(out)
    c.check(data.get("error") == "VALIDATION", "错误类型是 VALIDATION")
    # 换 bob 审批
    code, out, _ = f("--as", "bob@local", "approve", "--env", "prod", "--name", "unified_login_v2",
                     "--reason", "依赖开关首版上线")
    c.check(code == 0, f"bob 审批通过 (退出码={code})")
    # 检查状态
    code, out, _ = f("--format", "json", "current", "--env", "prod", "--name", "unified_login_v2")
    c.check(code == 0, "current 查询 OK")
    data = json.loads(out)
    c.check(data["effective"]["status"] == "PUBLISHED", "effective.status = PUBLISHED")
    c.check(data["effective"]["rollout_ratio"] == 100, "ratio = 100")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 2. 灰度比例越界
    # ------------------------------------------------------------------
    c = Case("[2] 灰度比例 150 → 必须 VALIDATION")
    code, out, _ = f("--as", "alice@local", "create", "--env", "prod", "--name", "risky",
                     "--ratio", "150")
    c.check(code == 2, f"create 返回码 {code} (期望 2)")
    data = json.loads(out)
    c.check("比例" in data.get("message", "") or "0 到 100" in data.get("message", ""),
            f"错误信息命中比例限制: {data.get('message')}")
    # 确认没写进去
    code, out, _ = f("--format", "json", "list", "--env", "prod", "--name", "risky")
    data = json.loads(out)
    c.check(data["count"] == 0, f"坏开关未写入 DB (count={data['count']})")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 3. 依赖缺失
    # ------------------------------------------------------------------
    c = Case("[3] 依赖缺失 → 必须 VALIDATION")
    code, out, _ = f("--as", "alice@local", "create", "--env", "prod", "--name", "bad_dep",
                     "--ratio", "50", "--dep", "ghost_switch_xxx")
    c.check(code == 2, f"create 返回码 {code} (期望 2)")
    data = json.loads(out)
    c.check("依赖" in data.get("message", "") and "不存在" in data.get("message", ""),
            f"错误信息命中依赖缺失: {data.get('message')}")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 4. 正常创建 DRAFT (new_checkout 依赖 unified_login_v2)
    # ------------------------------------------------------------------
    c = Case("[4] 正常创建 DRAFT new_checkout + 审批发布 V1")
    code, out, _ = f("--as", "alice@local", "create", "--env", "prod", "--name", "new_checkout",
                     "--ratio", "30", "--dep", "unified_login_v2",
                     "--whitelist", "user:1001", "user:1002", "--default", "1")
    c.check(code == 0, "create OK")
    data = json.loads(out)
    c.check(data["version"]["status"] == "DRAFT", "status = DRAFT")
    c.check(data["version"]["version"] == 1, "版本号 V1")
    # submit
    code, out, _ = f("--as", "alice@local", "submit", "--env", "prod", "--name", "new_checkout")
    c.check(code == 0, "submit OK")
    # approve
    code, out, _ = f("--as", "bob@local", "approve", "--env", "prod", "--name", "new_checkout",
                     "--reason", "灰度首版,依赖 ok")
    c.check(code == 0, "bob approve OK")
    code, out, _ = f("--format", "json", "current", "--env", "prod", "--name", "new_checkout")
    data = json.loads(out)
    c.check(data["effective"]["status"] == "PUBLISHED", "V1 生效")
    c.check(data["effective"]["rollout_ratio"] == 30, "ratio=30")
    c.check(data["draft"] is None, "当前无草稿")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 5. 坏 YAML/JSON 导入 → 0 行写入
    # ------------------------------------------------------------------
    c = Case("[5] 坏 YAML / 坏比例 / 坏依赖 导入 → 事务回滚 0 行")
    here = os.path.dirname(os.path.abspath(__file__))

    for fname, label in [("examples/bad_format.yaml", "坏格式 YAML"),
                          ("examples/bad_format.json", "坏格式 JSON"),
                          ("examples/bad_ratio.yaml", "越界比例 YAML"),
                          ("examples/bad_dep.yaml",   "缺失依赖 YAML")]:
        fpath = os.path.join(here, fname)
        code, out, _ = f("--as", "alice@local", "import", "--file", fpath)
        c.check(code == 2, f"{label}: 退出码={code} (期望 2)")

    # staging 应该为空 (good_config 尚未导入)
    code, out, _ = f("--format", "json", "list", "--env", "staging")
    data = json.loads(out)
    c.check(data["count"] == 0, "坏导入全部回滚 → staging 空")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 6. 合法 YAML 导入
    # ------------------------------------------------------------------
    c = Case("[6] good_config.yaml 导入成功, good_config.json 导入成功")
    here = os.path.dirname(os.path.abspath(__file__))
    code, out, _ = f("--as", "importer@local", "import", "--file",
                      os.path.join(here, "examples/good_config.yaml"))
    c.check(code == 0, f"YAML 导入退出码={code}")
    data = json.loads(out)
    c.check(data["count"] == 3, f"导入 3 条 (实际 {data['count']})")

    code, out, _ = f("--as", "importer@local", "import", "--file",
                      os.path.join(here, "examples/good_config.json"))
    c.check(code == 0, f"JSON 导入退出码={code}")
    data = json.loads(out)
    c.check(data["count"] == 2, f"JSON 导入 2 条 (实际 {data['count']})")

    # 都是草稿状态
    code, out, _ = f("--format", "json", "list", "--status", "DRAFT")
    data = json.loads(out)
    c.check(data["count"] >= 5, f"草稿数量 >= 5 (实际 {data['count']})")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 7. 发布 V2 → V1 自动变 ROLLED_BACK 并带 replace_reason
    # ------------------------------------------------------------------
    c = Case("[7] 发布 V2 (ratio 60)，V1 自动变 ROLLED_BACK 带替换原因")
    code, out, _ = f("--as", "alice@local", "create", "--env", "prod", "--name", "new_checkout",
                     "--ratio", "60", "--dep", "unified_login_v2", "--default", "1")
    c.check(code == 0, "V2 草稿创建 OK")
    f("--as", "alice@local", "submit", "--env", "prod", "--name", "new_checkout")
    code, out, _ = f("--as", "bob@local", "approve", "--env", "prod", "--name", "new_checkout",
                     "--reason", "扩大灰度到60%")
    c.check(code == 0, "V2 approve OK")
    # history 显示 V1 -> V2 的 diff + replace_reason
    code, out, _ = f("--format", "json", "history", "--env", "prod", "--name", "new_checkout")
    hist = json.loads(out)
    c.check(hist["count"] == 2, f"history count=2 (实际 {hist['count']})")
    v2_diff = hist["changes"][-1]
    c.check(v2_diff["from"] == 1 and v2_diff["to"] == 2, "V1 -> V2")
    c.check("扩大灰度到60%" in (v2_diff.get("replace_reason") or ""),
            f"replace_reason 记录原因: {v2_diff.get('replace_reason')}")
    # V1 现在是 ROLLED_BACK
    code, out, _ = f("--format", "json", "list", "--env", "prod", "--name", "new_checkout",
                      "--status", "ROLLED_BACK")
    data = json.loads(out)
    c.check(data["count"] == 1, f"V1 为 ROLLED_BACK (count={data['count']})")
    c.check("扩大灰度到60%" in (data["versions"][0].get("replace_reason") or ""),
            "V1 replace_reason 一致")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 8. 回滚 → 恢复到 V1 (生成 V3 = V1 快照)
    # ------------------------------------------------------------------
    c = Case("[8] rollback + restore=1 → V3 发布且带回滚原因")
    code, out, _ = f("rollback", "--env", "prod", "--name", "new_checkout",
                     "--reason", "线上监控看到支付错误率飙升", "--restore", "1")
    c.check(code == 0, "rollback OK")
    code, out, _ = f("--format", "json", "current", "--env", "prod", "--name", "new_checkout")
    data = json.loads(out)
    c.check(data["effective"]["version"] == 3, "生效版 V3")
    c.check(data["effective"]["rollout_ratio"] == 30, "V3 ratio=30 (与 V1 一致)")
    c.check("回滚" in (data["effective"].get("replace_reason") or ""),
            f"V3 replace_reason 包含回滚说明: {data['effective'].get('replace_reason')}")
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 9. 导出 + 再导入 (跨重启一致性)
    # ------------------------------------------------------------------
    c = Case("[9] 导出 YAML → 导入全新 DB → 内容一致")
    code, out, _ = f("export", "--env", "prod", "--format", "yaml", "-o", export_yaml)
    c.check(code == 0, f"export 退出码={code}")
    c.check(os.path.isfile(export_yaml), f"导出文件存在 {export_yaml}")
    with open(export_yaml, "r", encoding="utf-8") as fh:
        raw = fh.read()
    c.check("schema_version" in raw, "导出文件包含 schema_version")

    # 新建 DB 再导入
    code2, out2, _2 = run("--db", db2, "--as", "reimporter@local", "import", "--file", export_yaml)
    c.check(code2 == 0, f"新 DB 导入退出码={code2}")
    # 查询两个 DB 的 effective 快照内容字段对齐 (ratio / dependencies / default_value / whitelist)
    orig = build_app(db_path=db, actor="x")
    new = build_app(db_path=db2, actor="y")
    try:
        orig_eff = orig.repo.list_published_switches(env="prod")
        new_list = new.repo.list_versions(env="prod")  # 导入的都是 DRAFT
        c.check(len(orig_eff) >= 2, f"原 DB 至少 2 个生效开关 (实际 {len(orig_eff)})")
        c.check(len(new_list) == len(orig_eff),
                f"新 DB 导入 {len(new_list)} 条 vs 原 DB {len(orig_eff)} 个生效开关")
        # 字段一致性抽查: 取 new_checkout V1 DRAFT 与 orig effective V3 内容比对
        orig_nc = [x for x in orig_eff if x.name == "new_checkout"][0]
        new_nc = [x for x in new_list if x.name == "new_checkout"][0]
        c.check(orig_nc.rollout_ratio == new_nc.rollout_ratio, "ratio 一致")
        c.check(orig_nc.default_value == new_nc.default_value, "default_value 一致")
        c.check(orig_nc.dependencies == new_nc.dependencies, "dependencies 一致")
        c.check(orig_nc.whitelist == new_nc.whitelist, "whitelist 一致")
    finally:
        orig.close()
        new.close()
    all_ok &= c.report()

    # ------------------------------------------------------------------
    # 10. 重启一致性: 当前生效版 + 回滚原因 + 审计日志 + 导出内容
    # ------------------------------------------------------------------
    c = Case("[10] 重启一致性（重新打开同一个 DB 仍能读到正确数据）")
    import sqlite3
    con1 = sqlite3.connect(db)
    con1.row_factory = sqlite3.Row
    try:
        cur = con1.cursor()
        cur.execute("SELECT env,name,version,status,rollout_ratio,replace_reason,rollback_reason "
                    "FROM switch_version WHERE status='PUBLISHED' AND env='prod' AND name='new_checkout' "
                    "ORDER BY version DESC LIMIT 1")
        row = cur.fetchone()
        c.check(dict(row)["version"] == 3, "重启后生效版仍是 V3")
        c.check(dict(row)["rollout_ratio"] == 30, "重启后 ratio=30")
        c.check("回滚" in (dict(row).get("replace_reason") or ""),
                "重启后 replace_reason 仍然存在")
        cur.execute("SELECT COUNT(*) AS n FROM audit_log")
        audit_count = cur.fetchone()["n"]
        c.check(audit_count >= 15, f"审计日志条目 >= 15 (实际 {audit_count})")
    finally:
        con1.close()
    all_ok &= c.report()

    print("\n" + "=" * 60)
    print("总体结果:", "🎉 全部 PASS" if all_ok else "💥 存在 FAIL")
    print(f"临时 DB 目录: {tmpdir}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
