import { addFloat, getState, resetRoom, setState } from './store'
import type { LedgerEntry } from './types'

let ws: WebSocket | null = null
let nickname = ''
let manualClose = false
let retry = 0
let ledgerSeq = 0
let pendingAction: (() => void) | null = null
let authenticated = false
let reconnectTimer: number | null = null

export function send(msg: Record<string, unknown>) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg))
  }
}

/** 确保已建立会话后执行动作（创建/加入房间用）。 */
export function ensure(name: string, action: () => void) {
  localStorage.setItem('yap.name', name)
  const state = getState()
  if (authenticated && state.session && ws && ws.readyState === WebSocket.OPEN) {
    action()
    return
  }
  pendingAction = action
  if (!ws || ws.readyState === WebSocket.CLOSED) {
    connectWs(name)
  }
}

export function connectWs(name: string) {
  nickname = name
  manualClose = false
  open()
}

/** 主动离开当前房间：通知服务端，并立刻清空本地房间状态（回到首页）。 */
export function leaveRoom() {
  send({ type: 'leave_room' })
  ledgerSeq = 0
  resetRoom()
}

function open() {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (ws && ws.readyState !== WebSocket.CLOSED) return
  authenticated = false
  setState({ connecting: true })
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(`${scheme}//${location.host}/ws`)
  ws = socket

  socket.onopen = () => {
    if (ws !== socket) return
    retry = 0
    setState({ connected: true, connecting: false, error: null })
    const token = localStorage.getItem('yap.token')
    if (token) {
      send({ type: 'reconnect', token })
    } else {
      send({ type: 'hello', name: nickname || '玩家' })
    }
  }

  socket.onmessage = (ev) => {
    if (ws !== socket) return
    try {
      handle(JSON.parse(ev.data))
    } catch {
      /* 忽略无法解析的消息 */
    }
  }

  socket.onclose = () => {
    if (ws !== socket) return
    authenticated = false
    ws = null
    setState({ connected: false, connecting: false })
    if (!manualClose) {
      const delay = Math.min(8000, 400 * 2 ** retry)
      retry += 1
      reconnectTimer = window.setTimeout(open, delay)
    }
  }
}

function pushLedger(entry: LedgerEntry) {
  if (entry.seq <= ledgerSeq) return
  ledgerSeq = entry.seq
  setState((s) => ({ ledger: [...s.ledger, entry].slice(-300) }))
}

