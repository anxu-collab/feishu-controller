---
name: feishu-controller
description: |
  飞书/Lark 多维表格中控（读写分离，以在线多维表格为唯一事实源）：接管多维表格的数据盘点、口径对齐、结构漂移检测、四段式经营/项目汇报、同口径环比分析，以及带预览确认、幂等查重与逐行回执的受控回写。
  触发场景："看看飞书多维表格""做个多维表格汇报/周报/日报""统计一下飞书表数据""把这些记录写回飞书表格""录入多维表格""Feishu/Lark Bitable briefing, metrics, or writeback"。
  不适用于搭建常驻网关/机器人服务、整库批量迁移导表，或直接编辑飞书云文档正文。
---

# Feishu Controller（飞书多维表格中控）

把**在线飞书/Lark 多维表格**作为唯一事实源，以“汇报为主（约 80%）、受控回写为辅（约 20%）”的读写分离方式完成业务汇报与数据入表，并在本地按月追加只用于审计与环比基线的 JSONL 留痕（ledger）。本 Skill 是按需触发的交互流程，不启动任何常驻服务。

## 铁律（开始前必须遵守）

1. **在线多维表格是唯一事实源**：所有汇报数据必须在线实时读取。**严禁从本地 JSONL 留痕中读取业务事实**——本地 JSONL 仅用于审计追踪，唯一允许读作业务参考的场景是**严格同口径的环比基线**（且必须在汇报中注明“基线为本地快照”）。
2. **先确定结果，再写留痕**：不允许在汇报完成或回写确认之前预先写入留痕。每条路径只在终态确定后追加一次。
3. **回写必须经过“预览 → 用户明确确认 → 查重 → 写入 → 逐行回执”**：未经确认绝不调用写接口；用户拒绝的候选数据也须留痕（`status: "rejected"`），用于审计但不作为事实数据。
4. **任何 Agent 宿主均可运行**：不绑定特定 MCP 工具名、不强制依赖 Python 或持久化文件系统。两个脚本仅为确定性加速工具，无 Python 或无脚本时由 Agent 按 `references/ledger-schema.md` 规范读写；无任何可写存储时，**照常汇报并在汇报中醒目标明“本次未留痕”**。
5. **口径有歧义或表结构漂移时，每次只问 1 个最关键的问题**：不要连续审讯用户；确认后的口径持久化到 `lens_confirmed` 事件，后续复用不再重复询问。

## 文件与资源导航（按需加载）

- `references/capability-and-urls.md` — 能力握手清单（含可选 `resolve_wiki_node`）、`feishu.cn` / `larksuite.com` / wiki / 嵌入多维表格的 URL 解析、自动接入顺序、权限与读取身份处理。
- `references/ledger-schema.md` — Ledger 目录定位顺序、按月分文件规则、三类事件（`lens_confirmed`、`briefing_snapshot`、`bitable_writeback`）的完整 JSON Schema、脱敏与体积约束、跨进程文件锁规范。
- `references/briefing-and-writeback.md` — 四段式汇报模板、口径对齐与环比五项一致性门禁、数据量阈值与降采样策略、特殊字段归一化、受控回写预览表与幂等查重/部分失败重试规则。
- `scripts/ledger_append.py` — 可选加速脚本：带跨平台锁文件、按月切分、校验外层固定字段的原子追加工具（支持 Windows / macOS / Linux）。
- `scripts/bitable_metrics.py` — 可选加速脚本：字段指纹计算（`field_id` + `type` + `reader_identity`）、特殊字段归一化、按去重键确定性去重、聚合与严格五项一致环比对比。

---

## 执行流程

### Gate 0：锁定在线文档与定位 Ledger

执行详细规则见 `references/capability-and-urls.md` 与 `references/ledger-schema.md`。

1. **能力握手**：盘点当前宿主具备的飞书/Lark 多维表格读取、写入、可选 `resolve_wiki_node`、可选云文档搜索能力，以及本地文件读写/Python 执行能力。
   - 缺读能力 → 直接停止，告知用户需要开启飞书/Lark 多维表格读取能力。
   - 缺 `resolve_wiki_node` 但用户给的是 `/wiki/` 链接 → 请用户直接提供 `/base/<app_token>` 链接（或表格内复制的 Base 链接）。
   - 无可写本地存储 → 标记 `ledger_mode = "unavailable"`，**不中断**，汇报末尾标注“本次未留痕”。
2. **定位目标多维表格（自动接入顺序）**：
   1. 用户本次已提供链接 / `app_token` → 解析 `app_token`、`table_id`、`view_id`（支持 `feishu.cn` 与 `larksuite.com`，含文档内嵌多维表格块）。
   2. 未提供时，先查 Ledger 目录下最近使用过的 `<app_token>` 记录，向用户一句确认是否继续该表。
   3. 若无历史记录且宿主有搜索能力 → 搜索候选多维表格并列出让用户选。
   4. 均不可得 → 向用户索要多维表格链接。
3. **连通性与身份确认**：读表列表与目标表元数据验证可达性；记录当前读取身份（`reader_identity`，如应用/用户标识或 `"anonymous"`），避免高级权限下不同可见字段被误判为表结构漂移。
4. **定位 Ledger 根目录**（按顺序命中即止）：
   1. 环境变量 `FEISHU_CONTROLLER_HOME`
   2. 当前工作区根目录下的 `.feishu-controller/`
   3. 用户主目录下的 `.feishu-controller/`
   - 均不可写 → 进入 `ledger_mode = "unavailable"`。

### Step 1：读表结构与对齐口径

