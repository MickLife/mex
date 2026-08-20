# meX

meX 是一个运行于你的电脑本地的记忆系统，存储并提供用户个人相关的记忆上下文，尤其是用户画像相关的条目，从而帮助用户实现个性化的 AI 服务。

## 为什么需要 meX

当下，大模型的能力越来越强，AI 不止能帮助人 Coding，还能帮助人学习、决策、进步。

AI agent 越来越强，但它记不住你：新开一段对话，agent 就"失忆"了——你要反复自我介绍、重复背景与偏好；换个 agent 产品，又得从头再来。而没有你的背景，agent 给出的建议只能是泛而不精。

很多人希望能够在和 agent 的对话过程中形成对用户画像的记忆，并在对话需要时提供用户的个人画像上下文，从而实现 agent 为用户提供个性化的对话服务。典型场景如情感聊天、理财建议、工作/学习建议、求职辅助、简历撰写、人生决策辅助、个人成长辅助等等方面。

- **理财建议**：agent 知道你的风险偏好、收入结构与已有投资，配置建议贴合实际，而不是模板化的"分散投资"
- **工作 / 学习建议**：技能栈、职业经历、教育背景结构化可查，建议有针对性（例如基于你的技术栈推荐学习路线）
- **求职辅助 / 简历撰写**：`mex import --mode extract` 把旧履历、档案一键迁移入库，经历逐条结构化；写简历、准备面试时 agent 有据可依
- **人生决策辅助**：搬家、换工作、定居等重大选择前，agent 结合你的价值观、家庭状况与历史想法帮你权衡利弊
- **个人成长**：长期记录目标、习惯与反思，`mex search` 随时回顾；对话越多，画像越准
- **情感陪伴 / 情绪支持**：agent 记得你的关系状态、处境与情绪敏感点，回应更有温度；情绪波动等临时信息记在画像外，近期动态自动进画像快照

这就要求在人和不同的 agent 之间，存在一套个人的上下文记忆。


## meX 的核心优势

- 完全的数据掌控：meX 部署于你的本地设备，隐私不泄露
- 不同的 agent 之间共享：让你的不同 agent 之间共享同一份记忆（已支持deepseek-harness)
- 个人画像系统：由 schema 定义个人画像，记忆遵循唯一事实原则，可扩展，易维护，易使用
- 易于管理：提供增删改查、遗忘、人工 review 等 cli 命令，便于 agent 调用；
- 证据关联：可溯源记忆的生成原因、遗忘原因、操作历史，可恢复
- 便于切换：支持从文本抽取记忆，支持结构化导出，便于你切换设备和记忆提供商
- 开源，免费

## 快速开始

### 安装

```bash
# 依赖 Python 3.12 + uv
uv sync                       # 构建并安装 mex 到虚拟环境

# 开发模式直接运行：
uv run mex --version          # 等价于 python -m mex；mex 已在 .venv 的 PATH 中
```

真实使用时不设 `MEX_HOME`，数据默认存放在 `~/.mex/`；开发时用 `export MEX_HOME="$(pwd)/.mex-dev"` 把数据放在项目内，避免污染真实数据目录。

### 初始化

```bash
mex init
```

创建数据目录：`mex.db`（数据库）、`schema.yaml`（画像字段定义）、`config.yaml`（配置）。后续命令直接 `mex <命令>`（在 `uv run mex` 的语境下，即 `uv run mex init`）。

### 添加记忆

```bash
# 手动添加画像条目（默认置信度 confirmed）
mex add --topic work --sub-topic company --content "华为"
mex add --topic work --sub-topic position --content "无线部门工程师"

# 画像外记录：临时/碎片信息省略 sub_topic（不占画像快照，按时间索引）
mex add --topic finance --content "今天基金跌了 3%，有点心慌"

# TTL 自动遗忘：设过期时间，到期对查询不可见（可 mex gc 清理）
mex add --topic finance --content "尴尬对话速记" --expires 2026-08-20
mex add --topic finance --content "敏感内容" --expires "2026-08-20 23:00:00"

# 从对话文本自动抽取记忆（需要配置 LLM，见"配置"）
mex extract 对话记录.md
```

### 查看与检索

```bash
mex list                # 列出所有记忆
mex list --topic work --sub-topic award   # 按领域+字段筛选
mex profile             # 输出画像快照（agent 注入用）
mex search --keyword 基金
mex search --topic finance --since 2026-08-01
mex search --topic work --sub-topic projects   # 按领域+字段检索
mex gc                    # 软删已到期（TTL）记录（--dry-run 预览）
mex get <id>              # 按 id 查看单条记忆的完整内容（JSON）
mex history <id>        # 查看单条记忆的变更历史
mex review list         # 查看待审查的 AI 推断记忆
mex review approve <id> # 批准（提升为 confirmed）
mex review decline <id> # 拒绝（软删除）
```

### 备份与恢复

```bash
mex export --format json --output ~/backups/mex-2026-08-06.json   # 完整备份（含软删状态）
mex export --format markdown --output ./记忆导出.md                 # 人类可读导出
mex import 备份文件.json                                            # 恢复（默认不覆盖现有数据）
mex stats                                                          # 统计信息
```

### 清空全部记忆

```bash
mex clear                     # 交互式：三层确认后清空（见下方说明）
mex clear --yes --no-backup   # 快速：跳过确认且不备份（自动化场景）
```

`mex clear` 清空全部记忆、全文索引与变更历史，出发前依次进行：

