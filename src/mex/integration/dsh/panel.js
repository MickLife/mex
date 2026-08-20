/**
 * meX × DeepSeek Harness 插件 · Host 面板逻辑（抽取 agent 触发）。
 *
 * 职责：
 * 1. 监听 `agent/turn-stopping`（每轮对话即将关闭）——记录该会话"最近一轮
 *    已结束"的对话文本（user/assistant 消息），供面板提示与抽取使用；
 * 2. 面板状态查询：client 轮询该会话是否有未抽取的已结束轮次；
 * 3. 触发抽取：点击"Add to MeX"后，把未抽取轮次的对话文本 + 抽取 prompt
 *    交给一个全新的子代理（spawn），子代理调用 mex_* 工具写入记忆。
 *
 * 通信：通过 webServer 注册两个同源 HTTP 接口（/mex/panel-state、
 * /mex/extract），client half 用 fetch 调用。webServer / subagents /
 * agents / timer 均为可选依赖：未装配时面板相关功能静默降级。
 */

/** 抽取 prompt：参考 src/mex/llm/prompts.py 的抽取规范，精简为工具指令。 */
const EXTRACT_PROMPT = `你是"个人记忆抽取器"。以下是用户与 AI 助手的一段对话，请从中抽取值得长期记住的个人信息，并调用 meX 提供的工具写入记忆库。

抽取规则（必须遵守）：
1. 画像 vs 画像外：能归入某个画像字段（topic.sub_topic 均非空且被 schema 声明）的信息 → 写入画像（mex_add 的 topic/sub_topic 参数）；归入不了任何槽位的临时信息（情绪、想法、计划、当天完成的事）→ 写为画像外记录（sub_topic 省略，topic 可填领域或省略）。
2. 置信度五档：explicit（用户原话明确陈述）/ inferred（由多条事实合理推导）/ speculated（少量线索推测）/ uncertain（几乎无证据）。禁止 confirmed（仅供人工审查）。推断信息必须用后三档之一。
3. 质量标准：每条候选记忆必须同时满足：具体（保留名称/数字/日期/角色/原因）、独立（脱离对话也能理解）、有用（影响未来对话）、不重复（与已有记忆不重复）。
4. 时间规范：用具体日期（YYYY-MM-DD），禁止"最近/上周/昨天"等相对表述。
5. 写作规范：代词指代明确（禁止"它/这个/那家"等依赖上下文的指代）、名称用全称、内容自包含。

操作步骤：
1. 先用 mex_profile / mex_list 查看现有画像与记忆（了解字段与去重）。
2. 新信息用 mex_add 写入；已有信息有变化用 mex_update 更新。
3. 若无值得抽取的内容，不要调用任何写工具，直接回复"无新增记忆"。

最后一行必须输出：已写入N条记忆（N 为实际写入条数，未写入则为 0）。

对话内容：
{dialogue}`

/** 每会话最近一轮已结束的对话缓存：sessionId → { turn, dialogue }。 */
const pendingTurns = new Map()

/** 每会话已抽取到的最大 turn 号（内存态；重启后未抽取轮次需重新对话才会补录）。 */
const extractedTurns = new Map()

/** 从 ContentBlock 数组中提取纯文本。 */
function textOf(blocks) {
  if (!Array.isArray(blocks)) return ''
  return blocks
    .filter((b) => b !== null && typeof b === 'object' && b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text)
    .join('')
}

/**
 * 从 agent 的事件流中提取"第 targetTurn 轮"的用户/AI 对话文本。
 * 在 agent/turn-stopping 事件里调用：此时该轮 user/message 与
 * assistant/message 已全部 append，而 turn/end 尚未写入。
 * user/message 事件本身不带 turn 字段，按 turn/start 边界归属当前轮。
 */
function extractTurnDialogue(events, targetTurn) {
  const lines = []
  let currentTurn = 0
  for (const ev of events || []) {
    if (!ev || !ev.data) continue
    if (ev.type === 'turn/start' && typeof ev.data.turn === 'number') {
      currentTurn = ev.data.turn
      continue
    }
    if (ev.type === 'turn/end' && typeof ev.data.turn === 'number') {
      currentTurn = 0
      continue
    }
    if (currentTurn !== targetTurn) continue
    if (ev.type === 'user/message') {
      const text = textOf(ev.data.content)
      if (text) lines.push(`用户：${text}`)
    } else if (ev.type === 'assistant/message' && ev.data.turn === targetTurn) {
      const text = textOf(ev.data.message && ev.data.message.content)
      if (text) lines.push(`AI：${text}`)
    }
  }
  return lines.join('\n')
}

/** 从抽取 agent 的输出中解析"已写入N条记忆"。 */
function parseWritten(outputBlocks) {
  const text = (outputBlocks || [])
    .filter((b) => b !== null && typeof b === 'object' && b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text)
    .join('\n')
  const match = /已写入\s*(\d+)\s*条记忆/.exec(text)
  return match ? Number(match[1]) : 0
}

