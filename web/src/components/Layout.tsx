import React from 'react'
import { NavLink, Outlet } from 'react-router-dom'

const NAV = [
  { to: '/',          label: '首页',     desc: 'Dashboard',  icon: '📊', end: true },
  { to: '/research',  label: '研究中心', desc: 'Research',   icon: '🔬' },
  { to: '/knowledge', label: '知识中心', desc: 'Knowledge',  icon: '📚' },
  { to: '/assistant', label: 'AI 助手',  desc: 'Assistant',  icon: '💬' },
  { to: '/system',    label: '系统设置', desc: 'System',     icon: '⚙️' },
]

export default function Layout() {
  return (
    <div className="min-h-screen flex">
      {/* 侧边栏 */}
      <aside className="w-60 shrink-0 border-r border-ink-200 bg-white flex flex-col">
        <div className="px-5 py-5 border-b border-ink-100">
          <div className="text-lg font-bold text-ink-900">DailySM</div>
          <div className="text-xs text-ink-400 mt-0.5">行业智能投研平台</div>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1">
          {NAV.map(n => (
            <NavLink key={n.to} to={n.to} end={n.end as boolean | undefined}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg transition-colors ${
                  isActive ? 'bg-accent-soft text-accent' : 'text-ink-600 hover:bg-ink-100'
                }`
              }>
              <span className="text-lg leading-none">{n.icon}</span>
              <span className="flex-1">
                <span className="block text-sm font-medium">{n.label}</span>
                <span className="block text-[11px] opacity-60">{n.desc}</span>
              </span>
            </NavLink>
          ))}
        </nav>
        <div className="px-5 py-4 border-t border-ink-100 text-[11px] text-ink-400">
          数据驱动 · 多 Agent 协同
        </div>
      </aside>

      {/* 主内容 */}
      <main className="flex-1 min-w-0">
        <div className="max-w-7xl mx-auto px-6 py-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
