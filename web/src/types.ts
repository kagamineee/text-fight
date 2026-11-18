export type Phase = 'lobby' | 'declare' | 'adjudicate' | 'settle' | 'over'

export type BotDifficulty = 'easy' | 'normal' | 'hard' | 'inferno'

export interface PublicAttr {
  attr_id: string
  name: string
  narrative: string
  attr_type: string
  value: number
  cap: number
  expires_turn: number
  shield: boolean
  freeze: boolean
  charges: number
  per_turn: number
  indestructible: boolean
  damage_reduction: number
  shield_charges: number
  lethal_only: boolean
  block_heal_pct: number
}

export interface PrivateAttr extends PublicAttr {
  initial: number
  exchange_rate: number
  opened_turn: number
}

export interface PlayerView {
  player_id: string
  name: string
  seat: number
  kind: 'human' | 'bot'
  bot_difficulty?: BotDifficulty
  hp: number
  hp_max: number
  alive: boolean
  connected: boolean
  ready: boolean
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
  events?: Array<{ id: string; name: string; target: string }>
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
  result?: {
    reason?: string
    winner_name: string | null
    ranking: Array<{
      player_id: string
      name: string
      alive: boolean
      hp: number
      survived_turns: number
    }>
  } | null
  world_summary?: string
  active_effects?: Array<{
    effect_id: string
    name: string
    targets: string[]
    description: string
    remaining: number | null
    remaining_text: string
  }>
  disaster?: { level: number; damage: number }
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
  source?: 'ai' | 'stub' | 'rejected'
  clamped?: boolean
}

export interface ChatMsg {
  id: number
  name: string
  text: string
  channel: string
  own: boolean
}
