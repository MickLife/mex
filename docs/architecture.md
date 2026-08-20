# meX 架构设计文档

> 版本：v1.2（待用户审查）
> 日期：2026-08-06
> 依据：`docs/requirements.md`（最终需求）、三份竞品评估报告（已归档至 `docs/archive/research/`）
> 状态说明：本文档记录顶层架构决策及其理由，作为后续开发的依据。ADR 部分已与用户确认 ✅（ADR-12 已于 2026-08-20 修订：skill 承载读写、会话结束 hook 留待扩展）；§3.5-3.7、§4.3、§5.2、§6.4、§7、§9、§10 为补齐的 gap，待审查。

## 1. 产品定位与设计目标

meX 是一个**本地优先的个人记忆系统**：存储用户画像与事件记忆，为各类 AI agent（OpenCode / Claude Code / 其他）提供个性化的用户上下文。

设计目标按优先级排序：

1. **隐私与掌控**：数据完全本地，单文件存储，备份即复制文件。
2. **架构简单**：单一数据库、统一数据模型，一个普通 Python 工程师能看懂全部代码。
3. **记忆可信**：来源可溯、置信度可见、AI 推断带标记、删除可恢复。
4. **查询够用即快**：画像查询毫秒级；查询路径零 LLM 调用、零费用。

## 2. 架构决策记录（ADR）

### ADR-1：产品形态 —— Python 库 + CLI，直连 SQLite ✅

**决策**：核心逻辑为 Python 包 `mex`，对外暴露 CLI。**不启动常驻后台服务**，CLI 直接读写 SQLite（WAL 模式 + `busy_timeout` 重试）；代码分层（命令行 → 业务服务 → 存储）保证未来可在外层薄包一层 HTTP 服务。

**常驻进程 vs CLI 直连的分析**：

用户提出常驻进程的三个理由，逐一分析：

1. **多 agent / 多 session 并发使用**：SQLite 开启 WAL 模式后，读不阻塞写、写不阻塞读；多进程写入由数据库串行化。meX 的写事务只持锁毫秒级（extract 流程中耗时的 LLM 调用在事务外完成），配合 `busy_timeout` 自动重试，真实冲突概率极低。
2. **管理 LLM 请求并发**：extract 是用户手动触发的低频操作（每天数次），个人场景并发量小，模型 API 提供方自身有限流兜底。
3. **防止并发读写冲突**：同第 1 点，WAL 模式下读写互不阻塞，写-写由锁串行化。

常驻进程的成本：进程生命周期管理（启动 / 停止 / 崩溃恢复 / 开机自启）、端口占用、"服务没启动命令就失败"的心智负担。

**结论（已与用户确认）**：第一版 CLI 直连（WAL + 写事务统一封装），零运维；若未来真遇到并发问题或要加管理 UI，在业务服务层外面包一层 HTTP 服务是增量工作，不需推翻架构。

### ADR-2：存储引擎 —— SQLite 单文件 ✅

**决策**：全部数据存于单个 SQLite 文件（默认 `~/.mex/mex.db`），WAL 模式；使用其内置 FTS5 全文检索功能（按字切分的 trigram 分词器，对中文友好，毫秒级）。

**理由**：
- 零外部依赖（Python 标准库自带），无需安装数据库服务。
- 单文件天然满足"导出快照到磁盘""备份到云"的需求。
- 结构化约束（CHECK 约束由数据库强制；槽位唯一性由 schema 的 `unique` 声明 + 写入路径校验，见 ADR-4 v3 调整），解决 v1 markdown 方案"同一事实存两处、格式全靠 LLM 自觉"的问题。
- 个人数据量级（年约千条）下性能无压力。

### ADR-3：向量检索 —— 预留接口，第一版不做 ✅

**决策**：第一版只用 FTS5 关键词检索 + 结构化过滤。`memories` 表预留 `embedding` 列，查询层预留语义检索接口。

**理由**：
- meX 的核心是**结构化画像**（SQL 精确查询），不是海量自由文本；事件层数据量小，关键词检索足够。
- supermemory 报告的教训：纯向量"语义相似"检索反而漏掉时敏性关联，画像驱动才是正解。
- 避免下载本地 embedding 模型（1-2 GB）或持续支付 embedding API 费用。
- 未来数据量增长后，可用 sqlite-vec 扩展（保持单文件架构）无缝升级。

### ADR-4：数据模型 —— 统一 memories 表 + 画像投影模型 ✅

**决策**：所有记忆（画像槽位、画像外记录）存储于同一张 `memories` 表，不使用 layer 区分。画像 = **schema.yaml 槽位声明的投影**——`sub_topic` 非空且 `(topic, sub_topic)` 落在 schema 内的条目是**画像槽位**（强制 `topic`），其余（`sub_topic` 为空）是**画像外记录**（`topic` 可空，按时间索引，不占画像上下文）。

**理由**：
- 增删改查、软删除、历史审计、审查队列、备份导出只需实现一套，符合"架构保持简单"。
- 画像内外的分类由 schema 唯一决定：写入方（LLM / 用户）只负责"能否归入某槽位"，不再做"月级还是年级变化周期"这类模糊的主观判断，错误率更低、更客观。
- "同一事物只存一处、不存在事实冲突"对唯一画像槽位仍成立（见下方 v4 / v3 调整）。
- 个人数据量级下，单表查询性能无忧。

**v4 调整（移除 layer，改投影模型）**：删除 `memories` 表的 `layer` 列。此前 stable/state/event 中，stable/state 与 event 在功能上只有"是否进画像"一个区别；该区别改由"是否命中 schema 槽位"机械判定，无需存储维度。原 event 层数据与"领域级槽位"数据迁移后以 `sub_topic` 为空天然落在画像外。数据库结构版本 v3→v4 的迁移为 `ALTER TABLE memories DROP COLUMN layer`（自动执行，数据完整保留）。

**v3 调整回顾（唯一性上移到业务层）**：槽位唯一性不再由数据库唯一索引强制，改为 schema.yaml 的 `unique` 字段声明 + 写入路径（`rules.validate` + `find_by_slot`/`list_by_slot`）按声明决定 insert/update。原因：唯一性是**字段自身属性**（`company` 可多条、`name` 唯一），静态 DB 索引无法按 schema 配置动态生效。唯一槽位仍保证"一个槽位一条"；可多条槽位（`unique: false`）则允许多条独立记录共存（§3.5）。

### ADR-5：AI 推断记忆 —— 带标记生效，事后审查 ✅

**决策**：AI 推断的记忆立即生效可查，但带 `is_ai_inferred = 1` 与低置信度标记；`mex review` 命令支持事后批量确认/清理。

**理由**：
- 贴合需求原文"置信度告诉 agent 有多大可能信任这条记忆"——置信度是给 agent 看的，agent 自行谨慎采信。
- 不阻断使用流程，避免"用户懒得审查、记忆系统失效"的死角。
- 低置信记忆在 `mex profile` 输出中标注，视觉上可区分（见 §6.2）。

### ADR-6：Schema 强制 —— schema.yaml 配置驱动 ✅

**决策**：画像字段由 `~/.mex/schema.yaml` 定义合法清单，所有写入强制校验；扩展字段 = 编辑配置文件，**不允许**把扩展数据塞进单一大杂烩字段。

**schema.yaml 与 memories 表是什么关系？**

一句话：**memories 表存"数据"，schema.yaml 存"数据必须遵守的规则"**。

为什么需要规则？这要从 memories 表的存储方式说起。画像字段（如"公司""职位"）在表里**不是列，而是行**——每条画像记忆是一行：`topic=work, sub_topic=company, content=华为`。这种"行式存储"的原因是画像字段因人而异、必须用户可扩展：如果每个字段都是表的一个列，加一个字段就要改一次表结构（ALTER TABLE），行不通。

但行式存储有个天然代价：**数据库自己不知道"work 下面允许有哪些字段"**。没有约束的话，LLM 今天写 `work.company`，明天写 `job.employer`，后天写 `career.employer`——三个名字一个意思，画像就乱了。这正是 v1 markdown 方案"格式全靠 LLM 自觉"的病根。

schema.yaml 补上的就是这一层约束：它用人类可读的 YAML 声明"合法字段清单"，**所有写入**（手动 `mex add` 或 LLM 抽取）必须先过校验——topic 不在清单里，拒绝；sub_topic 不在清单里，拒绝。想加新字段？先编辑 schema.yaml 加一行，再写入。

