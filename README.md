# 功能开关灰度台 (Feature Switch Gray Console)
==============================================

**纯本地、零依赖外部系统的功能开关灰度发布管理台。提供 CLI 入口，SQLite 持久化，多模块架构实现。

> 不连接任何真实发布平台。所有数据保存在 `data/fswitch.db`（可通过 `--db` 或环境变量 `FSWITCH_DB` 覆盖）。

---

## 1. 模块架构

```
feature_switch/
├── __init__.py           # 版本号
├── __main__.py             # python -m feature_switch 入口
├── core/                   # 领域模型 (零外部依赖)
│   ├── enums.py           # VersionStatus / AuditAction / 状态机
│   └── models.py          # FeatureSwitch / SwitchVersion / AuditLog / VersionDiff
├── storage/                # SQLite 持久化
│   └── repository.py      # SwitchRepository (事务 + 索引)
├── validator/              # 纯函数校验 (不写 DB)
│   └── validators.py      # 比例 / 依赖 / 自审批 / YAML·JSON 解析
├── audit/                  # 审计记录器
│   └── audit.py
├── service/                # 业务编排 (单事务写所有变更)
│   ├── service.py         # 创建/编辑/审批/发布/回滚/废弃/查询
│   └── importer.py        # 导入导出 (事务回滚防半截写)
└── cli/                    # CLI (argparse)
    └── main.py            # fswitch 命令集

examples/                    # 验收样例（好 / 坏配置）
├── good_config.{yaml,json}  ✅ 合法样例
├── bad_ratio.yaml            ❌ 比例 150 越界
├── bad_dep.yaml            ❌ 依赖不存在
└── bad_format.{yaml,json}  ❌ 语法损坏
```

**数据实体关系**
- 每个 `env:name` 是一个 FeatureSwitch；
- 一个开关可以有多个 SwitchVersion（版本号单调递增）；
- 每个版本有 5 种状态之一；
- 所有变更写一份 AuditLog。

---

## 2. 版本状态机

```
DRAFT ──submit──▶ PENDING_APPROVAL ──approve──▶ PUBLISHED ──rollback──▶ ROLLED_BACK
  │                      │    │                    │    │
  │◀─────────────────────┘    │                    │    │
  │      reject (回到草稿)    │                    │    │
  │                           │                    │    │
  ▼                           ▼                    ▼    ▼
DEPRECATED (软删除，所有状态都能到这里)
```

- `DRAFT`: 草稿，允许就地 `edit`。其余状态不可编辑。
- `PUBLISHED`: 唯一"生效版本"。一个开关同时只能有一个 PUBLISHED（批准新发布时，旧 PUBLISHED 自动变 ROLLED_BACK 并写入 `replace_reason`）。
- `ROLLED_BACK`: 回滚后仍保留 `rollback_reason`，可以再次发布。

---

## 3. 安装 & 运行

```bash
# Python 3.9+
pip install -r requirements.txt   # 只有 PyYAML，没装也能用（自带 mini-YAML 解析子集）

# 方式一：用 console_scripts
pip install -e .
fswitch --help

# 方式二：模块式
python -m feature_switch --help
```

常用环境变量：
- `FSWITCH_DB`   SQLite 文件路径
- `FSWITCH_ACTOR` 操作人（用于审计，默认 `developer@local`

---

## 4. 核心命令一览

| 命令 | 作用 |
| --- | --- |
| `fswitch create --env ENV --name X --ratio N [--dep …]` | 创建草稿 |
| `fswitch edit  --env ENV --name X [--ratio N --dep …]` | 编辑草稿 |
| `fswitch submit --env ENV --name X` | DRAFT → PENDING_APPROVAL |
| `fswitch approve --as reviewer --env ENV --name X [--reason …]` | 审批+发布，自动把旧版标 ROLLED_BACK |
| `fswitch reject  --as reviewer --env ENV --name X --reason …` | 驳回回到 DRAFT |
| `fswitch rollback --env ENV --name X --reason … [--restore Vn]` | 回滚（可选恢复到某历史版） |
| `fswitch deprecate --env ENV --name X --reason …` | 软删除 |
| `fswitch list [--env E --status DRAFT PUBLISHED …]` | 查询 |
| `fswitch current --env E --name X` | 同时看生效版 + 草稿（区分！） |
| `fswitch history --env E --name X` | 版本变更历史，含为什么上一版被替换 |
| `fswitch audit [--env E --name X --limit N]` | 审计日志 |
| `fswitch export [--env E] --format yaml -o out.yaml` | 导出当前生效配置 |
| `fswitch import --file examples/good_config.yaml` | 导入（批量创建草稿，事务性） |

