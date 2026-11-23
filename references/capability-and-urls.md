# 能力握手、URL 解析与自动接入规范

本文件供 `feishu-controller` 在 **Gate 0** 与 **Step 1** 按需加载。所有规则不绑定特定 MCP 工具名或宿主实现，只定义领域能力契约。

---

## 1. 能力握手清单（Capability Handshake）

开始任务前，先检查当前宿主具备哪些能力，按等级决定运行模式：

| 能力类别 | 能力标识（抽象） | 必需性 | 缺失时处理 |
|---|---|---|---|
| 多维表格元信息读取 | `bitable_read_meta`（列数据表、列字段、列视图） | 必需 | 直接停止，告知用户需要开启飞书/Lark 多维表格读取能力 |
| 多维表格记录读取 | `bitable_read_records`（分页拉取/条件检索） | 必需 | 直接停止 |
| 多维表格记录写入 | `bitable_write_records`（新增/更新/批量） | 回写必需 | 汇报路径照常运行；回写路径停止，改为输出可复制的待填表格 |
| 知识库节点解析 | `resolve_wiki_node`（按 wiki token 查真实 `obj_token` 与 `obj_type`） | 可选 | 遇到 `/wiki/` 链接时停止猜 token，请用户直接提供 `/base/` 链接或在表格右上角复制多维表格链接 |
| 云空间搜索 | `search_bitable`（按关键词搜多维表格） | 可选 | 自动接入跳过搜索，退回历史 ledger 或直接向用户索要链接 |
| 本地文件系统读写 | `local_fs_rw` | 可选 | 设 `ledger_mode = "unavailable"`，**照常汇报并在汇报结尾标注“本次未留痕”** |
| Python 执行 | `python_exec` | 可选 | 跳过两个辅助脚本，由 Agent 直接执行计算与单行 JSONL 写入，并在汇报中标注“计算未经脚本校验” |

---

## 2. 多维表格 URL 解析规则

必须同时支持中国区 **`feishu.cn`** 与国际版 **`larksuite.com`**（含企业自定义子域，如 `*.feishu.cn`、`*.larksuite.com`）。

### 2.1 标准 Base 链接

典型格式：
- `https://<tenant>.feishu.cn/base/<app_token>?table=<table_id>&view=<view_id>`
- `https://<tenant>.larksuite.com/base/<app_token>?table=<table_id>&view=<view_id>`

提取规则：
- `app_token`：路径 `/base/` 后的首段 token（去除 query 与 fragment）
- `table_id`：query 参数 `table`（可选；缺失时在 Gate 0 列出该 Base 下所有数据表让用户选或按上下文匹配）
- `view_id`：query 参数 `view`（可选；若存在则优先纳入取数范围与环比五项条件）
- `domain`：记录 `feishu.cn` 或 `larksuite.com`（便于宿主选择对应开放平台域名）

### 2.2 知识库 Wiki 链接

典型格式：
- `https://<tenant>.feishu.cn/wiki/<node_token>?table=<table_id>&view=<view_id>`
- `https://<tenant>.larksuite.com/wiki/<node_token>?table=<table_id>&view=<view_id>`

处理规则：
1. **切勿直接把 `<node_token>` 当作 `app_token` 调用多维表格接口**（两者不是同一 ID）。
2. 若宿主具备 `resolve_wiki_node`：调用节点查询接口，检查返回 `obj_type == "bitable"`，取 `obj_token` 作为 `app_token`；若 `obj_type == "docx"` / `"doc"`，按 2.3 文档嵌入块流程处理。
3. 若宿主不具备 `resolve_wiki_node`：明确告知用户当前环境无法解析知识库节点，请用户直接粘贴 `/base/<app_token>` 链接（提示：在多维表格内点击“分享/复制链接”或在菜单中选择“在多维表格中打开”即可拿到 `/base/` 链接）。

### 2.3 嵌在云文档（Docx）中的多维表格

典型场景：用户给出 `/docx/<doc_token>` 链接，或 wiki 节点解析出的 `obj_type` 为 `docx`。

处理规则：
1. 若宿主具备读取文档块（blocks）的能力：查找类型为多维表格（bitable block）的块，其 token 通常形如 `<app_token>_<table_id>`，按最后一个下划线拆分为 `app_token` 与 `table_id`；若文档中有多个嵌入多维表格，列出标题/表格标识让用户选 1 个。
2. 若宿主不具备文档块读取能力：请用户点击嵌入表格右上角“在新标签页打开/全屏打开”，复制真实的 `/base/<app_token>` 链接。

---

## 3. 自动接入顺序（用户未贴链接时）

当用户只说“帮我汇报一下飞书表”“把这条写进多维表格”但没附链接时，严格按以下顺序查找，命中即向用户简短确认后继续：

1. **优先查本地 Ledger 历史**：检查已定位的 `<ledger_root>/` 下是否存在历史 `<app_token>/` 子目录（或索引文件 `recent.json`），取最近活跃的 `(app_token, table_id, table_name)`，一句确认：“继续使用上次的《{表名}》吗？”
2. **宿主搜索兜底**：若无本地记录（或处于 `ledger_mode = "unavailable"`），且宿主具备 `search_bitable` 能力，按用户提到的业务词搜索候选多维表格，列出最多 5 条（含名称与链接）请用户选定。
3. **向用户索要链接**：以上两步均无果时，直接请用户粘贴多维表格链接（优先 `/base/` 链接）。

---

## 4. 连通性验证、高级权限与读取身份（`reader_identity`）

### 4.1 连通性测试
拿到 `app_token` 后，立即做最小只读探活：
- 调用列数据表接口，确认目标 `table_id` 存在且可读；
- 若用户未指定 `table_id` 且 Base 仅 1 张表，自动选中；若有多张表，按用户表述匹配或请用户选 1 张。

### 4.2 高级权限与 `reader_identity`
飞书多维表格开启**高级权限**后，不同角色/身份看到的字段集合和记录范围可能不同。如果不区分读取身份，A 账号看到的 8 个字段与 B 账号看到的 12 个字段会被误判成“表结构漂移”。

处理规则：
1. 尽量确定稳定的 `reader_identity` 字符串，优先级如下：
   - 宿主提供的应用/用户唯一标识（如 `app:<app_id>`、`user:<open_id_hash>`——注意对个人 ID 做短哈希或脱敏，不存明文手机号/邮箱）；
   - 环境变量 `FEISHU_CONTROLLER_READER_ID`；
   - 均不可得时使用 `"default"`。
2. 计算 `schema_fingerprint` 时，始终把 `reader_identity` 单独记录在同条事件中。
3. **漂移判定规则**：
   - 仅当 `reader_identity` 相同且 `schema_fingerprint` 变化时，才判定为“表结构漂移”；
   - 若 `reader_identity` 发生变化，在汇报中提示“本次读取身份与上次不同（可能受高级权限字段/行可见性影响），环比标记为不可比”，不误报结构漂移。

### 4.3 已知飞书 API 坑位提示（供排查报错用）
- **字段分页截断**：列字段接口超过 100 个字段必须翻页，否则 `schema_fingerprint` 会不稳定。
- **批量更新报错伪装**：部分实现误把 `batch_update` 发成 `PUT`（实际应为 `POST`），或缺少 `base:record:update` 权限时，飞书可能返回 `99992402 field validation failed`。排查顺序：先核对字段类型与枚举值，再核对写权限与接口方法。