详细规则见 `references/briefing-and-writeback.md`。

1. **拉取目标表字段定义**（完整分页，防止字段 >100 截断）。
2. **计算结构指纹（`schema_fingerprint`）**：
   - 仅使用每个字段的 `field_id` + `type`（按 `field_id` 排序后哈希），**不使用字段名**，这样重命名字段不会误报结构漂移。
   - 连同 `reader_identity` 一并记录；仅在同一 `reader_identity` 下比较指纹是否漂移。
3. **加载最近口径（`lens_confirmed`）**：
   - 若 Ledger 可读，查找同一 `(app_token, table_id)` 最近一条 `lens_confirmed` 事件（含 `lens_version`、指标定义、去重键 `dedup_keys`、时间字段、时区默认 `Asia/Shanghai`、视图/筛选范围）。
   - 首次接入、存在结构漂移、或业务意图与已存口径冲突时：**只问 1 个最关键的澄清问题**（给出默认建议选项让用户拍板）。
   - 若用户确认了新口径或修订了旧口径：递增 `lens_version`，立即追加一条 `lens_confirmed` 事件到当月 JSONL（若可写）。
   - 口径尚未确认前，去重键默认仅使用 `record_id`。

### Step 2：在线取数与确定性计算

1. **压低数据量再取数**：优先使用用户指定视图（`view_id`）、筛选条件和 `field_names` 投影只取需要的字段。
2. **数据量门禁**：预估或分页累计超过 **5,000 行** 时暂停全量拉取，向用户说明当前体量并请其通过视图、时间窗或筛选条件缩小范围。
3. **完整分页取数**：持续翻页直至 `has_more` 为 false，严禁只拿第一页就出结论。
4. **归一化与去重计算**：
   - 归一化飞书特殊字段（单选/多选、人员、毫秒时间戳、公式/查找引用数组、超链接、富文本段）。
   - 按已确认口径的 `dedup_keys` 去重（未确认前按 `record_id`）。
   - 优先把拉取结果交给 `scripts/bitable_metrics.py` 做确定性去重、聚合与环比计算；若宿主无 Python 或无法运行脚本，由 Agent 直接计算，并**必须在汇报中注明“计算未经脚本校验”**。

### Step 3a：汇报路径（主路径，约 80%）

完整模板见 `references/briefing-and-writeback.md`。

1. **环比准入检查（五项全一致）**：仅当 `table_id`、`view_id`、筛选条件（`filter_signature`）、`lens_version`、`schema_fingerprint`（同一 `reader_identity`）**五项全部一致**时，才与最近一次 `briefing_snapshot` 做环比；任一项变化即标注“不可比（原因：…）”，不报错也不强行计算。
2. **输出四段式汇报**：
   - ① 核心结论（结论先行 + 关键指标 + 环比/不可比说明）
   - ② 结构拆解与明细线索（附引用的 `record_id`，脱敏非必要个人隐私）
   - ③ 异常、风险与待决事项
   - ④ 口径与可信度声明（表/视图/筛选/口径版本/时区、是否经脚本校验、若含环比注明“基线为本地快照；若在线历史记录被人修改，环比差值会混入该修改”、若无法写留痕注明“本次未留痕”）
3. **汇报完成后追加留痕**：往 `<ledger_root>/<app_token>/YYYY-MM.jsonl` 追加一条 `briefing_snapshot` 事件。
   - **严禁存整行原始记录**，只存汇总指标、汇报结论摘要与引用到的 `record_id` 列表，控制体积并避免留存姓名/手机号等 PII。

### Step 3b：受控回写路径（辅路径，约 20%）

完整规则见 `references/briefing-and-writeback.md`。

1. **提取与字段映射**：将待写入内容映射到目标表真实字段名与类型（检查单选/多选枚举、日期毫秒时间戳、数字类型等），标出缺失或需人工补齐的字段。
2. **展示预览并等待确认**：以清晰表格列出每一条候选记录的操作类型（新增/更新）、关键字段值、目标表名，明确询问用户“确认写入 / 修改某条 / 取消”。
   - **用户拒绝或取消**：不调用任何写接口；直接追加一条 `bitable_writeback` 事件，设 `status: "rejected"`，记录拒绝原因与条数摘要（不存敏感明文），结束本次回写。
3. **幂等查重（写前必做）**：用户确认后，按已确认口径的 `dedup_keys`（若无则按主键字段）在线查询目标表是否已存在同键记录：
   - 已存在且用户意图为新增 → 提示重复并默认跳过或转更新（按用户确认策略），防止重试写出重复行。
4. **分批执行与逐行回执**：
   - 记录每一行的结果（`index`、`action`、`status`: `succeeded` / `failed` / `skipped`、`record_id`、`error`）。
   - 若批量写入仅部分成功 → 向用户报告成功条数、失败条数与逐行错误原因；**重跑时仅重试失败行**，绝不重放已成功行。
5. **写入完成后追加留痕**：根据最终结果确定整体 `status`（`confirmed` / `partial` / `failed`），追加一条 `bitable_writeback` 事件（含逐行回执与 `record_id`，不含整行 PII 原文）。

---

## 降级与安全底线

- **无脚本环境**：手动按 `references/ledger-schema.md` 的 JSONL 格式单行追加，汇报注明“计算未经脚本校验”。
- **无可写存储**：汇报照常交付，标注“本次未留痕”；回写仍须完整执行预览、确认、查重与回执报告，并提示“本次回写未写入本地审计留痕”。
- **凭证与隐私**：不在留痕、报错或汇报中输出 App Secret、Token、手机号、身份证号等敏感明文。