/** 读取 node http 请求体（JSON），返回解析后的对象或 null。 */
function readJsonBody(req) {
  return new Promise((resolve) => {
    let raw = ''
    req.on('data', (chunk) => {
      raw += chunk
      if (raw.length > 1024 * 1024) {
        req.destroy()
        resolve(null)
      }
    })
    req.on('end', () => {
      try {
        resolve(JSON.parse(raw || '{}'))
      } catch {
        resolve(null)
      }
    })
    req.on('error', () => resolve(null))
  })
}

/** 写 JSON 响应（node http 风格）。 */
function sendJson(res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  })
  res.end(payload)
}

/** 解析 URL 查询参数（sessionId 等）。 */
function queryOf(req) {
  const url = new URL(req.url || '/', 'http://localhost')
  return Object.fromEntries(url.searchParams.entries())
}

/**
 * 注册面板接口与抽取逻辑。
 *
 * 注意：不能用 `ctx.get('webServer')` 一次性获取——bundle 插件的 apply 时机
 * 可能早于 webServer 服务注册（装配时序由 Cordis 依赖决定），拿到 undefined
 * 会导致路由永不注册、面板请求落到 SPA fallback。正确做法是用
 * `ctx.inject(['webServer'], ...)` 等待服务可用后再注册路由；headless 环境
 * 服务永不出现则回调不执行，插件其余功能（mex 工具）不受影响。
 *
 * @param ctx - bundle 插件上下文。
 */
export function applyMexPanel(ctx) {
  // 每轮对话即将关闭时缓存该轮对话文本，供面板提示与抽取使用。
  // 该监听不依赖 webServer，始终注册。
  ctx.on('agent/turn-stopping', ({ agent, turn }) => {
    try {
      // session.log 是实时追加数组；session.events 是惰性冻结快照，
      // 可能在 turn 进行中被提前固化，故这里读 log 拿到最新事件。
      const dialogue = extractTurnDialogue(agent.session.log, turn)
      if (dialogue.trim()) pendingTurns.set(agent.id, { turn, dialogue })
    } catch (error) {
      ctx.logger?.warn(`mex-panel: 缓存第 ${turn} 轮对话失败: ${String(error)}`)
    }
  })

  /** GET /mex/panel-state?sessionId=…：面板状态（是否有未抽取的已结束轮次）。 */
  const onPanelState = async (req, res) => {
    const sessionId = queryOf(req).sessionId
    if (!sessionId) {
      sendJson(res, 200, { ok: true, hasNew: false, error: '缺少 sessionId' })
      return
    }
    const pending = pendingTurns.get(sessionId)
    const lastTurn = pending ? pending.turn : 0
    const extracted = extractedTurns.get(sessionId) ?? 0
    sendJson(res, 200, { ok: true, hasNew: lastTurn > extracted, lastTurn, extractedTurn: extracted })
  }

  /** POST /mex/extract body {sessionId}：触发抽取 agent 对话。 */
  const onExtract = async (req, res) => {
    const body = await readJsonBody(req)
    const sessionId = body && typeof body.sessionId === 'string' ? body.sessionId : undefined
    const agents = ctx.get('agents')
    const subagents = ctx.get('subagents')
    if (!sessionId || agents === undefined || subagents === undefined) {
      sendJson(res, 200, { ok: false, error: '依赖服务不可用（agents/subagents 未装配）' })
      return
    }
    const pending = pendingTurns.get(sessionId)
    const lastTurn = pending ? pending.turn : 0
    const extracted = extractedTurns.get(sessionId) ?? 0
    if (lastTurn <= extracted || !pending) {
      sendJson(res, 200, { ok: false, error: '没有新的已结束对话可抽取' })
      return
    }
    const parent = agents.get(sessionId)
    if (parent === undefined) {
      sendJson(res, 200, { ok: false, error: '会话未激活（agent 不在运行中）' })
      return
    }
    const controller = new AbortController()
    try {
      const run = await subagents.start('spawn', {
        label: 'meX 记忆抽取',
        prompt: [{ type: 'text', text: EXTRACT_PROMPT.replace('{dialogue}', pending.dialogue) }],
        parent,
        signal: controller.signal,
        maxDepth: 3,
      })
      const result = await run.result
      // 抽取完成（含"无新增记忆"）：标记该轮已处理，避免重复触发。
      extractedTurns.set(sessionId, lastTurn)
      sendJson(res, 200, {
        ok: true,
        written: parseWritten(result && result.output),
        stopReason: result && result.stopReason,
        notes: result && result.stopReason === 'completed' ? undefined : '抽取未正常完成',
      })
    } catch (error) {
      sendJson(res, 200, { ok: false, error: error instanceof Error ? error.message : String(error) })
    }
  }

  // 等待 webServer 服务可用后再注册路由（web 环境）；回调返回 disposer，
  // 服务卸载时由 Cordis 收集清理。
  ctx.inject(['webServer'], (webCtx) => {
    const webServer = webCtx.webServer
    const d1 = webServer.register({ kind: 'exact', path: '/mex/panel-state', handler: onPanelState })
    const d2 = webServer.register({ kind: 'exact', path: '/mex/extract', handler: onExtract })
    return () => {
      d1()
      d2()
    }
  })
}
