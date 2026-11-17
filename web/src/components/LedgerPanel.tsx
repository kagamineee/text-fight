import { useEffect, useMemo, useRef, useState } from 'react'
import { useGame } from '../store'

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'declaration', label: '发送' },
  { key: 'adjudication', label: '裁定' },
  { key: 'system', label: '系统' },
]

export default function LedgerPanel() {
  const game = useGame()
  const [filter, setFilter] = useState('all')
  const listRef = useRef<HTMLDivElement>(null)

  const entries = useMemo(() => {
    const list = filter === 'all' ? game.ledger : game.ledger.filter((e) => e.type === filter)
    return list.slice(-120)
  }, [game.ledger, filter])

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [entries])

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
      <div className="ledger-list" ref={listRef}>
        {entries.map((e) => (
          <div key={e.seq} className={`ledger-item type-${e.type}`}>
            <div className="ledger-meta">
              <span>#{e.seq}</span>
              <span>第 {e.turn_no} 回合</span>
            </div>
            <div className="ledger-content">{e.content}</div>
          </div>
        ))}
        {entries.length === 0 && <div className="empty">暂无记录</div>}
      </div>
    </div>
  )
}
