import { useEffect, useRef, useState } from 'react'
import AgentStatus from '../components/AgentStatus'
import DevPanel from '../components/DevPanel'
import { ensure, send } from '../socket'
import { setState } from '../store'

interface PackOption {
  id: string
  name: string
  description: string
}

export default function Home() {
  const [name, setName] = useState(() => localStorage.getItem('yap.name') || '')
  const [mode, setMode] = useState<'join' | 'create'>('join')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState(
    () => new URLSearchParams(location.search).get('code')?.toUpperCase() || '',
  )
  const [packs, setPacks] = useState<PackOption[]>([])
  const [packId, setPackId] = useState('')
  const [packIssues, setPackIssues] = useState<string[]>([])
  const [devOpen, setDevOpen] = useState(false)
  const [packRevision, setPackRevision] = useState(0)
  const [importing, setImporting] = useState(false)
  const [importErrors, setImportErrors] = useState<string[]>([])
  const fileInput = useRef<HTMLInputElement>(null)
  const isLocal = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)

  useEffect(() => {
    let alive = true
    fetch('/api/packs')
      .then((r) => r.json())
      .then((data) => {
        if (!alive) return
        const list: PackOption[] = data.packs || []
        setPacks(list)
        setPackId((prev) => prev || data.default || list[0]?.id || '')
        setPackIssues((data.issues || []).map((item: { message: string }) => item.message))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [packRevision])

  const importPack = async (file: File) => {
    setImportErrors([])
    if (!/\.ya?ml$/i.test(file.name) || file.size > 200 * 1024) {
      setImportErrors(['请选择不超过 200 KB 的 .yaml 或 .yml 文件。'])
      return
    }
    setImporting(true)
    try {
      const yaml = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
      const upload = async (overwrite = false) => {
        const response = await fetch('/api/packs/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ yaml, overwrite }),
        })
        if (!response.ok) throw new Error('服务器未能完成导入，请稍后重试。')
        return response.json()
      }
      let data = await upload()
      if (data.conflict) {
        if (!window.confirm(`扩展包 ${data.pack.id} 已存在，是否替换？已开始的对局不受影响。`)) return
        data = await upload(true)
      }
      if (!data.ok) {
        setImportErrors(data.errors || ['导入失败。'])
        return
      }
      setPackId(data.pack.id)
      setPackRevision((revision) => revision + 1)
    } catch (error) {
      setImportErrors([error instanceof TypeError
        ? '读取失败，请确认文件为 UTF-8 编码且服务器已连接。'
        : (error as Error).message])
    } finally {
      setImporting(false)
    }
  }

  const currentPack = packs.find((p) => p.id === packId)

  const submit = () => {
    const nick = name.trim()
    if (!nick) {
      setState({ error: '先给自己起个名字' })
      return
    }
    if (!password.trim()) {
      setState({ error: '请输入房间口令' })
      return
    }
    if (mode === 'create' && password.length < 4) {
      setState({ error: '房间口令至少 4 位' })
      return
    }
    if (mode === 'join' && code.trim().length < 4) {
      setState({ error: '请输入 6 位房号' })
      return
    }
    setState({ error: null })
    ensure(nick, () => {
      if (mode === 'create') {
        send({ type: 'create_room', password, name: nick.trim(), pack_id: packId })
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

        {mode === 'create' && (
          <>
            <label className="field">
              <span>扩展包</span>
              <select value={packId} onChange={(e) => setPackId(e.target.value)}>
                {packs.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            {isLocal ? (
              <>
                <input
                  ref={fileInput}
                  type="file"
                  accept=".yaml,.yml"
                  hidden
                  onChange={(event) => {
                    const file = event.target.files?.[0]
                    event.target.value = ''
                    if (file) void importPack(file)
                  }}
                />
                <button className="btn btn-ghost" disabled={importing}
                  onClick={() => fileInput.current?.click()}>
                  {importing ? '导入中…' : '导入扩展包'}
                </button>
              </>
            ) : (
              <p className="hint">导入扩展包请在服务器电脑上打开 localhost:8000。</p>
            )}
            {importErrors.length > 0 && (
              <ul className="dev-errors">
                {importErrors.map((error, index) => <li key={index}>{error}</li>)}
              </ul>
            )}
            {currentPack?.description && (
              <p className="hint pack-desc">{currentPack.description}</p>
            )}
            {packIssues.length > 0 && (
              <p className="hint pack-issue">
                有 {packIssues.length} 个扩展包加载失败，已跳过。检查 {packIssues[0]}
              </p>
            )}
          </>
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
        <button className="dev-link" onClick={() => setDevOpen(true)}>
          开发者：编辑扩展包
        </button>
      </div>

      {devOpen && <DevPanel onClose={() => {
        setDevOpen(false)
        setPackRevision((revision) => revision + 1)
      }} />}
    </div>
  )
}
