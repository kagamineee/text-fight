import { useEffect, useState } from 'react'
import AttributePanel from '../components/AttributePanel'
import ChatPanel from '../components/ChatPanel'
import Countdown from '../components/Countdown'
import LedgerPanel from '../components/LedgerPanel'
import PlayerCard from '../components/PlayerCard'
import { send } from '../socket'
import { toast, useGame } from '../store'

const PHASE_LABEL: Record<string, string> = {
  lobby: '房间',
  declare: '发送',
  respond: '回应',
  adjudicate: '裁判',
  settle: '结算',
  over: '结束',
}

const PHASE_HINT: Record<string, string> = {
  declare: '开始头脑风暴，限时一分钟',
  adjudicate: '裁判在算',
  settle: '这一回合过去了',
}

export default function Table() {
  const game = useGame()
  const room = game.room!
  const me = room.players.find((p) => p.player_id === game.session?.player_id)
  const actor = room.players.find((p) => p.player_id === room.current_actor)
  const isMyTurn = room.current_actor === game.session?.player_id
  const isHost = room.host_id === game.session?.player_id

  const [content, setContent] = useState('')
  const [submitted, setSubmitted] = useState(false)

  const phase = room.phase
  const canDeclare = phase === 'declare' && isMyTurn && !!me?.alive

  useEffect(() => {
    setSubmitted(false)
    if (phase === 'declare') setContent('')
  }, [room.turn_no, phase])

  const submitDeclare = () => {
    if (!content.trim()) {
      toast('写点什么', 'warn')
      return
    }
    send({ type: 'declare', content: content.trim() })
    setSubmitted(true)
  }

  return (
    <div className="table">
      <header className="table-head">
        <div className="head-cell">
          <span className="label">回合</span>
          <b className="mono">{room.turn_no}</b>
        </div>
        <div className="head-cell">
          <span className="label">阶段</span>
          <b className={phase === 'settle' ? 'gold' : ''}>{PHASE_LABEL[phase] ?? phase}</b>
        </div>
        <div className="head-cell head-actor">
          <span className="label">行动者</span>
          <b>{actor?.name ?? '—'}</b>
        </div>
        <div className="head-cell">
          <Countdown deadline={room.deadline} />
        </div>
        <div className="head-spacer" />
        <button className="btn btn-tiny" onClick={() => send({ type: 'leave_room' })}>
          离开
        </button>
      </header>

      <div className="table-body">
        <aside className="col-left">
          {room.players.map((p) => (
            <PlayerCard key={p.player_id} player={p} isActor={p.player_id === room.current_actor} />
          ))}
          <AttributePanel />
        </aside>

        <main className="col-center">
          <div className="phase-banner">
            <span className="phase-name">{PHASE_LABEL[phase] ?? phase}</span>
            <span className="phase-hint">{PHASE_HINT[phase] ?? ''}</span>
          </div>

          {game.declaration ? (
            <div className="declaration-card">
              <div className="decl-head">
                <span className="decl-actor">{game.declaration.actor_name}</span>
              </div>
              <p className="decl-text">{game.declaration.content}</p>
            </div>
          ) : (
            <div className="declaration-card empty">
              {phase === 'declare' && actor ? `${actor.name} 正在构思…` : '这一回合还没人说话。'}
            </div>
          )}

          {game.adjudication && (
            <div className="verdict-card">
              <div className="verdict-head">
                <span>裁判</span>
              </div>
              <p className="verdict-narrative">{game.adjudication.narrative}</p>
              {game.adjudication.effects.length > 0 && (
                <div className="verdict-effects">
                  {game.adjudication.effects.map((e) => {
                    const target = room.players.find((p) => p.player_id === e.player_id)
                    return (
                      <div key={e.player_id} className="effect-row">
                        <span className="effect-name">{target?.name ?? '未知'}</span>
                        <span className={`effect-delta ${e.delta < 0 ? 'down' : 'up'}`}>
                          {e.delta > 0 ? '+' : ''}
                          {e.delta}
                        </span>
                        <span className="effect-hp mono">HP {e.hp}</span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          <div className="action-area">
            {room.over ? (
              <div className="over-box">
                <h2>{game.result?.winner_name ?? '全倒了'}</h2>
                <p className="hint">
                  {game.result?.winner_name ? '最后还站着的人。' : '没人站着。'}
                </p>
                <div className="ranking">
                  {(game.result?.ranking ?? []).map((r, i) => (
                    <div key={r.player_id} className="rank-row">
                      <span className="rank-no">{i + 1}</span>
                      <span className="rank-name">{r.name}</span>
                      <span className="mono">{r.alive ? `HP ${r.hp}` : '出局'}</span>
                      <span className="mono dim">{r.survived_turns} 回合</span>
                    </div>
                  ))}
                </div>
                {isHost ? (
                  <button
                    className="btn btn-primary settlement-btn"
                    onClick={() => send({ type: 'return_lobby' })}
                  >
                    回到房间
                  </button>
                ) : (
                  <p className="hint settlement-wait">等房主回到房间。</p>
                )}
              </div>
            ) : submitted ? (
              <div className="waiting">
                <span className="pulse-dot" />
                已发送，等裁判…
              </div>
            ) : canDeclare ? (
              <div className="declare-box">
                <textarea
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                      e.preventDefault()
                      submitDeclare()
                    }
                  }}
                  maxLength={800}
                  rows={5}
                />
                <div className="declare-foot">
                  <button className="btn btn-primary" onClick={submitDeclare}>
                    发送
                  </button>
                </div>
              </div>
            ) : (
              <div className="waiting">
                <span className="pulse-dot" />
                {actor ? `等 ${actor.name}…` : '等下一回合…'}
              </div>
            )}
          </div>
        </main>

        <aside className="col-right">
          <LedgerPanel />
          <ChatPanel />
        </aside>
      </div>
    </div>
  )
}
