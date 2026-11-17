import { useState } from 'react'
import AgentStatus from '../components/AgentStatus'
import { ensure, send } from '../socket'
import { toast } from '../store'

export default function Home() {
  const [name, setName] = useState(() => localStorage.getItem('yap.name') || '')
  const [mode, setMode] = useState<'join' | 'create'>('join')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState(
    () => new URLSearchParams(location.search).get('code')?.toUpperCase() || '',
  )

  const submit = () => {
    const nick = name.trim()
    if (!nick) {
      toast('先给自己起个名字', 'warn')
      return
    }
    if (!password.trim()) {
      toast('请输入房间口令', 'warn')
      return
    }
    if (mode === 'create' && password.length < 4) {
      toast('房间口令至少 4 位', 'warn')
      return
    }
    if (mode === 'join' && code.trim().length < 4) {
      toast('请输入 6 位房号', 'warn')
      return
    }
    ensure(nick, () => {
      if (mode === 'create') {
        send({ type: 'create_room', password, name: nick.trim() })
      } else {
        send({ type: 'join_room', code: code.trim(), password, name: nick.trim() })
      }
    })
  }

  return (
    <div className="home">
      <div className="home-hero">
        <h1>文字角斗场</h1>
        <p className="subtitle">来一场想象力的对局！</p>
      </div>

      <div className="home-card">
        <div className="tabs">
          <button
            className={`tab ${mode === 'join' ? 'active' : ''}`}
            onClick={() => setMode('join')}
          >
            加入房间
          </button>
          <button
            className={`tab ${mode === 'create' ? 'active' : ''}`}
            onClick={() => setMode('create')}
          >
            创建房间
          </button>
        </div>

        <label className="field">
          <span>你的名字</span>
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={16} />
        </label>

        {mode === 'join' && (
          <label className="field">
            <span>房号</span>
            <input
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              maxLength={8}
              className="mono"
            />
          </label>
        )}

        <label className="field">
          <span>房间口令</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit()
            }}
          />
        </label>

        <button className="btn btn-primary btn-block" onClick={submit}>
          {mode === 'create' ? '创建房间' : '进房间'}
        </button>
      </div>

      <div className="home-agent">
        <AgentStatus />
      </div>
    </div>
  )
}
