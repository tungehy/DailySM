import React from 'react'

/** 行内渲染：加粗 / 行内代码 */
function inline(s: string): React.ReactNode {
  const parts = s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**'))
      return <strong key={i} className="font-semibold text-ink-900">{p.slice(2, -2)}</strong>
    if (p.startsWith('`') && p.endsWith('`'))
      return <code key={i} className="px-1 py-0.5 rounded bg-ink-100 text-[0.9em] font-mono">{p.slice(1, -1)}</code>
    return <React.Fragment key={i}>{p}</React.Fragment>
  })
}

function isTableDivider(line: string): boolean {
  return /^\s*\|?[\s:|-]+\|?\s*$/.test(line) && line.includes('-')
}

function splitRow(line: string): string[] {
  let s = line.trim()
  if (s.startsWith('|')) s = s.slice(1)
  if (s.endsWith('|')) s = s.slice(0, -1)
  return s.split('|').map(c => c.trim())
}

/** 极简 markdown 渲染：标题/加粗/列表/表格/引用，够用于 AI 报告 */
export function Markdown({ text }: { text: string }) {
  if (!text) return null
  const lines = text.split(/\r?\n/)
  const out: React.ReactNode[] = []
  let list: React.ReactNode[] | null = null
  let key = 0
  let i = 0

  const flushList = () => {
    if (list) { out.push(<ul key={key++} className="my-2 list-disc pl-5 space-y-1">{list}</ul>); list = null }
  }

  while (i < lines.length) {
    const line = lines[i].trimEnd()

    // ---- 表格：| a | b |\n| --- | --- |\n| .. | ----
    if (line.trim().startsWith('|') && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
      flushList()
      const header = splitRow(line)
      const rows: string[][] = []
      let j = i + 2
      while (j < lines.length && lines[j].trim().startsWith('|')) {
        rows.push(splitRow(lines[j]))
        j++
      }
      out.push(
        <div key={key++} className="my-3 overflow-x-auto rounded-lg border border-ink-200">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="bg-ink-100">
                {header.map((h, ci) => (
                  <th key={ci} className="px-3 py-2 text-left font-semibold text-ink-800 border-b border-ink-200 whitespace-nowrap">{inline(h)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri} className={ri % 2 === 0 ? 'bg-white' : 'bg-ink-50'}>
                  {header.map((_, ci) => (
                    <td key={ci} className="px-3 py-2 text-ink-700 border-b border-ink-100 align-top">{inline(r[ci] ?? '')}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
      i = j
      continue
    }

    // ---- 列表
    if (/^\s*[-*]\s+/.test(line)) {
      list = list ?? []
      list.push(<li key={key++}>{inline(line.replace(/^\s*[-*]\s+/, ''))}</li>)
      i++; continue
    }
    flushList()

    if (!line.trim()) { out.push(<div key={key++} className="h-2" />); i++; continue }

    const h = line.match(/^(#{1,4})\s+(.*)/)
    if (h) {
      const level = h[1].length
      const cls = level === 1 ? 'text-lg mt-5 mb-2' : level === 2 ? 'text-base mt-4 mb-2' : 'text-sm mt-3 mb-1'
      out.push(<div key={key++} className={`font-bold text-ink-900 ${cls}`}>{inline(h[2])}</div>)
      i++; continue
    }
    if (/^---+$/.test(line)) { out.push(<hr key={key++} className="my-4 border-ink-200" />); i++; continue }
    if (/^>\s?/.test(line)) {
      out.push(<div key={key++} className="my-2 border-l-4 border-ink-200 pl-3 text-ink-600 italic">{inline(line.replace(/^>\s?/, ''))}</div>)
      i++; continue
    }
    out.push(<p key={key++} className="my-2 leading-6 text-ink-800">{inline(line)}</p>)
    i++
  }
  flushList()
  return <div className="md-body">{out}</div>
}
