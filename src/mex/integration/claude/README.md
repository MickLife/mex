# meX × Claude Code：对接步骤

> 由 `mex integrate claude` 生成，生成目录：`{{AGENT_DIR}}`

本目录包含 meX 与 Claude Code 对接所需的文件：

| 文件 | 用途 |
|---|---|
| `skills/mex/SKILL.md` | skill 说明书（教 agent 何时读取 / 写入记忆） |
| `README.md` | 本文件（对接步骤） |

> 说明：本集成以 skill 承载记忆读写（读：`mex profile` / `mex search`；写：用户说"记住这个"
> 时 `mex extract` 即时写入）。会话结束 hook 自动抽取能力统一留待后续扩展，暂不生成 hook 配置。

## 前置条件

1. meX 已初始化：`mex init`（数据目录 `{{MEX_HOME}}`）。
2. `mex` 命令在 PATH 中（项目 `pip install -e .` 或 `uv sync` 安装后生效）。

## 对接步骤

### 1. 安装 skill（教 Claude 用记忆）

skill 已生成到本目录 `skills/mex/SKILL.md`。Claude Code 从以下位置自动发现 skill：

- 全局（推荐，所有项目生效）：`{{AGENT_DIR}}/skills/mex/SKILL.md`（即 `~/.claude/skills/mex/SKILL.md`）
- 项目级（仅当前项目）：`./.claude/skills/mex/SKILL.md`

如果生成目录与上述位置一致，直接**重新打开一个 Claude Code 会话**即可生效；
若不一致，把 `skills/mex/` 目录复制到对应位置。

### 2. 验证

1. 重新打开一个 Claude Code 会话（让新 skill 生效）。
2. 让 Claude 执行 `mex profile`，确认能输出用户画像。
3. 对 Claude 说"记住这个：我喜欢喝美式咖啡"，确认它执行 `mex extract "我喜欢喝美式咖啡"`。
4. 运行 `mex list` 能看到新记忆。

## 数据与回滚

- 数据目录：`{{MEX_HOME}}`（数据库 `{{DB_PATH}}`，可用 `mex export` 备份）。
- 想取消集成：删掉 skill 目录（`skills/mex/`）即可，不影响既有记忆。
