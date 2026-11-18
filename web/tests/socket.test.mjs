import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

// 执行真实 store/socket 源码；仅替换浏览器和 React 的外部依赖。
function harness(token) {
  const saved = new Map(token ? [['yap.token', token]] : [])
  const timers = new Map()
  let timerId = 0
  class Socket {
    static OPEN = 1
    static CLOSED = 3
    static sockets = []
    readyState = 0
    sent = []
    constructor() { Socket.sockets.push(this) }
    send(raw) { this.sent.push(JSON.parse(raw)) }
    close() { this.readyState = 3; this.onclose?.() }
    open() { this.readyState = 1; this.onopen?.() }
    message(data) { this.onmessage?.({ data: JSON.stringify(data) }) }
  }
  const context = vm.createContext({
    console, Date, Set, WebSocket: Socket,
    localStorage: { getItem: k => saved.get(k) ?? null, setItem: (k, v) => saved.set(k, v), removeItem: k => saved.delete(k) },
    location: { protocol: 'http:', host: 'localhost:8000' },
    window: {
      setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id },
      clearTimeout(id) { timers.delete(id) },
    },
  })
  function load(name, require) {
    const source = readFileSync(new URL(`../src/${name}.ts`, import.meta.url), 'utf8')
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const module = { exports: {} }
    vm.runInContext(`(function(require, module, exports) {${compiled}\n})`, context)(require, module, module.exports)
    return module.exports
  }
  const store = load('store', () => ({ useSyncExternalStore() {} }))
  const api = load('socket', name => {
    assert.equal(name, './store')
    return store
  })
  function tick(delay) {
    const ready = [...timers.entries()].filter(([, timer]) => timer.delay === delay)
    for (const [id, timer] of ready) { timers.delete(id); timer.fn() }
  }
  return { api, store, saved, Socket, tick, timers }
}

const session = { type: 'session', token: 'token', player_id: 'p1', name: '测试' }

test('create waits for authenticated session, including during reconnect', () => {
  const h = harness()
  h.api.ensure('测试', () => h.api.send({ type: 'create_room' }))
  const socket = h.Socket.sockets[0]
  socket.open()
  assert.equal(socket.sent.length, 1)
  assert.equal(socket.sent[0].type, 'hello')
  socket.message(session)
  assert.equal(socket.sent[1].type, 'create_room')
  socket.close()
  h.tick(400)
  const replacement = h.Socket.sockets[1]
  replacement.open()
  h.api.ensure('测试', () => h.api.send({ type: 'join_room' }))
  assert.equal(replacement.sent.length, 1)
  replacement.message(session)
  assert.equal(replacement.sent[1].type, 'join_room')
})

test('leave clears room state while retaining a usable authenticated connection', () => {
  const h = harness()
  h.api.connectWs('测试')
  const socket = h.Socket.sockets[0]
  socket.open(); socket.message(session)
  h.store.setState({ room: { room_id: 'old' }, result: { winner_name: '旧结果' } })
  h.api.leaveRoom()
  assert.equal(h.store.getState().room, null)
  assert.equal(h.store.getState().result, null)
  h.api.ensure('测试', () => h.api.send({ type: 'create_room' }))
  assert.equal(socket.sent.at(-1).type, 'create_room')
  assert.equal(h.Socket.sockets.length, 1)
})

test('kick clears prior room data and allows a fresh room action', () => {
  const h = harness()
  h.api.connectWs('测试')
  const socket = h.Socket.sockets[0]
  socket.open(); socket.message(session)
  h.store.setState({ room: { room_id: 'old' }, result: { winner_name: '旧结果' }, chats: [{}], adjudication: {} })
  socket.message({ type: 'kicked', reason: '已移出' })
  assert.equal(h.store.getState().room, null)
  assert.equal(h.store.getState().adjudication, null)
  assert.equal(h.store.getState().chats.length, 0)
  h.api.ensure('测试', () => h.api.send({ type: 'create_room' }))
  assert.equal(socket.sent.at(-1).type, 'create_room')
})

