/**
 * meX × DeepSeek Harness 插件（standard Cordis bundle 的 Host half）。
 *
 * 把 meX 个人记忆系统的核心 CLI 命令镜像成 DSH 的原生 Tool（读 + 写 + 审查），
 * 薄壳设计：每个 Tool 只做「拼参数 → subprocess 执行 mex → 收集 stdout/stderr →
 * 映射退出码」，业务逻辑 100% 留在 mex CLI 内。
 *
 * 依赖：mex 可执行文件在 PATH 中（pip install -e . 或 uv sync 后生效），
 * 或用环境变量 MEX_BIN 指定绝对路径；数据目录由 mex 自身读 MEX_HOME（默认 ~/.mex）。
 *
 * 安装：把这个目录作为 DSH bundle 安装（见同目录 README.md），
 * 或 `dsh plugin add ./mex-dsh-plugin`。
 */

import { defineTool } from '@deepseek-ai/dsh-tools'
import { applyMexPanel } from './panel.js'

export const name = 'mex-dsh-plugin'

// subprocess 与 tools 均为硬依赖：前者执行 mex，后者注册 Tool。
export const inject = ['subprocess', 'tools']

/** 收集一个 mex 命令的 stdout/stderr/exitCode。 */
async function runMex(subprocess, args) {
  const bin = process.env.MEX_BIN || 'mex'
  const home = process.env.MEX_HOME || undefined
  let handle
  try {
    handle = subprocess.spawn({
      argv: [bin].concat(args),
      cwd: process.cwd(),
      stdio: {
        stdin: 'ignore',
        stdout: { maxBytes: 512 * 1024 },
        stderr: { maxBytes: 256 * 1024 },
      },
      graceMs: 30000,
      env: home ? { MEX_HOME: home } : undefined,
    })
  } catch (error) {
    return { ok: false, exitCode: -1, stdout: '', stderr: `spawn 失败: ${String(error)}` }
  }
  const outcome = await handle.done
  const outRead = handle.collected.stdout ? handle.collected.stdout.readFrom(0) : { text: '' }
  const errRead = handle.collected.stderr ? handle.collected.stderr.readFrom(0) : { text: '' }
  return {
    ok: outcome.exitCode === 0,
    exitCode: outcome.exitCode === null ? -1 : outcome.exitCode,
    stdout: outRead.text || '',
    stderr: errRead.text || '',
  }
}

/** 渲染：优先 stdout，否则 stderr。 */
function textRender(value) {
  const text = value && value.stdout ? String(value.stdout) : String((value && value.stderr) || '')
  const trimmed = text.trim()
  return [{ type: 'text', text: trimmed || '(无输出)' }]
}

/**
 * 记忆写作规范：meX 是跨项目记忆系统，写下的记忆会在任何项目中长期复用，
 * 必须脱离当前会话上下文自包含。拼入所有"写"工具的 description，让 agent
 * 每次写记忆时都看到（读工具不需要）。
 */
const WRITING_RULES =
  '写作规范：meX 是跨项目记忆系统，记忆会在任何项目中被长期读取。' +
  '1) 代词必须指代明确：禁止"本项目/它/那个/这家"等依赖上下文的指代，必须写出具体项目名/公司名/对象名；' +
  '2) 时间写具体日期（YYYY-MM-DD），不用"昨天/上周/最近"等相对时间；' +
  '3) 假设读者不知道本次对话上下文，内容自包含、可独立理解；' +
  '4) 人名/项目名用全称，首次出现必要时附简短说明（如"meX（本地记忆系统）"）。'

/** 统一输出 schema（所有 Tool 返回同构的 { ok, exitCode, stdout, stderr }）。 */
const OUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    ok: { type: 'boolean' },
    exitCode: { type: 'number' },
    stdout: { type: 'string' },
    stderr: { type: 'string' },
  },
}

/**
 * 参数 schema：dsh-tools 的 ParameterSchemaSpec 是「属性名 → 单个属性 schema」的映射，
 * 必填通过属性上的 required: true 表达（隐式 open object 根，无需外层包装）。
 */
function param(properties, required) {
  const spec = { ...properties }
  for (const key of required || []) {
    spec[key] = { ...spec[key], required: true }
  }
  return spec
}

