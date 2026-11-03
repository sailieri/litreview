<template>
  <div class="reader" v-if="detail">
    <!-- 左侧可收缩侧边栏：上=导航，下=详细信息 -->
    <aside class="side" :class="{ closed: sideClosed }">
      <div class="side-inner">
        <div class="side-top">
          <button class="rail-btn" @click="toggleSide" title="收起 / 展开侧栏">≡</button>
          <router-link class="back" :to="{ name: 'list' }" title="返回文献库">
            <span class="ic">←</span><span class="lb">文献库</span>
          </router-link>
        </div>

        <nav class="snav">
          <button
            v-for="t in tabs" :key="t.id"
            class="snav-item" :class="{ on: tab === t.id }"
            :title="t.label"
            @click="setTab(t.id)"
          >
            <span class="ic">{{ t.icon }}</span><span class="lb">{{ t.label }}</span>
          </button>
        </nav>

        <div class="side-tools">
          <button
            class="snav-item" :class="{ on: tab === 'bilingual' }"
            title="中英对照（中文译文）"
            @click="setTab('bilingual')"
          >
            <span class="ic">译</span><span class="lb">中英对照</span>
          </button>
          <button class="snav-item" title="切换主题" @click="toggleTheme">
            <span class="ic">◐</span><span class="lb">主题</span>
          </button>
        </div>

        <div class="side-detail">
          <div class="pane-label lb">详细信息</div>
          <div class="d-id dim">ID <b>#{{ pid }}</b></div>
          <div class="d-title" :title="p.title || detail.stem">{{ p.title || detail.stem }}</div>
          <div class="d-authors dim">{{ p.authors || '作者未知' }}</div>
          <div class="d-meta dim">
            <template v-if="p.year">{{ p.year }}</template>
            <template v-if="p.year && p.journal"> · </template>
            <template v-if="p.journal">{{ p.journal }}</template>
          </div>
          <div class="chips d-chips">
            <span class="chip" v-if="p.entity_count">{{ p.entity_count }} 实体</span>
            <span class="chip" v-if="p.hyperedge_count">{{ p.hyperedge_count }} 超边</span>
            <span class="chip plain">{{ detail.sections.length }} 章节</span>
            <span class="chip plain" v-if="!detail.pdf.available">无 PDF</span>
          </div>
          <a
            v-if="p.doi" class="d-doi dim"
            :href="`https://doi.org/${p.doi}`" target="_blank"
          >DOI: {{ p.doi }}</a>
          <div class="chips d-kw" v-if="keywords.length">
            <span class="chip plain" v-for="k in keywords" :key="k">{{ k }}</span>
          </div>
          <button
            class="d-agent" :class="{ on: agent.paperIds.includes(pid) }"
            @click="addToAgent"
            :title="agent.paperIds.includes(pid)
              ? '已在智能体工作区（再点一下：标记为本轮先读）'
              : '加入图书管理员工作区：下一轮提问时它会先读这篇论文'"
          >🤖 {{ agent.paperIds.includes(pid) ? '已在智能体工作区' : '加入智能体' }}</button>
        </div>

        <!-- 入库状态（读 ingest_status 表，2026-10-13）-->
        <div class="side-status">
          <div class="pane-label lb">入库状态</div>
          <template v-if="ingest">
            <div class="st-bar"><i :style="{ width: (ingest.percent || 0) + '%' }"></i></div>
            <div class="st-count dim">
              {{ ingest.done_count }} / {{ ingest.total }} 阶段完成（{{ ingest.percent }}%）
            </div>
            <ul class="st-list">
              <li v-for="s in ingest.stages" :key="s.key" :class="{ on: s.done }">
                <span class="dot"></span>{{ s.label }}
              </li>
            </ul>
            <div class="chips d-chips">
              <span class="chip" v-if="ingest.entity_count">{{ ingest.entity_count }} 实体</span>
              <span class="chip" v-if="ingest.hyperedge_count">{{ ingest.hyperedge_count }} 超边</span>
            </div>
            <div class="st-time dim" v-if="ingest.updated_at">更新于 {{ ingest.updated_at }}</div>
          </template>
          <div v-else class="dim st-none">暂无入库状态记录</div>
        </div>
      </div>
    </aside>

    <!-- 主区：PDF + 标签内容（顶部留白统一） -->
    <div class="rmain">
      <div class="rbody" :class="{ split: detail.pdf.available }">
        <PdfPane
          v-if="detail.pdf.available" :pid="pid" class="left"
          :style="{ flex: `0 0 ${leftPct}%`, width: `${leftPct}%` }"
        />
        <div
          v-if="detail.pdf.available" class="splitter"
          title="拖动调整左右宽度（双击复位）"
          @mousedown.prevent="startDrag"
          @dblclick="resetSplit"
        ></div>
        <section class="right">
          <!-- 摘要 -->
          <div v-if="tab === 'summary'" class="pane">
            <div class="card pad">
              <div class="pane-label">中文叙事摘要</div>
              <p class="narrative" v-if="detail.narrative">{{ detail.narrative }}</p>
              <p class="dim" v-else>本篇暂无叙事摘要（summaries 缺文件）。</p>
            </div>

            <div class="card pad" v-if="keywords.length">
              <div class="pane-label">关键词</div>
              <div class="chips">
                <span class="chip plain" v-for="k in keywords" :key="k">{{ k }}</span>
              </div>
            </div>

            <div class="card pad">
              <div class="pane-label">元数据</div>
              <table class="kv">
                <tr><td class="dim">题录</td><td>{{ p.title || '—' }}</td></tr>
                <tr><td class="dim">作者</td><td>{{ p.authors || '—' }}</td></tr>
                <tr><td class="dim">年份</td><td>{{ p.year || '—' }}</td></tr>
                <tr><td class="dim">期刊</td><td>{{ p.journal || '—' }}</td></tr>
                <tr><td class="dim">DOI</td><td>{{ p.doi || '—' }}</td></tr>
                <tr><td class="dim">库内 stem</td><td class="mono">{{ detail.stem }}</td></tr>
              </table>
            </div>
          </div>

          <!-- 正文（章节树 + 阅读区） -->
          <div v-else-if="tab === 'sections'" class="pane split2">
            <aside class="outline card">
              <button
                class="sec" v-for="s in flatOutline" :key="s.id"
                :class="{ on: curSection === s.id }"
                :style="{ paddingLeft: 10 + s.depth * 14 + 'px' }"
                @click="openSection(s.id)"
              >
                <span class="s-t">{{ s.title }}</span>
                <span class="s-c faint">{{ Math.round((s.char_count || 0) / 100) / 10 }}k</span>
              </button>
            </aside>
            <article class="content card pad">
              <div v-if="secLoading" class="state"><span class="spinner"></span></div>
              <div v-else-if="secError" class="state err">{{ secError }}</div>
              <template v-else-if="curSec">
                <h2 class="sec-title">{{ curSec.title }}</h2>
                <div class="prose body-text">{{ curSec.content }}</div>
              </template>
              <div v-else class="state dim">← 点击左侧章节开始阅读</div>
            </article>
          </div>

          <!-- 实体（原超图左栏，独立成 tab） -->
          <div v-else-if="tab === 'entities'" class="pane graph-split solo">
            <div class="gcol ent-col solo-col">
              <div class="pane-label">实体（{{ detail.entities.length }}）</div>
              <div v-if="!detail.entities.length" class="state dim">本篇暂无实体</div>
              <div class="ent card pad" v-for="e in detail.entities" :key="e.id">
                <div class="ent-head">
                  <span class="ent-name" :title="e.merged ? e.merged.merged_name : e.name">
                    {{ e.merged ? e.merged.merged_name : e.name }}
                  </span>
                  <span
                    class="chip merged" v-if="e.merged"
                    :title="`已归入公共实体「${e.merged.merged_name}」，共 ${e.merged.group_size} 个源实体`"
                  >已合并 {{ e.merged.group_size }}</span>
                  <span class="chip ent-type" v-if="e.type">{{ e.type }}</span>
                </div>
                <div class="ent-alias" v-if="e.merged && e.merged.aliases.length">
                  别名：{{ e.merged.aliases.slice(0, 6).join(' · ') }}
                </div>
                <p class="ent-desc">{{ e.description || '—' }}</p>
              </div>
            </div>
          </div>

          <!-- 超边（原超图右栏，独立成 tab） -->
          <div v-else-if="tab === 'edges'" class="pane graph-split solo">
            <div class="gcol he-col solo-col">
              <div class="pane-label">超边（{{ detail.hyperedges.length }}）</div>
              <div v-if="!detail.hyperedges.length" class="state dim">本篇暂无超边</div>
              <div class="he card pad" v-for="h in detail.hyperedges" :key="h.id">
                <div class="chips">
                  <span
                    class="chip" v-for="pt in h.participants" :key="pt.id"
                    :title="pt.description || ''"
                  >{{ pt.name }}<em class="etype">{{ pt.type }}</em></span>
                </div>
                <p class="he-desc">{{ h.description }}</p>
              </div>
            </div>
          </div>

          <!-- 中英对照：未翻译→按钮；翻译中→节点进度；完成→内嵌译文 -->
          <BilingualPane v-else-if="tab === 'bilingual'" :pid="pid" />

        </section>
      </div>
    </div>
  </div>

  <div v-else class="whole">
    <div v-if="error" class="state err">{{ error }}</div>
    <div v-else class="state"><span class="spinner"></span> 载入论文…</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'