类比：schema.yaml 是"表单的空白模板"（规定有哪些格子可填），memories 表是"填好的一摞表单"（每行记录一个格子的内容）。

它与 §3.5 的唯一性校验配合关系：schema.yaml 管"字段名合法 + 该字段唯一还是可多条"，写入路径校验管"唯一字段同一槽位只有一条有效值"，两者合起来实现"唯一事实"原则（可多条字段则允许多条独立记录）。

此外，schema.yaml 里的 topic 天然就是手动注入要求的"领域分区"筛选维度（理财 / 健康 / 求职 / 感情 / 家庭各是一个 topic）。

**校验流程的具体例子**

所有写入（`mex add`、`mex extract` 的 LLM 候选、`mex import`）都必须通过 `domain/schema.py` 的校验。以下假设 schema.yaml 内容如 §3.4 所示（定义了 basic_info / work / finance 等领域）。

**例 1：正常写入 → 通过**

```
mex add --topic work --sub-topic company --content "华为"
```

校验链：
1. `sub_topic` 非空 → 该条为**画像槽位**，`topic` 必填 ✓
2. schema.yaml 的 topics 中存在 `work` ✓
3. `work` 的 sub_topics 中存在 `company` ✓

→ 写入数据库，成为画像内容。

**例 1b：画像外记录 → 通过**

```
mex add --topic finance --content "随手记：下周要做一次理财复盘"
```

省略 `--sub-topic` → 该条为**画像外记录**：`sub_topic` 为空，不进入 `mex profile` 画像，按时间索引。`--topic` 如果填了，必须在 schema 内（保证 `mex search --topic finance` 领域检索可靠）；也可完全省略 `--topic`。

**例 2：topic 不在清单 → 拒绝**

```
mex add --topic job --sub-topic company --content "华为"
```

topics 中没有 `job` → **拒绝写入**，报错："topic 'job' 未在 schema.yaml 定义。相近的已有 topic：'work'。如确需新领域，请先编辑 schema.yaml。"

这防住的问题：没有校验时，LLM 今天写 `work` 明天写 `job`，同一类信息分裂到两个领域，画像作废。

**例 3：sub_topic 不在清单 → 拒绝**

```
mex add --topic work --sub-topic employer --content "华为"
```

`work` 存在 ✓，但 `work` 下定义的是 `company`，没有 `employer` → **拒绝**，报错并列出 work 下的合法字段。

这防住的问题：`company` / `employer` / `organization` 同义字段名泛滥——v1 markdown 方案"格式全靠 LLM 自觉"的病根，靠这道校验根治。低频信息没有合适字段时，走画像外记录（例 1b）而非自造字段。

**例 4：扩展字段的正规路径**

想记录宠物信息，schema 里没有对应字段。唯一途径：

1. 编辑 `~/.mex/schema.yaml`，追加：
   ```yaml
   pets:
     description: 宠物
     sub_topics:
       dog_name: { description: 狗的名字 }
   ```
2. 再执行 `mex add --topic pets --sub-topic dog_name --content "旺财"` → 通过。

"改配置 → 再写入"两步是刻意的：让"新增字段"成为一个**用户有意识的决定**，而不是写入路径随手为之——这就是需求中"扩展机制要正规化"的落地。

**例 5：LLM 抽取路径 —— 两道防线**

extract 流程中，schema 参与两次：

- **第一道（事前引导）**：`llm/prompts.py` 把 schema.yaml 的字段清单（含每个字段的 description）拼进提示词，LLM 只能往清单内的字段填写候选记忆；归入不了任何槽位的临时信息作为画像外记录输出（`sub_topic` 为空）。
- **第二道（事后校验）**：LLM 返回的候选列表逐条过 `domain/schema.py` 校验，画像槽位（`sub_topic` 非空）必须成对且合法；不合规的丢弃并在命令输出中说明（如"丢弃 1 条：work.salary 未定义，财务信息应属 finance.*"）。

双保险的原因：LLM 输出永远不可全信，提示词引导只能降低违规率，代码校验才是硬约束。

**例 6：画像外记录 —— 宽松但不放纵**

```
mex add --topic finance --content "今天基金跌了 3%，有点焦虑"
```

- 画像外记录的 `sub_topic` 必须为空；
- 但**一旦填了 `topic`，仍必须在 schema 清单内**——保证 `mex search --topic finance` 的领域筛选对画像外记录同样可靠；
- 画像槽位（`sub_topic` 非空）若不在 schema 内会被拒绝（防自造字段）。

### ADR-7：溯源机制 —— history 审计表 ✅

**决策**：独立 `history` 表记录每次增删改 / 恢复 / 确认操作的前后内容、操作者、时间、证据。

**理由**：借鉴 mem0 的 history 表设计（三份报告中最透明的审计方案），支撑"来源标记 + 证据关联"的人工审查需求。

### ADR-8：删除策略 —— 默认软删除，可选硬删除，支持 dry-run ✅

**决策**：
- `mex forget` 默认软删除：写 `forgotten_at` 时间戳 + `forgotten_reason` 原因（便于审查追溯），数据保留、查询排除，`mex restore` 可恢复。
- `--hard` 强制物理删除：记忆本体移除（history 中仍留一条删除记录），执行前交互确认或 `--yes` 跳过。
- `--dry-run`：批量条件删除时先预览将影响的条目，不实际执行；去掉该参数才真正生效。

**理由**：软删除是"后悔药"（对比 mem0/memobase 的物理删除更安全）；硬删除与 dry-run 给用户完全控制权。

### ADR-9：导出与备份 —— 仅本地文件导出，GitHub 推送不做 ✅

**决策**：`mex export --format json|markdown --output <路径>` 导出到本地文件。**不内置** git 推送/GitHub 备份功能。

**理由**（用户决策）：git 推送逻辑混入 CLI 会让职责混乱。用户可自行用 git 管理导出目录（见 §9 建议做法）。"GitHub 备份"需求降级为"导出产物适合纳入 git 管理"。

### ADR-10：LLM 使用 —— 仅写路径调用，查询零调用 ✅

**决策**：LLM（OpenAI 兼容 API，如已部署的 DeepSeek，key 走环境变量）只在 `extract`（对话抽取）、`import`（文件导入）、证据摘要生成时调用。所有查询命令（search/profile/list）零 LLM 调用、零费用、毫秒级。

**理由**：回应原始需求中"耗费 llm token 金钱成本"的痛点；保证查询性能不受网络波动影响。

### ADR-11：画像外记录累积 —— ✅ 修订（v5 起支持 TTL 自动遗忘）

**决策**：所有记忆（含画像外记录）默认永久保留，查询时按时间窗口过滤；不做自动压缩/归档。用户可对单条记忆设置 `expires_at`（TTL 存活时间）——到期的记录对查询**不可见**（惰性过滤，数据保留），可配 `mex gc` 软删清理（reason="TTL 过期"，可 restore）。原"第一版不做自动衰减"修订为"默认不衰减，用户可按需设 TTL 自动遗忘"。

**理由**：个人量级（年约千条）下 FTS5 检索无压力（保留原结论）；兼顾"隐私与数字遗忘"需求——用户能设定某些敏感对话到期自动销毁。`--expires` 只接受绝对日期/时间（本地转 UTC），不支持 `24h` 等相对时长（用户决定）。

### ADR-12：extract 触发与 agent 集成 —— skill 承载读写，hook 留待扩展 ✅（2026-08-20 修订）

**决策**：以 skill 说明书承载 agent 侧的读写引导，`mex integrate <agent>` 生成 skill（写入官方 `skills/<name>/SKILL.md` 发现路径）+ README：

- **写入即时路径（skill 引导）**：skill 告诉 agent——用户明确说"记住这个"时立即执行 `mex extract "<用户原话>"`；简单明确的单条事实也可用 `mex add`。重要信息即时入库，不等会话结束。
- **读取路径（skill 引导）**：skill 教 agent 在对话开始执行 `mex profile` 注入画像（自动模式）、涉及生活/情绪/近期状态时执行 `mex search --since`（时敏召回）、需要细节时执行 `mex search --topic/--keyword`（手动模式）。
- **手动命令是地基**：`mex extract --file <file>` / `mex extract "<文本>"` 随时可用。
- **集成落地**：`mex integrate <agent> --scope project|global` 生成 skill 说明书 + README，**由用户指定生成到项目级目录（仅该项目生效）还是全局目录（所有项目生效）**。
- **会话结束 hook 暂缓**：早期方案以"hook 兜底写"为主路径，但 OpenCode 官方配置 schema 无 hooks 字段（原生不支持 Claude Code 风格的生命周期 hook），第一版**不生成 hook 配置**，写路径由 skill 引导即时写承担。hook 能力（第三方插件桥接 / 包装脚本聚合会话）留待后续扩展。

