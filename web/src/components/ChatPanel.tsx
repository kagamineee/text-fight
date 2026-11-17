import { useEffect, useRef, useState } from 'react'
import { send } from '../socket'
import { useGame } from '../store'

export default function ChatPanel() {
  const game = useGame()
  const [text, setText] = useState('')
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [game.chats])

  const submit = () => {
    const value = text.trim()
    if (!value) return
    send({ type: 'chat', text: value, channel: 'table' })
    setText('')
  }

  return (
    <div className="panel chat-panel">
      <div className="panel-head">
        <span>桌面频道</span>
      </div>
      <div className="chat-list" ref={listRef}>
        {game.chats.map((c) => (
          <div key={c.id} className={`chat-line ${c.own ? 'own' : ''}`}>
            <span className="chat-name">{c.name}</span>
            <span className="chat-text">{c.text}</span>
          </div>
        ))}
        {game.chats.length === 0 && <div className="empty">还没有人说话呢</div>}
      </div>
      <div className="chat-input">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) submit()
          }}
          placeholder="对全桌说话…"
          maxLength={500}
        />
        <button className="btn btn-ghost" onClick={submit}>
          发送
        </button>
      </div>
    </div>
  )
}
