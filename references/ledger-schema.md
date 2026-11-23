# Ledger（JSONL 审计留痕）规范

本文件定义了本地 JSONL 留痕的目录定位、分月规则、事件结构、脱敏与体积约束，以及跨进程安全追加方式。任何能写文件的 Agent 宿主均可严格按此规范直接追加，不必依赖 Python 脚本。

---

## 1. 核心定位与唯一例外

- **审计留痕，不是影子库**：JSONL 只用于事后审计与追溯。**汇报所用的业务事实一律在线实时拉取，严禁从 JSONL 读回作为当前业务事实。**
- **唯一例外**：严格满足五项一致性门禁时，允许读取最近一条 `briefing_snapshot` 中的汇总指标作为**环比基线**（汇报中必须标明“基线为本地快照”）。
- **无独立 `extracted_data` 事件**：提取但未写入的数据不落单独事件，统一归入 `bitable_writeback`（被用户拒绝记 `status: "rejected"`），避免本地长出影子库。

---

## 2. 目录定位顺序与不可写降级

按以下优先级查找 Ledger 根目录（命中第一个存在或可创建的目录即止）：

1. 环境变量 `FEISHU_CONTROLLER_HOME`
2. 当前工作区根目录下的 `.feishu-controller`（即 `<workspace_root>/.feishu-controller`）
3. 用户主目录下的 `.feishu-controller`（Linux/macOS: `~/.feishu-controller`，Windows: `%USERPROFILE%\.feishu-controller`）

若三级路径均不可写（如只读沙箱、无持久化磁盘的云端 Agent / 飞书机器人宿主）：
- 设置 `ledger_mode = "unavailable"`；
- **照常完成在线取数、计算与汇报**；
- 在汇报的「口径与可信度声明」中醒目标明：**“本次未留痕（当前环境无可写存储）”**；
- 回写路径仍须完整执行预览、确认、查重、写入与回执报告，并提示“本次回写未写入本地审计留痕”。

---

## 3. 文件布局与按月切分

```text
<ledger_root>/
├── recent.json                           # 可选：记录最近访问的 app_token / table_id 以便自动接入
└── <app_token>/
    ├── 2026-10.jsonl                     # 按目标时区（默认 Asia/Shanghai）的 YYYY-MM 切分
    ├── 2026-11.jsonl
    └── .lock                             # 写入时使用的跨进程锁文件（写完自动释放）
```

切分与编码规则：
- 文件编码统一 **UTF-8（无 BOM）**，行尾 `\n`，每条事件占严格一行合法 JSON（JSONL）。
- 月份 `YYYY-MM` 按事件 `ts` 在目标时区（默认 `Asia/Shanghai`）的年月确定。
- 跨月查找最近 `lens_confirmed` 或 `briefing_snapshot` 时，按文件名倒序（最新月份往前）扫描最近文件，直到命中目标 `(table_id, ...)`。

---

## 4. 并发与原子写入规范（含 Windows 兼容）

Windows 上多个 Agent 同时对同一 JSONL 做普通追加可能出现行交错或共享冲突。写入须遵守：

1. **优先调用 `scripts/ledger_append.py`**：内部使用 `<app_token>/.lock` 文件锁（Windows 采用 `O_CREAT | O_EXCL` 原子建锁 + 陈旧锁超时清理 + 短暂退避重试），将整条 JSON 序列化为单个 `bytes` 块一次 `write()` + `flush()` + `fsync()`，写完立刻删除锁文件。
2. **无 Python 时手动追加**：先尝试独占创建 `.lock`（若支持），将单条压缩 JSON（末尾带 `\n`）一次性追加写入，避免分多次写入同一行。

---

## 5. 外层固定字段（脚本严格校验）

由于不同行业的 `payload` 差异极大，**校验脚本只校验外层固定字段，不限制 `payload` 内部业务键**。

每条 JSONL 事件必须包含以下外层字段：

```json
{
  "schema_version": 1,
  "event_id": "evt_20261124T101500Z_8f3a2c",
  "event_type": "lens_confirmed | briefing_snapshot | bitable_writeback",
  "ts": "2026-11-24T18:15:00+08:00",
  "timezone": "Asia/Shanghai",
  "app_token": "bascnXXXXXXXXXXXX",
  "table_id": "tblXXXXXXXXXXXX",
  "reader_identity": "default",
  "payload": {}
}
```