**理由**：

- skill 是 Claude Code / OpenCode 均支持的官方扩展点，承载读写引导无平台差异；hook 在 OpenCode 上无官方支持，作为主路径不成立。
- skill 引导即时写依赖大模型自主判断（可能漏记），hook 兜底可弥补此短板——这正是 hook 留待后续扩展的动机（本 ADR 记录在案）。
- "每轮对话后实时抽取"明确不做：LLM 延迟打断对话节奏，成本无谓放大。
- scope 参数的意义：用户可把工作项目（不需要记忆系统）与个性化对话场景分开——项目级配置只在该项目目录生效，全局配置对所有会话生效。

## 3. 数据模型

### 3.1 数据库表结构

```sql
-- 统一记忆表（ADR-4）
CREATE TABLE memories (
    id               TEXT PRIMARY KEY,     -- UUID 字符串（见下方说明）
    topic            TEXT,                 -- 领域主题；画像槽位必填，画像外可空
    sub_topic        TEXT,                 -- 画像字段；非空=画像槽位（须在 schema 内），空=画像外记录
    content          TEXT NOT NULL,        -- 记忆内容（默认单值字符串；多个值用可多条槽位的独立记录承载，结构化字段为 JSON 对象，见 §3.5）
    is_ai_inferred   INTEGER NOT NULL
                     CHECK (is_ai_inferred IN (0, 1)),  -- 布尔：1 = AI 推断，0 = 用户亲口
    confidence       TEXT NOT NULL
                     CHECK (confidence IN
                       ('confirmed', 'explicit', 'inferred', 'speculated', 'uncertain')),
    evidence         TEXT,                 -- 证据摘要（可选）
    embedding        BLOB,                 -- 预留：向量（ADR-3，第一版不用）
    created_at       TEXT NOT NULL,        -- UTC ISO 8601（见 §3.7）
    updated_at       TEXT NOT NULL,
    forgotten_at     TEXT,                 -- 软删除时间；NULL = 有效（ADR-8）
    forgotten_reason TEXT,                 -- 软删除原因，便于审查追溯
    expires_at       TEXT                  -- TTL 过期时间（UTC ISO，NULL = 永不过期；v5 新增）。
                                           -- 过期记录由查询统一过滤（expires_at IS NULL OR expires_at > now）
);

-- 槽位唯一性：v3 起不再由数据库唯一索引强制，改为 schema.yaml 的 unique 字段声明
-- + 写入路径校验（见 ADR-4 v3 调整、§3.5）。唯一槽位一槽一条；可多条槽位可多条共存。
-- 画像判定（画像槽位 vs 画像外记录）由 schema 槽位投影给出，不存储 layer（v4，见 ADR-4）。

-- 全文检索索引（FTS5 + trigram 分词，中文按字切分）
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, content='memories', content_rowid='rowid', tokenize='trigram'
);

-- 审计历史表（ADR-7）
CREATE TABLE history (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,            -- 关联的记忆
    event       TEXT NOT NULL
                CHECK (event IN ('add', 'update', 'forget', 'restore', 'approve', 'delete')),
    old_content TEXT,                     -- 修改前内容（add 时为 NULL）
    new_content TEXT,                     -- 修改后内容（forget/delete 时为 NULL）
    actor       TEXT NOT NULL
                CHECK (actor IN ('user', 'ai')),
    evidence    TEXT,                     -- 来源证据（如来自哪段对话/哪个文件）
    created_at  TEXT NOT NULL
);

-- LLM 用量记录表（§7.4）
CREATE TABLE llm_usage (
    id                TEXT PRIMARY KEY,
    purpose           TEXT NOT NULL,      -- extract / import
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

-- 增量抽取状态表（§8.3）
CREATE TABLE extraction_state (
    source_file       TEXT PRIMARY KEY,   -- 会话文件绝对路径
    last_position     INTEGER NOT NULL,   -- 已处理位置（如 JSONL 行号）
    last_extracted_at TEXT NOT NULL
);
```

**FTS5 索引维护**：`memories_fts` 是外部内容表（`content='memories'`），写入后需在**同一事务内**手动同步索引（新增/更新内容/删除时同步改 memories_fts），否则 search 查不到新内容——由 `store/fts.py` 统一封装，业务层不直接操作 FTS5。

**关于 id 的类型**：SQLite 没有原生 UUID 类型，可选 TEXT（36 字符，如 `550e8400-e29b-...`）或 BLOB（16 字节）。选 **TEXT**：个人量级下存储差异可忽略，而可读性（命令行排查、导出文件、history 关联展示）更重要。Python 侧用 `uuid.uuid4()` 生成字符串。

**关于枚举与布尔字段**：`confidence` / `event` / `actor` 用 TEXT 存枚举字符串，靠 CHECK 约束兜底，Python 侧用 `Enum` 类定义唯一取值来源。`is_ai_inferred` 为布尔——SQLite 无原生 BOOL 类型，用 INTEGER（0/1）存储，Python 侧映射为 `bool`。禁止散落各处的魔法字符串/魔法数字。

**`history.actor` 语义**：`user` = 人发起的操作（手动命令、审查确认）；`ai` = LLM 抽取流程产生的写入。

### 3.2 画像投影模型

一条记忆是"画像槽位"还是"画像外记录"，完全由"能否归入 schema.yaml 声明的槽位"机械判定，不设时间尺度分层：

| 形态 | topic 约束 | sub_topic 约束 | 进画像（mex profile） | 写入/覆盖语义 |
|---|---|---|---|---|
| **画像槽位** | 必填，须在 schema 内 | 非空，须在 schema 内且成对合法 | ✅ 是 | 唯一槽位覆盖、可多条槽位追加（§3.5） |
| **画像外记录** | 可空（填了须在 schema 内） | 必空（null） | ❌ 否（仅时间索引） | 一律追加（天然多条） |

- **画像 = schema 槽位投影**：`mex profile` 只输出命中 schema 槽位的画像槽位条目；画像外记录（`sub_topic` 为空）不占用画像上下文，按时间窗口检索（原"事件层"语义）。
- **写入方不用判断"变化周期"**：LLM / 用户只负责把信息尽量归入最合适的槽位；归入不了任何槽位的临时信息（今天完成的事、情绪、计划、想法、踩的坑）作为画像外记录输出。越界即跌出画像，无需额外标记。
- 缺失槽位的低频长期事实（如保险配置、支出结构）有两种去处：显式在 schema.yaml 声明一个字段使其进入画像，或以画像外记录留存（沿用重命名后的 ADR-4"画像 = schema 投影"语义，保证 `mex search --topic finance` 领域筛选对画像外记录同样可靠）。

### 3.3 置信度：五档枚举 + 判定规则

置信度不用连续数值（避免 LLM 拍出 `0.73` 这类不可解释的值），用**五个离散枚举值**，每个值有明确判定规则，LLM 抽取时按规则选档：

| 枚举值 | 权重 | 判定规则 | 谁能打这个档 |
|---|---|---|---|
| `confirmed` | 1.0 | 经用户人工确认的事实 | **仅人工**：`mex add` 默认档，或 `review approve` 后提升至此 |
| `explicit` | 0.9 | 用户原话明确陈述的事实，有直接原文证据 | LLM 抽取 / 人工 |
| `inferred` | 0.6 | 由多条明确事实合理推导，证据链清晰完整 | LLM 抽取 |
| `speculated` | 0.4 | 基于少量间接线索的推测，证据不充分 | LLM 抽取 |
| `uncertain` | 0.2 | 几乎无直接证据的猜测，仅供参考 | LLM 抽取 |

规则：

- 数据库存枚举字符串（CHECK 约束），权重只用于查询排序与截断优先级。
- **LLM 只能打后四档，`confirmed` 只能由人产生**——这条写进抽取提示词并在代码里强制。
- `is_ai_inferred` 与 `confidence` 是两个独立维度：is_ai_inferred 回答"是否 AI 推断"，confidence 回答"多可信"。通常用户亲口（0）对应 `explicit`、AI 推断（1）对应后三档，但不强绑定（如用户说"我可能有点焦虑"——is_ai_inferred=0 但只值 `speculated`）。
- 判定规则同时是 LLM 抽取提示词的一部分，保证标准一致。

### 3.4 schema.yaml 示例

