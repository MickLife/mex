# dsh 插件集成设计：meX 记忆面板与"Add to MeX"抽取链路

> 适用对象：mex-dsh-plugin（DeepSeek Harness 插件）。
> 本文档记录 dsh 侧集成**实现细节**，与 `architecture.md` §8 的 agent 集成章节互补：
> §8 描述 claude/opencode 的 skill 机制（即时写 + 会话结束 hook 留待扩展）；
> 本文描述 dsh 特有形态——浏览器 UI 面板 + 用户主动触发的"每轮对话后抽取"。
> 接口以代码 docstring 为准；本文是设计说明，不替代代码。

## 1. 定位与触发时机

| 维度 | 说明 |
|---|---|
| 触发方式 | **用户主动点击**面板按钮（Add to MeX），非自动实时抽取 |
| 触发粒度 | 一轮（dsh 的 Turn）对话结束后可触发一次 |
| 与 ADR-12 的关系 | 不冲突。ADR-12 §8.1 的"每轮对话后自动抽取 ❌ 不做"针对的是**自动实时抽取**（LLM 延迟打断对话节奏、成本无谓放大）；本面板由**用户显式点击**触发，成本与节奏由用户掌控，属于 §8.1 表格之外的用户主动形态 |
| 依赖 | dsh 默认装配的 spawn 子代理（`dsh-subagent-spawn-in-process`），无需额外安装 |

## 2. 整体架构

```
┌─────────────┐  ① POST /mex/extract   ┌──────────────────────────┐
│  浏览器面板    │ ──────────────────────► │ dsh Host（panel.js）       │
│  "Add to MeX"│                        │  ② 取缓存的本轮对话文本      │
└─────────────┘                        │  ③ 组装抽取 prompt          │
       │ ⑥ openSubagent                │  ④ 触发一个全新子代理（保留） │
       ▼ (打开子代理会话)                └────────────┬─────────────┘
┌──────────────────────┐                            │ ⑤ 子代理开始思考
│ 子代理会话视图（透明） │ ◄───────────────────────────┘
│ 用户实时看到：          │
│  · 思考过程（流式）     │     子代理调用 mex_search / mex_add …
│  · 工具调用卡片        │
│  · 最终"已写入N条"     │
└──────────────────────┘
```

**透明性设计**：抽取子代理**保留**（不 dispose），作为当前会话的 one-shot
子代理进入 dsh 侧边栏子代理目录；点击"Add to MeX"后 client 立即调用
`sessions.openSubagent(address)` 打开该子代理的会话视图——用户**实时看到
抽取 agent 调用工具与思考的完整过程**，不再是隐藏的黑盒。抽取完成后该
子代理会话保留在目录中，可随时回看（§4.8）。

## 3. 组件与文件职责

| 文件 | half | 职责 |
|---|---|---|
| `client.js` | Client（浏览器） | 注册 `shell.overlay` 浮动层面板（右下角）；轮询 `/mex/panel-state`；"Add to MeX"按钮 → POST `/mex/extract`；展示抽取结果 |
| `panel.js` | Host（Node） | 监听 `agent/turn-stopping` 缓存对话；注册 `/mex/panel-state` 与 `/mex/extract` 两个 HTTP 接口；组装抽取 prompt；触发 spawn 子代理 |
| `index.js` | Host（Node） | 注册 13 个 `mex_*` 工具（薄壳：subprocess 执行 `mex` CLI）；接入 `applyMexPanel(ctx)` |
| `package.json` | — | 声明 `dsh.client` 入口（Client half 打包产物 + 依赖注入） |

## 4. 触发链路逐步说明

### 4.1 点击按钮 → 浏览器发请求（client.js）

`onAddToMex` 只把**当前会话 id** 发给 Host，对话内容本身不随请求传输：

```js
fetch('/mex/extract', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ sessionId: sessionId }),
})
```

### 4.2 Host 取"本轮对话文本"（panel.js）

Host 维护内存缓存 `pendingTurns`（`sessionId → { turn, dialogue }`），来源是监听 dsh 事件：

```js
ctx.on('agent/turn-stopping', ({ agent, turn }) => {
  const dialogue = extractTurnDialogue(agent.session.log, turn)
  if (dialogue.trim()) pendingTurns.set(agent.id, { turn, dialogue })
})
```

- **`agent/turn-stopping`**：dsh 在"每一轮对话即将结束"时发出的信号（Turn 概念：一次"提问 → agent 完整回答"）。回答完、要收尾时触发。
- `extractTurnDialogue` 遍历会话日志，把 `user/message` 事件写为 `用户：…`，把 `assistant/message` 事件写为 `AI：…`，按 turn 号筛选目标轮次。
- 读 `agent.session.log`（实时追加数组）而非 `session.events`（惰性冻结快照，可能在 turn 进行中被提前固化，拿不到最新事件）。

