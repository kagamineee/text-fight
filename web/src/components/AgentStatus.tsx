import { useEffect, useState } from 'react'
import {
  isAgentReady,
  refreshHealth,
  saveAgentConfig,
  saveAgentSettings,
  testAgent,
  useAgentChecking,
  useAgentHealth,
  useAgentSettings,
  type AgentHealth,
} from '../agent'

interface Props {
  className?: string
}

const PROVIDERS = [
  {
    id: 'deepseek',
    label: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat',
  },
  {
    id: 'openai',
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    model: 'gpt-4o-mini',
  },
  {
    id: 'moonshot',
    label: 'Kimi（月之暗面）',
    baseUrl: 'https://api.moonshot.cn/v1',
    model: 'moonshot-v1-8k',
  },
  {
    id: 'dashscope',
    label: '阿里云百炼',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen-plus',
  },
  {
    id: 'zhipu',
    label: '智谱 GLM',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'glm-4-flash',
  },
  {
    id: 'siliconflow',
    label: '硅基流动',
    baseUrl: 'https://api.siliconflow.cn/v1',
    model: 'Qwen/Qwen2.5-7B-Instruct',
  },
  {
    id: 'custom',
    label: '自定义（OpenAI 兼容）',
    baseUrl: '',
    model: '',
  },
]

function matchProvider(health: AgentHealth | null): string {
  const upstream = (health?.upstream ?? '').replace(/\/+$/, '')
  if (!upstream) return 'deepseek'
  return PROVIDERS.find((p) => p.baseUrl && p.baseUrl === upstream)?.id ?? 'custom'
}

export default function AgentStatus({ className = '' }: Props) {
  const settings = useAgentSettings()
  const health = useAgentHealth()
  const [open, setOpen] = useState(false)
  const ready = isAgentReady(health)
  const needsConfig = Boolean(health?.ok) && health?.configured === false

  useEffect(() => {
    void refreshHealth()
    const timer = window.setInterval(() => void refreshHealth(), 60000)
    return () => window.clearInterval(timer)
  }, [settings.baseUrl])

  const label = !health?.ok
    ? 'AI 未连接'
    : needsConfig
      ? 'AI 待配置'
      : `AI 已连接${health.model ? ` · ${health.model}` : ''}`

  return (
    <>
      <button
        className={`agent-badge ${ready ? 'is-on' : 'is-off'} ${className}`}
        onClick={() => {
          setOpen(true)
          void refreshHealth()
        }}
        title={
          !health?.ok
            ? '本机 AI 代理未连接，点击查看启动方法'
            : needsConfig
              ? '代理已启动，还没填模型服务，点击填写'
              : '本机 AI 代理已连接，点击设置'
        }
      >
        <span className={`dot ${ready ? 'on' : 'off'}`} />
        {label}
      </button>

      {open && <AgentDialog onClose={() => setOpen(false)} />}
    </>
  )
}