```yaml
# ~/.mex/schema.yaml
topics:
  basic_info:
    description: 基础信息
    sub_topics:
      name:       { description: 姓名 }
      gender:     { description: 性别 }
      birth_year: { description: 出生年份 }
      hometown:   { description: 籍贯 }
      location:   { description: 现居地 }
  work:
    description: 工作与职业（含创业与开源）
    sub_topics:
      position:   { description: 当前职位 }                        # 省略=唯一：当前职位
      company:    { description: 当前公司 }                        # 省略=唯一：当前任职公司
      experience:                          # 可多条 + 结构化 JSON 字段
        description: 工作经历
        unique: false
        fields:                            # fields 声明：content 为 JSON 对象
          start:
            description: 开始时间
            format: time                   # 支持 YYYY/YYYY-MM/YYYY-MM-DD
            required: true
          end:
            description: 结束时间          # 至今写"至今"
            format: time
            required: true
          company:
            description: 公司名
            required: true
          position:  职位
          department: 部门
          summary:   内容概要
          kind:      类型（全职/实习）
      tech_stack: { description: 技术栈/技能, unique: false }       # 可多条：每个技能一条
      startup_status:   { description: 创业状态 }                    # 创业（startup_ 前缀）
      oss_projects:     { description: 开源项目, unique: false }     # 开源（oss_ 前缀）
      goal_current:     { description: 当前职业目标 }                # 职业发展（goal_ 前缀）
  edu:
    description: 教育背景
    sub_topics:
      education:                          # 可多条 + 结构化 JSON 字段
        description: 教育经历
        unique: false
        fields:
          start:   开始年份
          end:     结束年份
          degree:  学历
          school:  学校
          major:   专业
      language:    { description: 语言能力, unique: false }
  research:                                # 科研成果与荣誉（学生/职场通用）
    description: 科研成果与荣誉
    sub_topics:
      paper:                              # 可多条 + 结构化 JSON 字段
        description: 论文
        unique: false
        fields:
          title:   标题
          venue:   期刊/会议
          year:    年份
          authors: 作者
          status:  状态
      patent:                             # 可多条 + 结构化 JSON 字段
        description: 专利
        unique: false
        fields:
          name:   专利名称
          type:   类型
          year:   年份
          status: 状态
  # ... 按需扩展：新增领域或字段 = 编辑此文件（ADR-6）
```

领域按「覆盖学生 + 职场人群」聚合为 12 个：`basic_info` / `work`（含创业与开源）/ `edu` / `research`（论文专利获奖） / `finance` / `family` / `relationship` / `leisure`（爱好与旅行）/ `health` / `values` / `interaction`（交互偏好）/ `goals`（目标与计划）。领域内语义相近的板块用前缀分区（`startup_*`、`oss_*`、`goal_*`），避免字段重名。

初版 schema 只覆盖用户自身需求；"泛化到各类人群的 schema 设计"推迟到后续版本。

**字段唯一性（`unique`，默认 true，省略即唯一）**：

| 取值 | 语义 | 适用字段 | content 形态 |
|---|---|---|---|
| 唯一（省略 `unique`） | 一个槽位只存一条有效记录，新值覆盖旧值（旧值进 history） | 当前态：`name`、`location`、当前职位、当前公司 | 单值字符串 |
| 可多条（`unique: false`） | 一个槽位可存多条独立记录，追加不覆盖，每条各有 content/置信度/证据/时间戳 | 积累态：工作经历、项目、技术栈、论文、获奖 | 单值字符串，或 JSON 对象（结构化字段，见下） |

判定指引：**"当前态"字段选唯一（新值覆盖旧值）；"积累态"字段选可多条（追加不覆盖）**。`content` 默认是单值字符串——存多个值用"可多条"字段每条一个值，不再用 JSON 数组（`multiple` 已退役，见 §3.5）。

**结构化字段（`fields` 声明）**：某些"一段完整经历/成果"需要多属性合一（起止时间、公司、职位、部门、内容概要……），若拆成多个槽位会割裂一段经历的完整性。此类字段在 schema 中声明 `fields`（键名 → 含义，键序即展示顺序），此时该字段**每条记录的 content 是一个 JSON 对象**（值统一为字符串），如 `work.experience`：

```json
{"start": "2019", "end": "2023", "company": "星辰软件", "position": "后端工程师",
 "department": "交易平台部", "summary": "订单系统重构与性能优化", "kind": "全职"}
```

结构化字段语义：
- 仍然受 `unique` 约束：`unique: false` 时可多条（每条是一段独立经历），省略则唯一（单条 JSON 记录）；
- **字段键约束**：`fields` 的每个键可声明两种约束——
  - `required: true`：该键必须在每条记录中出现，缺失即校验失败；
  - `format: time`：键值必须是 `YYYY` / `YYYY-MM` / `YYYY-MM-DD`（或结束键的"至今"），防止时间字段随意填写；
  - 缺省（纯字符串写法 ```summary: 内容概要```）表示可选、自由文本；
- 写入校验（`mex add` 与 extract 候选）要求 content 为合法 JSON 对象、键必须在 `fields` 声明内、必填键齐全、格式符合声明，否则丢弃/报错；
- 画像快照（§6.2）与 CLI 展示时按 `fields` 键序渲染为可读文本，不暴露原始 JSON；
- LLM 抽取（§7.1）收到字段清单中的 JSON 模板（含 `[必填]` / `[YYYY/YYYY-MM]` 标注），按模板产出。

**画像外记录**：字段清单是"通用字段"而非穷举。当一条信息在领域内找不到合适字段时（如 `finance` 下的保险配置、今天完成的事），可省略 `sub_topic`（并视情况填或不填 `topic`）直接写入画像外记录：

```
mex add --topic finance --content "随手记：下周要做一次理财复盘"
```

该条目 `sub_topic` 为空，不进入 `mex profile` 画像，按时间索引。LLM 抽取时遵循同样规则（提示词 §7.1）：优先精确画像字段 → 归入不了任何槽位的临时信息才写为画像外记录（`sub_topic` 为 null）。

### 3.5 唯一事实约束与可多条槽位

**唯一事实约束**：同一画像槽位 `(topic, sub_topic)` 的唯一性由 schema 的 `unique` 字段声明决定（ADR-4 v3 调整）：

- **唯一槽位**（`unique: true`，默认）：一个槽位只允许一条有效条目。已有条目时 `mex add` 报错并建议 `mex update`；extract 走"已存在则更新内容、不存在则新增"。
- **可多条槽位**（`unique: false`）：一个槽位允许多条独立记录共存，每条各有 content / 置信度 / 证据 / 时间戳。`mex add` 直接追加；extract 每条候选直接插入。
- **画像外记录**（`sub_topic` 为空）天然多条，无唯一性约束，一律追加——它们不属于任何画像槽位，不触发唯一事实。

更新走 `mex update`（按 id 更新单条内容）；换槽位用 `add + forget`。

**可多条槽位的去重**：extract 时，提示词给出该槽位已有全部 content 供 LLM 去重，代码层对"完全相同 content"的候选跳过并计入 discarded；相似度去重列入后续迭代。

**`multiple`（JSON 数组）已退役**：v2 用 `multiple: true` + content 存 JSON 数组（如 `["Python","Go"]`）表达"多值"。v3 起退役——"可多条 + 单值"在所有维度上更优（每个值独立置信度/证据/时间戳、独立 FTS 索引、增删不影响其他条目）。加载旧 schema 时 `multiple: true` 自动当作 `unique: false` 并告警。`content` 默认是单值字符串。

**结构化 JSON 字段（§3.4 `fields` 声明）**：这是"单值字符串"模型的唯一扩展——当单个事实本身由多个属性组成（一段工作经历、一个项目、一篇论文）时，用 **JSON 对象作为该条记录的 content**，键集由 schema 的 `fields` 声明约束，每个键还可声明 `required`（必填）与 `format`（如时间格式）约束。它与已退役的 `multiple`（JSON 数组）本质不同：`multiple` 是"多个并列值"用一个字段装（被可多条槽位取代），`fields` 是"一个复合事实的多属性"用一个 JSON 对象表达（拆成多个槽位会割裂完整性）。结构化字段同样受 `unique` 约束（可多条时每条一段独立经历），写入受键集合 + 必填 + 格式校验，展示按键序渲染为可读文本。

### 3.6 schema 演进与数据库迁移

**schema.yaml 的演进**（删除 / 重命名字段后，已有数据怎么办）：

