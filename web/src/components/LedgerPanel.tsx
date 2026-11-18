import { useEffect, useMemo, useRef, useState } from 'react'
import { useGame } from '../store'

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'declaration', label: '发送' },
  { key: 'adjudication', label: '裁定' },
  { key: 'system', label: '系统' },
]

const SEAT_COLORS = [
  '#e0b86a',
  '#7fc4a3',
  '#9db4e8',
  '#d99a9a',
  '#c9a0dc',
  '#8ec9c0',
  '#e8a06a',
  '#a8c98b',
]

const TYPE_LABEL: Record<string, string> = {
  adjudication: '裁定',
  system: '系统',
  attribute_open: '属性',
  disaster: '天灾',
  inheritance: '继承',
}

export default function LedgerPanel() {
  const game = useGame()
  const [filter, setFilter] = useState('all')
  const listRef = useRef<HTMLDivElement>(null)
  // 用户回看历史时不跟随新消息；只有本来就贴着底部时才自动滚到最新
  const stickRef = useRef(true)

  const entries = useMemo(() => {
    const list = filter === 'all' ? game.ledger : game.ledger.filter((e) => e.type === filter)
    return list.slice(-120)
  }, [game.ledger, filter])

  // 主动切换筛选 = 想看最新一类消息，恢复跟随
  useEffect(() => {
    stickRef.current = true
  }, [filter])

  useEffect(() => {
    const el = listRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [entries])

  const onScroll = () => {
    const el = listRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }

  const playerOf = (id: string | null) => {
    if (!id) return null
    return game.room?.players.find((p) => p.player_id === id) ?? null
  }

  const colorOf = (id: string | null) => {
    const player = playerOf(id)
    if (!player) return undefined
    return SEAT_COLORS[player.seat % SEAT_COLORS.length]
  }

  return (
    <div className="panel ledger-panel">
      <div className="panel-head">
        <span>对局情况</span>
        <span className="panel-hint">{game.ledger.length} 条</span>
      </div>
      <div className="ledger-filters">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            className={`chip-btn ${filter === f.key ? 'active' : ''}`}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </button>
        ))}
      </div>
      <div className="ledger-list" ref={listRef} onScroll={onScroll}>
        {entries.map((e) => {
          const speaker = e.type === 'declaration' ? playerOf(e.actor) : null
          const adjudicator = e.type === 'adjudication' ? playerOf(e.actor) : null
          const color = speaker ? colorOf(speaker.player_id) : undefined
          const tag = TYPE_LABEL[e.type]
          return (
            <div
              key={e.seq}
              className={`ledger-item type-${e.type}${e.type === 'system' ? ' is-system' : ''}`}
              style={speaker && color ? { borderLeftColor: color } : undefined}
            >
              <div className="ledger-meta">
                {speaker && (
                  <span className="ledger-name" style={{ color }}>
                    {speaker.name}
                  </span>
                )}
                {adjudicator && (
                  <span className="ledger-tag">
                    裁定 · {adjudicator.name}
                  </span>
                )}
                {tag && !speaker && !adjudicator && <span className="ledger-tag">{tag}</span>}
                <span className="ledger-num">
                  #{e.seq} · 第 {e.turn_no} 回合
                </span>
              </div>
              <div className="ledger-content">{e.content}</div>
            </div>
          )
        })}
        {entries.length === 0 && <div className="empty">暂无记录</div>}
      </div>
    </div>
  )
}
