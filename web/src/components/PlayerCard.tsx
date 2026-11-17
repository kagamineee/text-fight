import { useGame } from '../store'
import type { PlayerView } from '../types'

function typeLabel(t: string) {
  if (t === 'attack') return '攻'
  if (t === 'defense') return '守'
  return '辅'
}

const DIFF_LABEL: Record<string, string> = {
  easy: '简单',
  normal: '普通',
  hard: '困难',
  inferno: '炼狱',
}

export default function PlayerCard({
  player,
  isActor,
}: {
  player: PlayerView
  isActor: boolean
}) {
  const game = useGame()
  const isHost = game.room?.host_id === player.player_id
  const isMe = game.session?.player_id === player.player_id
  const ratio = Math.max(0, Math.min(1, player.hp / Math.max(1, player.hp_max)))
  const low = ratio <= 0.3
  const floats = game.floats.filter((f) => f.player_id === player.player_id)

  return (
    <div
      className={[
        'player-card',
        isActor ? 'actor' : '',
        !player.alive ? 'dead' : '',
        isMe ? 'me' : '',
      ].join(' ')}
    >
      <div className="player-top">
        <div className="player-name">
          {player.name}
          {isHost && <span className="badge badge-host">房主</span>}
          {player.kind === 'bot' && (
            <span className={`badge badge-bot diff-${player.bot_difficulty ?? 'normal'}`}>
              AI·{DIFF_LABEL[player.bot_difficulty ?? 'normal']}
            </span>
          )}
          {isMe && <span className="badge badge-me">你</span>}
        </div>
        <span className={`dot ${player.connected || player.kind === 'bot' ? 'on' : 'off'}`} />
      </div>

      <div className="hp-row">
        <div className="hp-bar">
          <div
            className={`hp-fill ${low ? 'low' : ''}`}
            style={{ width: `${ratio * 100}%` }}
          />
        </div>
        <div className="hp-text">
          {player.hp}
          <span className="hp-max">/{player.hp_max}</span>
        </div>
      </div>

      {floats.map((f) => (
        <span key={f.id} className={`hp-float ${f.delta < 0 ? 'down' : 'up'}`}>
          {f.delta > 0 ? '+' : ''}
          {f.delta}
        </span>
      ))}

      {player.attributes.length > 0 && (
        <div className="attr-chips">
          {player.attributes.map((a) => {
            const left = a.expires_turn
              ? Math.max(1, a.expires_turn - (game.room?.turn_no ?? 0) + 1)
              : 0
            return (
              <span
                key={a.attr_id}
                className={`attr-chip chip-${a.attr_type}${a.shield ? ' chip-shield' : ''}`}
                title={a.narrative}
              >
                <b>{typeLabel(a.attr_type)}</b>
                {a.name}
                {a.shield ? (
                  <span className="attr-shield">免伤</span>
                ) : (
                  <span className="attr-val">
                    {a.value}/{a.cap}
                  </span>
                )}
                {a.freeze && (
                  <span className="attr-freeze">
                    冻结{a.charges > 0 ? `·剩${a.charges}回合` : ''}
                  </span>
                )}
                {a.per_turn !== 0 && (
                  <span className={`attr-tick ${a.per_turn < 0 ? 'down' : 'up'}`}>
                    每回合{a.per_turn > 0 ? '+' : ''}
                    {a.per_turn}
                  </span>
                )}
                {left > 0 && <span className="attr-left">剩{left}回合</span>}
              </span>
            )
          })}
        </div>
      )}

      {!player.alive && <div className="dead-mark">出局</div>}
    </div>
  )
}
