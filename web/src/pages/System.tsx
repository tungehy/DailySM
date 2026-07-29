import React, { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { AgentStatus } from '../api/types'
import { Badge, Card, Empty, SectionTitle, Spinner } from '../components/ui'

function statusTone(s?: string | null): 'green' | 'amber' | 'gray' | 'red' {
  if (s === 'active') return 'green'
  if (s === 'no_data') return 'gray'
  if (s === 'error') return 'red'
  return 'amber'
}

export default function System() {
  const [agents, setAgents] = useState<AgentStatus[]>([])
  const [schedules, setSchedules] = useState<any>(null)
  const [config, setConfig] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    Promise.allSettled([
      api.get<{ agents: AgentStatus[] }>('/api/system/agents').then(r => setAgents(r.agents)),
      api.get<{ schedules: any }>('/api/system/schedules').then(r => setSchedules(r.schedules)),
      api.get<{ config: any }>('/api/system/config').then(r => setConfig(r.config)),
    ]).finally(() => setLoading(false))
  }, [])

  const saveCommon = async () => {
    if (!config) return
    setSaving(true); setSaved(false)
    try {
      await api.put('/api/system/config', {
        updates: {
          'decision_agent.top_n': config.decision_agent?.top_n,
          'decision_agent.save_report': config.decision_agent?.save_report,
          'chat_agent.news_min': config.chat_agent?.news_min,
          'chat_agent.history_turns': config.chat_agent?.history_turns,
          'research.top_k': config.research?.top_k,
          'research.chunk_size': config.research?.chunk_size,
          'notifications.enabled': config.notifications?.enabled,
          // B站 / 下载 / 转录
          'bilibili.days_filter': config.bilibili?.days_filter,
          'bilibili.request_interval': config.bilibili?.request_interval,
          'bilibili.up_hosts': (config.bilibili?.up_hosts || []).filter((u: any) => u && u.uid),
          'bilibili.sessdata': config.bilibili?.sessdata,
          'bilibili.bili_jct': config.bilibili?.bili_jct,
          'bilibili.dedeuserid': config.bilibili?.dedeuserid,
          'transcription.model_size': config.transcription?.model_size,
          'transcription.device': config.transcription?.device,
        },
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } finally { setSaving(false) }
  }

  if (loading) return <Spinner label="加载系统状态…" />

  const set = (path: string[], val: any) => {
    setConfig((c: any) => {
      const copy = JSON.parse(JSON.stringify(c))
      let cur = copy
      for (const k of path.slice(0, -1)) { cur[k] = cur[k] || {}; cur = cur[k] }
      cur[path[path.length - 1]] = val
      return copy
    })
  }

  const upHosts = (): any[] => (config?.bilibili?.up_hosts || [])
  const setUpHosts = (list: any[]) => set(['bilibili', 'up_hosts'], list)
  const addUpHost = () => setUpHosts([...upHosts(), { uid: '', name: '', max_videos: 1 }])
  const removeUpHost = (i: number) => setUpHosts(upHosts().filter((_, j) => j !== i))
  const setUpHost = (i: number, key: string, val: any) =>
    setUpHosts(upHosts().map((u, j) => (j === i ? { ...u, [key]: val } : u)))

  const agentSched = schedules?.agents || {}

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-xl font-bold text-ink-900">系统设置</h1>
        <p className="text-sm text-ink-400 mt-1">Agent 运行状态、调度任务与常用配置</p>
      </header>

      {/* Agent 运行状态 */}
      <section>
        <SectionTitle>Agent 运行状态</SectionTitle>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {agents.map(a => (
            <Card key={a.agent}>
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-ink-900">{a.label}</span>
                <Badge tone={statusTone(a.status)}>{a.status || '未知'}</Badge>
              </div>
              <div className="mt-2 space-y-1 text-xs text-ink-500">
                <div>最近运行：{a.last_run ? a.last_run.slice(0, 16) : '—'}</div>
                <div>数据日期：{a.trade_date || '—'}</div>
                {a.confidence != null && <div>置信度：{(a.confidence * 100).toFixed(0)}%</div>}
              </div>
            </Card>
          ))}
        </div>
      </section>

      {/* 调度任务 */}
      <section>
        <SectionTitle sub="config/schedules.yaml">调度任务</SectionTitle>
        <Card>
          {Object.keys(agentSched).length === 0 ? <Empty text="未配置调度任务" /> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-ink-400 border-b border-ink-100">
                    <th className="py-2 pr-4">任务</th><th className="py-2 pr-4">cron</th>
                    <th className="py-2 pr-4">状态</th><th className="py-2">说明</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(agentSched).map(([name, s]: [string, any]) => (
                    <tr key={name} className="border-b border-ink-50">
                      <td className="py-2 pr-4 font-medium text-ink-800">{name}</td>
                      <td className="py-2 pr-4 font-mono text-xs text-ink-600">{s.schedule || '—'}</td>
                      <td className="py-2 pr-4">
                        <Badge tone={s.enabled ? 'green' : 'gray'}>{s.enabled ? '启用' : '停用'}</Badge>
                      </td>
                      <td className="py-2 text-xs text-ink-500">{s.description || ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </section>

      {/* 常用配置 */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <SectionTitle>常用配置</SectionTitle>
          <div className="flex items-center gap-3">
            {saved && <span className="text-xs text-down">已保存 ✓</span>}
            <button onClick={saveCommon} disabled={saving}
                    className="px-4 py-2 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-50">
              {saving ? '保存中…' : '保存修改'}
            </button>
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Card title="决策 Agent">
            <Field label="推荐板块数 top_n">
              <NumInput value={config?.decision_agent?.top_n} onChange={v => set(['decision_agent', 'top_n'], v)} />
            </Field>
          </Card>
          <Card title="对话助手">
            <Field label="新闻补采阈值 news_min">
              <NumInput value={config?.chat_agent?.news_min} onChange={v => set(['chat_agent', 'news_min'], v)} />
            </Field>
            <Field label="对话轮数 history_turns">
              <NumInput value={config?.chat_agent?.history_turns} onChange={v => set(['chat_agent', 'history_turns'], v)} />
            </Field>
          </Card>
          <Card title="研报 RAG">
            <Field label="检索条数 top_k">
              <NumInput value={config?.research?.top_k} onChange={v => set(['research', 'top_k'], v)} />
            </Field>
            <Field label="切块大小 chunk_size">
              <NumInput value={config?.research?.chunk_size} onChange={v => set(['research', 'chunk_size'], v)} />
            </Field>
          </Card>
          <Card title="通知">
            <Field label="启用通知">
              <input type="checkbox" checked={!!config?.notifications?.enabled}
                     onChange={e => set(['notifications', 'enabled'], e.target.checked)}
                     className="h-4 w-4 accent-accent" />
            </Field>
          </Card>
        </div>
        <p className="mt-3 text-xs text-ink-400">
          仅展示常用项。完整配置（含 LLM / Embedding / 数据库 / Webhook）请直接编辑 config/config.yaml；密钥字段不在此显示。
        </p>
      </section>

      {/* B站 / 视频流水线配置 */}
      <section>
        <SectionTitle sub="视频观点流水线（pipeline）">B站 / 下载 / 转录配置</SectionTitle>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Card title="B站采集">
            <Field label="视频时间窗（天）">
              <NumInput value={config?.bilibili?.days_filter} onChange={v => set(['bilibili', 'days_filter'], v)} />
            </Field>
            <Field label="请求间隔（秒）">
              <NumInput value={config?.bilibili?.request_interval} onChange={v => set(['bilibili', 'request_interval'], v)} />
            </Field>
            <div className="mt-3 pt-3 border-t border-ink-100">
              <div className="flex items-center justify-between mb-2">
                <span className="text-sm font-medium text-ink-800">B 站目标 UP 主配置</span>
                <button onClick={addUpHost}
                        className="px-2.5 py-1 rounded-lg bg-accent text-white text-xs font-medium">
                  + 添加 UP 主
                </button>
              </div>
              <div className="space-y-2">
                {(config?.bilibili?.up_hosts || []).map((u: any, i: number) => (
                  <div key={i} className="flex items-center gap-2">
                    <input type="text" value={u?.uid ?? ''} placeholder="uid"
                           onChange={e => setUpHost(i, 'uid', e.target.value)}
                           className="w-32 px-2 py-1.5 rounded border border-ink-200 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30" />
                    <input type="text" value={u?.name ?? ''} placeholder="name"
                           onChange={e => setUpHost(i, 'name', e.target.value)}
                           className="flex-1 px-2 py-1.5 rounded border border-ink-200 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30" />
                    <input type="number" value={u?.max_videos ?? ''} placeholder="max_videos" min={1}
                           onChange={e => setUpHost(i, 'max_videos', Number(e.target.value))}
                           className="w-24 px-2 py-1.5 rounded border border-ink-200 text-sm text-right focus:outline-none focus:ring-2 focus:ring-accent/30" />
                    <button onClick={() => removeUpHost(i)}
                            className="px-2 text-ink-300 hover:text-up text-lg leading-none shrink-0" title="删除">×</button>
                  </div>
                ))}
                {(config?.bilibili?.up_hosts || []).length === 0 && (
                  <div className="text-xs text-ink-400 py-1">暂无 UP 主，点击右上角「+ 添加 UP 主」</div>
                )}
              </div>
            </div>
          </Card>
          <Card title="Whisper 转录">
            <Field label="模型大小">
              <TextInput value={config?.transcription?.model_size} onChange={v => set(['transcription', 'model_size'], v)} placeholder="base / small / medium" />
            </Field>
            <Field label="设备">
              <TextInput value={config?.transcription?.device} onChange={v => set(['transcription', 'device'], v)} placeholder="cpu / cuda" />
            </Field>
          </Card>
          <Card title="B站 Cookie（登录凭证，可选）">
            <Field label="SESSDATA">
              <TextInput value={config?.bilibili?.sessdata} onChange={v => set(['bilibili', 'sessdata'], v)} placeholder="留空则不修改" />
            </Field>
            <Field label="bili_jct">
              <TextInput value={config?.bilibili?.bili_jct} onChange={v => set(['bilibili', 'bili_jct'], v)} placeholder="留空则不修改" />
            </Field>
            <Field label="dedeuserid">
              <TextInput value={config?.bilibili?.dedeuserid} onChange={v => set(['bilibili', 'dedeuserid'], v)} placeholder="留空则不修改" />
            </Field>
          </Card>
        </div>
      </section>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-sm text-ink-600">{label}</span>
      {children}
    </div>
  )
}

function NumInput({ value, onChange }: { value: number | undefined; onChange: (v: number) => void }) {
  return (
    <input type="number" value={value ?? ''} onChange={e => onChange(Number(e.target.value))}
           className="w-24 px-2 py-1 rounded border border-ink-200 text-sm text-right focus:outline-none focus:ring-2 focus:ring-accent/30" />
  )
}

function TextInput({ value, onChange, placeholder }: { value: string | undefined; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <input type="text" value={value ?? ''} onChange={e => onChange(e.target.value)} placeholder={placeholder}
           className="w-44 px-2 py-1 rounded border border-ink-200 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30" />
  )
}