1. **数量确认**：展示将被清空的各表数量；
2. **备份确认**：询问是否先生成记忆快照。选 `y` 会**立即执行备份**并打印落盘路径（落到 `backups/` 目录，建议保留）；
3. **最终二次确认**：显示即将删除的总条数与备份状态，再次确认才执行。

清空完成后，若生成了备份，会额外提示恢复命令（`mex import <备份路径>`），便于日后还原。

`config.yaml`、`schema.yaml` 及 LLM 用量等非记忆数据一律保留、不受影响。非交互场景需 `--yes`。

## 核心概念

### 画像槽位与画像外记录

| 类别 | 判定 | 示例 | 常驻画像上下文 |
|---|---|---|---|
| 画像槽位 | 能归入 `schema.yaml` 的 `topic.sub_topic` 字段 | 姓名、公司、教育背景、价值观 | ✅ `mex profile` 输出 |
| 画像外记录 | 归入不了任何字段的临时/碎片信息（省略 `sub_topic`） | 今天完成的事、情绪波动、计划 | ❌ 仅 `mex search` 召回（近 7 天的记录会进 profile"近期动态"小节） |

`mex profile` 只输出画像槽位（能落到 schema 字段的稳定事实）；临时/碎片信息写为画像外记录，通过 `mex search` 按时间/关键词检索，不固定占用上下文。

### schema.yaml：画像字段白名单

`schema.yaml` 定义"允许记录哪些字段"。所有写入（手动或 AI 抽取）都必须命中白名单，否则被拒绝——这是画像长期整洁的保证：

```yaml
topics:
  work:
    description: 工作与职业
    sub_topics:
      company:    { description: 公司 }
      tech_stack: { description: 技术栈, unique: false }   # 可多条：每个技能一条
```

新增领域或字段 = 编辑 `~/.mex/schema.yaml` 加一行。修改后用 `mex doctor` 检查已有数据与 schema 的一致性。

### 置信度五档

| 档位 | 含义 |
|---|---|
| `confirmed` | 经你人工确认（`mex add` 默认档 / `review approve` 后） |
| `explicit` | 你原话明确陈述，有直接证据 |
| `inferred` | 由多条明确事实推导，证据链清晰 |
| `speculated` | 基于少量线索的推测 |
| `uncertain` | 几乎无证据的猜测 |

AI 抽取时只能打后四档；`confirmed` 只能由人产生。低置信条目在 `mex profile` 输出中标注"（待确认）"。

## 与 agent 集成（OpenCode / Claude Code / dsh）

`mex integrate` 一键生成与 agent 的对接文件（hook 配置 + skill 说明书 / DSH 插件 bundle），详细对接步骤见对应内层文档：

```bash
mex integrate claude --scope global     # 全局：所有项目生效（默认）
mex integrate opencode --scope project  # 仅当前项目生效
mex integrate dsh --scope project       # 生成 DSH 插件 bundle ./mex-dsh-plugin
```

| Agent | 对接文档 |
|---|---|
| Claude Code | [src/mex/integration/claude/README.md](src/mex/integration/claude/README.md) |
| OpenCode | [src/mex/integration/opencode/README.md](src/mex/integration/opencode/README.md) |
| DeepSeek Harness（dsh） | [src/mex/integration/dsh/README.md](src/mex/integration/dsh/README.md)（含开发者更新插件流程） |

## 配置

`mex init` 会生成 `~/.mex/config.yaml`（权限自动收紧为 600）。配置大模型供应商和模型，运行：

```bash
mex config llm                      # 交互式引导：选择供应商 → 地址 → API Key → 模型
mex config llm --provider deepseek --api-key sk-xxx --model deepseek-chat   # 或直接参数
mex config show                     # 查看当前配置（key 打码）
```

内置供应商预设：OpenAI / DeepSeek / Ollama（本地）/ Moonshot / 通义千问 / 自定义。config.yaml 结构：

```yaml
llm:
  base_url: ""          # OpenAI 兼容 API 端点
  api_key: ""           # API Key（明文存于本文件，权限 600 仅本人可读）
  api_key_env: ""       # 可选：环境变量名，设置后优先于 api_key（CI/脚本场景）
  model: ""
  timeout_seconds: 600
  max_retries: 3           # 失败重试（指数退避 2s → 4s → 8s）
  retry_base_seconds: 2
profile:
  max_tokens: 3000         # mex profile 输出上限
```

API key 双通道：日常存于 config.yaml（chmod 600 保护）；如需环境变量覆盖，在 `api_key_env` 里填变量名即可。优先级：**环境变量 > config.yaml > 内置默认值**。`MEX_HOME` 可覆盖数据目录。

## 数据安全

- 数据全部在本地 `~/.mex/mex.db`（SQLite 单文件），备份 = 复制文件
- 定期 `mex export --format json` 导出，可自行 `git init` 私有仓库管理历史版本
- 删除默认是软删除（数据保留、查询排除），`mex restore <id>` 随时恢复
- **TTL 自动遗忘**：`--expires` 设过期时间，到期对查询不可见（数据保留），`mex gc` 软删清理

## 开发与测试

```bash
uv run pytest            # 500+ 测试
ruff check src tests     # lint
```

架构设计见 [docs/architecture.md](docs/architecture.md)（含全部架构决策记录）；需求见 [docs/requirements.md](docs/requirements.md)。

## 当前状态

第一版（M1-M8）已全部完成：手动写入、查询画像、LLM 抽取、审查、备份迁移、agent 集成。规划中的后续迭代：事件压缩归档、向量语义检索、管理 UI 等（见架构文档"后续迭代方向"）。
