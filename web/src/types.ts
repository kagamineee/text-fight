export type Phase = 'lobby' | 'declare' | 'adjudicate' | 'settle' | 'over'

export interface PublicAttr {
  attr_id: string
  name: string
  narrative: string
  attr_type: string
}

export interface PrivateAttr extends PublicAttr {
  value: number
  cap: number
  initial: number
  exchange_rate: number
  consume_rule: string
  opened_turn: number
}

export interface PlayerView {
  player_id: string
  name: string
  seat: number
  kind: 'human' | 'bot'
  hp: number
  hp_max: number
  alive: boolean
  connected: boolean
  ready: boolean
  appeals_left: number
  attributes: PublicAttr[]
}

export interface PackView {
  id: string
  name: string
  description: string
  hp: { base: number; regen: number }
  intensity: { cap_base: number; cap_growth: number; level_words: Record<string, number> }
  attributes: { max_count: number; max_initial: number; max_cap: number; max_exchange_rate: number }
  timers: { declare_s: number; judge_s: number }
}

export interface RoomState {
  room_id: string
  code: string
  phase: Phase
  host_id: string
  pack: PackView
  players: PlayerView[]
  turn_no: number
  current_actor: string | null
  deadline: number | null
  over: boolean
}

export interface LedgerEntry {
  seq: number
  turn_no: number
  actor: string | null
  type: string
  content: string
  intensity: number | null
  status: string
}

export interface Declaration {
  actor: string
  actor_name: string
  content: string
}

export interface Adjudication {
  ledger_seq: number
  narrative: string
  effects: { player_id: string; delta: number; hp: number }[]
}

export interface ChatMsg {
  id: number
  name: string
  text: string
  channel: string
  own: boolean
}

export interface Toast {
  id: number
  text: string
  kind: 'info' | 'warn' | 'good'
}
