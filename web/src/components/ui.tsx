import React from 'react'

export function Card({ title, extra, children, className = '' }: {
  title?: React.ReactNode; extra?: React.ReactNode; children: React.ReactNode; className?: string
}) {
  return (
    <div className={`card p-4 ${className}`}>
      {(title || extra) && (
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm font-semibold text-ink-900">{title}</div>
          <div>{extra}</div>
        </div>
      )}
      {children}
    </div>
  )
}

export function SectionTitle({ children, sub }: { children: React.ReactNode; sub?: string }) {
  return (
    <div className="flex items-baseline gap-3 mb-3">
      <h2 className="text-base font-semibold text-ink-900">{children}</h2>
      {sub && <span className="text-xs text-ink-400">{sub}</span>}
    </div>
  )
}

export function Spinner({ label = '加载中…' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-ink-400 text-sm py-8 justify-center">
      <span className="inline-block h-4 w-4 rounded-full border-2 border-ink-300 border-t-accent animate-spin" />
      {label}
    </div>
  )
}

export function Empty({ text = '暂无数据' }: { text?: string }) {
  return <div className="text-ink-400 text-sm py-8 text-center">{text}</div>
}

export function Badge({ children, tone = 'gray' }: { children: React.ReactNode; tone?: 'gray' | 'red' | 'green' | 'blue' | 'amber' }) {
  const map = {
    gray: 'bg-ink-100 text-ink-600',
    red: 'bg-red-50 text-up',
    green: 'bg-green-50 text-down',
    blue: 'bg-accent-soft text-accent',
    amber: 'bg-amber-50 text-amber-600',
  }
  return <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${map[tone]}`}>{children}</span>
}

/** A股涨跌配色：涨红 跌绿 */
export function changeColor(v: number | null | undefined): string {
  if (v == null) return 'text-ink-500'
  if (v > 0) return 'text-up'
  if (v < 0) return 'text-down'
  return 'text-ink-500'
}

export function fmtPct(v: number | null | undefined): string {
  if (v == null) return '--'
  return (v > 0 ? '+' : '') + v.toFixed(2) + '%'
}