test('fatal error clears invalid session and reconnects once', () => {
  const h = harness('expired')
  h.api.connectWs('测试')
  const socket = h.Socket.sockets[0]
  socket.open()
  socket.message({ type: 'error', fatal: true, message: '过期' })
  assert.equal(h.saved.has('yap.token'), false)
  assert.equal(h.store.getState().session, null)
  h.tick(400)
  assert.equal(h.Socket.sockets.length, 2)
  const next = h.Socket.sockets[1]
  next.open()
  assert.equal(next.sent[0].type, 'hello')
  socket.onclose()
  assert.equal(h.store.getState().connected, true)
})

test('game failure reason survives result handling and snapshot restoration', () => {
  const h = harness()
  h.api.connectWs('测试')
  const socket = h.Socket.sockets[0]
  socket.open(); socket.message(session)
  socket.message({ type: 'room_state', room_id: 'r', phase: 'declare' })
  socket.message({ type: 'game_over', winner_name: null, ranking: [], reason: '对局异常' })
  assert.equal(h.store.getState().result.reason, '对局异常')
  socket.message({ type: 'snapshot', state: { room_id: 'r', phase: 'over', result: { reason: '对局异常', ranking: [] } }, ledger: [], last_seq: 3 })
  assert.equal(h.store.getState().result.reason, '对局异常')
})

test('attribute notifications do not maintain an obsolete second attribute cache', () => {
  const h = harness()
  h.api.connectWs('测试')
  const socket = h.Socket.sockets[0]
  socket.open(); socket.message(session)
  socket.message({ type: 'attribute_private', notice: '新属性', attr: { attr_id: 'a', name: '盾' } })
  socket.message({ type: 'attribute_private', notice: '护盾次数耗尽', attr: { attr_id: 'a', name: '盾' } })
  assert.equal('myAttrs' in h.store.getState(), false)
})

test('expired lobby seat clears the stale room when server restores only the session', () => {
  const h = harness('token')
  h.store.setState({ room: { room_id: 'expired' }, ledger: [{ seq: 20 }] })
  h.api.connectWs('测试')
  const socket = h.Socket.sockets[0]
  socket.open()
  socket.message({ ...session, room_id: null })
  assert.equal(h.store.getState().room, null)
  assert.equal(h.store.getState().ledger.length, 0)
  assert.equal(h.store.getState().session.token, 'token')
})

test('event notifications never create popup state or notification timers', () => {
  const h = harness()
  h.api.connectWs('测试')
  const socket = h.Socket.sockets[0]
  socket.open(); socket.message(session)
  const timersBefore = h.timers.size
  for (const type of ['attribute_opened', 'attribute_removed', 'attribute_private', 'inheritance', 'special_event', 'frozen_skip']) {
    socket.message({ type, name: '事件', attr: { name: '效果' }, notice: '新属性' })
  }
  assert.equal('toasts' in h.store.getState(), false)
  assert.equal('toast' in h.store, false)
  assert.equal(h.timers.size, timersBefore)
  socket.message({ type: 'error', message: '口令错误' })
  assert.equal(h.store.getState().error, '口令错误')
})

test('effect execution uses the ledger and the authoritative snapshot', () => {
  const h = harness()
  h.api.connectWs('测试')
  const socket = h.Socket.sockets[0]
  socket.open(); socket.message(session)
  socket.message({ type: 'room_state', room_id: 'r', active_effects: [
    { effect_id: 'effect-1', name: '铁索连环', remaining: 10, description: '剩余10次全局行动' },
  ] })
  socket.message({ type: 'effect_event', ledger_seq: 1, turn_no: 35, narrative: '铁索连环到期' })
  assert.equal(h.store.getState().ledger[0].content, '铁索连环到期')
  assert.equal(h.store.getState().room.active_effects[0].remaining, 10)
  socket.message({ type: 'room_state', room_id: 'r', active_effects: [] })
  assert.equal(h.store.getState().room.active_effects.length, 0)
})
