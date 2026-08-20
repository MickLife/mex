
## 对话约束
用户是一名 python 开发软件工程师，目前希望开发个人记忆系统 meX，用于获取个性化的大模型对话服务。
永远用中文和用户沟通
提问时，每次只问一个问题，并附上可能的选项和你的建议
用户没有 agent 产品的开发经验，沟通时注意专有词汇应加以解释，不允许直接用行业“黑话”、晦涩难懂的业内英文词汇与用户沟通。

## python编码约束
保持代码架构整洁、易于阅读，单文件 < 400 行，单函数 < 50 行。
复杂模块/函数/特殊变量/特殊逻辑要适当补充 docstring 或注释，使用 google 风格 docstring
使用loguru库的日志功能，不要使用logging
修改代码后，要使用`ruff check`命令进行检查，能够自动修复的问题要自动修复(`ruff check --fix`)，其他问题由你来看情况修复。
函数的入参、返回值要补全类型注解。

## 项目开发约定

### 架构与文档
- 权威依据：`docs/architecture.md`（已定稿，含全部 ADR 决策）。**修改架构文档必须先征求用户同意**，不得直接改。
- 需求定稿：`docs/requirements.md`。
- 开发期产物（竞品调研、选型、子模块规格、交接草案等）已归档至 `docs/archive/`，不再维护，仅供回溯；**接口以代码 docstring + `architecture.md` 为准**。

### 数据目录（重要）
- 默认数据目录：`~/.mex/`（真实使用时）。
- **开发与测试期间必须用环境变量覆盖**，绝不允许污染 `~/.mex/`：
  - 测试：`MEX_HOME` 指向 pytest `tmp_path`（子模块文档已约定）；
  - 开发冒烟：`export MEX_HOME="$(pwd)/.mex-dev"`。
- `.mex/` 已被 .gitignore 忽略（含隐私数据，禁止入库）。
- **临时文件/缓存一律写在项目内 `data/tmp/`**（已被 .gitignore 忽略），严禁写入 /tmp 等项目外部目录；`data/` 下其他子目录如与隐私相关也按需忽略。

### 工具链
- Python 3.12，uv 管理环境（`.venv/`，`uv sync` 安装依赖）。
- 依赖配置在 `pyproject.toml`（运行依赖 pyyaml/typer；开发依赖 pytest/pytest-cov）。
- ruff 已通过 `uv tool install ruff` 全局安装，配置在 pyproject.toml 的 `[tool.ruff]`（行宽 120）。
- 测试框架 pytest；测试不调用真实 LLM API（用依赖注入的 fake LLM）。

### 沟通
- 代码改动前先说明计划；文档改动必须经用户批准后执行。
