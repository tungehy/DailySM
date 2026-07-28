import React, { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { HeatmapData, Opinion, SummaryResp } from '../api/types'
import { Markdown } from '../components/Markdown'
import { Badge, Card, Empty, SectionTitle, Spinner } from '../components/ui'

/** 热度 -> 颜色：蓝(0) ~ 白(50) ~ 红(100) */
function heatColor(v: number | null): string {
  if (v == null) return 'rgba(0,0,0,0.03)'
  const t = Math.max(0, Math.min(100, v)) / 100
  // 0 蓝(#3b82f6) -> 0.5 灰白 -> 1 红(#ef4444)
  const lerp = (a: number, b: number) => Math.round(a + (b - a) * Math.abs(t - 0.5) * 2)
  if (t >= 0.5) return `rgb(${239}, ${lerp(68, 235)}, ${lerp(68, 240)})`
  return `rgb(${lerp(59, 240)}, ${lerp(130, 245)}, ${lerp(246, 245)})`
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

      {/* 热力图 */}
      <section>
        <SectionTitle sub={hover ? `${hover.s} · ${hover.d} · 热度 ${hover.v != null ? hover.v.toFixed(0) : '--'}` : '最近 30 个交易日'}>
          行业热点热力图
        </SectionTitle>
        <Card className="overflow-x-auto">
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
      </section>

      {/* 市场观点 + 宏观/新闻 */}
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
                  <span>{o.opinion_count} 条观点</span>
                  <span>净分 {o.net_score.toFixed(2)}</span>
                  <span className="truncate">{o.sources.join(' / ')}</span>
                </div>
              </div>
            ))}
            {opinions.length === 0 && <Empty />}
          </div>
        </div>

        <div className="lg:col-span-2 space-y-6">
          <div>
            <SectionTitle sub={macro?.date ? `更新于 ${macro.date}` : undefined}>宏观分析摘要</SectionTitle>
            <Card>{macro?.found ? <Markdown text={macro.summary} /> : <Empty text="暂无宏观分析" />}</Card>
          </div>
          <div>
            <SectionTitle sub={news?.date ? `更新于 ${news.date}` : undefined}>新闻情绪摘要</SectionTitle>
            <Card>{news?.found ? <Markdown text={news.summary} /> : <Empty text="暂无新闻分析" />}</Card>
          </div>
        </div>
      </section>
    </div>
  )
}
