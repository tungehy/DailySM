import React from 'react'
import { Line, LineChart, ResponsiveContainer, Tooltip, YAxis } from 'recharts'

interface Props {
  data: { date: string; value: number | null }[]
  color?: string
  height?: number
  /** 数值格式化（如 0.00 或加单位） */
  format?: (v: number) => string
}

/** 2026-07-28 -> 07-28 */
function mmdd(d: any): string {
  const s = String(d ?? '')
  return s.length >= 10 ? s.slice(5, 10) : s
}

function Tip({ active, payload, format }: any) {
  if (!active || !payload || payload.length === 0) return null
  const v = payload[0].value
  const date = payload[0]?.payload?.date
  return (
    <div className="px-2 py-1 rounded-md bg-ink-900 text-white text-xs shadow-lg">
      <div className="opacity-70">{mmdd(date)}</div>
      <div className="font-semibold">{v == null ? '--' : (format ? format(v) : Number(v).toFixed(2))}</div>
    </div>
  )
}

/** 迷你折线图（带悬停数值提示，用于卡片内） */
export function Sparkline({ data, color = '#3b82f6', height = 48, format }: Props) {
  const values = data.map(d => d.value).filter((v): v is number => v != null)
  if (values.length === 0) return <div style={{ height }} className="flex items-center text-ink-300 text-xs">无数据</div>
  const min = Math.min(...values)
  const max = Math.max(...values)
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 2, bottom: 2, left: 0, right: 0 }}>
        <YAxis hide domain={[min, max]} />
        <Tooltip content={<Tip format={format} />} cursor={{ stroke: '#cbd5e1', strokeWidth: 1 }} />
        <Line type="monotone" dataKey="value" stroke={color} strokeWidth={1.8}
              dot={false} activeDot={{ r: 3 }} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}
