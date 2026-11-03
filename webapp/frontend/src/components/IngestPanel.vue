<template>
  <section class="card pane ingest">
    <h3>
      入库情况
      <span class="hint">数字 = 数据库真实落地 / 总数（红色 = 状态标记声称完成但数据未落地）· 生成于 {{ data?.generated_at || '—' }}</span>
      <span class="grow"></span>
      <button class="mini" :disabled="loading" @click="load">刷新</button>
    </h3>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="error" class="state err">{{ error }}</div>

    <template v-else-if="data">
      <!-- 模型能力探活（入库能跑到哪一步，由这里决定） -->
      <div class="caps">
        <span class="caps-label">模型能力</span>
        <span class="cap" v-for="c in capList" :key="c.key" :title="c.endpoint || c.error">
          <span class="dot" :class="c.ok ? 'ok' : 'bad'"></span>
          {{ c.label }}
          <i v-if="c.ok">{{ c.latency }}ms</i>
          <i v-else class="err">不可达</i>
        </span>
        <span class="grow"></span>
        <button class="mini" :disabled="probing" @click="probe(true)">
          {{ probing ? '检查中…' : '重新检查' }}
        </button>
      </div>

      <!-- 8 阶段进度 -->
      <div class="stages">
        <div class="stage" v-for="s in data.stages" :key="s.key">
          <div class="srow">
            <span class="sl">{{ s.label }}</span>
            <span class="sn" :class="{ bad: !s.consistent }">
              {{ s.data_done }}<i>/{{ s.total }}</i>
            </span>
          </div>
          <span class="track">
            <i :class="{ bad: !s.consistent, full: s.data_done >= s.total }"
               :style="{ width: pct(s.data_done, s.total) + '%' }"></i>
          </span>
          <div class="snote">
            <span>{{ s.basis }}</span>
            <span v-if="!s.consistent" class="flagdiff">标记 {{ s.flag_done }}</span>
          </div>
        </div>
      </div>

      <!-- 对账：标记 / 数据 / 文件 -->
      <table class="tbl">
        <thead>
          <tr>
            <th class="w20"></th>
            <th>检查项</th>
            <th class="w130">标记 / 数据 / 文件</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in data.checks" :key="c.name">
            <td><span class="dot" :class="c.ok ? 'ok' : 'bad'" :title="c.ok ? '一致' : '有缺口'"></span></td>
            <td class="nowrap">{{ c.name }}</td>
            <td class="mono">{{ c.flag }} / {{ c.data }} / {{ c.files }}</td>
            <td class="note">{{ c.note }}</td>
          </tr>
        </tbody>
      </table>

      <!-- 缺口筛选 + 修复入口 -->
      <div class="gaps">
        <button v-for="g in gapList" :key="g.key" class="chip-btn"
                :class="{ on: issue === g.key, zero: !g.n }"
                :disabled="!g.n" @click="pick(g.key)">
          {{ g.label }} <b>{{ g.n }}</b>
        </button>
        <span class="grow"></span>
        <button class="mini act" :disabled="busy || !data.gaps.summary_col_empty"
                @click="repair">
          {{ busy ? '处理中…' : '回填摘要列' }}
        </button>
      </div>

      <!-- 按能力补齐（先探活，能力不够的按钮置灰） -->
      <div class="fills">
        <span class="caps-label">按能力补齐</span>
        <button v-for="f in fillActions" :key="f.action" class="mini"
                :class="{ act: f.ok }"
                :disabled="!f.ok || filling === f.action"
                :title="f.ok ? '把缺失的补齐（已存在的会跳过）' : '不可用：' + f.reason"
                @click="runFill(f)">
          {{ filling === f.action ? '启动中…' : f.label }}
          <span v-if="!f.ok" class="off">不可用</span>
        </button>
      </div>

      <div v-if="msg" class="msg">{{ msg }}</div>

      <!-- 明细清单 -->
      <div v-if="issue && items" class="list">
        <div class="list-head">
          <span>{{ items.issue_label }} · 共 {{ items.total }} 条</span>
          <span class="grow"></span>
          <button class="mini" :disabled="page <= 1" @click="loadItems(issue, page - 1)">上一页</button>
          <span class="mono pageno">{{ page }} / {{ Math.max(1, Math.ceil(items.total / size)) }}</span>
          <button class="mini" :disabled="page * size >= items.total" @click="loadItems(issue, page + 1)">下一页</button>
        </div>
        <table class="tbl">
          <tbody>
            <tr v-for="it in items.items" :key="it.id">
              <td class="mono w60">{{ it.id }}</td>
              <td class="title">{{ it.title }}</td>
              <td class="mono w60">{{ it.year || '—' }}</td>
              <td class="w90">
                <span class="tag" :class="it.has_summary_file ? 'ok' : 'bad'">
                  {{ it.has_summary_file ? '有摘要文件' : '无文件' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api.js'

const GAP_LABEL = {
  metadata_incomplete: '元数据不全',
  hypergraph_missing: '缺实体/超边',
  entity_missing: '缺实体',
  summary_col_empty: '有摘要文件但列空',
  summary_file_missing: '标记完成但无摘要文件',
  extract_missing: '缺解析产物',
  normalize_missing: '缺规范化产物',
  pdf_missing: '缺 PDF 原件',
}

const data = ref(null)
const items = ref(null)
const issue = ref('')
const page = ref(1)
const size = 30
const loading = ref(true)
const error = ref('')
const msg = ref('')
const busy = ref(false)
const health = ref(null)
const probing = ref(false)
const filling = ref('')

const CAP_LABEL = {
  llm: 'LLM（规范化 / 摘要 / 超边）',
  judge: '决策模型（实体合并）',
  embedding: '向量模型（embedding）',
}

const capList = computed(() => {
  const c = health.value?.capabilities || {}
  return Object.keys(CAP_LABEL).map((k) => ({
    key: k,
    label: CAP_LABEL[k],
    ok: !!c[k]?.ok,
    latency: c[k]?.latency_ms ?? 0,
    error: c[k]?.error || '不可达',
    endpoint: c[k]?.endpoint || '',
  }))
})

const FILL_META = [
  { action: 'summary', stage: 'summary', label: '摘要' },
  { action: 'hypergraph', stage: 'hypergraph', label: '实体/超边' },
  { action: 'embedding', stage: 'embedding', label: '向量' },
]

const fillActions = computed(() =>
  FILL_META.map((f) => {
    const st = (health.value?.stages || []).find((s) => s.key === f.stage)
    const blocked = st?.blocked_by || []
    return { ...f, ok: !!st?.ok, reason: blocked.length ? blocked.join(' / ') + ' 不可达' : '' }
  })
)

const gapList = computed(() =>
  Object.keys(GAP_LABEL).map((k) => ({
    key: k,
    label: GAP_LABEL[k],
    n: data.value?.gaps?.[k] ?? 0,
  }))
)

const pct = (a, b) => (b ? Math.round((a / b) * 1000) / 10 : 0)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.ingestOverview()
  } catch (e) {
    error.value = `加载失败：${e.message}`
  }
  loading.value = false
}