import { toggleTheme } from '../theme.js'
import PdfPane from '../components/PdfPane.vue'
import BilingualPane from '../components/BilingualPane.vue'
import { agentStore as agent } from '../agentStore.js'

const route = useRoute()
const router = useRouter()

const tabs = [
  { id: 'summary', label: '摘要', icon: '摘' },
  { id: 'sections', label: '正文', icon: '文' },
  { id: 'entities', label: '实体', icon: '实' },
  { id: 'edges', label: '超边', icon: '边' },
]
// 分隔区 tab（与主题按钮同列）：中英对照
const TOOL_TABS = ['bilingual']
const isTab = (id) => tabs.some((t) => t.id === id) || TOOL_TABS.includes(id)
const normTab = (id) => (id === 'graph' ? 'entities' : id)   // 旧书签：超图→实体

const detail = ref(null)
const error = ref('')
const tab = ref('summary')
const ingest = ref(null)        // 入库状态（GET /api/ingest/status/{pid}）

// 左右分栏宽度（拖动分隔条，持久化）
const leftPct = ref(Math.min(75, Math.max(25,
  Number(localStorage.getItem('litread.split')) || 54)))
let dragging = false
function startDrag(e) {
  dragging = true
  const rect = e.currentTarget.parentElement.getBoundingClientRect()
  const move = (ev) => {
    if (!dragging) return
    let pct = ((ev.clientX - rect.left) / rect.width) * 100
    pct = Math.min(75, Math.max(25, pct))
    leftPct.value = Math.round(pct * 10) / 10
  }
  const up = () => {
    dragging = false
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
    localStorage.setItem('litread.split', String(leftPct.value))
  }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
}
function resetSplit() {
  leftPct.value = 54
  localStorage.setItem('litread.split', '54')
}