外层字段约束：
- `schema_version`：整数，当前固定 `1`。
- `event_id`：非空唯一字符串（建议 `evt_<UTC时间戳>_<6位随机十六进制>`）。
- `event_type`：仅允许以下三类枚举之一：
  1. `lens_confirmed`
  2. `briefing_snapshot`
  3. `bitable_writeback`
- `ts`：带时区偏移的 ISO 8601 时间戳（如 `2026-11-24T18:15:00+08:00`）。
- `timezone`：IANA 时区名，默认 `"Asia/Shanghai"`（用户可在口径中修改）。
- `app_token`、`table_id`、`reader_identity`：非空字符串。
- `payload`：JSON 对象（`dict`），内容随 `event_type` 与行业变化。

---

## 6. 三类事件的 `payload` 约定

### 6.1 `lens_confirmed`（业务口径确认事件）

在 **Step 1** 用户首次确认或修订口径后追加。下次读取同 `(app_token, table_id)` 时，直接取最近一条，不再重新推断。

```json
{
  "lens_version": 2,
  "table_name": "任务表",
  "view_id": "vewXXXXXXXX",
  "filter_signature": "status!=已取消",
  "schema_fingerprint": "sha256:9c4a...",
  "dedup_keys": ["任务标题", "负责人"],
  "time_field": "更新时间",
  "timezone": "Asia/Shanghai",
  "metrics": [
    {"name": "总任务数", "op": "count"},
    {"name": "逾期数", "op": "count_where", "rule": "截止日期 < today && 状态 != 已完成"}
  ],
  "notes": "用户确认以任务标题+负责人作为业务去重键"
}
```

### 6.2 `briefing_snapshot`（汇报完成快照）

**只能在 Step 3a 汇报输出完成后追加**，确保快照包含最终汇报的结论与汇总数据。

**隐私与体积硬红线**：
- **禁止保存整行原始记录**（避免把姓名、手机号、邮箱、客户明文写进本地且导致文件膨胀）。
- 只保存：本次汇聚指标、汇报结论摘要、引用的 `record_id` 列表、五项环比核对字段与脚本校验标记。

```json
{
  "lens_version": 2,
  "table_name": "任务表",
  "view_id": "vewXXXXXXXX",
  "filter_signature": "status!=已取消",
  "schema_fingerprint": "sha256:9c4a...",
  "row_count_raw": 142,
  "row_count_deduped": 138,
  "script_verified": true,
  "comparable_to_prev": true,
  "prev_event_id": "evt_20261117T100000Z_1a2b3c",
  "metrics": {
    "总任务数": 138,
    "逾期数": 6,
    "完成率": 0.7246
  },
  "conclusion_summary": "本周任务总数 138（环比 +12），完成率 72.5%（环比 +4.1pp），6 项逾期集中在供应链联调。",
  "cited_record_ids": ["recAAA", "recBBB", "recCCC"]
}
```

### 6.3 `bitable_writeback`（受控回写审计事件）

**只能在 Step 3b 终态确定后追加**（用户拒绝、全部成功、部分成功或全部失败，均落终态）：

- `status` 枚举（必填）：
  - `"confirmed"`：用户确认且全部行写入成功（或按策略查重跳过无失败）
  - `"partial"`：用户确认，批量中部分行成功、部分行失败
  - `"failed"`：用户确认，但写入全部失败
  - `"rejected"`：用户在预览阶段拒绝或取消（未触发任何写接口，仅审计留痕，**绝不作为业务事实**）

```json
{
  "status": "partial",
  "lens_version": 2,
  "dedup_keys": ["任务标题", "负责人"],
  "summary": {
    "proposed": 5,
    "created": 3,
    "updated": 0,
    "skipped_duplicate": 1,
    "failed": 1
  },
  "rejection_reason": null,
  "rows": [
    {"index": 0, "action": "create", "status": "succeeded", "record_id": "rec101", "dedup_hash": "sha256:aa11...", "error": null},
    {"index": 1, "action": "skip", "status": "skipped", "record_id": "rec088", "dedup_hash": "sha256:bb22...", "error": "duplicate_key"},
    {"index": 2, "action": "create", "status": "failed", "record_id": null, "dedup_hash": "sha256:cc33...", "error": "99992402: 单选值不在枚举内"}
  ]
}
```

> 说明：`rows` 中用 `dedup_hash`（去重键值的哈希）和 `record_id` 标识行，避免把候选行里的姓名、手机号等原始 PII 写入本地审计文件。
