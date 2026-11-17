import { useGame } from '../store'

export default function AttributePanel() {
  const game = useGame()
  const limit = game.room?.pack.attributes.max_count ?? 3

  return (
    <div className="panel attr-panel">
      <div className="panel-head">
        <span>我的属性</span>
        <span className="panel-hint">
          {game.myAttrs.length}/{limit}
        </span>
      </div>

      <div className="my-attrs">
        {game.myAttrs.length === 0 && <div className="empty">还没有属性</div>}
        {game.myAttrs.map((a) => (
          <div key={a.attr_id} className={`my-attr type-${a.attr_type}`}>
            <div className="my-attr-head">
              <span className="my-attr-name">{a.name}</span>
              <span className="my-attr-value mono">
                {a.value}/{a.cap}
              </span>
            </div>
            <div className="my-attr-meta">
              <span>×{a.exchange_rate}</span>
              <span>{a.narrative || '没写来历'}</span>
            </div>
            <div className="my-attr-bar">
              <div
                className="my-attr-fill"
                style={{ width: `${Math.min(100, (a.value / Math.max(1, a.cap)) * 100)}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
