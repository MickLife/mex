# meX × DeepSeek Harness 插件

把 meX 个人记忆系统的核心 CLI 命令镜像成 DeepSeek Harness 的原生工具（读 + 写 + 审查）。

> 由 `mex integrate dsh` 生成；也可以直接把这个目录当作 DSH bundle 安装。

## 前置条件

1. `mex` 命令安装在 PATH 中（`pip install -e .` 或 `uv sync` 后生效），
   或用环境变量 `MEX_BIN=/abs/path/to/mex` 指定。
2. 数据目录由 mex 自身读 `MEX_HOME`（默认 `~/.mex`）。

## 对接步骤（从零接入 dsh）

前置：meX 已初始化（`mex init`），且 `mex` 命令在 PATH 中（见上方"前置条件"）。

### 1. 安装 dsh 与 pnpm（一次性）

```sh
npm install -g @deepseek-ai/dsh pnpm
dsh --version && pnpm --version
which dsh    # 应显示 ~/.local/bin/dsh
```

### 2. 生成插件 bundle 并装进 profile

在 mex 项目根目录：

```sh
mex integrate dsh --scope project    # 生成 ./mex-dsh-plugin
dsh plugin --profile <profile名> add ./mex-dsh-plugin
```

> `--scope project` 生成到 `./mex-dsh-plugin`（推荐）；`--scope global` 生成到 `~/.dsh/mex-dsh-plugin`。
> 也可以不装 profile，把 `cordis.patch.yml` 的 insert 行合并到 `~/.dsh/cordis.patch.yml`
> （对所有 profile 生效）。

### 3. 让 mex 命令进入 PATH（插件通过执行 mex 访问记忆库）

```sh
# 方式 A（推荐）：把 mex 安装为全局可执行包
cd <mex 项目目录> && .venv/bin/pip install -e .

# 方式 B：手动建符号链接（务必用绝对路径，相对路径会变成断链）
ln -s /Users/you/workspace/mex/.venv/bin/mex ~/.local/bin/mex
```

### 4. 验证并重启 dsh

```sh
dsh --profile web --dump-config | grep -A5 mex   # 应能看到 mex 插件配置
# Ctrl+C 停掉当前 dsh 后重新启动：
dsh web
```

新开一个会话，让 agent"读一下我的画像"，应能返回 `mex profile` 的内容（对应 `mex_profile` 工具）。

## meX 记忆面板（Client half）

插件自带一个浏览器 UI 面板，默认停靠在页面**右下角**：

- 每轮对话（dsh 的 Turn）结束后，面板提示"本轮对话已结束，可加入记忆"；
- 点击 **Add to MeX** 按钮：插件触发一轮全新的 agent 对话，自动把
  **抽取记忆 prompt + 本轮对话文本**交给该 agent，由 agent 调用 `mex_add` /
  `mex_update` 等工具把值得记住的信息写入 meX；
- 抽取 prompt 依据 `src/mex/llm/prompts.py` 的抽取规范精简而成：画像 vs
  画像外区分、置信度五档（禁止 confirmed）、质量标准（具体/独立/有用/不重复）、
  时间规范（具体日期），并引导 agent 先用 `mex_profile` / `mex_list` 查重。

**透明可见（非黑盒）**：点击后面板自动打开抽取子代理的**会话视图**——用户
实时看到抽取 agent 的思考过程与每一步工具调用（`mex_search` / `mex_add` 等），
与查看普通对话完全一致。抽取子代理**保留**在当前会话的侧边栏子代理目录中
（运行状态圆点：进行中 → 完成），可随时点开回看这次抽取做了什么；面板同时
显示"抽取中…"与"查看运行过程"按钮，完成后显示"已写入 N 条记忆"。

实现：Host half（`panel.js`）监听 `agent/turn-stopping` 事件缓存本轮对话，
注册 `/mex/panel-state`（状态轮询）与 `/mex/extract`（触发抽取）两个 HTTP 接口，
并监听 `subagent/end` 标记抽取完成；Client half（`client.js`）注册到
`shell.overlay` 浮动层并调用这两个接口，通过 `sessions.openSubagent` 打开
子代理会话视图。面板是**可拖动浮窗**（标题栏拖拽，位置记忆在 localStorage，
默认停靠右下角）；颜色跟随 dsh 主题——深色用主题 token，浅色微调为更浅的
灰底与更深绿提示，保证可读性。

> **注意**：抽取依赖 spawn 子代理（`dsh-subagent-spawn-in-process`，dsh 默认
> 装配），且每次抽取以"最近一轮对话"为单位——多轮未点只抽取最近一轮，点一次抽一轮。
>
> **完整实现细节**（触发链路、prompt 设计、防重复机制、与 ADR-12 的关系等）
> 见设计文档 [`docs/dsh-integration.md`](../../../../docs/dsh-integration.md)。