async function loadItems(k, p = 1) {
  issue.value = k
  page.value = p
  try {
    items.value = await api.ingestItems({ issue: k, page: p, size })
  } catch (e) {
    msg.value = `清单加载失败：${e.message}`
  }
}

function pick(k) {
  if (issue.value === k) {
    issue.value = ''
    items.value = null
    return
  }
  loadItems(k, 1)
}

async function repair() {
  msg.value = ''
  busy.value = true
  try {
    const dry = await api.ingestRepair({ action: 'backfill_summary', dry_run: true })
    if (!dry.would_update) {
      msg.value = '没有需要回填的记录。'
      return
    }
    const ok = window.confirm(
      `将把 ${dry.would_update} 条摘要回填到 papers.summary。\n\n` +
      '· 只填空值，已有内容一律跳过（可重复执行）\n' +
      '· 不调用任何大模型，不产生费用\n' +
      '· 回填后请再点一次「刷新」核对\n\n继续？'
    )
    if (!ok) {
      msg.value = '已取消，未改动任何数据。'
      return
    }
    const r = await api.ingestRepair({ action: 'backfill_summary', dry_run: false })
    msg.value = `已回填 ${r.updated} 条摘要。`
    await load()
    if (issue.value) await loadItems(issue.value, 1)
  } catch (e) {
    msg.value = `回填失败：${e.message}`
  } finally {
    busy.value = false
  }
}

async function probe(refresh = false) {
  probing.value = true
  try {
    health.value = await api.modelsHealth(refresh)
  } catch (e) {
    msg.value = `模型探活失败：${e.message}`
  }
  probing.value = false
}

