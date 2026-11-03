async function j(url, opts) {
  const r = await fetch(url, opts)
  if (!r.ok) {
    const body = await r.text().catch(() => '')
    throw new Error(`${r.status} ${body.slice(0, 300)}`)
  }
  return r.json()
}

export const api = {
  stats: () => j('/api/stats'),
  facets: () => j('/api/facets'),
  insights: () => j('/api/insights'),
  // 入库情况管理面板（2026-11-02）
  ingestOverview: () => j('/api/ingest/overview'),
  ingestItems: (params) => j('/api/ingest/items?' + new URLSearchParams(params)),
  ingestRepair: (body) =>
    j('/api/ingest/repair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  // 模型能力探活 + 按能力补齐（2026-11-02）
  modelsHealth: (refresh) => j('/api/models/health' + (refresh ? '?refresh=true' : '')),
  ingestFill: (action, dryRun) =>
    j('/api/ingest/fill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, dry_run: !dryRun }),
    }),
  papers: (params) => j('/api/papers?' + new URLSearchParams(params)),
  favorite: (id) => j(`/api/papers/${id}/favorite`, { method: 'POST' }),
  // 多收藏夹（2026-10-27）
  collections: () => j('/api/collections'),
  collectionCreate: (name) =>
    j('/api/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  collectionRename: (id, name) =>
    j(`/api/collections/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  collectionDelete: (id) => j(`/api/collections/${id}`, { method: 'DELETE' }),
  setPaperCollections: (pid, ids) =>
    j(`/api/papers/${pid}/collections`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    }),
  movePapers: (fromId, paperIds, toId) =>
    j(`/api/collections/${fromId}/move`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paper_ids: paperIds, to_collection_id: toId }),
    }),
  paper: (id) => j(`/api/papers/${id}`),
  section: (pid, sid) => j(`/api/papers/${pid}/section/${sid}`),
  paperChat: (pid, message, history) =>
    j(`/api/papers/${pid}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, history }),
    }),
  ask: (question, paperId) =>
    j('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, paper_id: paperId ?? null }),
    }),
  // 图书管理员智能体（threads CRUD；对话走 SSE，见 AgentPanel 内 fetch 流解析）
  agentThreads: () => j('/api/agent/threads'),
  agentThread: (id) => j(`/api/agent/threads/${id}`),
  agentThreadDelete: (id) => j(`/api/agent/threads/${id}`, { method: 'DELETE' }),
  agentThreadArchive: (id) => j(`/api/agent/threads/${id}/archive`, { method: 'POST' }),
  // 导入 PDF + 入库进度（2026-10-13）
  importPdf: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return j('/api/import', { method: 'POST', body: fd })
  },
  importConfirm: (jobId) => j(`/api/import/${jobId}/confirm`, { method: 'POST' }),
  ingestJobs: () => j('/api/ingest/jobs'),
  ingestStatus: (pid) => j(`/api/ingest/status/${pid}`),
}