点击按钮时对话文本**已在** Host 内存，请求处理函数直接取 `pending.dialogue`。

### 4.3 防重复机制

`extractedTurns`（`sessionId → turn`）记录每个会话"已抽取到第几轮"：

- 点击时比较 `lastTurn > extracted` 才允许抽取，否则返回"没有新的已结束对话可抽取"。
- 同一轮不会重复抽取、不会重复写入。

### 4.4 组装抽取 prompt（panel.js 的 EXTRACT_PROMPT）

参考 `src/mex/llm/prompts.py` 的抽取规范，精简为给 agent 的工具指令：

1. **画像 vs 画像外**：能归入 `topic.sub_topic` 槽位的写画像，否则写画像外记录（sub_topic 省略）
2. **置信度五档**：explicit / inferred / speculated / uncertain，禁止 confirmed（仅供人工审查）
3. **质量标准**：具体（保留名称/数字/日期/角色）、独立（脱离对话可理解）、有用、不重复
4. **时间规范**：写具体日期（YYYY-MM-DD），禁止"最近/上周/昨天"等相对表述
5. **操作步骤**：先 `mex_profile`/`mex_list` 查重 → `mex_add` 新增 / `mex_update` 更新 → 无内容不写
6. 最后要求输出"已写入N条记忆"（供面板显示条数）

组装时替换占位符：

```js
prompt: [{ type: 'text', text: EXTRACT_PROMPT.replace('{dialogue}', dialogue) }]
```

### 4.5 触发全新子代理（panel.js）

```js
const run = await subagents.start('spawn', {
  label: 'meX 记忆抽取',
  prompt: [{ type: 'text', text: ... }],   // 4.4 组装好的 prompt
  parent,                                    // 当前会话的 agent（作为父级）
  signal: controller.signal,
  maxDepth: 3,
})
```

- `subagents.start('spawn', ...)` 是 dsh 的子代理机制（`dsh-subagent-spawn-in-process` 提供）。
- 创建一个**全新的子代理**：独立会话、独立上下文、**看不到用户与 AI 的主对话**（零父上下文）。注意：是全新会话（`agents.create` 随机新 id），**不是** fork 主对话。
- 把 prompt 作为子代理的第一条用户消息。
- **子代理保留（不 dispose）**：与 dsh 标准工具的"前台调用结束即 dispose"不同，这里刻意保留——子代理作为当前会话的 one-shot 子代理进入侧边栏目录，用户可点开查看完整过程（§4.8）。
- **为什么用子代理**：抽取是独立任务（读记忆库、写记忆库），不应污染当前对话，也不应看到主对话外的内容。隔离的临时 agent 最干净。
- 子代理继承所有已注册工具——**包括 13 个 `mex_*` 工具**，这是它能写记忆的原因。

### 4.6 子代理调用 mex 工具写记忆

子代理自主决策，典型流程：

1. `mex_profile` / `mex_list` → 查看记忆库已有内容（了解字段、避免重复）
2. 每条值得记的信息 → `mex_add`（topic / sub_topic / content / confidence）
3. 已有信息变化 → `mex_update`
4. 无值得记的内容 → 不调用任何写工具，直接回复"无新增记忆"

写工具的 description 内置"写作规范"（代词明确、具体日期、自包含、用全称），子代理每次调用可见，与 4.4 的 prompt 双重约束。

### 4.7 mex 工具薄壳：subprocess 执行 CLI（index.js）

```js
async execute(args) {
  return await runMex(spec.subprocess, spec.argsFn(args))
}
```

`runMex` 用 `subprocess.spawn` 启动真实的 `mex` 命令行进程，例如子代理调用
`mex_add(topic="work", sub_topic="company", content="星辰科技")` 底层执行：

```bash
mex add --topic work --sub-topic company --content "星辰科技"
```

`mex add` 是 meX 项目自己的 CLI（`src/mex/cli/write.py`），内部把记忆写入
`~/.mex/mex.db`（SQLite）。**业务逻辑 100% 留在 meX CLI 内**——唯一事实原则、
槽位校验、审计、置信度处理都由 meX 承担，插件只做"参数翻译 + 进程调用 + 结果透传"。

### 4.8 透明可见：打开子代理会话，实时查看全程

**设计目标**：抽取不是黑盒——用户点击后能看到子代理调用工具与思考的全程。

触发链路（异步）：

1. **Host 立即返回，不等待**：`onExtract` 在 `subagents.start` 返回后马上响应
   `{ ok, childSessionId, parentSessionId, mode: 'one-shot' }`，**不** `await run.result`，
   **不** dispose（子代理保留）。
