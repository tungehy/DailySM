import React, { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { HotSector, IndexCard, OversoldSector, ReportDay } from '../api/types'
import { Sparkline } from '../components/Sparkline'
import { Markdown } from '../components/Markdown'
import { Badge, Card, Empty, SectionTitle, Spinner, changeColor, fmtPct } from '../components/ui'

interface WatchItem { id: number; watch_type: string; name: string; note: string | null; heat: number | null; price_change_pct: number | null }
interface OversoldPeriod { sector: string; cum_change_pct: number; period_days: number; heat: number | null }

const OVERSOLD_DAYS = [5, 20, 30, 60]

export default function Dashboard() {
  const [indices, setIndices] = useState<IndexCard[]>([])
  const [hot, setHot] = useState<HotSector[]>([])
  const [hotDate, setHotDate] = useState<string | null>(null)
  const [oversold, setOversold] = useState<OversoldPeriod[]>([])
  const [oversoldDays, setOversoldDays] = useState(5)
  const [oversoldLoading, setOversoldLoading] = useState(false)
  const [report, setReport] = useState<ReportDay | null>(null)
  const [watchlist, setWatchlist] = useState<WatchItem[]>([])
  const [newWatch, setNewWatch] = useState('')
  const [loading, setLoading] = useState(true)

  const loadWatch = () => api.get<{ watchlist: WatchItem[] }>('/api/dashboard/watchlist').then(r => setWatchlist(r.watchlist))
  const loadOversold = (days: number) => {
    setOversoldLoading(true)
    api.get<{ sectors: OversoldPeriod[] }>(`/api/dashboard/sectors/oversold_period?days=${days}&n=8`)
      .then(r => setOversold(r.sectors))
      .finally(() => setOversoldLoading(false))
  }

  useEffect(() => {
    Promise.allSettled([
      api.get<{ indices: IndexCard[] }>('/api/dashboard/indices').then(r => setIndices(r.indices)),
      api.get<{ date: string | null; sectors: HotSector[] }>('/api/dashboard/sectors/hot?n=8').then(r => { setHot(r.sectors); setHotDate(r.date) }),
      api.get<ReportDay>('/api/dashboard/report').then(setReport),
      loadWatch(),
    ]).finally(() => setLoading(false))
    loadOversold(oversoldDays)
  }, [])

  const changeOversoldDays = (d: number) => { setOversoldDays(d); loadOversold(d) }

  const addWatch = async () => {
    const name = newWatch.trim()
    if (!name) return
    await api.post('/api/dashboard/watchlist', { name, watch_type: 'sector' })
    setNewWatch('')
    loadWatch()
  }
  const delWatch = async (name: string) => {
    await api.delete(`/api/dashboard/watchlist/${encodeURIComponent(name)}`)
    loadWatch()
  }

  if (loading) return <Spinner label="加载大盘与板块数据…" />

  return (
    <div className="space-y-8">
      <header className="flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-bold text-ink-900">市场总览</h1>
          <p className="text-sm text-ink-400 mt-1">关键指数、热门板块与今日 AI 投资决策</p>
        </div>
        {hotDate && <span className="text-xs text-ink-400">数据日期 {hotDate}</span>}
      </header>

      {/* 大盘指数卡片 */}
      <section>
        <SectionTitle>大盘指数</SectionTitle>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
          {indices.map(ix => (
            <div key={ix.code} className="card p-4">
              <div className="text-xs text-ink-400">{ix.name}</div>
              <div className="mt-1 flex items-baseline gap-2">
                <span className="text-lg font-bold text-ink-900">{ix.close != null ? ix.close.toFixed(2) : '--'}</span>
              </div>
              <div className={`text-sm font-medium ${changeColor(ix.change_pct)}`}>{fmtPct(ix.change_pct)}</div>
              <div className="mt-2">
                <Sparkline data={ix.history.map(h => ({ date: h.date, value: h.close }))}
                           color={(ix.change_pct ?? 0) >= 0 ? '#ef4444' : '#22c55e'} height={44}
                           format={v => v.toFixed(2)} />
              </div>
              <div className="mt-1 text-[10px] text-ink-300">{ix.trade_date}</div>
            </div>
          ))}
          {indices.length === 0 && <div className="col-span-full"><Empty text="暂无指数数据，请先运行 python main.py index fetch" /></div>}
        </div>
      </section>

      {/* 热门 + 超跌 板块 */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <SectionTitle sub="按热度排序">热门板块</SectionTitle>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {hot.map(s => (
              <div key={s.sector} className="card p-4">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-ink-900">{s.sector}</span>
                  <Badge tone={(s.heat ?? 0) >= 60 ? 'red' : (s.heat ?? 0) <= 35 ? 'green' : 'gray'}>
                    热度 {s.heat != null ? s.heat.toFixed(0) : '--'}
                  </Badge>
                </div>
                <div className="mt-1 text-[11px] text-ink-400">
                  资金 {s.continuous_capital_score != null ? s.continuous_capital_score.toFixed(0) : '--'} ·
                  龙头 {s.continuous_leader_score != null ? s.continuous_leader_score.toFixed(0) : '--'}
                </div>
                <div className="mt-2">
                  <Sparkline data={s.history.map(h => ({ date: h.date, value: h.heat }))} color="#f59e0b" height={40}
                             format={v => `热度 ${v.toFixed(0)}`} />
                </div>
              </div>
            ))}
            {hot.length === 0 && <div className="col-span-full"><Empty text="暂无板块数据，请先运行 python main.py run-all" /></div>}
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-baseline gap-3">
              <h2 className="text-base font-semibold text-ink-900">超跌板块</h2>
              <span className="text-xs text-ink-400">近 {oversoldDays} 日累计跌幅</span>
            </div>
            <div className="flex gap-1 rounded-lg bg-ink-100 p-1">
              {OVERSOLD_DAYS.map(d => (
                <button key={d} onClick={() => changeOversoldDays(d)}
                        className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                          oversoldDays === d ? 'bg-white text-accent shadow-sm' : 'text-ink-500 hover:text-ink-800'
                        }`}>
                  {d}天
                </button>
              ))}
            </div>
          </div>
          <div className="card divide-y divide-ink-100 min-h-[120px]">
            {oversoldLoading ? <Spinner label="计算中…" /> : (
              <>
                {oversold.map(s => (
                  <div key={s.sector} className="flex items-center justify-between px-4 py-2.5">
                    <span className="text-sm text-ink-800">{s.sector}</span>
                    <div className="flex items-center gap-4">
                      <span className="text-xs text-ink-400">热度 {s.heat != null ? s.heat.toFixed(0) : '--'}</span>
                      <span className={`text-sm font-semibold w-20 text-right ${changeColor(s.cum_change_pct)}`}>
                        {fmtPct(s.cum_change_pct)}
                      </span>
                    </div>
                  </div>
                ))}
                {oversold.length === 0 && <Empty text="暂无数据" />}
              </>
            )}
          </div>
        </div>
      </section>

      {/* 我的关注 */}
      <section>
        <SectionTitle sub="自选 ETF / 行业 / 主题">我的关注</SectionTitle>
        <Card>
          <div className="flex gap-2 mb-3">
            <input value={newWatch} onChange={e => setNewWatch(e.target.value)}
                   onKeyDown={e => e.key === 'Enter' && addWatch()}
                   placeholder="输入板块名关注，如：半导体"
                   className="flex-1 px-3 py-2 rounded-lg border border-ink-200 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30" />
            <button onClick={addWatch} className="px-4 py-2 rounded-lg bg-accent text-white text-sm font-medium">关注</button>
          </div>
          {watchlist.length === 0 ? <Empty text="暂无关注，输入板块名添加" /> : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {watchlist.map(w => (
                <div key={w.id} className="flex items-center justify-between border border-ink-100 rounded-lg px-3 py-2">
                  <span className="text-sm text-ink-800">{w.name}</span>
                  <div className="flex items-center gap-3">
                    <span className="text-xs text-ink-400">热度 {w.heat != null ? w.heat.toFixed(0) : '--'}</span>
                    <span className={`text-xs font-medium ${changeColor(w.price_change_pct)}`}>{fmtPct(w.price_change_pct)}</span>
                    <button onClick={() => delWatch(w.name)} className="text-xs text-ink-300 hover:text-up">移除</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </section>

      {/* 今日 AI 投资决策 */}
      <section>
        <SectionTitle sub={report?.date ? `更新于 ${report.date}` : undefined}>今日 AI 投资决策日报</SectionTitle>
        <Card>
          {report?.found
            ? <Markdown text={report.summary} />
            : <Empty text="暂无决策日报，请先运行 python main.py run-all" />}
        </Card>
      </section>
    </div>
  )
}
