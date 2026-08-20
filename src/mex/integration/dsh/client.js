// meX × DeepSeek Harness 插件 · Client half（浏览器 UI 面板）。
//
// 打包产物格式（window.__ModuleLoader__.load），依赖从 dsh 的模块表解析：
// - react：staticModules 静态提供；
// - slots：通过 inject 声明，运行时由 dsh-client-runtime 提供。
//
// 面板注册到 shell.overlay（全屏浮动层，additive），默认停靠在页面右下角；
// 每轮对话结束后面板提示"可加入记忆"，点击"Add to MeX"通过 HTTP 接口
// 通知 Host 触发一轮新的抽取 agent 对话（见 index.js）。
window.__ModuleLoader__.load({
  id: 'mex-dsh-plugin',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    var React = require('react')

    // 面板需要 slots 服务（注册 UI）。
    var inject = ['slots']

    // 轮询间隔（毫秒）。
    var POLL_MS = 3000

    /** 解析面板状态响应：{ hasNew, lastTurn, extractedSeq, error }。 */
    function parseState(data) {
      if (data === null || typeof data !== 'object') return null
      return data
    }

    /**
     * meX 面板：右下角浮动卡片。
     * 从 shell.overlay 的标准 props 取当前 session（useSessions），
     * 轮询 Host 的面板状态接口，显示"可加入记忆"提示与触发按钮。
     */
    function MexPanel(props) {
      var useSessions = props.useSessions
      var sessionId = useSessions(function (s) { return s.current })

      var state = React.useState(null)
      var panel = state[0]
      var setPanel = state[1]

      var busyState = React.useState(false)
      var busy = busyState[0]
      var setBusy = busyState[1]

      var resultState = React.useState(null)
      var result = resultState[0]
      var setResult = resultState[1]

      // 轮询面板状态：session 切换或每 POLL_MS 刷新一次。
      React.useEffect(function () {
        var alive = true
        var timer = null
        var tick = function () {
          if (!sessionId) return
          fetch('/mex/panel-state?sessionId=' + encodeURIComponent(sessionId))
            .then(function (res) { return res.json() })
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

      /** 点击 Add to MeX：通知 Host 触发抽取 agent 对话。 */
      var onAddToMex = function () {
        if (!sessionId || busy) return
        setBusy(true)
        setResult(null)
        fetch('/mex/extract', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ sessionId: sessionId }),
        })
          .then(function (res) { return res.json() })
          .then(function (data) { setResult(data) })
          .catch(function (err) {
            setResult({ ok: false, error: String(err) })
          })
          .finally(function () { setBusy(false) })
      }

      var hasNew = panel !== null && panel.ok === true && panel.hasNew === true
      var canRun = sessionId !== undefined && !busy

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
        background: hasNew ? '#4ade80' : '#52525b',
      }

      var hintStyle = {
        color: hasNew ? '#bbf7d0' : '#a1a1aa',
        lineHeight: '18px',
      }

      var buttonStyle = {
        width: '100%',
        padding: '8px 10px',
        border: 'none',
        borderRadius: '8px',
        background: hasNew ? '#16a34a' : '#3f3f46',
        color: hasNew ? '#ffffff' : '#a1a1aa',
        cursor: canRun ? 'pointer' : 'not-allowed',
        fontWeight: 600,
        fontSize: '13px',
      }

      var resultText = ''
      if (result !== null) {
        if (result.ok) {
          resultText = '已写入 ' + (result.written || 0) + ' 条记忆'
          if (result.notes) resultText += '（' + result.notes + '）'
        } else {
          resultText = '抽取失败：' + (result.error || '未知错误')
        }
      }

      return React.createElement(
        'div',
        { style: cardStyle, 'data-plugin': 'mex-dsh-plugin' },
        React.createElement(
          'div',
          { style: titleStyle },
          React.createElement('span', { style: badgeStyle }),
          React.createElement('span', null, 'meX 记忆'),
        ),
        React.createElement(
          'div',
          { style: hintStyle },
          hasNew ? '本轮对话已结束，可加入记忆' : busy ? '正在抽取记忆…' : result !== null ? resultText : '对话结束后点击下方按钮抽取记忆',
        ),
        React.createElement(
          'button',
          { style: buttonStyle, onClick: onAddToMex, disabled: !canRun },
          busy ? '抽取中…' : 'Add to MeX',
        ),
      )
    }

    /**
     * Client 插件入口：注册 shell.overlay 面板。
     * @param ctx - client 根上下文（注入 slots）。
     */
    function apply(ctx) {
      ctx.slots.inject('shell.overlay', function () {
        return ctx.slots.register(
          { name: 'shell.overlay', id: 'mex-panel', order: 9999, label: 'meX 记忆' },
          MexPanel,
        )
      })
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