/** 加入智能体工作区：下一轮提问时图书管理员会先读这篇（读过后保留显示、不再强读） */
function addToAgent() {
  if (!agent.addPaper(pid.value)) {
    alert('智能体工作区最多 12 篇，请先在抽屉里移出一些')
    return
  }
  agent.openPanel()
}
const pid = computed(() => Number(route.params.id))
const p = computed(() => detail.value?.paper || {})

// 侧栏收合（持久化）
const sideClosed = ref(localStorage.getItem('litread.side') === '1')
function toggleSide() {
  sideClosed.value = !sideClosed.value
  localStorage.setItem('litread.side', sideClosed.value ? '1' : '0')
}

const keywords = computed(() => {
  const k = detail.value?.keywords
  if (Array.isArray(k)) return k
  if (typeof k === 'string' && k.trim()) return k.split(/[,;]\s*/)
  return []
})

// ── 章节树（parent_id 优先，缺失时按 level 回挂） ─────────
const flatOutline = computed(() => {
  const secs = detail.value?.sections || []
  const byId = {}
  secs.forEach((s) => (byId[s.id] = { ...s, children: [], depth: 0 }))
  const roots = []
  const lastAtLevel = {}
  for (const s of secs) {
    const node = byId[s.id]
    let parent = null
    // parent_id 仅在与 level 一致时可信（历史数据有脏 parent：指向兄弟/自指，
    // 2026-09-29 已修库，这里保留防御防回潮）
    if (s.parent_id && byId[s.parent_id] && s.parent_id !== s.id
        && byId[s.parent_id].level === s.level - 1) {
      parent = byId[s.parent_id]
    } else if (s.level > 1) {
      for (let l = s.level - 1; l >= 1; l--) {
        if (lastAtLevel[l]) { parent = lastAtLevel[l]; break }
      }
    }
    if (parent) parent.children.push(node)
    else roots.push(node)
    lastAtLevel[s.level] = node
    for (let l = s.level + 1; l < 12; l++) delete lastAtLevel[l]
  }
  const flat = []
  const walk = (n, depth) => {
    n.depth = depth
    flat.push(n)
    n.children.forEach((c) => walk(c, depth + 1))
  }
  roots.forEach((r) => walk(r, 0))
  return flat
})

