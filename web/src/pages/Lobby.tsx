import { send } from '../socket'
import { toast, useGame } from '../store'

export default function Lobby() {
  const game = useGame()
  const room = game.room!
  const me = game.session?.player_id
  const isHost = me === room.host_id
  const mePlayer = room.players.find((p) => p.player_id === me)
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
      </div>

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
                  {p.kind === 'bot' && <span className="badge badge-bot">AI</span>}
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
            <div className="lobby-actions">
              <button className="btn btn-ghost" onClick={() => send({ type: 'add_bot' })}>
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

      <button className="btn btn-ghost lobby-leave" onClick={() => send({ type: 'leave_room' })}>
        离开房间
      </button>
    </div>
  )
}
