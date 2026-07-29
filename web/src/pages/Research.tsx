import React, { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { HeatmapData, Opinion, SummaryResp } from '../api/types'
import { Markdown } from '../components/Markdown'
import { Badge, Card, Empty, SectionTitle, Spinner } from '../components/ui'

/**
 * 热度 -> 颜色：以 50 为中性（白）。
 * >50 红色，值越高红色越深；<50 蓝色，值越低蓝色越深。
 */
function heatColor(v: number | null): string {
  if (v == null) return 'rgba(0,0,0,0.03)'
  const t = Math.max(0, Math.min(100, v))
  // s: 偏离 50 的程度 0(=50) ~ 1(=0 或 100)
  const s = Math.abs(t - 50) / 50
  const lerp = (a: number, b: number) => Math.round(a + (b - a) * s)
  if (t >= 50) {
    // 白(255,255,255) -> 深红(185,28,28)
    return `rgb(${lerp(255, 185)}, ${lerp(255, 28)}, ${lerp(255, 28)})`
  }
  // 白(255,255,255) -> 深蓝(29,78,216)
  return `rgb(${lerp(255, 29)}, ${lerp(255, 78)}, ${lerp(255, 216)})`
}

function directionTone(d: string): 'red' | 'green' | 'gray' {
  return d === 'bullish' ? 'red' : d === 'bearish' ? 'green' : 'gray'
}
const DIRECTION_CN: Record<string, string> = { bullish: '看多', bearish: '看空', neutral: '中性' }

export default function Research() {
  const [heat, setHeat] = useState<HeatmapData | null>(null)
  const [opinions, setOpinions] = useState<Opinion[]>([])
  const [macro, setMacro] = useState<SummaryResp | null>(null)
  const [news, setNews] = useState<SummaryResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [hover, setHover] = useState<{ s: string; d: string; v: number | null } | null>(null)
  const [heatOpen, setHeatOpen] = useState(false)

  useEffect(() => {
    Promise.allSettled([
      api.get<HeatmapData>('/api/research/heatmap?days=30').then(setHeat),
      api.get<{ opinions: Opinion[] }>('/api/research/opinions?top_n=30').then(r => setOpinions(r.opinions)),
      api.get<SummaryResp>('/api/research/macro').then(setMacro),
      api.get<SummaryResp>('/api/research/news').then(setNews),
    ]).finally(() => setLoading(false))
  }, [])

  if (loading) return <Spinner label="加载研究中心数据…" />

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-xl font-bold text-ink-900">研究中心</h1>
        <p className="text-sm text-ink-400 mt-1">30 天行业热点热力图、市场观点与宏观/新闻分析</p>
      </header>

      {/* 新闻情绪摘要（置顶） */}
      <section>
        <SectionTitle sub={news?.date ? `更新于 ${news.date}` : undefined}>新闻情绪摘要</SectionTitle>
        <Card>{news?.found ? <Markdown text={news.summary} /> : <Empty text="暂无新闻分析" />}</Card>
      </section>

      {/* 市场观点 + 宏观分析 */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1">
          <SectionTitle sub="视频/研报/新闻统一观点">市场观点</SectionTitle>
          <div className="card divide-y divide-ink-100 max-h-[520px] overflow-y-auto">
            {opinions.map(o => (
              <div key={o.target_name} className="px-4 py-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-ink-900">{o.target_name}</span>
                  <Badge tone={directionTone(o.direction)}>{DIRECTION_CN[o.direction] || o.direction}</Badge>
                </div>
                <div className="mt-1 text-xs text-ink-500 line-clamp-2">{o.top_reason}</div>
                <div className="mt-1 flex items-center gap-2 text-[11px] text-ink-400">
                  {o.latest_time && <span>{o.latest_time.slice(5, 10)}</span>}
                  <span>{o.opinion_count} 条观点</span>
                  <span>净分 {o.net_score.toFixed(2)}</span>
                  <span className="truncate">{o.sources.join(' / ')}</span>
                </div>
              </div>
            ))}
            {opinions.length === 0 && <Empty />}
          </div>
        </div>

        <div className="lg:col-span-2">
          <SectionTitle sub={macro?.date ? `更新于 ${macro.date}` : undefined}>宏观分析摘要</SectionTitle>
          <Card>{macro?.found ? <Markdown text={macro.summary} /> : <Empty text="暂无宏观分析" />}</Card>
        </div>
      </section>

      {/* 行业热点热力图（折叠，默认收起，置于页底） */}
      <section>
        <button onClick={() => setHeatOpen(o => !o)}
                className="w-full flex items-center justify-between px-4 py-3 rounded-xl border border-ink-200 bg-white hover:bg-ink-50 transition-colors">
          <span className="text-base font-semibold text-ink-900">行业热点热力图</span>
          <span className="flex items-center gap-2 text-sm text-ink-400">
            {heatOpen
              ? (hover ? `${hover.s} · ${hover.d} · 热度 ${hover.v != null ? hover.v.toFixed(0) : '--'}` : '最近 30 个交易日')
              : '最近 30 个交易日'}
            <span className={`inline-block transition-transform ${heatOpen ? 'rotate-180' : ''}`}>▾</span>
          </span>
        </button>
        {heatOpen && (
          <Card className="overflow-x-auto mt-3">
            {heat && heat.sectors.length > 0 ? (
              <table className="border-collapse" style={{ minWidth: heat.dates.length * 34 + 96 }}>
                <thead>
                  <tr>
                    <th className="sticky left-0 bg-white text-left text-xs text-ink-400 font-normal pr-2 py-1">板块</th>
                    {heat.dates.map(d => (
                      <th key={d} className="text-[10px] text-ink-400 font-normal px-0.5 py-1" style={{ writingMode: 'vertical-rl' }}>{d.slice(5)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {heat.matrix.map(row => (
                    <tr key={row.sector}>
                      <td className="sticky left-0 bg-white text-xs text-ink-800 pr-2 py-0.5 whitespace-nowrap">{row.sector}</td>
                      {row.values.map((v, i) => (
                        <td key={i}
                            onMouseEnter={() => setHover({ s: row.sector, d: heat.dates[i], v })}
                            className="w-8 h-6 cursor-pointer border border-white"
                            style={{ backgroundColor: heatColor(v) }} />
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <Empty text="暂无热度数据，请先运行 python main.py run-all" />}
          </Card>
        )}
      </section>
    </div>
  )
}
