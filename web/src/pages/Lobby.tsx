import { useState } from 'react'
import AgentStatus from '../components/AgentStatus'
import { isAgentReady, useAgentHealth } from '../agent'
import { leaveRoom, send } from '../socket'
import { toast, useGame } from '../store'
import type { BotDifficulty } from '../types'

const DIFFICULTIES: { key: BotDifficulty; label: string; hint: string }[] = [
  { key: 'easy', label: '简单', hint: '会乱打，也会犹豫' },
  { key: 'normal', label: '普通', hint: '挑残血，受伤会自愈' },
  { key: 'hard', label: '困难', hint: '高伤，会武装、强化属性' },
  { key: 'inferno', label: '炼狱', hint: '追斩杀、拆属性，最冷静' },
]

const DIFF_LABEL: Record<BotDifficulty, string> = {
  easy: '简单',
  normal: '普通',
  hard: '困难',
  inferno: '炼狱',
}

export default function Lobby() {
  const game = useGame()
  const room = game.room!
  const me = game.session?.player_id
  const isHost = me === room.host_id
  const mePlayer = room.players.find((p) => p.player_id === me)
  const [botDifficulty, setBotDifficulty] = useState<BotDifficulty>('normal')
  const agentHealth = useAgentHealth()
  const allReady =
    room.players.length >= 2 &&
    room.players.every((p) => p.kind === 'bot' || p.ready)
  const waiting = room.players.filter((p) => p.kind === 'human' && !p.ready)

  const copyInvite = async () => {
    const link = `${location.origin}/?code=${room.code}`
    try {
      await navigator.clipboard.writeText(link)
      toast('链接已复制', 'good')
    } catch {
      toast(`房号：${room.code}`, 'info')
    }
  }

  return (
    <div className="lobby">
      <div className="lobby-code">
        <span className="label">房号</span>
        <span className="code-value mono">{room.code}</span>
        <button className="btn btn-ghost" onClick={copyInvite}>
          复制邀请
        </button>
        <AgentStatus />
      </div>

      {isHost && agentHealth && !isAgentReady(agentHealth) && (
        <p className="hint host-agent-warn">
          房主的裁判 AI 还没就绪：对局仍可开始，但会一直用模板裁决。重新运行
          <code> run.bat</code>，再点右上角填好模型服务。
        </p>
      )}

      <div className="lobby-grid">
        <div className="panel">
          <div className="panel-head">
            <span>参与者</span>
            <span className="panel-hint">{room.players.length}/8</span>
          </div>
          <div className="lobby-players">
            {room.players.map((p) => (
              <div key={p.player_id} className="lobby-player">
                <span className="lobby-seat">{p.seat + 1}</span>
                <span className="lobby-name">
                  {p.name}
                  {p.player_id === room.host_id && <span className="badge badge-host">房主</span>}
                  {p.kind === 'bot' && (
                    <span className={`badge badge-bot diff-${p.bot_difficulty ?? 'normal'}`}>
                      AI·{DIFF_LABEL[p.bot_difficulty ?? 'normal']}
                    </span>
                  )}
                  {p.player_id === me && <span className="badge badge-me">你</span>}
                </span>
                <span className={`ready-state ${p.kind === 'bot' || p.ready ? 'on' : ''}`}>
                  {p.kind === 'bot' ? '就绪' : p.ready ? '已准备' : '未准备'}
                </span>
                <span className={`dot ${p.kind === 'bot' || p.ready ? 'on' : 'off'}`} />
                {isHost && p.player_id !== me && (
                  <button
                    className="btn btn-tiny"
                    onClick={() =>
                      send(
                        p.kind === 'bot'
                          ? { type: 'remove_bot', bot_id: p.player_id }
                          : { type: 'kick', player_id: p.player_id },
                      )
                    }
                  >
                    移出
                  </button>
                )}
              </div>
            ))}
          </div>

          {mePlayer && (
            <button
              className={`btn btn-block ${mePlayer.ready ? 'btn-ghost' : 'btn-primary'} ready-btn`}
              onClick={() => send({ type: 'set_ready', ready: !mePlayer.ready })}
            >
              {mePlayer.ready ? '取消准备' : '准备'}
            </button>
          )}

          {isHost && (
            <div className="bot-picker">
              <span className="bot-picker-label">假人难度</span>
              <div className="difficulty-picker">
                {DIFFICULTIES.map((d) => (
                  <button
                    key={d.key}
                    type="button"
                    title={d.hint}
                    className={`chip-btn ${botDifficulty === d.key ? 'active' : ''}`}
                    onClick={() => setBotDifficulty(d.key)}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {isHost && (
            <div className="lobby-actions">
              <button
                className="btn btn-ghost"
                onClick={() => send({ type: 'add_bot', difficulty: botDifficulty })}
              >
                + 添加假人
              </button>
              <button
                className="btn btn-primary"
                disabled={!allReady}
                onClick={() => send({ type: 'start_game' })}
              >
                开始对局
              </button>
            </div>
          )}
          {isHost && !allReady && (
            <p className="hint">
              {waiting.length > 0
                ? `尚未准备的玩家：${waiting.map((p) => p.name).join('、')}`
                : '至少 2 个人才能开局。'}
            </p>
          )}
          {!isHost && (
            <p className="hint">
              {allReady ? '人都准备好了，等房主开局。' : '准备之后等房主开局。'}
            </p>
          )}
        </div>

        <div className="panel">
          <div className="panel-head">
            <span>扩展包</span>
          </div>
          <div className="pack-info">
            <h3>{room.pack.name}</h3>
            <p className="hint">{room.pack.description}</p>
            <ul className="pack-stats">
              <li>
                初始 HP <b>{room.pack.hp.base}</b>
              </li>
              <li>
                每回合回复 <b>{room.pack.hp.regen}</b>
              </li>
              <li>
                强度曲线 <b>{room.pack.intensity.cap_base}</b> +{' '}
                <b>{room.pack.intensity.cap_growth}</b> × 回合
              </li>
              <li>
                属性上限 <b>{room.pack.attributes.max_count}</b> 个
              </li>
              <li>
                发送时限 <b>{room.pack.timers.declare_s}s</b>
              </li>
            </ul>
          </div>
        </div>
      </div>

      <button className="btn btn-ghost lobby-leave" onClick={leaveRoom}>
        离开房间
      </button>
    </div>
  )
}