全局参数：
- `--db PATH`
- `--as ACTOR`  操作人（审批时 `--as` 必须 `!= author`，否则被拦截）
- `--format table|json`（`export` 用 `--format yaml|json`）

---

## 5. 失败路径拦截说明

| 场景 | 拦截位置 | 退出码 |
| --- | --- | --- |
| `rollout_ratio` 非整数或不在 `[0,100]` | `validators.validate_ratio` | `VALIDATION=2 |
| 依赖开关在同环境未发布且不在当前导入批内 | `validators.validate_dependencies` | `VALIDATION=2` |
| 作者 `==` 审批者（自己审自己的草稿） | `validators.validate_not_self_approve` | `VALIDATION=2` |
| 坏 YAML / 坏 JSON 语法 | `parse_yaml` / `parse_json` | `VALIDATION=2` |
| 状态机不允许的流转 | `validators.validate_transition` | `VALIDATION=2` |
| 编辑非 DRAFT 状态的版本 | service 层 `_resolve_version` | `VALIDATION=2` |
| 回滚没填 `--reason` | service.rollback | `VALIDATION=2` |
| 导入文件中途任何一条失败 → 整个批全部回滚（**不写半截数据** | ConfigImporter 单事务 | `VALIDATION=2` |

---

## 6. 可复现验收命令集

把下面的脚本保存为 `smoke_test.ps1 或逐条复制到 PowerShell，全部命令**应按预期 PASS / FAIL**。

```powershell
# ============ 准备 ============
$DB = "$PWD\data\fswitch_acceptance.db"
Remove-Item $DB -ErrorAction SilentlyContinue
$F  = "python -m feature_switch --db $DB"

Write-Host "=== [1/14] 创建依赖开关 (V1, V2 先发布, ratio=100) ==="
Invoke-Expression "$F --as alice@local create --env prod --name unified_login_v2 --ratio 100 --default 1
Invoke-Expression "$F --as alice@local submit --env prod --name unified_login_v2
Invoke-Expression "$F --as bob@local   approve --env prod --name unified_login_v2 --reason '依赖开关首版上线'
```

预期：三条 OK。最后一条 V1 PUBLISHED。

```powershell
Write-Host "=== [2/14] 创建灰度比例越界 → 必须 FAIL ==="
Invoke-Expression "$F --as alice@local create --env prod --name risky --ratio 150"
# exit code 应为 2; JSON.error == VALIDATION
if ($LASTEXITCODE -ne 2) { throw "FAIL: ratio 没拦住" }
```

```powershell
Write-Host "=== [3/14] 依赖不存在 → 必须 FAIL ==="
Invoke-Expression "$F --as alice@local create --env prod --name bad_dep_switch --ratio 50 --dep ghost_switch_xxx"
if ($LASTEXITCODE -ne 2) { throw "FAIL: dep 缺失没拦住" }
```

```powershell
Write-Host "=== [4/14] 正常创建 DRAFT (依赖 unified_login_v2) ==="
Invoke-Expression "$F --as alice@local create --env prod --name new_checkout --ratio 30 --dep unified_login_v2 --whitelist user:1001 user:1002 --default 1
```

```powershell
Write-Host "=== [5/14] 自己审自己 → 必须 FAIL ==="
Invoke-Expression "$F --as alice@local submit --env prod --name new_checkout
Invoke-Expression "$F --as alice@local approve --env prod --name new_checkout   # author == approver
if ($LASTEXITCODE -ne 2) { throw "FAIL: 自审批没拦住" }
```

```powershell
Write-Host "=== [6/14] 用另一个人审批通过，成功发布 ==="
Invoke-Expression "$F --as bob@local approve --env prod --name new_checkout --reason '灰度首版,依赖 ok,替换原因测试"
```

```powershell
Write-Host "=== [7/14] 查询：区分生效版 vs 草稿 ==="
Invoke-Expression "$F current --env prod --name new_checkout"
# 应看到 effective=PUBLISHED, draft=(无)
```

```powershell
Write-Host "=== [8/14] 坏 YAML 导入 → 0 行写入 (事务回滚) ==="
Invoke-Expression "$F --as alice@local import --file examples/bad_format.yaml"
if ($LASTEXITCODE -ne 2) { throw "FAIL: 坏 YAML 没拦住" }
Invoke-Expression "$F list --env staging"
# staging 应该空（半截数据没写入）
```

