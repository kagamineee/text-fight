import { useEffect, useState } from 'react'
import { agentBaseUrl } from '../agent'

interface FieldGuide {
  path: string
  label: string
  note: string
}

interface DevResponse {
  ok?: boolean
  errors?: string[]
  yaml?: string
  example?: string
  fields?: FieldGuide[]
  file?: string
  pack?: { id: string; name: string; events: number; hp: number } | null
}

export default function DevPanel({ onClose }: { onClose: () => void }) {
  const [yaml, setYaml] = useState('')
  const [example, setExample] = useState('')
  const [prompt, setPrompt] = useState('')
  const [fields, setFields] = useState<FieldGuide[]>([])
  const [errors, setErrors] = useState<string[]>([])
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState<'' | 'draft' | 'validate' | 'save'>('')

  useEffect(() => {
    fetch('/api/dev/schema')
      .then((r) => r.json())
      .then((data: DevResponse) => {
        if (data.ok) {
          setFields(data.fields || [])
          setExample(data.example || '')
          setYaml((current) => current || data.example || '')
        } else {
          setErrors(data.errors || ['开发者接口不可用。'])
        }
      })
      .catch(() => setErrors(['读取开发者接口失败，确认服务已启动。']))
  }, [])

  const post = async (path: string, body: object): Promise<DevResponse> => {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    return res.json()
  }

  const draft = async () => {
    if (!prompt.trim()) {
      setErrors(['先写一句世界观设想，再让 AI 起草。'])
      return
    }
    setBusy('draft')
    setErrors([])
    setStatus('正在生成草稿…')
    try {
      const data = await post('/api/dev/draft', {
        prompt: prompt.trim(),
        base_url: agentBaseUrl(),
      })
      if (data.yaml) setYaml(data.yaml)
      setErrors(data.errors || [])
      setStatus(data.ok ? '草稿已生成，先校验再保存。' : '草稿生成了，但校验没通过。')
    } catch {
      setErrors(['生成失败：请求没发出去。'])
      setStatus('')
    } finally {
      setBusy('')
    }
  }

  const validate = async () => {
    setBusy('validate')
    setErrors([])
    setStatus('校验中…')
    try {
      const data = await post('/api/dev/validate', { yaml })
      setErrors(data.errors || [])
      setStatus(
        data.ok && data.pack
          ? `校验通过：${data.pack.name}（${data.pack.events} 个事件，初始血量 ${data.pack.hp}）`
          : '校验没通过，按下面的提示改。',
      )
    } catch {
      setErrors(['校验失败：请求没发出去。'])
      setStatus('')
    } finally {
      setBusy('')
    }
  }

  const save = async () => {
    setBusy('save')
    setErrors([])
    setStatus('保存中…')
    try {
      const data = await post('/api/dev/save', { yaml })
      setErrors(data.errors || [])
      if (data.ok) {
        setStatus(`已保存到 ${data.file}，扩展包列表已热加载。`)
      } else {
        setStatus('保存失败：先修好校验错误。')
      }
    } catch {
      setErrors(['保存失败：请求没发出去。'])
      setStatus('')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="dev-panel" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>扩展开发生成器</span>
          <button className="modal-close" onClick={onClose} title="关闭">
            ×
          </button>
        </div>

        <p className="hint dev-hint">
          本机专用。写完先校验，保存后会写入用户包目录并立即出现在建房列表里；内置包不会被覆盖。
        </p>

        <div className="dev-ai">
          <input
            className="dev-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="例如：一个赛博修仙世界，义体与飞剑并存，天灾是数据风暴"
            onKeyDown={(e) => {
              if (e.key === 'Enter') void draft()
            }}
          />
          <button className="btn btn-ghost" onClick={() => void draft()} disabled={busy !== ''}>
            {busy === 'draft' ? '生成中…' : 'AI 起草'}
          </button>
        </div>

        <textarea
          className="dev-editor mono"
          value={yaml}
          onChange={(e) => setYaml(e.target.value)}
          spellCheck={false}
        />

        <div className="dev-actions">
          <button
            className="btn btn-ghost btn-tiny"
            onClick={() => setYaml(example)}
            disabled={busy !== ''}
          >
            插入示例
          </button>
          <span className="dev-spacer" />
          <button className="btn btn-ghost" onClick={() => void validate()} disabled={busy !== ''}>
            {busy === 'validate' ? '校验中…' : '校验'}
          </button>
          <button className="btn btn-primary" onClick={() => void save()} disabled={busy !== ''}>
            {busy === 'save' ? '保存中…' : '保存并热加载'}
          </button>
        </div>

        {status && <div className="dev-status">{status}</div>}
        {errors.length > 0 && (
          <ul className="dev-errors">
            {errors.map((err, i) => (
              <li key={i}>{err}</li>
            ))}
          </ul>
        )}

        <details className="dev-guide">
          <summary>字段速查</summary>
          <ul>
            {fields.map((f) => (
              <li key={f.path}>
                <code>{f.path}</code> {f.label}：{f.note}
              </li>
            ))}
          </ul>
        </details>
      </div>
    </div>
  )
}