- 原则：**schema 变更永远不自动改动已有数据**（安全优先）。
- 提供 `mex doctor` 命令：检查并报告数据与 schema 的不一致，两类：① 有效画像条目的槽位已不在 schema 中；② 结构化字段的 content 不符合其结构化约束（如字段升级为结构化后遗留的旧纯文本记录）。由用户手动处理——`mex update` 迁移到新字段/结构化格式，或 `mex forget` 删除。
- 重命名 = 删除 + 新增，走同样路径。

**数据库结构迁移**（未来版本表结构变更）：

- 用 `PRAGMA user_version` 记录结构版本号（当前 **v5**：memories 表新增 `expires_at` 列，支持 TTL 自动遗忘；v5 迁移为 `ALTER TABLE memories ADD COLUMN expires_at TEXT`。回顾 v4：移除 memories 表的 `layer` 列，采用画像投影模型，v4 迁移为 `ALTER TABLE memories DROP COLUMN layer`。回顾 v3：槽位唯一性从数据库唯一索引上移到 schema.yaml 的 `unique` 字段声明 + 写入路径校验，删除 `idx_unique_slot_v2`。迁移由各命令入口的 `ensure_initialized` 自动执行，幂等且数据完整保留）。
- 每次命令启动时检查版本，低于代码期望版本则自动按序执行迁移脚本（轻量方案，不引入 alembic 等外部迁移框架）。

### 3.7 时间戳规范

- **存储**：UTC，ISO 8601 秒级字符串，如 `2026-08-06T08:30:00Z`。统一 UTC 避免时区混乱（跨设备、导出文件兼容）。
- **显示**：CLI 输出一律转换为本地时区展示。
- **查询参数**：`--since` / `--until` 接受本地日期（`2026-08-01`）或日期时间，内部转 UTC 后查询。

## 4. 模块设计

### 4.1 编码规范（架构级约束）

- **单文件 ≤ 400 行，单函数 ≤ 50 行**，超出即拆分。
- 禁止上帝类 / 上帝函数：一个类、一个模块只承担一个职责。
- 依赖方向单向：`cli → services → store / domain / adapters`，禁止反向依赖；`llm` 只被 `services` 调用。

### 4.2 包结构

采用 src 布局（代码全部放在 `src/` 内）：防止从项目根目录意外导入未安装的包，与 Python 打包发布规范一致。

```
src/mex/
├── cli/                    # 命令行入口层：只做参数解析与输出格式化
│   ├── __init__.py         # Typer app 组装
│   ├── write.py            # add / update / forget / restore
│   ├── query.py            # search / list / profile / history / doctor
│   ├── extract.py          # extract / review
│   └── backup.py           # export / import / stats / init
├── services/               # 业务服务层：编排完整业务流程
│   ├── extract.py          # 对话抽取主流程（extract 与 import 共用核心）
│   ├── profile.py          # 画像快照生成（注入用文本）
│   ├── search.py           # 多条件检索组装
│   ├── review.py           # 审查队列：列出 / 确认 / 拒绝
│   └── backup.py           # 导出 / 导入
├── store/                  # 存储层：只负责 SQL，无业务逻辑
│   ├── connection.py       # 连接、WAL 配置、写事务封装（见 §4.3）
│   ├── memories.py         # memories 表 CRUD
│   ├── history.py          # history 表写入与查询
│   └── fts.py              # FTS5 索引同步与检索
├── domain/                 # 领域层：数据结构、枚举、规则，无 I/O
│   ├── memory.py           # 记忆条目数据类 + Layer/Confidence 枚举
│   ├── schema.py           # schema.yaml 加载与校验（ADR-6 的唯一校验入口）
│   └── rules.py            # 写入规则：层约束、槽位约束、置信度档位权限
├── adapters/               # 适配层：各 agent 会话格式 → 纯对话文本（§8.2）
│   ├── plain.py            # 纯文本直通（默认）
│   ├── claude.py           # Claude Code JSONL 会话解析
│   └── opencode.py         # OpenCode 会话存储解析
└── llm/                    # LLM 层：唯一网络出口（ADR-10）
    ├── client.py           # OpenAI 兼容客户端封装
    └── prompts.py          # 提示词模板（含置信度判定规则）
```

设计要点：

- **`domain/schema.py` 是 schema 强制的唯一入口**：任何写入路径都必须经过它校验，防止绕过（ADR-6）。
- **`llm/` 是唯一网络依赖点**：其余模块全部纯本地运行，测试时不需 mock 网络。
- **抽取与导入共用核心**：`services/extract.py` 同时服务日常对话抽取和 v1 markdown 档案迁移。

### 4.3 并发与连接管理

- **连接生命周期**：每条 CLI 命令打开一个连接，命令结束关闭——短命进程，无连接池。
- **WAL 模式**：`mex init` 时执行 `PRAGMA journal_mode=WAL`（持久化设置，只需一次）。
- **写冲突等待**：每次连接设置 `PRAGMA busy_timeout=5000`（5 秒），多进程写冲突时自动等待而非立即报错。
- **写事务**：所有写入（含多步写入的 extract 批量入库）包在 `BEGIN IMMEDIATE` 事务中，要么全部成功要么回滚，不留中间态。
- **LLM 调用在事务外**：extract 先完成 LLM 调用与校验，最后才开事务批量写入，持锁时间毫秒级。

## 5. CLI 命令清单

### 5.1 命令表

| 命令 | 功能 | 关键参数 |
|---|---|---|
| `mex init` | 初始化 `~/.mex/`（建库、生成默认 schema.yaml / config.yaml） | |
| `mex add` | 手动添加记忆 | `--topic --sub-topic --content [--confidence] [--expires <YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS>]`（默认 confirmed；`--sub-topic` 省略=画像外记录；`--expires` 设 TTL，本地转 UTC） |
| `mex update <id>` | 更新内容/置信度/过期时间（槽位不可变；换槽位用 add + forget） | `--content [--confidence] [--expires ...] [--clear-expires]` |
| `mex forget <id>` 或条件批量 | 删除（默认软删除，ADR-8） | `--reason "..."` `--hard` `--dry-run` `--yes` |
| `mex restore <id>` | 恢复已删除 | |
| `mex gc` | 软删已到期（TTL）记录 | `--dry-run`（ADR-11：惰性过滤已令其不可见，gc 仅清理） |
| `mex list` | 列出记忆（文本输出默认最近 10 条） | `--topic --sub-topic --include-forgotten [--limit N] [--offset N] [--all]`（`--limit` 每页条数、`--offset` 跳过前 N 条翻页，末页提示剩余条数；`--limit 0` 或 `--all` 打印全部；`--json` 模式默认输出全部、显式 `--limit/--offset` 仍生效） |
| `mex search` | 多条件检索（手动注入） | `--topic --sub-topic --keyword --since --until --limit`（画像槽位与画像外记录统一检索） |
| `mex profile` | 输出画像快照，供 agent 注入 | `--max-tokens --topics` |
| `mex extract "<文本>"` / `mex extract --file <file>` | 从对话文本抽取记忆（调 LLM） | `--from plain\|claude\|opencode`（会话文件解析，见 §8.2） |
| `mex review` | 审查推断记忆 | `list / approve <id> / decline <id> [--reason]` |
| `mex history <id>` | 查看单条记忆的变更历史 | |
| `mex get <id>` | 按 id 查看单条记忆完整内容（恒定 JSON 输出） | |
| `mex doctor` | 检查数据与 schema 的一致性（§3.6） | |
| `mex export` | 导出本地文件（ADR-9） | `--format json\|markdown --output <路径>` |
| `mex import <file>` | 从导出文件恢复，或从 v1 markdown 抽取迁移 | `--mode restore\|extract` `--on-conflict skip\|overwrite` |
| `mex config llm` | 交互式配置 LLM 供应商与模型（TTY 引导选择预设；非 TTY 需参数） | `--provider openai\|deepseek\|ollama\|moonshot\|qwen\|custom` `--base-url` `--api-key` `--model` |
| `mex config show` | 查看当前 LLM 配置（key 打码） | `--json` |
| `mex integrate <agent>` | 生成 agent 的 skill 说明书与 README（ADR-12） | `--scope project\|global`（默认 global） |
| `mex stats` | 统计：画像内/画像外条目数、待审查数、库大小、LLM 累计用量 | |

斜杠命令入口不属于 mex 本身：在 agent 侧配置 skill 文件调用 `mex profile` 即可。

### 5.2 全局约定