// ── 章节内容（按需拉取 + 缓存） ───────────────────────────
const curSection = ref(null)
const curSec = ref(null)
const secLoading = ref(false)
const secError = ref('')
const secCache = new Map()

async function openSection(sid) {
  curSection.value = sid
  secError.value = ''
  if (secCache.has(sid)) {
    curSec.value = secCache.get(sid)
    return
  }
  secLoading.value = true
  try {
    const d = await api.section(pid.value, sid)
    secCache.set(sid, d)
    curSec.value = d
  } catch (e) {
    secError.value = `章节载入失败：${e.message}`
  } finally {
    secLoading.value = false
  }
}

function setTab(id) {
  tab.value = id
  router.replace({ query: { ...route.query, tab: id } })
}

async function load() {
  detail.value = null
  error.value = ''
  curSection.value = null
  curSec.value = null
  secCache.clear()
  const tq = normTab(String(route.query.tab || ''))
  tab.value = (tq && isTab(tq)) ? tq : 'summary'
  try {
    detail.value = await api.paper(pid.value)
    // 默认打开第一个正文章节，读起来不用多点一下
    if (tab.value === 'sections' && detail.value.sections.length) {
      openSection(detail.value.sections[0].id)
    }
  } catch (e) {
    error.value = `载入失败：${e.message}`
  }
  loadIngest()
}

async function loadIngest() {
  ingest.value = null
  try {
    ingest.value = await api.ingestStatus(pid.value)
  } catch {
    ingest.value = null      // 无记录（404）或未入库
  }
}

onMounted(load)
watch(() => route.params.id, load)
watch(() => route.query.tab, (v) => {
  const n = normTab(String(v || ''))
  if (n && n !== tab.value && isTab(n)) tab.value = n
})
watch(tab, (v) => {
  if (v === 'sections' && !curSection.value && detail.value?.sections?.length) {
    openSection(detail.value.sections[0].id)
  }
})
</script>

<style scoped>
.reader { display: flex; height: 100%; }

