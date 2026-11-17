import { addFloat, getState, setState, toast } from './store'
import type { LedgerEntry, PrivateAttr } from './types'

let ws: WebSocket | null = null
let nickname = ''
let manualClose = false
let retry = 0
let ledgerSeq = 0
let pendingAction: (() => void) | null = null

export function send(msg: Record<string, unknown>) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg))
  }
}

/** 确保已建立会话后执行动作（创建/加入房间用）。 */
export function ensure(name: string, action: () => void) {
  localStorage.setItem('yap.name', name)
  const state = getState()
  if (state.session && ws && ws.readyState === WebSocket.OPEN) {
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

export function disconnectWs() {
  manualClose = true
  ws?.close()
  ws = null
}

function open() {
  setState({ connecting: true })
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${scheme}//${location.host}/ws`)

  ws.onopen = () => {
    retry = 0
    setState({ connected: true, connecting: false, error: null })
    const token = localStorage.getItem('yap.token')
    if (token) {
      send({ type: 'reconnect', token })
    } else {
      send({ type: 'hello', name: nickname || '玩家' })
    }
  }

  ws.onmessage = (ev) => {
    try {
      handle(JSON.parse(ev.data))
    } catch {
      /* 忽略无法解析的消息 */
    }
  }

  ws.onclose = () => {
    setState({ connected: false, connecting: false })
    if (!manualClose) {
      const delay = Math.min(8000, 400 * 2 ** retry)
      retry += 1
      window.setTimeout(open, delay)
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
        ledger: s.room && s.room.room_id === msg.room_id ? s.ledger : [],
      }))
      return

    case 'snapshot': {
      ledgerSeq = 0
      const attrs: PrivateAttr[] = msg.private?.attributes ?? []
      setState({
        room: msg.state,
        myAttrs: attrs,
        ledger: msg.ledger ?? [],
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
      toast('对局开始', 'good')
      return

    case 'turn_begin': {
      const hp = msg.hp ?? {}
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

    case 'attribute_opened':
      toast(`${msg.player_name ?? '有人'} 多了个属性「${msg.name}」`, 'info')
      return

    case 'attribute_private': {
      const attr: PrivateAttr | undefined = msg.attr
      if (!attr) return
      if (msg.notice === '删掉了') {
        setState((s) => ({ myAttrs: s.myAttrs.filter((a) => a.attr_id !== attr.attr_id) }))
        toast(`「${attr.name}」没了`, 'warn')
        return
      }
      setState((s) => {
        const exists = s.myAttrs.some((a) => a.attr_id === attr.attr_id)
        if (exists) {
          return {
            myAttrs: s.myAttrs.map((a) =>
              a.attr_id === attr.attr_id
                ? { ...a, ...attr, narrative: a.narrative, attr_type: a.attr_type }
                : a,
            ),
          }
        }
        return { myAttrs: [...s.myAttrs, attr] }
      })
      if (msg.notice === '新属性') toast(`「${attr.name}」记上了`, 'good')
      if (msg.notice === '变化') toast(`「${attr.name}」现在是 ${attr.value}`, 'info')
      return
    }

    case 'player_dead':
      toast(`${msg.name} 出局`, 'warn')
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
      toast(msg.winner_name ? `${msg.winner_name} 获胜` : '无人幸存', 'good')
      setState((s) => ({
        room: s.room ? { ...s.room, over: true, phase: 'over' } : s.room,
        result: { winner_name: msg.winner_name ?? null, ranking: msg.ranking ?? [] },
      }))
      return

    case 'lobby_returned':
      ledgerSeq = 0
      setState({
        ledger: [],
        declaration: null,
        adjudication: null,
        myAttrs: [],
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
      toast(msg.reason ?? '你被移出了房间', 'warn')
      setState({ room: null, myAttrs: [], ledger: [], declaration: null })
      return

    case 'error':
      toast(msg.message, 'warn')
      setState({ error: msg.message })
      if (msg.fatal) {
        localStorage.removeItem('yap.token')
        setState({ room: null, myAttrs: [] })
        manualClose = true
        ws?.close()
        window.setTimeout(() => {
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