- **`--json` 全局选项**：所有命令支持以 JSON 格式输出结果，供 agent 程序化解析（人类可读的表格输出仍是默认）。
- **退出码**：
  - `0` 成功；
  - `1` 用户错误（参数非法、schema 校验失败、目标条目不存在）；
  - `2` 外部错误（LLM 调用失败、数据库 IO 错误）。
- **错误输出**：写往 stderr，格式为"发生了什么 + 建议操作"，例如："topic 'job' 未在 schema.yaml 定义。相近的已有 topic：'work'。如确需新领域，请先编辑 ~/.mex/schema.yaml。"
- **写命令成功输出**：stdout 输出受影响条目的摘要（id + 内容），便于用户确认操作结果。

## 6. 关键流程

### 6.1 对话抽取流程（extract）

1. 读入对话文本：纯文本文件 / 标准输入 / `--from claude|opencode` 指定的 agent 会话文件（经适配层转成统一纯文本，见 §8.2）。
2. 增量过滤（仅会话文件来源）：跳过 `extraction_state` 中已处理的部分，只保留新增对话（见 §8.3）。
3. 加载相关现有画像条目，作为 LLM 提示词中的"已知事实"，辅助去重与冲突检测。
4. 调用 LLM（契约见 §7），得到候选记忆列表。
5. 本地校验：schema 合法性、层规则、置信度档位权限（LLM 不得打 `confirmed`）、`action` 字段合法性（`new`/`update`/`uncertain_update`，缺失默认 `new`）；不合规的丢弃并在输出中说明。
6. 开一个写事务批量入库：
   - 唯一槽位的画像条目：槽位已存在 → 更新内容，旧值写入 history；不存在 → 新增。其中 `action=uncertain_update` 的更新强制置信度 `uncertain` 且置 `is_ai_inferred=true`（进入 `mex review` 审查队列）；`action=update` 保持 LLM 给的置信度。
   - 可多条槽位的画像条目：每条候选直接追加为新记录（content 与已有完全相同的跳过并计入 discarded）。
   - 事件条目：直接追加。
7. 更新 `extraction_state` 的处理位置（同事务）。
8. 输出摘要：新增 N 条、更新 M 条、丢弃 K 条（附原因），其中 X 条 AI 推断可用 `mex review` 查看；附本次 LLM 用量。

### 6.2 画像快照流程（profile）

1. SQL 查询全部有效（未删除）条目，过滤出**画像槽位**（`sub_topic` 非空且在 schema 内）作为画像主体；画像外记录（`sub_topic` 为空）不进入主体。
2. 按 schema.yaml 的 topic 顺序组织为单一"# 用户画像"部分。同 topic 内按 sub_topic 分组：唯一槽位输出其行；可多条槽位输出前 3 条（按置信度权重 + 更新时间排序）+ 末尾"  （共 X 条，mex search --topic <领域> 查看全部）"提示（X > 3 时），控制 token 又给 AI "该领域有丰富记录"的信号。
3. 置信度标注：`speculated` / `uncertain` 追加"（待确认）"；`inferred` 追加"（推断）"；`explicit` / `confirmed` 不标注。
4. 近期动态：额外查询最近 `recent_days` 天（默认 7）内的**画像外记录**（`sub_topic` 为空、未软删），按 `created_at` 倒序取最多 `recent_limit` 条（默认 5），在全部 schema topic 小节之后输出"## 近期动态"小节（每行 `- <content>`）；`--recent-limit 0` 关闭本节。
5. token 预算：`max_tokens` 拆**画像主体 70% / 近期动态 30%**（各预留 20% 余量）。主体超限按置信度权重从高到低保留（`_drop_lowest`）；近期动态超限按 `created_at` 倒序保留最新。`--topics` 筛选对主体与近期动态同样生效。
6. 输出纯文本，agent 直接嵌入系统提示词。

**输出格式示例**：

```
# 用户画像
## 基础信息
- 姓名：小陈
- 现居地：杭州
## 工作与职业
- 公司：远光软件
- 公司：前进云计算
- 职位：后端工程师
- 技术栈：Python
- 技术栈：Go
- 工作经历：2021年订单系统重构，接口响应从800ms降至120ms
- 工作经历：2022年搭建数据同步管道，支撑日增千万级记录
- 工作经历：2020年微服务网关改造，统一鉴权与限流
  （共 19 条，mex search --topic work 查看全部）
- 求职状态与偏好：求职状态：在职看机会；期望岗位：后端开发
## 三观与原则
- 风险偏好：稳健偏保守（待确认）
## 交互偏好
- 沟通风格偏好：结论先行，轻松幽默
## 目标与计划
- 短期目标：目标内容：通过系统架构师考试；截止时间：2026-09；所属领域：职业
## 近期动态
- 正在准备明天的架构评审答辩
- 本周启动晨跑计划
```

（原"当前状态"分节随 stable/state 分层语义一并移除；近期状态信息若值得进画像，自行归入相应画像字段，否则为画像外记录，由上述"近期动态"小节按时间窗口纳入画像快照。）

**token 估算**：不引入 tokenizer 库（避免为估算值增加重依赖），用字符数粗估：`估算 token ≈ 中文字符数 × 1.5 + 英文单词数 × 1.3`，并在截断时预留 20% 余量。该估算只对截断决策负责，不影响正确性。

### 6.3 审查流程（review）

1. `mex review list`：按时间倒序列出 `is_ai_inferred = 1` 且置信度非 `confirmed` 的记忆，显示置信度、证据与来源（`uncertain_update` 产生的候选也在此队列）。
2. `approve`：置信度提升为 `confirmed`，history 记录 `approve` 事件。
3. `decline`：软删除（写 `forgotten_at` + 原因），仍可 restore。若该条目是通过覆盖旧值产生的（history 有 `update` 事件），命令输出会提示旧值内容，用户可用 `mex add` 手动恢复。

### 6.4 导入流程与冲突处理（import）

`--mode extract`：读入任意文本/markdown 文件（含 v1 档案），走与 §6.1 完全相同的 LLM 抽取流程。**不记录 extraction_state**（一次性操作，重复导入的幂等由用户自行保证）。

`--mode restore`：导入 `mex export` 产出的 JSON（含 id 与全部字段），**不经过 LLM**，逐条处理：

| 冲突类型 | 默认行为 | `--on-conflict overwrite` |
|---|---|---|
| 同 id 且内容一致 | 跳过 | 跳过 |
| 同 id 但内容不同 | 跳过，列入冲突报告 | 覆盖，旧值写入 history |
| 不同 id 但画像槽位冲突 | 跳过，列入冲突报告 | 覆盖（旧条目软删除，写入 history） |
| 槽位不在当前 schema 中 | 跳过，列入冲突报告，提示先编辑 schema.yaml | 同默认（schema 约束不可被导入绕过） |

注：槽位冲突检查只对**唯一槽位**生效；可多条槽位（`unique: false`）允许同槽位多条共存，直接 insert 不视为冲突。

结束后输出摘要：新增 N、跳过 M、冲突 K（附清单）。安全优先：**默认行为下 restore 不会破坏任何现有数据**。

## 7. LLM 集成契约

extract / import(extract 模式) 是仅有的 LLM 调用点，契约如下。

### 7.1 提示词输入组成

system 消息按序拼接 8 部分，外加 user 消息（对话文本）。**不区分模式**：dialogue（日常对话）与 import（档案/履历导入）共用同一套归一化提示词（`llm/prompts.py` 的三引号常量文本）。

1. **任务说明**：角色统一为"个人记忆抽取器"，输入可为日常对话或档案/履历文档（文档输入信息密度高：身份级稳定事实写入对应画像字段；经历细节逐条写入可多条字段如 `work.experience`、`edu.education`、`research.paper`，每条一个独立记录；归入不了字段的作为画像外记录输出）；画像与画像外二元指导（§3.2）；槽位选择规则；只抽取"值得长期记住"的信息，忽略寒暄。话题领域不限（技术/工作/教育/家庭/感情/健康/理财/休闲/三观等同属画像范围）。
2. **schema 字段清单**：每个 topic/sub_topic 及其 description、是否可多条（`unique: false` 标注「可多条」）——LLM 只能往清单内字段填写（ADR-6 第一道防线）。
3. **置信度判定规则**：§3.3 的五档定义，且**禁止打 `confirmed`**。
4. **当前已有画像**：相关槽位的现有内容（可多条槽位列出全部已有 content），供去重与冲突检测。
5. **记忆质量标准**：具体/独立/有用/不重复四准则；信息密集的档案文档逐条捕获（实质内容必须保留）；合并与拆分规则（可多条字段每值单独成条，不合并成数组；显式事实与推断分开）；结构化字段的 content 必须输出 JSON 对象字符串；时间规范（禁止相对表述）。
6. **候选动作判定**：要求每条候选输出 `action` 字段（`new`/`update`/`uncertain_update`/`retire`）——区分"新事实"、"确信更新当前态唯一槽位"、"不确定是否该更新"（后者代码强制 `uncertain` 置信度 + `is_ai_inferred=true`，进入 `mex review` 人工审查）与"移除可多条槽位中一条已有记录"（retire）。这是"不依赖 LLM 自觉打对置信度"的代码层强制机制。
7. **抽取示例**：一套 few-shot 示例（档案逐条捕获写入可多条字段 + 可多条字段去重新增 + 画像槽位 update/uncertain_update + 画像外记录 + 健康/理财领域 + 放弃旧态度 retire）。
8. **输出格式要求**：§7.2 的 JSON 契约，只输出 JSON，不要任何额外文字。
9. **对话文本**（user 消息）。

