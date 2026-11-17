import { useState } from 'react'
import { Database } from 'lucide-react'
import { queryLocalResearch } from '@/lib/api'
import type { FrontendExtension } from '@/extensions/types'

function ResearchPage() {
  const [symbol, setSymbol] = useState('600519.SH')
  const [kind, setKind] = useState('valuation')
  const [day, setDay] = useState(() => new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date(Date.now() - 86400000)))
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  async function run(refresh = false) {
    setBusy(true); setError(''); setResult(null)
    try { setResult(await queryLocalResearch({ symbol, kind, day, refresh })) }
    catch (err) { setError(err instanceof Error ? err.message : '查询失败') }
    finally { setBusy(false) }
  }
  const content = (result?.result as { content?: { type: string; text?: string }[] } | undefined)?.content
  const text = content?.filter(item => item.type === 'text').map(item => {
    try { return JSON.stringify(JSON.parse(item.text ?? ''), null, 2) }
    catch { return item.text }
  }).join('\n') ?? (result ? JSON.stringify(result.rows, null, 2) : '')
  return <div className="p-6 space-y-4">
    <h1 className="text-xl font-semibold">多源研究</h1>
    <p className="text-sm text-muted">华泰 MCP 提供估值、财报与主营构成；Tushare 提供龙虎榜。财报与主营查询最近一期，日期用于估值和龙虎榜。</p>
    <form onSubmit={event => { event.preventDefault(); void run() }} className="flex flex-wrap gap-3">
      <input aria-label="证券代码" className="bg-elevated border border-border rounded p-2" value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())} required pattern="[0-9]{6}\.(SH|SZ|BJ)" />
      <select aria-label="研究类型" className="bg-elevated border border-border rounded p-2" value={kind} onChange={e => setKind(e.target.value)}>
        <option value="valuation">华泰 · 估值</option><option value="financial">华泰 · 财务报表</option>
        <option value="business">华泰 · 主营构成</option><option value="dragon_tiger">Tushare · 龙虎榜</option>
      </select>
      <input aria-label="查询日期" type="date" className="bg-elevated border border-border rounded p-2" value={day} onChange={e => setDay(e.target.value)} required />
      <button disabled={busy} className="bg-elevated border border-border rounded px-4">{busy ? '查询中…' : '查询'}</button>
      <button type="button" disabled={busy} onClick={() => void run(true)} className="border border-border rounded px-4">刷新数据</button>
    </form>
    {error && <p role="alert" className="text-red-500">{error}</p>}
    {result && <div className="space-y-3">
      <p className="text-sm text-muted">来源：{String((result.source as { name?: string })?.name ?? '')} · 获取时间：{String(result.received_at ?? '')} · {String(result.cache_status ?? '')}</p>
      <pre className="overflow-auto whitespace-pre-wrap rounded border border-border bg-elevated p-4 text-sm">{text}</pre>
    </div>}
  </div>
}

const extension: FrontendExtension = {
  id: 'local.research', apiVersion: 1,
  routes: [{ id: 'local-research', path: '/local-research', component: ResearchPage }],
  navigation: [{ id: 'local-research', routeId: 'local-research', label: '多源研究', icon: Database }],
}
export default extension
