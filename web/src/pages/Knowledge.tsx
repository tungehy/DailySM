import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import type { ReportDetail, ReportItem } from '../api/types'
import { Markdown } from '../components/Markdown'
import { Badge, Empty, Spinner } from '../components/ui'

const TYPE_CN: Record<string, string> = {
  macro_strategy: '宏观策略', industry_deep: '行业深度',
  company_note: '公司点评', data_flash: '数据快评',
}

function statusBadge(s?: string) {
  if (s === 'processing') return <Badge tone="amber">处理中…</Badge>
  if (s === 'error') return <Badge tone="red">失败</Badge>
  return <Badge tone="green">完成</Badge>
}

export default function Knowledge() {
  const [reports, setReports] = useState<ReportItem[]>([])
  const [selected, setSelected] = useState<ReportDetail | null>(null)
  const [loadingList, setLoadingList] = useState(true)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [searchText, setSearchText] = useState('')
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [results, setResults] = useState<any[] | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const loadList = () => {
    api.get<{ reports: ReportItem[] }>('/api/knowledge/reports?limit=200')
      .then(r => setReports(r.reports))
      .finally(() => setLoadingList(false))
  }
  useEffect(() => { setLoadingList(true); loadList() }, [])

  // 处理中任务轮询刷新
  const hasProcessing = useMemo(() => reports.some(r => r.status === 'processing'), [reports])
  useEffect(() => {
    if (!hasProcessing) return
    const t = setInterval(loadList, 3000)
    return () => clearInterval(t)
  }, [hasProcessing])

  const filtered = useMemo(
    () => reports.filter(r => (r.title || '').includes(searchText)),
    [reports, searchText],
  )

  const openDetail = (id: number | string) => {
    if (typeof id !== 'number') return
    setLoadingDetail(true)
    api.get<ReportDetail>(`/api/knowledge/reports/${id}`)
      .then(setSelected)
      .catch(() => setSelected(null))
      .finally(() => setLoadingDetail(false))
  }

  const onUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setUploading(true)
    const form = new FormData()
    Array.from(files).forEach(f => form.append('files', f))
    try { await api.upload('/api/knowledge/reports/upload', form); loadList() }
    finally { setUploading(false); if (fileRef.current) fileRef.current.value = '' }
  }

  const onDelete = async (id: number | string) => {
    if (typeof id !== 'number') return
    if (!confirm('确定删除该研报（含向量切块）？')) return
    await api.delete(`/api/knowledge/reports/${id}`)
    if (selected?.id === id) setSelected(null)
    loadList()
  }

  const doSearch = async () => {
    if (!query.trim()) return
    setSearching(true); setResults(null)
    try {
      const r = await api.get<{ results: any[] }>(`/api/knowledge/search?q=${encodeURIComponent(query)}&top_k=6`)
      setResults(r.results)
    } finally { setSearching(false) }
  }

  const processingCount = reports.filter(r => r.status === 'processing').length
  const failedCount = reports.filter(r => r.status === 'error').length

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-bold text-ink-900">知识中心</h1>
        <p className="text-sm text-ink-400 mt-1">研报管理（导入 / Embedding / 原件预览）与 RAG 时效加权检索</p>
      </header>

      {/* RAG 检索 */}
      <section className="card p-4">
        <div className="flex gap-2">
          <input value={query} onChange={e => setQuery(e.target.value)}
                 onKeyDown={e => e.key === 'Enter' && doSearch()}
                 placeholder="输入问题，如：半导体行业下半年配置逻辑"
                 className="flex-1 px-3 py-2 rounded-lg border border-ink-200 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30" />
          <button onClick={doSearch} disabled={searching}
                  className="px-4 py-2 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-50">
            {searching ? '检索中…' : '检索'}
          </button>
        </div>
        {results && (
          <div className="mt-4 space-y-3">
            {results.length === 0 && <Empty text="未检索到相关内容" />}
            {results.map((r, i) => (
              <div key={i} className="border border-ink-100 rounded-lg p-3">
                <div className="flex items-center gap-2 text-xs text-ink-400 mb-1">
                  <span className="font-medium text-ink-700">{r.report_title || r.institution || '研报片段'}</span>
                  {r.institution && <span>{r.institution}</span>}
                  <span>相似度 {r.similarity?.toFixed(2)}</span>
                  <span>新鲜度 {r.decay?.toFixed(2)}</span>
                  <span>综合 {r.score?.toFixed(2)}</span>
                </div>
                <div className="text-sm text-ink-800 leading-6">{r.content}</div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 研报库：左列表 1 / 右预览 2 */}
      <section className="card overflow-hidden">
        {/* 顶部工具栏 */}
        <div className="border-b border-ink-100 p-4 space-y-3">
          <div className="flex items-center gap-3">
            <input value={searchText} onChange={e => setSearchText(e.target.value)}
                   placeholder="搜索研报名称…"
                   className="max-w-md flex-1 px-3 py-2 rounded-lg border border-ink-200 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30" />
            <div className="ml-auto flex gap-2">
              <input ref={fileRef} type="file" multiple accept=".md,.txt,.json,.docx,.pdf"
                     className="hidden" onChange={e => onUpload(e.target.files)} />
              <button onClick={() => fileRef.current?.click()} disabled={uploading}
                      className="px-4 py-2 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-50">
                {uploading ? '上传中…' : '批量导入研报'}
              </button>
            </div>
          </div>
          {/* 状态条 */}
          {reports.length > 0 && (
            <div className={`px-3 py-2 rounded-lg border-l-4 text-sm flex items-center gap-2 ${
              processingCount > 0 ? 'bg-blue-50 border-accent text-accent' : 'bg-ink-50 border-ink-300 text-ink-600'
            }`}>
              {processingCount > 0
                ? <><span className="inline-block h-3 w-3 rounded-full border-2 border-accent/40 border-t-accent animate-spin" />正在处理 {processingCount} 个研报…</>
                : <>共 {reports.length} 个研报{failedCount > 0 && <span className="text-up">（失败 {failedCount}）</span>}</>}
            </div>
          )}
        </div>

        {/* 左右分栏：列表 1 / 预览 2 */}
        <div className="flex" style={{ height: 640 }}>
          {/* 左：列表 */}
          <div className="w-1/3 min-w-[280px] border-r border-ink-100 overflow-y-auto">
            {loadingList ? <Spinner /> : filtered.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center text-ink-300 gap-3">
                <Empty text="还没有研报" />
                <button onClick={() => fileRef.current?.click()} className="px-4 py-2 rounded-lg bg-accent text-white text-sm">上传研报</button>
              </div>
            ) : filtered.map(r => (
              <div key={String(r.id)} onClick={() => openDetail(r.id)}
                   className={`px-4 py-3 cursor-pointer border-b border-ink-50 hover:bg-ink-50 ${
                     selected?.id === r.id ? 'bg-accent-soft' : ''}`}>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium text-ink-900 truncate">{r.title}</span>
                  <div className="flex items-center gap-2 shrink-0">
                    {statusBadge(r.status)}
                    <button onClick={e => { e.stopPropagation(); onDelete(r.id) }}
                            className="text-xs text-ink-300 hover:text-up">删除</button>
                  </div>
                </div>
                <div className="mt-1 flex items-center gap-2 text-[11px] text-ink-400">
                  {r.institution && <span>{r.institution}</span>}
                  {r.report_type && <span>{TYPE_CN[r.report_type] || r.report_type}</span>}
                  <span>{(r.publish_time || '').slice(0, 10)}</span>
                  <span>{r.chunk_count} 块</span>
                </div>
              </div>
            ))}
          </div>

          {/* 右：原件预览 */}
          <div className="flex-1 bg-ink-50 overflow-hidden">
            {loadingDetail ? (
              <div className="h-full flex items-center justify-center"><Spinner label="加载研报…" /></div>
            ) : selected ? (
              <ReportViewer detail={selected} />
            ) : (
              <div className="h-full flex items-center justify-center text-ink-300"><Empty text="点击左侧研报查看原件" /></div>
            )}
          </div>
        </div>
      </section>
    </div>
  )
}

/** 右侧阅读器：PDF 原件用 iframe 直接渲染；其余格式显示提取文本 + AI 摘要 */
function ReportViewer({ detail }: { detail: ReportDetail & { file_ext?: string | null; has_file?: boolean } }) {
  const isPdf = detail.file_ext === 'pdf' && detail.has_file
  return (
    <div className="h-full flex flex-col">
      <div className="px-5 py-3 bg-white border-b border-ink-100">
        <div className="text-sm font-bold text-ink-900">{detail.title}</div>
        <div className="mt-0.5 flex items-center gap-3 text-xs text-ink-400">
          {detail.institution && <span>{detail.institution}</span>}
          {detail.analyst && <span>{detail.analyst}</span>}
          <span>{(detail.publish_time || '').slice(0, 10)}</span>
          <span>{detail.chunk_count} 块</span>
        </div>
      </div>
      <div className="flex-1 overflow-hidden">
        {isPdf ? (
          <iframe src={`/api/knowledge/reports/${detail.id}/file`} title={detail.title}
                  className="w-full h-full border-0" />
        ) : (
          <div className="h-full overflow-y-auto p-5 bg-white">
            {detail.summary && (
              <div className="mb-4 p-3 rounded-lg bg-accent-soft">
                <div className="text-xs font-semibold text-accent mb-1">AI 摘要</div>
                <div className="text-sm text-ink-800 leading-6">{detail.summary}</div>
              </div>
            )}
            <Markdown text={detail.raw_text || '（无正文）'} />
          </div>
        )}
      </div>
    </div>
  )
}