### 7.2 输出 JSON 契约

```json
{
  "memories": [
    {
      "action": "new",
      "topic": "work",
      "sub_topic": "company",
      "content": "远光软件",
      "is_ai_inferred": false,
      "confidence": "explicit",
      "evidence": "用户说：\"我在远光软件做后端开发\""
    },
    {
      "action": "new",
      "topic": "work",
      "sub_topic": "tech_stack",
      "content": "Rust",
      "is_ai_inferred": false,
      "confidence": "explicit",
      "evidence": "用户说：\"最近在学 Rust\""
    },
    {
      "action": "new",
      "topic": "finance",
      "sub_topic": null,
      "content": "基金单日下跌 3%，用户表达了焦虑",
      "is_ai_inferred": true,
      "confidence": "speculated",
      "evidence": "用户提到\"今天跌得有点心慌\""
    }
  ]
}
```

规则：

- `action` 必填，四选一：`new`（新事实/可多条追加/画像外记录）、`update`（确信更新已有唯一画像槽位）、`uncertain_update`（可能要更新唯一槽位但 LLM 不确定，代码强制置信度 `uncertain` 并进入 `mex review` 人工审查）、`retire`（移除可多条槽位中一条已有记录，见下）。缺失时默认 `new`（向后兼容）。
- `retire`（矛盾消解范围 A）：只用于可多条槽位（积累态/「可多条」字段）。用户不再持有某旧态度/旧值（放弃某态度、改掉某习惯）时输出 retire，须指明 `topic`/`sub_topic`/`content`；代码按「槽位 + content 完全相同」软删该条已有记录（旧值进 history 可恢复，reason 记 retire），找不到完全相同记录则不删（discarded）；唯一槽位禁用 retire（其变更走 update/uncertain_update）。
- **画像槽位**（`topic` 与 `sub_topic` 均非空）：两者联合必须在 schema 字段清单内，属于画像内容；**画像外记录**：`sub_topic` 必须为 `null`，`topic` 可填清单内领域（便于检索）或省略。
- `is_ai_inferred` 为布尔：用户亲口陈述的内容为 `false`；AI 推断（含跨句、跨对话的总结）为 `true`。`uncertain_update` 的候选代码强制置 `is_ai_inferred=true`。
- 可多条字段（清单中标注「可多条」）的每个值单独成一条候选，`content` 为单值字符串（如技术栈的 Python、Go、Rust 各一条），不合并成数组；与当前已有值相同的不再输出；`action` 用 `new`（追加）。
- 结构化字段（清单中标明「content 为 JSON 对象」的字段，§3.4 `fields` 声明）的候选：`content` 必须输出 JSON 对象字符串，只含清单中给出的键、键值一律为字符串、未提及的键省略；不要把该字段的多个键拆成多条记录，也不要把 JSON 写成叙述文本。代码校验 JSON 合法性与键集合，不合法即丢弃。
- `evidence` 为证据摘要（引用用户原话），始终要求输出并存储，供人工审查回溯来源。
- 无值得抽取的内容时返回 `{"memories": []}`，属正常结果。

### 7.3 失败处理与重试

| 失败类型 | 行为 |
|---|---|
| LLM 返回非法 JSON | 先尝试提取文本中的 ```json 代码块或最外层 `{...}`；失败则带提醒重试 1 次；再失败则报错（退出码 2），**不写入任何数据** |
| API 超时 / 限流（429）/ 服务端错误（5xx） | 统一指数退避重试：间隔 **2s → 4s → 8s**（基数 × 2 的幂），最多重试 3 次；仍失败报错（退出码 2），不写入任何数据。单次请求超时默认 600 秒（可配置） |
| 部分候选不合规 | **不是失败**：合规的正常写入，不合规的丢弃并在摘要中列明原因 |

关键不变量：**LLM 调用全部完成后才开写事务**——任何 LLM 层面的失败都不会留下半更新的数据库。

### 7.4 用量记录

每次 LLM 调用写入 `llm_usage` 表（用途、模型、prompt/completion tokens、时间）。`mex stats` 展示累计用量，满足"掌握 token 花销"的诉求（费用金额由用户按模型单价自行换算，不内置价格表——价格易过时，硬编码是负债）。

## 8. Agent 集成

meX 不修改 agent 软件本身，通过 agent 软件公开的 skill 机制引导其读写记忆（ADR-12）；
会话结束 hook（OpenCode 官方不支持）留待后续扩展。

### 8.1 触发时机总览

| 时机 | 机制 | 说明 |
|---|---|---|
| 用户明确说"记住这个" | skill 引导 agent 即时执行 | `mex extract "<用户原话>"`（或 `mex add` 写单条事实），重要信息即时入库 |
| 对话开始 | skill 引导 agent 加载背景 | `mex profile`，把画像注入上下文 |
| 需要细节 / 近期状态 | skill 引导 agent 检索 | `mex search --topic`（定向）/ `mex search --since`（时敏召回） |
| 任意时刻 | 手动 `mex extract --file <file>` 或 `mex extract "<文本>"` | 地基，永远可用 |
| 会话结束自动抽取 | ❌ 暂缓（hook） | OpenCode 官方配置无 hooks 字段，第一版不生成 hook 配置，留待后续扩展（ADR-12） |
| 每轮对话后 | ❌ 明确不做 | LLM 延迟打断对话节奏，成本无谓放大 |

### 8.2 会话解析适配器

不同 agent 软件的会话存储格式不同，适配层负责统一转成纯对话文本（`用户：...\n助手：...`），之后的抽取流程完全一致：

- `mex extract --from claude --file <会话文件>`：解析 Claude Code 的 JSONL 会话文件
- `mex extract --from opencode --file <会话文件>`：解析 OpenCode 的会话存储
- `mex extract "<对话文本>"`：直接传文本（位置参数永远是文本，与 `--file` 二选一）

新增一种 agent 支持 = 在 `adapters/` 加一个解析模块，不动其他代码。

### 8.3 增量抽取

`extraction_state` 表（§3.1）记录每个会话文件"已处理到哪个位置"：

- 对同一会话文件重复触发 extract 时（重复触发、手动补抽），只处理 `last_position` 之后的新增内容——不重复抽取、不重复写入、不重复扣费。
- 处理位置与记忆写入在同一事务中更新（§6.1 第 7 步），保证"处理成功才记录"。
- 边界情况：若文件比上次记录的位置还短（被重建 / 截断），视为新文件从头处理。

### 8.4 mex integrate 命令

```
mex integrate claude --scope global     # 生成到 ~/.claude/
mex integrate claude --scope project    # 生成到 ./.claude/
mex integrate opencode --scope global   # 生成到 ~/.config/opencode/
mex integrate opencode --scope project  # 生成到 ./.opencode/
mex integrate workbuddy --scope global  # 生成到 ~/.workbuddy/skills/
mex integrate workbuddy --scope project # 生成到 ./.workbuddy/skills/
```

- `--scope project`（项目级）：配置生成到当前项目目录，只对该目录下的 agent 会话生效。
- `--scope global`（全局，**默认**）：生成到用户级配置目录，对所有项目的会话生效。
- 生成内容：skill 说明书 + README（对接步骤）。skill 的写入位置按 agent 不同：
  claude/opencode 写入官方 `skills/<name>/SKILL.md` 发现路径；workbuddy 的目标目录
  （`~/.workbuddy/skills` / `./.workbuddy/skills`）本身就是 skill 根目录，直接写 `mex/SKILL.md`。
  不再生成 hook 配置——OpenCode 官方配置 schema 无 hooks 字段，会话结束自动抽取留待后续扩展（ADR-12）。
- 执行后输出实际写入的文件路径清单，便于检查；重复执行覆盖旧文件（幂等）。

典型用法：全局开启 = 所有会话都接入记忆系统；项目级 = 只给特定项目接入（如把纯工作代码项目排除在外）。

### 8.5 skill 说明书内容要点

生成的 skill 文件教 agent 读写记忆（含写作原则）：

1. **对话开始**：执行 `mex profile`，把输出作为用户背景注入上下文（自动模式）。
2. **对话涉及用户生活、情绪或近期状态时**：先执行 `mex search --since 7d` 拉取近期记录（含画像外记录与画像槽位），感知用户最近发生了什么（时敏性召回；天数默认 7，可配置）。
3. **需要细节时**：执行 `mex search --topic <领域> [--keyword ...]`（手动模式）。
4. **用户明确说"记住这个"**：立即把用户原话经 `mex extract "<用户原话>"` 写入；简单明确的单条事实也可用 `mex add`。
5. **不要主动、频繁调用写命令**：除非用户明确要求或说出"记住这个"，否则不主动调用 `mex extract` / `mex add`——写路径按需触发，避免重复花费 LLM 成本或写入噪音。
6. **写作原则**：具体（保留名称/数字/日期/角色/原因）、独立（不读上下文可理解，禁止"本项目/它/那个"等指代）、有用、不重复；时间写具体日期（YYYY-MM-DD）；稳定事实写画像槽位（topic + sub_topic）、临时状态写画像外记录（省略 sub_topic）；人名/项目名用全称。

## 9. 本地存储布局

```
~/.mex/
├── mex.db        # SQLite 数据库（memories + history + llm_usage + FTS5 索引）
├── config.yaml   # 配置（见下）
└── schema.yaml   # 画像字段定义（ADR-6）
```

数据目录默认为 `~/.mex`，可用环境变量 `MEX_HOME` 覆盖（测试隔离、多实例场景使用）。

**安装方式**：项目为可安装的 Python 包，`pip install -e .`（开发模式）或 `uv sync` 安装后 `mex` 命令进入 PATH。integrate 生成的 skill 依赖 PATH 中的 `mex`，故安装是集成的前置条件。

**config.yaml 完整配置项**：

```yaml
llm:
  base_url: ""          # OpenAI 兼容 API 端点（mex config llm 配置）
  api_key: ""           # API Key：明文存储于本文件，init 时自动 chmod 600（仅本人可读）
  api_key_env: ""       # 可选：环境变量名，设置后优先于 api_key（CI/脚本场景）
  model: ""
  timeout_seconds: 600
  max_retries: 3           # 超时/限流/服务端错误的最大重试次数
  retry_base_seconds: 2    # 指数退避基数：第 n 次重试等待 base × 2^(n-1) 秒（即 2s → 4s → 8s）
