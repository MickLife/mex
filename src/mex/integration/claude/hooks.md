# meX × Claude Code：会话结束自动抽取（hook 配置）

> 由 `mex integrate claude` 生成，数据目录：`{{MEX_HOME}}`

把下面的 JSON 片段合并到 Claude Code 的 hook 配置中，会话结束时会自动把
本次对话交给 meX 抽取个人记忆（异步执行，不阻塞你开新会话）。

## 配置位置

- 全局（所有项目生效）：`~/.claude/settings.json` 的 `"hooks"` 字段
- 项目级（仅当前项目）：`./.claude/settings.json` 的 `"hooks"` 字段

## JSON 片段

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "mex extract --file <会话文件路径> --from claude" }
        ]
      }
    ]
  }
}
```

## 使用说明

1. 把 `<会话文件路径>` 替换为实际会话文件路径。Claude Code 的会话存储为
   JSONL，典型位置：`~/.claude/projects/<项目名>/<会话id>.jsonl`。
2. 确保 `mex` 命令在 PATH 中（项目 `pip install -e .` 或 `uv sync` 安装后生效）。
3. 记忆写入 meX 数据库：`{{DB_PATH}}`（可用环境变量 `MEX_HOME` 覆盖数据目录）。
4. 同一会话文件重复触发 extract 会自动增量处理（只抽取新增内容，不重复扣费）。

> 注：hook 事件名与配置结构以 Claude Code 官方文档为准；本模板给出当前主流写法，
> 如与你使用的版本不符，请按官方文档调整事件名。