```powershell
Write-Host "=== [9/14] 坏比例 YAML 导入 → 0 行写入 ==="
Invoke-Expression "$F --as alice@local import --file examples/bad_ratio.yaml"
if ($LASTEXITCODE -ne 2) { throw "FAIL: 越界比例没拦住" }
```

```powershell
Write-Host "=== [10/14] 合法 YAML 导入成功，且 bad_dep.yaml 里的缺失依赖仍被拦截 ==="
Invoke-Expression "$F --as alice@local import --file examples/good_config.yaml"   # PASS
Invoke-Expression "$F --as alice@local import --file examples/bad_dep.yaml"     # FAIL
if ($LASTEXITCODE -ne 2) { throw "FAIL: bad_dep 没拦住" }
```

```powershell
Write-Host "=== [11/14] 发布 V2：比例调 60% 后发布，旧 V1 自动 ROLLED_BACK，并带替换原因 ==="
Invoke-Expression "$F --as alice@local create --env prod --name new_checkout --ratio 60 --dep unified_login_v2 --default 1
Invoke-Expression "$F --as alice@local submit --env prod --name new_checkout
Invoke-Expression "$F --as bob@local approve --env prod --name new_checkout --reason '扩大灰度到60%'
Invoke-Expression "$F history --env prod --name new_checkout
# history 里 V1 -> V2 的 diff 应带替换原因
```

```powershell
Write-Host "=== [12/14] 回滚 V2 并恢复到 V1 ==="
Invoke-Expression "$F rollback --env prod --name new_checkout --reason '线上监控看到支付错误率飙升' --restore 1
Invoke-Expression "$F current --env prod --name new_checkout
# effective = V3（内容 = V1 ratio=30），并带 replace_reason = '回滚自 V1，原因：线上监控看到支付错误率飙升'
```

```powershell
Write-Host "=== [13/14] 导出 + 再导入（跨重启一致性）==="
Invoke-Expression "$F export --env prod --format yaml -o data\exported_prod.yaml
Invoke-Expression "$DB2 = `"$PWD\data\fswitch_roundtrip.db"
Remove-Item $DB2 -ErrorAction SilentlyContinue
Invoke-Expression "python -m feature_switch --db $DB2 --as importer@local import --file data\exported_prod.yaml"
# 在新 DB 里 list 到的 effective 快照应与原 DB 一致
```

```powershell
Write-Host "=== [14/14] 审计日志 ==="
Invoke-Expression "$F audit --env prod --name new_checkout --limit 20 --format json
```

---

## 7. 重启后一致性验证

```powershell
# 原 DB 重启后仍匹配：
# 1) 当前生效版本 (V3)、
# 2) 每版回滚/替换原因、
# 3) 审计日志、
# 4) 导出的 YAML/JSON 再回导结果
# 全部一致。
python -c "
import sqlite3, json
conn = sqlite3.connect(r'data/fswitch_acceptance.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 1. 生效版
cur.execute("SELECT env,name,version,status,rollout_ratio,replace_reason FROM switch_version
             WHERE status='PUBLISHED' AND env='prod' AND name='new_checkout' ORDER BY version DESC LIMIT 1")
r = cur.fetchone()
assert r, '没有生效版'
print('EFFECTIVE:', dict(r))

# 2. 审计日志数量
cur.execute('SELECT COUNT(*) FROM audit_log')
print('AUDIT_COUNT:', cur.fetchone()[0])

# 3. ROLLED_BACK 版的替换原因
cur.execute("SELECT version,rollback_reason,replace_reason FROM switch_version
             WHERE env='prod' AND name='new_checkout' AND status='ROLLED_BACK'")
for row in cur.fetchall():
    print('ROLLED_BACK VERSION:', dict(row))
"
```

---

## 8. 样例配置文件：`examples/good_config.yaml`

```yaml
schema_version: "1.0"
switches:
  - env: prod
    name: new_checkout_flow
    author: alice@local
    rollout_ratio: 30
    whitelist: [user:1001, user:1002]
    dependencies: [unified_login_v2]
    default_value: true
  - env: prod
    name: unified_login_v2
    author: alice@local
    rollout_ratio: 100
    whitelist: []
    dependencies: []
    default_value: true
  - env: staging
    name: dark_mode_ui
    author: bob@local
    rollout_ratio: 100
    whitelist: [qa:tester-a]
    dependencies: []
    default_value: false
```

导入的开关会被创建为 **DRAFT**（不会自动发布），需要走 `submit → approve` 流程。
