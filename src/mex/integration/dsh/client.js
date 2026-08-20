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
      var hasNew = panel !== null && panel.ok === true && panel.hasNew === true
      var canRun = sessionId !== undefined && !busy && !running

      // 面板卡片：固定右下角，深色底，紧凑布局。
      var cardStyle = {
        position: 'fixed',
        right: '16px',
        bottom: '16px',
        zIndex: 9999,
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        width: '240px',
        padding: '12px',
        borderRadius: '12px',
        background: 'rgba(24, 24, 27, 0.92)',
        color: '#e4e4e7',
        fontFamily: 'system-ui, sans-serif',
        fontSize: '13px',
        boxShadow: '0 8px 24px rgba(0, 0, 0, 0.28)',
        pointerEvents: 'auto',
      }

      var titleStyle = {
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        fontWeight: 600,
        fontSize: '14px',
      }

      var badgeStyle = {
        width: '8px',
        height: '8px',
        borderRadius: '50%',
        background: running ? '#facc15' : hasNew ? '#4ade80' : '#52525b',
      }

      var hintStyle = {
        color: running ? '#fde68a' : hasNew ? '#bbf7d0' : '#a1a1aa',
        lineHeight: '18px',
      }

      var buttonStyle = {
        width: '100%',
        padding: '8px 10px',
        border: 'none',
        borderRadius: '8px',
        background: hasNew && !running ? '#16a34a' : '#3f3f46',
        color: hasNew && !running ? '#ffffff' : '#a1a1aa',
        cursor: canRun ? 'pointer' : 'not-allowed',
        fontWeight: 600,
        fontSize: '13px',
      }

      var viewStyle = {
        width: '100%',
        padding: '6px 10px',
        border: '1px solid #3f3f46',
        borderRadius: '8px',
        background: 'transparent',
        color: '#e4e4e7',
        cursor: 'pointer',
        fontWeight: 500,
        fontSize: '12px',
      }

      // 提示文本：优先运行状态，其次抽取完成，其次可抽取，兜底引导。
      var hintText
      if (running) {
        hintText = '抽取中…点击下方按钮查看运行过程'
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
          { style: titleStyle },
          React.createElement('span', { style: badgeStyle }),
          React.createElement('span', null, 'meX 记忆'),
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