2. **Client 打开子代理会话视图**：面板拿到 `childSessionId` 后调用
   `sessions.openSubagent({ parentSessionId, childSessionId, mode: 'one-shot' })`，
   界面切换到该子代理的会话——用户**实时看到**抽取 agent 的思考过程、
   每一步 `mex_*` 工具调用卡片与结果，与查看普通对话完全一致。
3. **运行状态**：Host 维护 `extractionState`（`sessionId → { phase: 'running',
   childSessionId }`），`/mex/panel-state` 返回给面板；面板显示"抽取中…"，
   并提供"查看运行过程"按钮（再次 `openSubagent`，从侧边栏/面板均可进入）。
4. **完成标记（`subagent/end`）**：Host 监听 `subagent/end` 事件，按
   `info.id`（子代理 session id）关联到父会话，把该轮标记为已抽取（防重复），
   并从 `info.lastAssistantMessage` 解析"已写入N条记忆"，更新
   `extractionState` 为 `{ phase: 'done', written, stopReason }`。
5. **保留回看**：抽取完成的子代理会话保留在侧边栏子代理目录（运行状态圆点
   变"done"），用户可随时点开回看这次抽取做了什么。

```js
// panel.js：subagent/end 完成标记
ctx.on('subagent/end', (info) => {
  const entry = extractionState.get(info.id)   // childSessionId → 父会话关联
  if (entry === undefined) return
  extractedTurns.set(entry.parentSessionId, entry.turn)
  extractionState.set(entry.parentSessionId, {
    phase: 'done',
    childSessionId: info.id,
    written: parseWritten(info.lastAssistantMessage),
    stopReason: info.stopReason,
  })
})
```

面板收到 `extraction.phase === 'done'` 后显示"抽取完成：已写入 N 条记忆"。

## 5. 关键设计决策

| 决策 | 原因 |
|---|---|
| 用子代理而非直接调 `mex extract` | 需求要求"触发一轮新的 agent 对话"；子代理是 dsh 的隔离 agent 机制，能看到 mex 工具但看不到主对话上下文——安全且不干扰当前对话 |
| prompt 参考 prompts.py 但改写成"工具指令" | prompts.py 是给 `mex extract` 用的（输出 JSON 让代码解析）；本场景 agent 自主**操作工具**，所以 prompt 是"规范 + 操作步骤" |
| 每轮一个对话文本 + `extractedTurns` 防重复 | 避免同一轮对话被反复抽取写脏数据 |
| 子代理保留 + `openSubagent` 打开会话视图 | 抽取过程**透明可见**：用户实时看到工具调用与思考，不再是黑盒；完成后仍可回看 |
| 异步触发（不 `await run.result`）+ `subagent/end` 完成标记 | HTTP 立即返回、不阻塞请求；完成状态由事件驱动，面板轮询展示 |
| `ctx.inject(['webServer'], cb)` 等待路由注册 | bundle 插件 apply 时机可能早于 webServer 服务注册（Cordis 按 inject 依赖激活，mex 只依赖 subprocess/tools）；一次性 `ctx.get('webServer')` 会拿到 undefined 导致路由永不注册、请求落到 SPA fallback（曾导致 405 空 body、前端 `Unexpected end of JSON input`）。headless 无 webServer 时回调不执行、不影响 mex 工具 |
| 读 `agent.session.log` 而非 `session.events` | events 是惰性冻结快照，turn 进行中可能已被固化；log 是实时追加数组 |

## 6. HTTP 接口

| 接口 | 方法 | 入参 | 返回 |
|---|---|---|---|
| `/mex/panel-state` | GET | `sessionId` query | `{ ok, hasNew, lastTurn, extractedTurn, extraction }`（`extraction` 为 `{ phase: 'running'\|'done', childSessionId, written?, stopReason? }`） |
| `/mex/extract` | POST | body `{ sessionId }` | `{ ok, started, childSessionId, parentSessionId, mode: 'one-shot' }`（异步触发，立即返回） |

同源 HTTP（webServer 注册 exact route），Client half 用 fetch 调用。

## 7. 局限与后续

- 抽取以"最近一轮"为单位：点一次抽一轮，多轮未点只抽最近一轮。
- 子代理是真实 LLM 对话，判断有自由度（漏记/置信度偏差），建议定期 `mex review list` 审查 AI 推断记忆。
- 每次点击都启动一个子代理（一次 LLM 调用）；高频点击会放大成本——当前由"同一轮防重复 + 运行中防重入"机制兜底。
- 子代理保留在侧边栏目录：抽取完成后不回看也不会自动清理，长期使用目录会累积历史抽取会话（可接受；如需清理可删除对应子代理会话）。
- 未来若做"会话结束自动抽取"，可复用 4.2 的 `pendingTurns` 缓存与 4.4 的 prompt，在 hook/事件回调里触发（ADR-12 留待扩展的 hook 能力）。
