import { useEffect, useRef, useState } from 'react'
import AgentStatus from '../components/AgentStatus'
import ChatPanel from '../components/ChatPanel'
import Countdown from '../components/Countdown'
import LedgerPanel from '../components/LedgerPanel'
import PlayerCard from '../components/PlayerCard'
import {
  isAgentReady,
  streamDraft,
  useAgentHealth,
  useAgentSettings,
} from '../agent'
import { leaveRoom, send } from '../socket'
import { toast, useGame } from '../store'

const PHASE_LABEL: Record<string, string> = {
  lobby: '房间',
  declare: '发送',
  adjudicate: '裁判',
  settle: '结算',
  over: '结束',
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
  const [drafting, setDrafting] = useState(false)
  const [pendingDraft, setPendingDraft] = useState('')
  const agentSettings = useAgentSettings()
  const agentHealth = useAgentHealth()
  const pendingRef = useRef('')
  const draftAbort = useRef<AbortController | null>(null)
  const autoDraftTurn = useRef(0)
  const canDeclareRef = useRef(false)

  const phase = room.phase
  const canDeclare = phase === 'declare' && isMyTurn && !!me?.alive
  canDeclareRef.current = canDeclare

  useEffect(() => {
    setSubmitted(false)
  }, [room.turn_no])

  useEffect(() => {
    if (!canDeclare) return
    if (pendingRef.current) {
      setContent(pendingRef.current)
      pendingRef.current = ''
      setPendingDraft('')
      autoDraftTurn.current = room.turn_no
      return
    }
    setContent('')
    if (
      agentSettings.autoDraft &&
      isAgentReady(agentHealth) &&
      autoDraftTurn.current !== room.turn_no
    ) {
      autoDraftTurn.current = room.turn_no
      void runDraft(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canDeclare, room.turn_no])

  useEffect(() => {
    if (!canDeclare) draftAbort.current?.abort()
  }, [canDeclare])

  const runDraft = async (preparing: boolean) => {
    if (drafting) return
    if (!isAgentReady(agentHealth)) {
      toast('AI 还没就绪：重新运行 run.bat 启动代理，再点右上角填模型。', 'warn')
      return
    }
    draftAbort.current?.abort()
    const ctrl = new AbortController()
    draftAbort.current = ctrl
    setDrafting(true)
    let text = ''
    try {
      await streamDraft({
        preparing,
        signal: ctrl.signal,
        onText: (next) => {
          text = next
          if (preparing && !canDeclareRef.current) {
            pendingRef.current = next
            setPendingDraft(next)
          } else {
            setContent(next)
          }
        },
      })
      if (preparing && text.trim() && !canDeclareRef.current) {
        toast('下一手草稿已备好', 'good')
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') return
      toast(`起草失败：${(err as Error).message}`, 'warn')
    } finally {
      setDrafting(false)
    }
  }

  const submitDeclare = () => {
    if (!content.trim()) {
      toast('写点什么', 'warn')
      return
    }
    draftAbort.current?.abort()
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
        <AgentStatus />
        <button className="btn btn-tiny" onClick={leaveRoom}>
          离开
        </button>
      </header>

      <div className="table-body">
        <aside className="col-left">
          {room.players.map((p) => (
            <PlayerCard key={p.player_id} player={p} isActor={p.player_id === room.current_actor} />
          ))}
        </aside>

        <main className="col-center">
          <LedgerPanel />

          <div className="action-area">
            {room.over ? (
              <div className="over-box">
                <h2>{game.result?.winner_name ?? '平局'}</h2>
                <p className="hint">
                  {game.result?.winner_name ? '最终赢家！' : '所有玩家全部死亡'}
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
                <div className="draft-bar">
                  <button
                    className="btn btn-tiny"
                    onClick={() => void runDraft(false)}
                    disabled={drafting}
                  >
                    {drafting ? '起草中…' : content ? '重新起草' : '让 AI 起草'}
                  </button>
                </div>
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
                {phase === 'declare' && me?.alive && (
                  <button
                    className="btn btn-tiny waiting-draft"
                    onClick={() => void runDraft(true)}
                    disabled={drafting}
                  >
                    {drafting ? '起草中…' : pendingDraft ? '重新起草下一手' : '提前起草'}
                  </button>
                )}
                {pendingDraft && (
                  <span className="draft-ready">下一手草稿已备好</span>
                )}
              </div>
            )}
          </div>
        </main>

        <aside className="col-right">
          <ChatPanel />
        </aside>
      </div>
    </div>
  )
}
