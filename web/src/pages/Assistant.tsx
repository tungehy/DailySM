import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { ChatMessage, Holding } from '../api/types'
import { Markdown } from '../components/Markdown'
import { Badge, Empty } from '../components/ui'

interface Msg { role: string; content: string }

export default function Assistant() {
  const [sessionId, setSessionId] = useState<string>('')
  const [messages, setMessages] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [thinking, setThinking] = useState(false)
  const [holdings, setHoldings] = useState<Holding[]>([])
  const [sessions, setSessions] = useState<{ session_id: string; last_at: string; count: number }[]>([])
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api.get<{ holdings: Holding[] }>('/api/assistant/portfolio').then(r => setHoldings(r.holdings)).catch(() => {})
    // 加载最近会话，自动续上最近一次对话
    api.get<{ sessions: any[] }>('/api/assistant/sessions?limit=10')
      .then(r => {
        setSessions(r.sessions)
        if (r.sessions.length > 0) loadHistory(r.sessions[0].session_id)
      })
      .catch(() => {})
  }, [])

  const loadHistory = (sid: string) => {
    api.get<{ messages: Msg[] }>(`/api/assistant/history?session_id=${sid}&limit=100`)
      .then(r => {
        setSessionId(sid)
        setMessages(r.messages.map(m => ({ role: m.role, content: m.content })))
      })
      .catch(() => {})
  }

  const newChat = () => { setSessionId(''); setMessages([]) }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, thinking])

  const send = async () => {
    const text = input.trim()
    if (!text || thinking) return
    const userMsg: Msg = { role: 'user', content: text }
    setMessages(m => [...m, userMsg])
    setInput('')
    setThinking(true)
    try {
      const r = await api.post<{ session_id: string; answer: string }>('/api/assistant/chat', {
        message: text, session_id: sessionId || undefined,
      })
      setSessionId(r.session_id)
      setMessages(m => [...m, { role: 'assistant', content: r.answer }])
      // 刷新历史会话列表（新对话入库后出现）
      api.get<{ sessions: any[] }>('/api/assistant/sessions?limit=10').then(rr => setSessions(rr.sessions)).catch(() => {})
    } catch (e: any) {
      setMessages(m => [...m, { role: 'assistant', content: `出错了：${e.message}` }])
    } finally {
      setThinking(false)
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-3rem)]">
      <header className="mb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-ink-900">AI 投资助手</h1>
            <p className="text-sm text-ink-400 mt-1">
              自动结合实时新闻、各 Agent 分析、统一观点库与您的持仓，给出个性化建议
            </p>
          </div>
          <button onClick={newChat} className="px-4 py-2 rounded-lg border border-ink-200 text-sm text-ink-700 hover:bg-ink-100 shrink-0">
            + 新对话
          </button>
        </div>
        {sessions.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2 items-center">
            <span className="text-xs text-ink-400">历史会话：</span>
            {sessions.map(s => (
              <button key={s.session_id} onClick={() => loadHistory(s.session_id)}
                      className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                        sessionId === s.session_id ? 'bg-accent-soft border-accent text-accent' : 'border-ink-200 text-ink-500 hover:bg-ink-100'
                      }`}>
                {s.last_at.slice(5, 16)} · {s.count}条
              </button>
            ))}
          </div>
        )}
        {holdings.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            <span className="text-xs text-ink-400">当前持仓：</span>
            {holdings.map(h => (
              <Badge key={h.id} tone="blue">{h.name}{h.position_pct != null ? ` ${h.position_pct.toFixed(0)}%` : ''}</Badge>
            ))}
          </div>
        )}
      </header>

      {/* 消息区 */}
      <div className="flex-1 min-h-0 overflow-y-auto space-y-4 pr-1">
        {messages.length === 0 && (
          <div className="h-full flex flex-col items-center justify-center text-ink-300">
            <div className="text-4xl mb-3">💬</div>
            <div className="text-sm">试试提问：</div>
            <div className="mt-2 flex flex-wrap gap-2 justify-center max-w-md">
              {['半导体最近怎么样，我该怎么办？', '当前市场主线是什么？', '我的持仓风险大吗？'].map(q => (
                <button key={q} onClick={() => setInput(q)}
                        className="px-3 py-1.5 rounded-full border border-ink-200 text-xs text-ink-600 hover:bg-ink-100">
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[85%] rounded-2xl px-4 py-3 ${
              m.role === 'user'
                ? 'bg-accent text-white'
                : 'bg-white border border-ink-200'
            }`}>
              {m.role === 'assistant'
                ? <Markdown text={m.content} />
                : <div className="text-sm leading-6 whitespace-pre-wrap">{m.content}</div>}
            </div>
          </div>
        ))}
        {thinking && (
          <div className="flex justify-start">
            <div className="bg-white border border-ink-200 rounded-2xl px-4 py-3 text-sm text-ink-400 flex items-center gap-2">
              <span className="inline-block h-3 w-3 rounded-full border-2 border-ink-300 border-t-accent animate-spin" />
              正在检索新闻、观点与持仓信息…
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* 输入区 */}
      <div className="mt-4 flex gap-2">
        <textarea value={input} onChange={e => setInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
                  placeholder="输入问题…（Enter 发送，Shift+Enter 换行）"
                  rows={2}
                  className="flex-1 px-4 py-3 rounded-xl border border-ink-200 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30 resize-none" />
        <button onClick={send} disabled={thinking || !input.trim()}
                className="self-end px-5 py-2.5 rounded-xl bg-accent text-white text-sm font-medium disabled:opacity-40">
          发送
        </button>
      </div>
    </div>
  )
}