## 提供的工具（13 个）

| 工具 | 对应 mex 命令 | 说明 |
|---|---|---|
| `mex_add` | `mex add` | 手动添加记忆 |
| `mex_update` | `mex update` | 更新记忆 |
| `mex_remember` | `mex extract "<文本>"` | 即时抽取记忆（调 LLM） |
| `mex_forget` | `mex forget` | 删除（默认软删除） |
| `mex_restore` | `mex restore` | 恢复软删 |
| `mex_profile` | `mex profile` | 输出画像快照 |
| `mex_search` | `mex search` | 多条件检索 |
| `mex_get` | `mex get` | 单条详情 |
| `mex_list` | `mex list` | 列出记忆 |
| `mex_history` | `mex history` | 变更历史 |
| `mex_review_list` | `mex review list` | 待审查列表 |
| `mex_review_approve` | `mex review approve` | 批准推断记忆 |
| `mex_review_decline` | `mex review decline` | 拒绝推断记忆 |

> **跨项目写作规范**：meX 是跨项目记忆系统，`mex_add` / `mex_update` / `mex_remember` 三个写工具的
> description 内置了写作规范——代词必须指代明确（禁止"本项目/它/那个"等）、时间写具体日期、
> 内容自包含、名称用全称，确保记忆脱离写入会话后仍可被任何项目独立理解。

## 停用

`dsh plugin --profile <profile名> remove mex-dsh-plugin`，或从 `~/.dsh/cordis.patch.yml` 移除该行。

## 开发与更新插件（mex 开发者）

插件的**唯一源码位置**是 `src/mex/integration/dsh/` 下的六个模板文件（`index.js` / `panel.js` / `client.js` / `package.json` / `cordis.patch.yml` / `README.md`）。`mex-dsh-plugin/` 是 `mex integrate dsh` 的**生成产物**（已 gitignore），**不要直接改它**——改了会在下次重新生成时被覆盖丢失。

### 更新生效三步

1. **改模板**：编辑 `src/mex/integration/dsh/` 下的源码（`index.js` 工具实现、`panel.js` Host 面板逻辑、`client.js` 浏览器面板）或 `cordis.patch.yml`（补丁层）。
   开发环境是 `pip install -e .`（editable 安装），改完即生效，无需重装。
2. **重新生成**：在 mex 项目根目录执行：

   ```sh
   mex integrate dsh --scope project    # 覆盖 ./mex-dsh-plugin/
   ```

   dsh 安装插件用的是 pnpm 的 `link:` 协议——profile 的 node_modules 里是指向该目录的**符号链接**
   （`~/.dsh/profiles/<名>/node_modules/mex-dsh-plugin → <mex项目>/mex-dsh-plugin`），
   重新生成即原地更新，**无需重新执行 `dsh plugin add`**。
3. **重启 dsh**：Ctrl+C 停掉 `dsh web` 后重新启动。dsh 进程**不会热加载插件代码**，必须重启才生效。

### 注意事项

- **新增 npm 依赖**（改了 `package.json` 的 `dependencies`）：重新生成后还需在 `mex-dsh-plugin/` 下执行
  `pnpm install`，否则 `@deepseek-ai/dsh-tools` 等运行时依赖不会更新。
- **发布给 PyPI 用户**：非 editable 安装的用户需 `pip install -U mex` 升级后重新 `mex integrate dsh`。
- **一致性检查**：重新生成后确认模板与产物无漂移：

  ```sh
  diff -rq src/mex/integration/dsh/ mex-dsh-plugin/  # 排除 node_modules/package-lock.json
  ```

  若不一致，说明有人改过生成产物或模板未重新生成——以模板为准，重新生成一次。

### 验证

```sh
dsh --profile web --dump-config | grep -A5 mex   # 插件已装配
```

新开一个 dsh 会话，让 agent"读一下我的画像"，应能返回 `mex profile` 的内容（对应 `mex_profile` 工具）。

## 常见问题

- **插件装完不生效**：dsh 不会热加载插件，先 Ctrl+C 停掉再 `dsh web` 重启。
- **报错 `spawn mex ENOENT`**：`mex` 不在 PATH 里。执行上文"对接步骤 3"，并确认 `which mex` 有输出。
- **符号链接"断了"**：`ls -l ~/.local/bin/mex` 显示 `broken symbolic link`，说明建链接时用了相对路径，用"对接步骤 3"方式 B 的绝对路径重建即可。
