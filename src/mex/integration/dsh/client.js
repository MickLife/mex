// meX × DeepSeek Harness 插件 · Client half（浏览器 UI 面板）。
//
// 打包产物格式（window.__ModuleLoader__.load），依赖从 dsh 的模块表解析：
// - react：staticModules 静态提供；
// - slots / sessions：通过 inject 声明，运行时由 dsh-client-runtime 提供。
//
// 面板注册到 shell.overlay（全屏浮动层，additive），默认停靠在页面右下角；
// 每轮对话结束后面板提示"可加入记忆"。点击"Add to MeX"通过 HTTP 接口
// 通知 Host 触发一轮新的抽取 agent 对话（见 panel.js），随后调用
// sessions.openSubagent 打开该子代理的会话视图——用户可实时看到抽取
// agent 调用工具与思考的完整过程（非黑盒）。子代理保留在侧边栏目录。
window.__ModuleLoader__.load({
  id: 'mex-dsh-plugin',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    var React = require('react')

    // 面板需要 slots（注册 UI）与 sessions（打开子代理会话视图）。
    var inject = ['slots', 'sessions']

    // 轮询间隔（毫秒）。
    var POLL_MS = 3000

    /** 解析面板状态响应：{ hasNew, lastTurn, extractedSeq, extraction, error }。 */
    function parseState(data) {
      if (data === null || typeof data !== 'object') return null
      return data
    }

    /**
     * meX 面板：右下角浮动卡片。
     * 从 shell.overlay 的标准 props 取当前 session（useSessions），
     * 轮询 Host 的面板状态接口，显示"可加入记忆"提示、抽取运行状态
     * 与触发按钮。sessions 服务实例由 apply 通过闭包注入 props.sessions。
     */
    function MexPanel(props) {
      var useSessions = props.useSessions
      var sessionId = useSessions(function (s) { return s.current })
      var sessions = props.sessions

      /** 解析 JSON 响应；非 JSON（405 空 body / HTML fallback）转为结构化错误提示。 */
      var parseJson = function (res) {
        return res.text().then(function (text) {
          if (!text) return { ok: false, error: '接口返回空响应（Host 服务可能未就绪，请重启 dsh 后重试）' }
          try {
            return JSON.parse(text)
          } catch (err) {
            return { ok: false, error: '接口返回非 JSON 响应（' + String(err) + '），请重启 dsh 后重试' }
          }
        })
      }

      var state = React.useState(null)
      var panel = state[0]
      var setPanel = state[1]

      var busyState = React.useState(false)
      var busy = busyState[0]
      var setBusy = busyState[1]

      // 主题检测：body[data-ds-dark-theme] 由 dsh 主题系统按偏好
      // （深色/浅色/跟随系统）解析，跟随系统时也已归一化。
      var darkState = React.useState(false)
      var isDark = darkState[0]
      var setIsDark = darkState[1]

      // 面板位置：null 表示默认右下角；拖动后为 { left, top }，持久化到 localStorage。
      var posState = React.useState(null)
      var position = posState[0]
      var setPosition = posState[1]
      var dragRef = React.useRef(null)

      React.useEffect(function () {
        var detect = function () {
          try {
            setIsDark(document.body.hasAttribute('data-ds-dark-theme'))
          } catch (err) {
            setIsDark(false)
          }
        }
        detect()
        var observer = null
        try {
          observer = new MutationObserver(detect)
          observer.observe(document.body, { attributes: true, attributeFilter: ['data-ds-dark-theme'] })
        } catch (err) { /* 无 MutationObserver 时只检测一次 */ }
        return function () {
          if (observer !== null) observer.disconnect()
        }
      }, [])

      // 恢复上次拖动位置（localStorage 持久化）。
      React.useEffect(function () {
        try {
          var raw = window.localStorage.getItem('mex-panel-pos')
          if (raw) {
            var saved = JSON.parse(raw)
            if (typeof saved.left === 'number' && typeof saved.top === 'number') {
              setPosition({ left: saved.left, top: saved.top })
            }
          }
        } catch (err) { /* 无 localStorage 时忽略 */ }
      }, [])

      /** 拖拽面板：标题栏按下 → 跟随鼠标移动 → 释放保存位置。 */
      var onTitleMouseDown = function (e) {
        if (e.button !== 0) return
        e.preventDefault()
        var startX = e.clientX
        var startY = e.clientY
        var startLeft = position !== null ? position.left : window.innerWidth - 240 - 16
        var startTop = position !== null ? position.top : window.innerHeight - 200
        dragRef.current = { startX, startY, startLeft, startTop }
        var onMove = function (ev) {
          var d = dragRef.current
          if (d === null || d === undefined) return
          setPosition({
            left: Math.max(0, d.startLeft + ev.clientX - d.startX),
            top: Math.max(0, d.startTop + ev.clientY - d.startY),
          })
        }
        var onUp = function () {
          dragRef.current = null
          window.removeEventListener('mousemove', onMove)
          window.removeEventListener('mouseup', onUp)
          // 保存位置（读取最新 state 不可靠，这里从 DOM 计算）
          try {
            var el = document.querySelector('[data-plugin="mex-dsh-plugin"]')
            if (el) {
              var rect = el.getBoundingClientRect()
              window.localStorage.setItem('mex-panel-pos', JSON.stringify({ left: rect.left, top: rect.top }))
            }
          } catch (err) { /* 忽略 */ }
        }
        window.addEventListener('mousemove', onMove)
        window.addEventListener('mouseup', onUp)
      }

      // 轮询面板状态：session 切换或每 POLL_MS 刷新一次。
      React.useEffect(function () {
        var alive = true
        var timer = null
        var tick = function () {
          if (!sessionId) return
          fetch('/mex/panel-state?sessionId=' + encodeURIComponent(sessionId))
            .then(parseJson)
            .then(function (data) {
              if (alive) setPanel(parseState(data))
            })
            .catch(function () { /* 网络瞬断忽略，下一轮重试 */ })
        }
        tick()
        timer = setInterval(tick, POLL_MS)
        return function () {
          alive = false
          if (timer !== null) clearInterval(timer)
        }
      }, [sessionId])

      /** 打开抽取子代理的会话视图（用户可查看工具调用与思考全程）。 */
      var openExtraction = function (childSessionId) {
        if (sessions === null || sessions === undefined || !sessionId || !childSessionId) return
        try {
          sessions.openSubagent({
            parentSessionId: sessionId,
            childSessionId: childSessionId,
            mode: 'one-shot',
          })
        } catch (err) {
          // openSubagent 失败不影响抽取本身，静默忽略。
        }
      }

      /** 点击 Add to MeX：触发抽取，并打开子代理会话视图（非黑盒）。 */
      var onAddToMex = function () {
        if (!sessionId || busy) return
        setBusy(true)
        fetch('/mex/extract', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ sessionId: sessionId }),
        })
          .then(parseJson)
          .then(function (data) {
            if (data && data.ok && data.childSessionId) {
              openExtraction(data.childSessionId)
            }
          })
          .catch(function () { /* 网络错误：轮询状态会反映真实情况 */ })
          .finally(function () { setBusy(false) })
      }

      var extraction = panel !== null ? (panel.extraction || null) : null
      var running = extraction !== null && extraction.phase === 'running'
      var done = extraction !== null && extraction.phase === 'done'
      // agentRunning：当前对话正在进行（等待模型输出 / 执行工具），
      // 本轮尚未结束，按钮不可用、不算"有新轮次可抽取"。
      var agentRunning = panel !== null && panel.ok === true && panel.agentRunning === true
      var hasNew = panel !== null && panel.ok === true && panel.hasNew === true
      var canRun = sessionId !== undefined && !busy && !running && !agentRunning

      // 配色：深色主题用 dsh token（自动跟随）；浅色主题微调——
      // 背景用非常浅的灰，成功绿加深保证可读性。
      // 深色主题背景用 --dsw-specific-menu（= bg-layer-3 = neutral-bluish-800，
      // rgb(53,54,56)，比 bg-overlay 更深，与官方弹层/菜单一致）；
      // 浅色主题用非常浅的灰。
      var cardBg = isDark ? 'var(--dsw-specific-menu)' : '#f5f6f8'
      var labelMain = isDark ? 'var(--dsw-alias-label-primary)' : '#1f2328'
      var labelSecondary = isDark ? 'var(--dsw-alias-label-secondary)' : '#6b7280'
      var borderColor = isDark ? 'var(--dsw-alias-border-l1)' : '#e3e6ea'
      var successColor = isDark ? 'var(--dsw-alias-state-success-primary)' : '#15803d'
      var warnColor = isDark ? 'var(--dsw-alias-state-warn-primary)' : '#b45309'
      var disabledBg = isDark ? 'var(--dsw-alias-bg-layer-1)' : '#e8eaed'

      // 面板卡片：默认右下角；用户拖动后固定到指定位置。
      var cardStyle = {
        position: 'fixed',
        zIndex: 9999,
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        width: '240px',
        padding: '12px',
        borderRadius: '12px',
        background: cardBg,
        color: labelMain,
        fontFamily: 'system-ui, sans-serif',
        fontSize: '13px',
        boxShadow: 'var(--dsw-shadow-lv3, 0 8px 24px rgba(0, 0, 0, 0.28))',
        border: '1px solid ' + borderColor,
        pointerEvents: 'auto',
        userSelect: 'none',
      }
      if (position !== null) {
        cardStyle.left = position.left + 'px'
        cardStyle.top = position.top + 'px'
      } else {
        cardStyle.right = '16px'
        cardStyle.bottom = '16px'
      }

      var titleStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        fontWeight: 600,
        fontSize: '14px',
        cursor: 'move',
      }

      var badgeStyle = {
        width: '8px',
        height: '8px',
        borderRadius: '50%',
        background: running ? warnColor : hasNew ? successColor : labelSecondary,
      }

      var hintStyle = {
        color: running ? warnColor : hasNew ? successColor : labelSecondary,
        lineHeight: '18px',
      }

      var buttonStyle = {
        width: '100%',
        padding: '8px 10px',
        border: 'none',
        borderRadius: '8px',
        background: hasNew && !running ? successColor : disabledBg,
        color: hasNew && !running ? '#ffffff' : labelSecondary,
        cursor: canRun ? 'pointer' : 'not-allowed',
        fontWeight: 600,
        fontSize: '13px',
      }

      var viewStyle = {
        width: '100%',
        padding: '6px 10px',
        border: '1px solid ' + borderColor,
        borderRadius: '8px',
        background: 'transparent',
        color: labelMain,
        cursor: 'pointer',
        fontWeight: 500,
        fontSize: '12px',
      }

      // 提示文本：优先抽取运行状态，其次对话进行中，其次抽取完成，
      // 再次可抽取，兜底引导。
      var hintText
      if (running) {
        hintText = '抽取中…点击下方按钮查看运行过程'
      } else if (agentRunning) {
        hintText = '对话进行中，本轮结束后可加入记忆'
      } else if (done) {
        hintText = '抽取完成：已写入 ' + (extraction.written || 0) + ' 条记忆'
      } else if (hasNew) {
        hintText = '本轮对话已结束，可加入记忆'
      } else {
        hintText = '对话结束后点击下方按钮抽取记忆'
      }

      var children = [
        React.createElement(
          'div',
          { style: titleStyle, onMouseDown: onTitleMouseDown, title: '拖动面板' },
          React.createElement('span', { style: badgeStyle }),
          React.createElement('span', null, 'meX 记忆'),
          React.createElement('span', { style: { color: labelSecondary, fontWeight: 400, fontSize: '11px', marginLeft: 'auto' } }, '⣿'),
        ),
        React.createElement('div', { style: hintStyle }, hintText),
        React.createElement(
          'button',
          { style: buttonStyle, onClick: onAddToMex, disabled: !canRun },
          running ? '抽取中…' : 'Add to MeX',
        ),
      ]

      // 抽取进行中：提供"查看运行过程"入口（打开子代理会话视图）。
      if (running && extraction !== null && extraction.childSessionId) {
        children.push(
          React.createElement(
            'button',
            {
              style: viewStyle,
              onClick: function () { openExtraction(extraction.childSessionId) },
            },
            '查看运行过程',
          ),
        )
      }

      return React.createElement(
        'div',
        { style: cardStyle, 'data-plugin': 'mex-dsh-plugin' },
        children,
      )
    }

    /**
     * Client 插件入口：注册 shell.overlay 面板，并把 sessions 服务注入组件。
     * @param ctx - client 根上下文（注入 slots / sessions）。
     */
    function apply(ctx) {
      var sessions = ctx.sessions
      ctx.slots.inject('shell.overlay', function () {
        return ctx.slots.register(
          { name: 'shell.overlay', id: 'mex-panel', order: 9999, label: 'meX 记忆' },
          // 包一层：把 sessions 服务实例通过 props 传给面板组件。
          function (props) {
            return React.createElement(MexPanel, Object.assign({}, props, { sessions: sessions }))
          },
        )
      })
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
