import { useSyncExternalStore } from 'react'

export interface AgentSettings {
  baseUrl: string
  passcode: string
}

export interface AgentHealth {
  ok: boolean
  configured?: boolean
  model?: string
  has_key?: boolean
  key_hint?: string
  upstream?: string
  error?: string
}

const STORAGE_KEY = 'yap.agent'
const DEFAULT_SETTINGS: AgentSettings = {
  baseUrl: 'http://127.0.0.1:17666',
  passcode: '',
}

export function isAgentReady(health: AgentHealth | null): boolean {
  return Boolean(health?.ok && health.configured !== false)
}

function readSettings(): AgentSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULT_SETTINGS }
    const data = JSON.parse(raw)
    return {
      baseUrl: typeof data.baseUrl === 'string' ? data.baseUrl : DEFAULT_SETTINGS.baseUrl,
      passcode: typeof data.passcode === 'string' ? data.passcode : '',
    }
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

let settings: AgentSettings = readSettings()
let health: AgentHealth | null = null
let checking = false
const listeners = new Set<() => void>()

function emit() {
  listeners.forEach((fn) => fn())
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

export function useAgentSettings(): AgentSettings {
  return useSyncExternalStore(subscribe, () => settings)
}

export function useAgentHealth(): AgentHealth | null {
  return useSyncExternalStore(subscribe, () => health)
}

export function useAgentChecking(): boolean {
  return useSyncExternalStore(subscribe, () => checking)
}

export function saveAgentSettings(patch: Partial<AgentSettings>) {
  settings = { ...settings, ...patch }
  localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
  emit()
}

export function agentBaseUrl(): string {
  return settings.baseUrl.trim().replace(/\/+$/, '') || DEFAULT_SETTINGS.baseUrl
}

export async function refreshHealth(): Promise<AgentHealth> {
  if (checking) return health ?? { ok: false, error: '检查中' }
  checking = true
  emit()
  let result: AgentHealth
  try {
    const res = await fetch(`${agentBaseUrl()}/health`, {
      signal: AbortSignal.timeout(4000),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    result = toHealth(await res.json())
  } catch (err) {
    result = { ok: false, error: (err as Error).message || '连接失败' }
  }
  health = result
  checking = false
  emit()
  return result
}

function toHealth(data: {
  ok?: boolean
  configured?: boolean
  model?: string
  has_key?: boolean
  key_hint?: string
  upstream?: string
}): AgentHealth {
  return {
    ok: Boolean(data.ok),
    configured: data.configured !== false,
    model: data.model,
    has_key: Boolean(data.has_key),
    key_hint: data.key_hint,
    upstream: data.upstream,
  }
}

function errorText(data: unknown, fallback: string): string {
  const message = (data as { error?: { message?: string } | string } | null)?.error
  if (typeof message === 'string' && message) return message
  if (message && typeof message === 'object' && message.message) return message.message
  return fallback
}

export interface AgentConfigInput {
  baseUrl: string
  model: string
  apiKey: string
}

/** 把上游地址/模型/密钥写进本机代理，由代理保存到本地文件。 */
export async function saveAgentConfig(input: AgentConfigInput): Promise<AgentHealth> {
  const base = agentBaseUrl()
  let res: Response
  try {
    res = await fetch(`${base}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_url: input.baseUrl.trim(),
        model: input.model.trim(),
        api_key: input.apiKey.trim(),
      }),
      signal: AbortSignal.timeout(10000),
    })
  } catch {
    throw new Error(`连不上代理（${base}），先运行 run.bat（会同时启动 AI 代理）。`)
  }
  const data = await res.json().catch(() => null)
  if (!res.ok) throw new Error(errorText(data, `保存失败（HTTP ${res.status}）`))
  const saved = toHealth(data ?? {})
  health = saved
  emit()
  return saved
}

/** 让代理真实调用一次上游，验证地址、模型和密钥。 */
export async function testAgent(): Promise<{ ok: boolean; reply?: string; error?: string }> {
  const base = agentBaseUrl()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (settings.passcode) headers['X-Yap-Passcode'] = settings.passcode
  let res: Response
  try {
    res = await fetch(`${base}/v1/test`, {
      method: 'POST',
      headers,
      body: '{}',
      signal: AbortSignal.timeout(40000),
    })
  } catch {
    return { ok: false, error: `连不上代理（${base}），先运行 run.bat（会同时启动 AI 代理）。` }
  }
  const data = await res.json().catch(() => null)
  if (!res.ok) return { ok: false, error: errorText(data, `测试失败（HTTP ${res.status}）`) }
  return {
    ok: Boolean(data?.ok),
    reply: typeof data?.reply === 'string' ? data.reply : undefined,
    error: typeof data?.error === 'string' ? data.error : undefined,
  }
}
