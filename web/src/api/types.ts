export interface IndexPoint { date: string; close: number }
export interface IndexCard {
  code: string; name: string; market: string
  close: number | null; change_pct: number | null; trade_date: string
  history: IndexPoint[]
}

export interface HotSector {
  sector: string; heat: number | null; sort_score: number | null
  continuous_capital_score: number | null; continuous_leader_score: number | null
  history: { date: string; heat: number | null }[]
}
export interface OversoldSector {
  sector: string; price_change_pct: number | null; heat: number | null; turnover_amount: number | null
}

export interface ReportDay {
  found: boolean; date: string | null; summary: string; confidence: number | null; created_at: string
  raw: unknown
}

export interface HeatmapData { sectors: string[]; dates: string[]; matrix: { sector: string; values: (number | null)[] }[] }

export interface Opinion {
  target_name: string; net_score: number; direction: string
  bullish_weight: number; bearish_weight: number; opinion_count: number
  sources: string[]; top_reason: string
}

export interface SummaryResp { found: boolean; date: string | null; summary: string; confidence: number | null }

export interface AgentStatus {
  agent: string; label: string; found?: boolean
  trade_date?: string | null; created_at?: string | null
  confidence?: number | null; status?: string | null; summary_preview?: string
  last_run?: string | null
}

export interface ReportItem {
  id: number | string; title: string; institution: string | null; analyst?: string | null
  report_type: string | null; publish_time: string | null; industries: string[]
  chunk_count: number; created_at: string | null; status?: string
}

export interface ReportDetail extends ReportItem {
  summary: string | null; raw_text: string | null
}

export interface ChatMessage { role: string; content: string; created_at?: string }

export interface Holding {
  id: number; holding_type: string; name: string; code: string | null
  cost_price: number | null; position_pct: number | null; shares: number | null; note: string | null
}