async function runFill(f) {
  filling.value = f.action
  msg.value = ''
  try {
    const dry = await api.ingestFill(f.action, true)
    if (!dry.ok) {
      msg.value = `「${f.label}」不可用：${dry.reason || '模型不可达'}`
      return
    }
    const ok = window.confirm(
      `开始补齐「${f.label}」？\n\n` +
      '· 只处理缺失的部分，已存在的一律跳过\n' +
      '· 后台运行，可以关闭页面\n' +
      '· 完成后点「刷新」核对数字'
    )
    if (!ok) {
      msg.value = '已取消。'
      return
    }
    const r = await api.ingestFill(f.action, false)
    msg.value = r.started
      ? `「${f.label}」补齐任务已启动（${r.job_id}），跑完点刷新`
      : `未启动：${r.reason}`
  } catch (e) {
    msg.value = `补齐失败：${e.message}`
  } finally {
    filling.value = ''
  }
}

onMounted(() => {
  load()
  probe()
})
</script>

<style scoped>
.ingest { margin-top: 14px; }
.grow { flex: 1; }

.stages {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px 16px;
  margin-bottom: 16px;
}
.stage .srow { display: flex; align-items: baseline; justify-content: space-between; }
.stage .sl { font-size: 12.5px; color: var(--text-dim); }
.stage .sn { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--text); }
.stage .sn i { font-style: normal; color: var(--text-faint); font-size: 11px; }
.stage .track {
  display: block; height: 6px; margin-top: 5px; border-radius: 4px;
  background: var(--bg-hover); overflow: hidden;
}
.stage .track i {
  display: block; height: 100%; border-radius: 4px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  transition: width 0.3s;
}
.snote {
  display: flex; justify-content: space-between; gap: 6px;
  margin-top: 3px; font-size: 10.5px; color: var(--text-faint);
}
.stage .sn.bad { color: #d9534f; }
.stage .track i.bad { background: #d9534f; }
.flagdiff { color: #d9534f; }

.tbl { width: 100%; border-collapse: collapse; font-size: 12.8px; }
.tbl th {
  text-align: left; font-weight: 500; color: var(--text-faint);
  padding: 6px 8px; border-bottom: 1px solid var(--line);
}
.tbl td { padding: 6px 8px; border-bottom: 1px solid var(--line); color: var(--text-dim); }
.tbl tr:last-child td { border-bottom: none; }
.mono { font-variant-numeric: tabular-nums; font-family: var(--font-mono, monospace); }
.nowrap { white-space: nowrap; color: var(--text); }
.note { color: var(--text-faint); }
.title { color: var(--text); max-width: 620px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.w20 { width: 20px; }
.w60 { width: 60px; }
.w90 { width: 96px; }
.w130 { width: 130px; }

.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
.dot.ok { background: #4caf7d; }
.dot.bad { background: #d9534f; }

.caps, .fills {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  margin: 4px 0 12px; padding: 8px 11px; border-radius: 8px;
  background: var(--bg-hover); border: 1px solid var(--line);
}
.caps-label { font-size: 12px; color: var(--text-faint); }
.cap { font-size: 12.5px; color: var(--text-dim); display: inline-flex; align-items: center; gap: 5px; }
.cap i { font-style: normal; font-size: 11px; color: var(--text-faint); }
.cap i.err { color: #d9534f; }
.off { margin-left: 5px; font-size: 11px; color: var(--text-faint); }

.gaps { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 14px 0 6px; }
.chip-btn {
  font-size: 12.5px; padding: 5px 11px; border-radius: 14px; cursor: pointer;
  background: var(--bg-hover); color: var(--text-dim);
  border: 1px solid var(--line);
}
.chip-btn b { font-weight: 600; color: var(--accent); margin-left: 4px; }
.chip-btn.on { border-color: var(--accent); color: var(--text); }
.chip-btn.zero { opacity: 0.45; cursor: default; }

.mini {
  font-size: 12.5px; padding: 5px 11px; border-radius: 6px; cursor: pointer;
  background: var(--bg-hover); color: var(--text-dim); border: 1px solid var(--line);
}
.mini:disabled { opacity: 0.45; cursor: default; }
.mini.act { border-color: var(--accent); color: var(--text); }

.msg { margin: 10px 0; font-size: 12.8px; color: var(--accent); }

.list { margin-top: 12px; }
.list-head { display: flex; align-items: center; gap: 8px; font-size: 12.8px; color: var(--text-dim); margin-bottom: 6px; }
.pageno { color: var(--text-faint); }

.tag { font-size: 11.5px; padding: 2px 7px; border-radius: 10px; }
.tag.ok { background: rgba(76, 175, 125, 0.16); color: #4caf7d; }
.tag.bad { background: rgba(217, 83, 79, 0.16); color: #d9534f; }

.state { padding: 30px; text-align: center; color: var(--text-dim); }
.state.err { color: var(--warn); }

@media (max-width: 900px) {
  .stages { grid-template-columns: repeat(2, 1fr); }
}
</style>
