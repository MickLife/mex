# meX × OpenCode：对接步骤

> 由 `mex integrate opencode` 生成，生成目录：`{{AGENT_DIR}}`

本目录包含 meX 与 OpenCode 对接所需的 3 个文件：

| 文件 | 用途 |
|---|---|
| `hooks.md` | hook 配置说明 + JSON 片段（会话结束自动抽取记忆） |
| `skill.md` | skill 说明书（教 agent 何时读取 / 写入记忆） |
| `README.md` | 本文件（对接步骤） |

## 前置条件

1. meX 已初始化：`mex init`（数据目录 `{{MEX_HOME}}`）。
2. `mex` 命令在 PATH 中（项目 `pip install -e .` 或 `uv sync` 安装后生效）。

## 对接步骤

### 1. 配置 hook（会话结束自动抽取）

打开 `hooks.md`，把其中的 JSON 片段粘贴到 OpenCode 的 `opencode.json`：

- 全局（推荐，所有项目生效）：`~/.config/opencode/opencode.json` 的 `"hooks"` 字段
- 项目级（仅当前项目）：`./.opencode/opencode.json` 的 `"hooks"` 字段

注意把 JSON 中的 `<会话文件路径>` 替换为实际会话文件路径
（OpenCode 会话存储在 `~/.local/share/opencode/` 下，按项目分目录）。

### 2. 安装 skill（教 agent 用记忆）

把 `skill.md` 的内容放入 skill 文件：

- 全局：`~/.config/opencode/skill/mex/SKILL.md`
- 项目级：`./.opencode/skill/mex/SKILL.md`

即新建 `mex` 目录并把 `skill.md` 复制为其中的 `SKILL.md`。

### 3. 验证

1. 重新打开一个 OpenCode 会话（让新 hook / skill 生效）。
2. 让 agent 执行 `mex profile`，确认能输出用户画像。
3. 对 agent 说"记住这个 我喜欢喝美式咖啡"，确认它执行
   `mex extract "我喜欢喝美式咖啡"`。
4. 结束会话，确认没有报错；运行 `mex list` 能看到新记忆。

## 数据与回滚

- 数据目录：`{{MEX_HOME}}`（数据库 `{{DB_PATH}}`，可用 `mex export` 备份）。
- 想取消集成：删掉 opencode.json 中粘贴的 hook 片段与 skill 目录即可，不影响既有记忆。
