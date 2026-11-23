# feishu-controller

飞书 / Lark 多维表格中控 Agent Skill：以**在线多维表格为唯一事实源**，采用**读写分离**（汇报约 80% / 受控回写约 20%）的按需交互工作流，完成口径对齐、结构漂移检测、四段式汇报、同口径环比与幂等受控回写，并在本地按月追加只用于审计和环比基线的 JSONL 留痕。

## 核心特性

- **在线唯一事实源**：汇报数据一律在线实时拉取；严禁从本地 JSONL 读业务事实（唯一例外是严格同口径环比基线，且在汇报中显式声明）。
- **终态后再留痕**：汇报完成后追加 `briefing_snapshot`（含结论与指标，不存整行原始 PII）；回写确认并拿到回执或被用户拒绝后再追加 `bitable_writeback`（`confirmed` / `partial` / `failed` / `rejected`）。
- **口径持久化与严格五项环比门禁**：用户确认口径写入 `lens_confirmed`，下次直接复用；仅当 `table_id`、`view_id`、`filter_signature`、`lens_version`、`schema_fingerprint`（同一 `reader_identity`）全部一致时才做环比，否则标明「不可比」。
- **零强依赖、跨宿主运行**：不绑定特定 MCP 工具名，支持中国区 `feishu.cn` 与国际版 `larksuite.com`、`/base/`、`/wiki/`（可选 `resolve_wiki_node`）及云文档嵌入多维表格；无 Python 或无可写存储时自动优雅降级。
- **受控幂等回写**：预览确认 → 写前查重 → 分批写入与逐行回执 → 部分失败仅重试失败行。

## 目录结构

```text
feishu-controller/
├── SKILL.md                               # 主指令与执行流程（Gate 0 → Step 1 → Step 2 → Step 3a/3b）
├── references/
│   ├── capability-and-urls.md             # 能力握手、URL 解析、自动接入与高级权限身份
│   ├── briefing-and-writeback.md          # 指纹算法、字段归一化、四段式汇报、回写幂等规范
│   └── ledger-schema.md                   # Ledger 目录定位、按月切分、三类事件 Schema 与锁文件规范
└── scripts/                               # 可选确定性加速脚本（仅使用 Python 标准库）
    ├── bitable_metrics.py                 # 结构指纹、归一化、去重、聚合、五项环比、去重键哈希
    └── ledger_append.py                   # 路径定位、外层字段校验、跨平台锁文件原子追加、跨月查询
```

## 安装使用

将本仓库克隆到你的 Agent Skill 目录即可（例如 `~/.agents/skills/feishu-controller`）：

```bash
git clone https://github.com/anxu-collab/feishu-controller.git ~/.agents/skills/feishu-controller
```

或直接在支持 GitHub Skill 导入的 Agent 平台中填入本仓库 URL。

## 可选脚本快速验证

脚本仅依赖 Python 3.9+ 标准库，无需安装第三方包：

```bash
python scripts/ledger_append.py --help
python scripts/bitable_metrics.py --help
```