function handle(msg: any) {
  if (msg.server_now) {
    setState({ offset: msg.server_now - Date.now() })
  }

  switch (msg.type) {
    case 'welcome':
    case 'pong':
      return

    case 'session': {
      authenticated = true
      if (msg.room_id === null) {
        ledgerSeq = 0
        resetRoom()
      }
      localStorage.setItem('yap.token', msg.token)
      localStorage.setItem('yap.name', msg.name)
      setState({
        session: { token: msg.token, player_id: msg.player_id, name: msg.name },
      })
      const action = pendingAction
      pendingAction = null
      action?.()
      return
    }

    case 'room_state':
      setState((s) => ({
        room: msg,
        result: msg.result ?? null,
        ledger: s.room && s.room.room_id === msg.room_id ? s.ledger : [],
      }))
      return

    case 'snapshot': {
      ledgerSeq = 0
      setState({
        room: msg.state,
        ledger: msg.ledger ?? [],
        result: msg.state?.result ?? null,
      })
      ledgerSeq = msg.last_seq ?? 0
      return
    }

    case 'game_started':
      setState({
        room: msg.state,
        declaration: null,
        adjudication: null,
        result: null,
      })
      return

    case 'turn_begin': {
      const hp = msg.hp ?? {}
      const prev = getState().room
      // 回血可见化：回合开始的自然回复显示 +N 浮动，不再静默改血量
      if (prev) {
        for (const p of prev.players) {
          const next = hp[p.player_id]
          if (next !== undefined && next > p.hp) addFloat(p.player_id, next - p.hp)
        }
      }
      setState((s) => {
        if (!s.room) return {}
        return {
          room: {
            ...s.room,
            turn_no: msg.turn_no,
            current_actor: msg.current_player,
            players: s.room.players.map((p) =>
              hp[p.player_id] !== undefined ? { ...p, hp: hp[p.player_id] } : p,
            ),
          },
          declaration: null,
        }
      })
      return
    }

    case 'phase_change':
      setState((s) => {
        if (!s.room) return {}
        return {
          room: {
            ...s.room,
            phase: msg.phase,
            deadline: msg.deadline ?? null,
            current_actor: msg.current_actor ?? s.room.current_actor,
          },
        }
      })
      return

    case 'declaration': {
      setState({ declaration: msg })
      pushLedger({
        seq: msg.seq,
        turn_no: msg.turn_no,
        actor: msg.actor,
        type: 'declaration',
        content: msg.content,
        intensity: null,
        status: 'active',
      })
      return
    }

    case 'adjudication':
      setState({ adjudication: msg })
      pushLedger({
        seq: msg.ledger_seq,
        turn_no: getState().room?.turn_no ?? 0,
        actor: null,
        type: 'adjudication',
        content: msg.narrative ?? '',
        intensity: null,
        status: 'active',
      })
      return

    case 'judge_fallback':
      if (msg.ledger_seq) {
        pushLedger({
          seq: msg.ledger_seq,
          turn_no: msg.turn_no ?? getState().room?.turn_no ?? 0,
          actor: null,
          type: 'system',
          content: `裁判降级：${msg.reason}`,
          intensity: null,
          status: 'active',
        })
      }
      return

    case 'world_summary':
      setState((s) =>
        s.room ? { room: { ...s.room, world_summary: msg.text } } : {},
      )
      return

    case 'hp_update': {
      setState((s) => {
        if (!s.room) return {}
        return {
          room: {
            ...s.room,
            players: s.room.players.map((p) =>
              p.player_id === msg.player_id ? { ...p, hp: msg.hp } : p,
            ),
          },
        }
      })
      if (msg.delta) addFloat(msg.player_id, msg.delta)
      return
    }

    case 'effect_event':
      pushLedger({ seq: msg.ledger_seq, turn_no: msg.turn_no, actor: null,
        type: 'system', content: msg.narrative, intensity: null, status: 'active' })
      return

    case 'disaster': {
      setState((s) =>
        s.room
          ? { room: { ...s.room, disaster: { level: msg.level, damage: msg.damage } } }
          : {},
      )
      return
    }

    case 'player_dead':
      setState((s) => {
        if (!s.room) return {}
        return {
          room: {
            ...s.room,
            players: s.room.players.map((p) =>
              p.player_id === msg.player_id ? { ...p, alive: false, hp: 0 } : p,
            ),
          },
        }
      })
      return

    case 'game_over':
      setState((s) => ({
        room: s.room ? { ...s.room, over: true, phase: 'over' } : s.room,
        result: { winner_name: msg.winner_name ?? null, ranking: msg.ranking ?? [], reason: msg.reason },
      }))
      return

    case 'lobby_returned':
      ledgerSeq = 0
      setState({
        ledger: [],
        declaration: null,
        adjudication: null,
        chats: [],
        result: null,
      })
      return

    case 'chat':
      setState((s) => ({
        chats: [
          ...s.chats,
          {
            id: msg.seq ?? Date.now(),
            name: msg.name,
            text: msg.text,
            channel: msg.channel,
            own: msg.actor === getState().session?.player_id,
          },
        ].slice(-200),
      }))
      return

    case 'kicked':
      ledgerSeq = 0
      resetRoom()
      setState({ error: msg.reason ?? '你被移出了房间' })
      return

    case 'error':
      setState({ error: msg.message })
      if (msg.fatal) {
        localStorage.removeItem('yap.token')
        ledgerSeq = 0
        resetRoom()
        setState({ session: null })
        manualClose = true
        authenticated = false
        ws?.close()
        reconnectTimer = window.setTimeout(() => {
          manualClose = false
          retry = 0
          open()
        }, 400)
      }
      return

    default:
      return
  }
}