function AgentDialog({ onClose }: { onClose: () => void }) {
  const settings = useAgentSettings()
  const health = useAgentHealth()
  const checking = useAgentChecking()
  const [provider, setProvider] = useState(() => matchProvider(health))
  const [proxyUrl, setProxyUrl] = useState(settings.baseUrl)
  const [baseUrl, setBaseUrl] = useState(health?.upstream || PROVIDERS[0].baseUrl)
  const [model, setModel] = useState(health?.model || PROVIDERS[0].model)
  const [apiKey, setApiKey] = useState('')
  const [passcode, setPasscode] = useState(settings.passcode)
  const [busy, setBusy] = useState<'' | 'save' | 'test'>('')
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const pickProvider = (id: string) => {
    setProvider(id)
    const preset = PROVIDERS.find((p) => p.id === id)
    if (preset && preset.id !== 'custom') {
      setBaseUrl(preset.baseUrl)
      setModel(preset.model)
    }
    setMessage(null)
  }

  /** 先把界面上的地址/口令记住，再把模型配置交给代理保存。 */
  const persist = async () => {
    if (!baseUrl.trim()) throw new Error('先选服务商，或填写上游地址。')
    saveAgentSettings({ baseUrl: proxyUrl.trim() || settings.baseUrl, passcode })
    await saveAgentConfig({ baseUrl, model, apiKey })
  }

  const onSave = async () => {
    setBusy('save')
    setMessage(null)
    try {
      await persist()
      onClose()
    } catch (err) {
      setMessage({ ok: false, text: (err as Error).message })
      setBusy('')
    }
  }

  const onTest = async () => {
    setBusy('test')
    setMessage(null)
    try {
      await persist()
      const result = await testAgent()
      if (result.ok) {
        setMessage({ ok: true, text: `连接成功，模型回复：${result.reply || '（空）'}` })
      } else {
        setMessage({ ok: false, text: result.error || '上游没有通过测试' })
      }
      void refreshHealth()
    } catch (err) {
      setMessage({ ok: false, text: (err as Error).message })
    }
    setBusy('')
  }

  const statusText = (() => {
    if (message) return message.text
    if (checking) return '正在检查代理…'
    if (!health?.ok) {
      return `代理未启动：${health?.error ?? '没有响应'}。重新运行项目根目录的 run.bat。`
    }
    if (health.configured === false) return '代理已启动，但还没填模型服务。'
    return `已配置${health.model ? ` ${health.model}` : ''}${health.has_key ? '' : '，但没有 API Key'}`
  })()

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal agent-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>AI 设置</span>
          <button className="modal-close" onClick={onClose}>
            ×
          </button>
        </div>

        <p className="hint">
          填一次就行，会保存在本机。这个代理只负责房主裁判和困难/炼狱假人；玩家手写宣言，不需要配置自己的 API。
        </p>

        <label className="field">
          <span>服务商</span>
          <select value={provider} onChange={(e) => pickProvider(e.target.value)}>
            {PROVIDERS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span>上游地址</span>
          <input
            className="mono"
            value={baseUrl}
            onChange={(e) => {
              setBaseUrl(e.target.value)
              setProvider('custom')
            }}
            placeholder="https://api.deepseek.com/v1"
          />
        </label>

        <label className="field">
          <span>模型</span>
          <input
            className="mono"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="deepseek-chat"
          />
        </label>

        <label className="field">
          <span>API Key</span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={
              health?.has_key ? `已保存 ${health.key_hint ?? ''}，留空不改` : '粘贴服务商控制台里的 Key'
            }
            autoComplete="off"
          />
        </label>

        <div className={`agent-health ${message ? (message.ok ? 'ok' : 'bad') : health?.ok && health.configured !== false ? 'ok' : 'bad'}`}>
          {statusText}
        </div>

        <details className="agent-advanced">
          <summary>高级</summary>
          <label className="field">
            <span>代理地址（改过端口才需要动）</span>
            <input
              className="mono"
              value={proxyUrl}
              onChange={(e) => setProxyUrl(e.target.value)}
              placeholder="http://127.0.0.1:17666"
            />
          </label>
          <label className="field">
            <span>代理口令（config.toml 里设过才需要填）</span>
            <input
              type="password"
              value={passcode}
              onChange={(e) => setPasscode(e.target.value)}
              placeholder="可留空"
              autoComplete="off"
            />
          </label>
          <p className="hint">
            用局域网 IP 或 https 打开页面时，浏览器会拦截本机代理。必要时把访问地址加进
            <code> agent/config.toml</code> 的 <code>allowed_origins</code>，
            或直接用 http 打开。
          </p>
        </details>

        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onTest} disabled={busy !== ''}>
            {busy === 'test' ? '测试中…' : '测试连接'}
          </button>
          <button className="btn btn-primary" onClick={onSave} disabled={busy !== ''}>
            {busy === 'save' ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