/* ── 左侧可收缩侧边栏 ── */
.side {
  flex: none;
  width: 248px;
  border-right: 1px solid var(--line);
  background: var(--bg-panel);
  overflow: hidden;
  transition: width var(--t-med);
}
.side.closed { width: 56px; }
.side-inner {
  width: 248px;
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 12px 10px 14px;
  gap: 10px;
}
.side-top { display: flex; align-items: center; gap: 8px; }
.rail-btn {
  flex: none;
  width: 30px; height: 30px;
  border-radius: var(--r-sm);
  font-size: 16px;
  color: var(--text-dim);
  transition: background var(--t-fast), color var(--t-fast);
}
.rail-btn:hover { background: var(--bg-hover); color: var(--text); }
.back {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px;
  border-radius: var(--r-sm);
  color: var(--text-dim);
  font-size: 13.5px;
  text-decoration: none;
  transition: background var(--t-fast), color var(--t-fast);
}
.back:hover { background: var(--bg-hover); color: var(--text); text-decoration: none; }

.snav { display: flex; flex-direction: column; gap: 2px; }
.snav-item {
  display: flex; align-items: center; gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border-radius: var(--r-sm);
  color: var(--text-dim);
  font-size: 14px;
  text-align: left;
  transition: background var(--t-fast), color var(--t-fast);
}
.snav-item:hover { background: var(--bg-hover); color: var(--text); }
.snav-item.on { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.ic {
  flex: none;
  width: 18px;
  text-align: center;
  font-size: 14px;
}
.side.closed .lb { display: none; }
.side.closed .side-detail { display: none; }
.side.closed .side-status { display: none; }

.side-tools {
  display: flex; flex-direction: column; gap: 2px;
  padding-top: 8px;
  border-top: 1px solid var(--line);
}

.side-detail {
  margin-top: auto;
  min-height: 0;
  overflow-y: auto;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}
.d-id { font-size: 12px; margin-bottom: 5px; font-variant-numeric: tabular-nums; }
.d-id b { color: var(--accent); font-weight: 700; }
.d-title {
  font-family: var(--font-serif);
  font-size: 15px;
  line-height: 1.45;
  font-weight: 650;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.d-authors { font-size: 12.5px; margin-top: 6px; }
.d-meta { font-size: 12.5px; margin-top: 3px; }
.d-chips { margin-top: 8px; }
.d-doi {
  display: block;
  margin-top: 8px;
  font-size: 12px;
  word-break: break-all;
}
.d-kw { margin-top: 8px; }
.d-agent {
  display: block;
  width: 100%;
  margin-top: 10px;
  padding: 6px 10px;
  font-size: 12.5px;
  font-weight: 600;
  font-family: inherit;
  border: 1px solid var(--accent);
  border-radius: var(--r-sm);
  background: var(--accent-soft);
  color: var(--accent);
  cursor: pointer;
  transition: background var(--t-fast), color var(--t-fast);
}
.d-agent:hover { background: var(--accent); color: #fff; }
.d-agent.on { background: transparent; border-color: var(--line-strong); color: var(--text-dim); }
.d-agent.on:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }

/* ── 主区 ── */
.rmain { flex: 1; min-width: 0; display: flex; flex-direction: column; }
/* 顶部留白：PDF 与内容列表不再直接贴顶端 */
.rbody { flex: 1; display: flex; min-height: 0; padding: 14px 14px 0; }
.rbody.split .right { flex: 1; }
.left { flex: 0 0 54%; width: 54%; }
/* 拖动分隔条：左栏（英文原文）↔ 右栏（中文译文/内容） */
.splitter {
  flex: 0 0 8px;
  margin: 0 -4px;
  cursor: col-resize;
  position: relative;
  z-index: 3;
}
.splitter::after {
  content: '';
  position: absolute;
  top: 0; bottom: 0; left: 3.5px;
  width: 1px;
  background: var(--line);
  transition: width var(--t-fast), left var(--t-fast), background var(--t-fast);
}
.splitter:hover::after, .splitter:active::after {
  width: 3px;
  left: 2.5px;
  background: var(--accent);
}
.right { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.pane { flex: 1; min-height: 0; overflow-y: auto; padding: 6px 12px 40px; }
.pad { padding: 16px 20px; margin-bottom: 14px; }
.pane-label {
  font-size: 12px;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--text-faint);
  margin-bottom: 10px;
}
.narrative {
  font-family: var(--font-serif);
  font-size: 16.5px;
  line-height: 1.95;
  margin: 0;
  white-space: pre-wrap;
}
.chips { display: flex; flex-wrap: wrap; gap: 7px; }
.kv { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.kv td { padding: 5px 8px 5px 0; border-bottom: 1px dashed var(--line); vertical-align: top; }
.kv tr:last-child td { border-bottom: none; }
.mono { font-family: var(--font-mono); font-size: 12.5px; word-break: break-all; }
.etype {
  font-style: normal;
  font-size: 10.5px;
  opacity: 0.65;
  margin-left: 5px;
}

/* ── 正文 tab ── */
.split2 { display: flex; gap: 16px; padding: 6px 12px 40px; overflow: hidden; }
.outline {
  flex: 0 0 235px;
  overflow-y: auto;
  padding: 8px;
  align-self: flex-start;
  max-height: 100%;
}
.sec {
  display: flex;
  width: 100%;
  gap: 8px;
  align-items: baseline;
  justify-content: space-between;
  padding: 5px 8px;
  border-radius: var(--r-sm);
  font-size: 13px;
  color: var(--text-dim);
  text-align: left;
  transition: background var(--t-fast);
}
.sec:hover { background: var(--bg-hover); color: var(--text); }
.sec.on { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.s-t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.s-c { font-size: 10.5px; flex: none; }
.content { flex: 1; min-width: 0; overflow-y: auto; padding: 22px 26px; max-height: 100%; }
.sec-title { font-family: var(--font-serif); font-size: 20px; margin: 0 0 14px; }
.body-text { font-size: 15.5px; color: var(--text); }

/* ── 超图 tab：左实体 | 右超边；solo = 单栏占满（实体/超边独立 tab） ── */
.graph-split {
  display: flex;
  gap: 16px;
  overflow: hidden;
  padding: 6px 12px 40px;
}
.graph-split.solo .solo-col { flex: 1 !important; min-width: 0; }
.gcol { min-height: 0; overflow-y: auto; padding-right: 2px; }
.ent-col { flex: 0 0 300px; }
.he-col { flex: 1; min-width: 0; }
.ent { margin-bottom: 10px; }
.ent-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ent-name {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
/* 类型固定在行尾：不管前面有没有合并标签，位置不变 */
.ent-head .ent-type { margin-left: auto; flex: none; }
.ent-head .chip.merged { flex: none; }
.ent-desc {
  margin: 7px 0 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-dim);
}
.he { margin-bottom: 12px; }
.he-desc {
  margin: 10px 0 0;
  font-family: var(--font-serif);
  font-size: 15px;
  line-height: 1.8;
  white-space: pre-wrap;
}

.state { padding: 40px; text-align: center; color: var(--text-dim); }
.state.err { color: var(--warn); }
.whole { padding: 80px 20px; }
/* ── 入库状态面板（2026-10-13）── */
.side-status { flex: none; margin-top: 4px; padding-top: 10px;
  border-top: 1px solid var(--line); }
.st-bar { height: 5px; border-radius: 3px; background: rgba(128,128,128,.2);
  overflow: hidden; margin: 6px 0 4px; }
.st-bar i { display: block; height: 100%; background: #3b82f6; transition: width .3s; }
.st-count { font-size: 11.5px; margin-bottom: 6px; }
.st-list { list-style: none; margin: 6px 0; padding: 0; font-size: 12px; }
.st-list li { display: flex; align-items: center; gap: 6px; padding: 2px 0; opacity: .45; }
.st-list li.on { opacity: 1; }
.st-list .dot { width: 7px; height: 7px; border-radius: 50%;
  background: rgba(128,128,128,.5); flex: none; }
.st-list li.on .dot { background: #22c55e; }
.st-time { font-size: 11px; margin-top: 4px; }
.st-none { font-size: 12px; }
/* ── 实体合并展示（2026-10-13）── */
.chip.merged { background: rgba(34,197,94,.18); border-color: rgba(34,197,94,.45); }
.ent-alias { font-size: 12px; color: var(--fg-dim, #888); margin-top: 4px;
  line-height: 1.5; }
</style>