profile:
  max_tokens: 3000     # mex profile 默认输出上限；可被命令行 --max-tokens 覆盖
```

**API key 存储策略（用户决策）**：双通道——日常用 `mex config llm` 把 key 明文存于 config.yaml（文件权限 600，仅本人可读）；`api_key_env` 配置后，其指定的环境变量存在时优先（CI/脚本场景覆盖）。配置优先级：**环境变量 > config.yaml > 内置默认值**。

备份建议（非 mex 内置功能，ADR-9）：定期 `mex export --format json --output ~/backups/mex/`，该目录可由用户自行 `git init` 并推送**私有**仓库。

## 10. 测试策略

框架：pytest。按分层采取不同测试方式：

| 层 | 测试方式 |
|---|---|
| `domain` | 纯单元测试，无 I/O：schema 校验、写入规则、枚举、置信度档位权限 |
| `store` | 用 pytest `tmp_path` 提供临时 SQLite 库，跑**真实 SQL**：CRUD、唯一约束、FTS5 检索、软删除过滤 |
| `services` | 真实临时库 + **fake LLM 客户端**（`llm/client.py` 通过依赖注入替换，返回录制的 JSON 响应） |
| `cli` | 用 Typer 的 `CliRunner` 做端到端命令测试，覆盖成功路径与典型错误路径（退出码、错误输出） |

硬性约束：

- 测试通过 `MEX_HOME` 指向临时目录，**绝不触碰真实 `~/.mex`**。
- 测试不调用真实 LLM API。
- 覆盖率目标：`domain` / `store` ≥ 90%；`services` / `cli` 覆盖关键路径（写入全流程、抽取全流程、profile 生成、import 冲突）。

## 11. 第一版非目标（明确不做）

| 不做项 | 说明 |
|---|---|
| 向量检索 | 预留接口（ADR-3）。**已知局限**：第一版关键词检索对"语义近义但字面不同"的内容召回有限（如搜"心情低落"命中不了"最近失恋"），靠事件层按需查询（§8.5）与结构化过滤缓解，属预期行为而非 bug，由后续向量检索解决 |
| GitHub 推送 | 用户决策（ADR-9），导出文件可自行 git 管理 |
| 常驻 HTTP 服务 | ADR-1 已定案 CLI 直连；服务化列入后续迭代 |
| 管理 UI | 需求已推迟，CLI 优先 |
| 账号体系 / 多用户 | 需求已砍掉，仅本地单用户 |
| 事件自动衰减/归档 | 列入后续迭代（ADR-11） |
| 各类人群的泛化 schema | 需求已推迟，先满足用户自身 |
| 追加式多值更新（--append） | 可多条槽位已天然支持追加（add 即追加一条独立记录），无需单独的 --append 参数 |

## 12. 后续迭代方向（按优先级）

> 优先级按"让记忆更准 → 更好用 → 更易接入"排序。已完成项标注 ✅。

### P0 — 让记忆更准（写入侧智能）

1. **更新判断准确率 + 不确定进 review**：
   - 背景：v3 槽位解耦后，"矛盾并存"已被结构防住（可多条追加、唯一覆盖不并存）。真正的问题是 **LLM 对当前态唯一字段（location、position 等）的更新判断不准**——用户说"我下周去深圳出差"，LLM 可能误判为搬家而覆盖 location；旧值虽进 history 可恢复，但用户可能无感。
   - 方案：
     - 提示词改进：教 LLM 区分"画像槽位的永久变更"（该覆盖）vs"临时状态/历史提及/他人情况"（不该动画像槽位，值得记的落为画像外记录）；判断不确定时对唯一槽位候选标 `uncertain` 置信度。
     - review 兜底：`uncertain` 候选照常写入（覆盖旧值、旧值进 history），因 `uncertain` 自然进 review 队列；用户 approve 保留新值，decline 软删新值并提示旧值可从 history 恢复。

### P1 — 让记忆更好用（使用侧体验，小代价大收益）

2. **重复信息强化**：同一事实反复出现时提升置信度 / 刷新更新时间戳（对应 supermemory 的 Preferences 强化）。与 v3 的 `_dedup_many_slots` 去重逻辑天然衔接——检测到"完全相同 content"时由"跳过"改为"强化"。
3. **被覆盖旧内容可检索**：唯一槽位更新时旧值只进 history 表，`mex search` 查不到。给 search 加 `--history` 选项，把 history 纳入检索（"曾经住过上海"也能搜到）。

### P2 — 让记忆不膨胀（长期使用治理）

4. **旧事件压缩归档**：`mex compact`，把 N 个月前的事件调 LLM 压缩为摘要条目，原事件软删除——控制事件层长期膨胀。
5. **临时事实时间衰减**：带时间属性的事件级事实到期自动降权（如"下周体检"），ADR-11 拒绝的自动衰减的轻量变体。

### P3 — 让记忆更易接入（生态，视需求排期）

6. **MCP 服务薄封装**：agent 免 shell 直接调用（标准工具协议，Claude Code / OpenCode 原生支持）。
7. **向量检索升级**：sqlite-vec + 本地 embedding 模型，提升事件层语义召回。代价大（引入模型或 API 费用，破坏"查询零费用"），有结构化画像 + 按需 search 兜底，非最急。
8. **本地 HTTP 服务化**：若多 agent 并发冲突实际出现，或管理 UI 需要后端，在业务服务层外包一层 HTTP 服务（ADR-1）。

### P4 — 体验扩展（远期）

9. **管理 UI**：浏览记忆、审查队列、用量统计的可视化界面。
10. **Schema 模板库**：面向不同人群（开发者 / 投资者 / 学生）的预置 schema.yaml。
