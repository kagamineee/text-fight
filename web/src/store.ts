import { useSyncExternalStore } from 'react'
import type {
  Adjudication,
  ChatMsg,
  Declaration,
  LedgerEntry,
  RoomState,
} from './types'

export interface HpFloat {
  id: number
  player_id: string
  delta: number
}

export interface RankRow {
  player_id: string
  name: string
  alive: boolean
  hp: number
  survived_turns: number
}

export interface GameResult {
  reason?: string
  winner_name: string | null
  ranking: RankRow[]
}

export interface GameState {
  connected: boolean
  connecting: boolean
  session: { token: string; player_id: string; name: string } | null
  room: RoomState | null
  ledger: LedgerEntry[]
  declaration: Declaration | null
  adjudication: Adjudication | null
  chats: ChatMsg[]
  floats: HpFloat[]
  error: string | null
  offset: number
  result: GameResult | null
}

const initial: GameState = {
  connected: false,
  connecting: false,
  session: null,
  room: null,
  ledger: [],
  declaration: null,
  adjudication: null,
  chats: [],
  floats: [],
  error: null,
  offset: 0,
  result: null,
}

let state: GameState = initial
const listeners = new Set<() => void>()

export function getState(): GameState {
  return state
}

export function setState(patch: Partial<GameState> | ((s: GameState) => Partial<GameState>)) {
  const next = typeof patch === 'function' ? patch(state) : patch
  state = { ...state, ...next }
  listeners.forEach((l) => l())
}

export function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

export function useGame(): GameState {
  return useSyncExternalStore(subscribe, getState)
}

let floatSeq = 1
export function addFloat(player_id: string, delta: number) {
  const id = floatSeq++
  setState((s) => ({ floats: [...s.floats, { id, player_id, delta }] }))
  window.setTimeout(() => {
    setState((s) => ({ floats: s.floats.filter((f) => f.id !== id) }))
  }, 1300)
}

export function resetRoom() {
  setState({
    room: null,
    ledger: [],
    declaration: null,
    adjudication: null,
    chats: [],
    result: null,
  })
}