/** 生成一个 Tool 定义：name/description/parameters + 命令参数映射 argsFn。 */
function makeTool(spec) {
  return defineTool({
    name: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    output: {
      schema: OUT_SCHEMA,
      render: (_args, value) => textRender(value),
    },
    async execute(args) {
      return await runMex(spec.subprocess, spec.argsFn(args))
    },
  })
}

export function apply(ctx) {
  const subprocess = ctx.subprocess
  const tools = ctx.tools

  const toolsSpecs = [
    {
      name: 'mex_add',
      description:
        '手动添加一条记忆到 meX（默认置信度 confirmed）。topic=领域主题（如 work/health/finance/basic_info）；sub_topic=画像字段（省略则作为画像外记录）；content=记忆内容（必填）。' +
        WRITING_RULES,
      parameters: param(
        {
          topic: { type: 'string', description: '领域主题' },
          sub_topic: { type: 'string', description: '画像字段' },
          content: { type: 'string', description: '记忆内容（必填）' },
          confidence: { type: 'string', description: '置信度：confirmed/explicit/inferred/speculated/uncertain' },
          expires: { type: 'string', description: '过期时间（YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）' },
        },
        ['content'],
      ),
      argsFn(a) {
        const r = ['add']
        if (a.topic) r.push('--topic', a.topic)
        if (a.sub_topic) r.push('--sub-topic', a.sub_topic)
        r.push('--content', a.content)
        if (a.confidence) r.push('--confidence', a.confidence)
        if (a.expires) r.push('--expires', a.expires)
        return r
      },
    },
    {
      name: 'mex_update',
      description:
        '更新一条已有记忆的内容/置信度/过期时间（槽位不可变）。' + WRITING_RULES,
      parameters: param(
        {
          memory_id: { type: 'string', description: '目标记忆 id（必填）' },
          content: { type: 'string', description: '新内容' },
          confidence: { type: 'string', description: '新置信度档位' },
          expires: { type: 'string', description: '设置过期时间' },
          clear_expires: { type: 'boolean', description: '清除过期时间' },
        },
        ['memory_id'],
      ),
      argsFn(a) {
        const r = ['update', a.memory_id]
        if (a.content) r.push('--content', a.content)
        if (a.confidence) r.push('--confidence', a.confidence)
        if (a.expires) r.push('--expires', a.expires)
        if (a.clear_expires) r.push('--clear-expires')
        return r
      },
    },
    {
      name: 'mex_remember',
      description:
        '从一段原文文本即时抽取记忆并写入 meX（调用 LLM）。当用户明确说"记住这个"时使用。' +
        WRITING_RULES,
      parameters: param({ text: { type: 'string', description: '要抽取记忆的原文文本（必填）' } }, ['text']),
      argsFn(a) { return ['extract', a.text] },
    },
    {
      name: 'mex_forget',
      description: '删除记忆：默认软删除（可恢复）。可传 memory_id 删单条，或用 topic/keyword/since/until 条件批量。',
      parameters: param({
        memory_id: { type: 'string', description: '单条记忆 id' },
        topic: { type: 'string', description: '批量筛选：领域' },
        keyword: { type: 'string', description: '批量筛选：关键词' },
        since: { type: 'string', description: '创建日期下限 YYYY-MM-DD' },
        until: { type: 'string', description: '创建日期上限 YYYY-MM-DD' },
        reason: { type: 'string', description: '遗忘原因' },
        hard: { type: 'boolean', description: '物理删除' },
        dry_run: { type: 'boolean', description: '仅预览不执行' },
        yes: { type: 'boolean', description: '跳过物理删除确认' },
      }),
      argsFn(a) {
        const r = ['forget']
        if (a.memory_id) r.push(a.memory_id)
        if (a.topic) r.push('--topic', a.topic)
        if (a.keyword) r.push('--keyword', a.keyword)
        if (a.since) r.push('--since', a.since)
        if (a.until) r.push('--until', a.until)
        if (a.reason) r.push('--reason', a.reason)
        if (a.hard) r.push('--hard')
        if (a.dry_run) r.push('--dry-run')
        if (a.yes) r.push('--yes')
        return r
      },
    },
    {
      name: 'mex_restore',
      description: '恢复一条软删除的记忆。',
      parameters: param({ memory_id: { type: 'string', description: '目标记忆 id（必填）' } }, ['memory_id']),
      argsFn(a) { return ['restore', a.memory_id] },
    },
    {
      name: 'mex_profile',
      description: '输出用户画像快照文本。可选 max_tokens 限制输出、topics 筛选领域。',
      parameters: param({
        max_tokens: { type: 'number', description: '输出上限' },
        topics: { type: 'string', description: '逗号分隔的领域筛选' },
      }),
      argsFn(a) {
        const r = ['profile']
        if (a.max_tokens) r.push('--max-tokens', String(a.max_tokens))
        if (a.topics) r.push('--topics', a.topics)
        return r
      },
    },
    {
      name: 'mex_search',
      description: '多条件检索 meX 记忆（画像槽位与画像外记录统一检索）。',
      parameters: param({
        topic: { type: 'string', description: '仅检索该领域' },
        sub_topic: { type: 'string', description: '仅检索该字段（须配合 topic）' },
        keyword: { type: 'string', description: '关键词检索' },
        since: { type: 'string', description: '本地日期下限 YYYY-MM-DD' },
        until: { type: 'string', description: '本地日期上限 YYYY-MM-DD' },
        limit: { type: 'number', description: '返回条数上限（1-1000，默认 50）' },
        include_forgotten: { type: 'boolean', description: '包含软删条目' },
      }),
      argsFn(a) {
        const r = ['search']
        if (a.topic) r.push('--topic', a.topic)
        if (a.sub_topic) r.push('--sub-topic', a.sub_topic)
        if (a.keyword) r.push('--keyword', a.keyword)
        if (a.since) r.push('--since', a.since)
        if (a.until) r.push('--until', a.until)
        if (a.limit) r.push('--limit', String(a.limit))
        if (a.include_forgotten) r.push('--include-forgotten')
        r.push('--json')
        return r
      },
    },
    {
      name: 'mex_get',
      description: '按 id 查看单条记忆的完整内容（JSON 输出）。',
      parameters: param({ memory_id: { type: 'string', description: '记忆 id（必填）' } }, ['memory_id']),
      argsFn(a) { return ['get', a.memory_id] },
    },
    {
      name: 'mex_list',
      description: '列出 meX 记忆（默认不含已删除条目）。',
      parameters: param({
        topic: { type: 'string', description: '按领域筛选' },
        sub_topic: { type: 'string', description: '按字段筛选（须配合 topic）' },
        include_forgotten: { type: 'boolean', description: '包含已删除条目' },
      }),
      argsFn(a) {
        const r = ['list']
        if (a.topic) r.push('--topic', a.topic)
        if (a.sub_topic) r.push('--sub-topic', a.sub_topic)
        if (a.include_forgotten) r.push('--include-forgotten')
        r.push('--json')
        return r
      },
    },
    {
      name: 'mex_history',
      description: '查看单条记忆的变更历史。',
      parameters: param({ memory_id: { type: 'string', description: '记忆 id（必填）' } }, ['memory_id']),
      argsFn(a) { return ['history', a.memory_id] },
    },
    {
      name: 'mex_review_list',
      description: '列出待审查的 AI 推断记忆。',
      parameters: param({}),
      argsFn() { return ['review', 'list', '--json'] },
    },
    {
      name: 'mex_review_approve',
      description: '批准一条 AI 推断记忆（置信度提升为 confirmed）。',
      parameters: param({ memory_id: { type: 'string', description: '记忆 id（必填）' } }, ['memory_id']),
      argsFn(a) { return ['review', 'approve', a.memory_id, '--json'] },
    },
    {
      name: 'mex_review_decline',
      description: '拒绝一条 AI 推断记忆（软删除）。',
      parameters: param(
        {
          memory_id: { type: 'string', description: '记忆 id（必填）' },
          reason: { type: 'string', description: '拒绝原因' },
        },
        ['memory_id'],
      ),
      argsFn(a) {
        const r = ['review', 'decline', a.memory_id]
        if (a.reason) r.push('--reason', a.reason)
        r.push('--json')
        return r
      },
    },
  ]

  for (const spec of toolsSpecs) {
    tools.register(makeTool({ ...spec, subprocess }))
  }

  // 面板逻辑：注册 /mex/panel-state 与 /mex/extract 接口，
  // 供 Client half 的右下角面板轮询状态与触发抽取 agent 对话。
  return applyMexPanel(ctx)
}
