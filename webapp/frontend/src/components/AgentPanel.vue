<template>
  <!-- 悬浮入口（全局，任何页面可见） -->
  <button v-if="!agent.open" class="agent-fab" @click="agent.openPanel()" title="图书管理员智能体">
    <span class="fab-ico">🤖</span> 图书管理员
  </button>

  <!-- 抽屉 -->
  <transition name="slide">
    <aside v-if="agent.open" class="agent-panel">
      <header class="ap-head">
        <div class="ap-title">🤖 图书管理员</div>
        <div class="ap-acts">
          <button class="mini" @click="showHist = !showHist">历史</button>
          <button class="mini" @click="newThread">新会话</button>
          <button
            class="mini arch" v-if="activeId && !archived"
            @click="archiveThread" title="归档本会话（冻结对话，并让智能体整理提取超边）"
          >归档</button>
          <button class="mini close" @click="agent.closePanel()">✕</button>
          <!-- 历史下拉 -->
          <div class="hist-pop" v-if="showHist">
            <div v-if="!threads.length" class="hist-empty dim">还没有会话</div>
            <div
              class="hist-item" v-for="t in threads" :key="t.id"
              :class="{ on: t.id === activeId }"
              @click="switchThread(t.id); showHist = false"
            >
              <span class="ht">{{ t.title || '未命名会话' }}</span>
              <span class="dim hn">{{ t.n_msgs }}条</span>
              <button class="hdel" title="删除" @click.stop="removeThread(t.id)">×</button>
            </div>
          </div>
        </div>
      </header>

      <!-- 工作区 -->
      <div class="ap-ws">
        <span class="ws-lbl">工作区</span>
        <span
          class="ws-pid" v-for="p in agent.paperIds" :key="p"
          :class="{ fresh: agent.pendingIds.includes(p) }"
        >#{{ p }}<i @click="rmPaper(p)" title="移出工作区">×</i></span>
        <span class="dim" v-if="!agent.paperIds.length">空 · 可添加论文让智能体精读</span>
        <span class="ws-note" v-if="agent.pendingIds.length">▲ 新加 · 下一轮先读</span>
        <button class="mini add" @click="toggleAdd">+ 论文</button>

        <div class="add-pop" v-if="showAdd">
          <input
            v-model="addQ" placeholder="输入论文 id，或搜标题关键词…"
            @keydown.enter.prevent="addSearch"
            @input="onAddInput"
          />
          <div class="sugs" v-if="addSugs.length">
            <div class="sug" v-for="r in addSugs" :key="r.id" @click="addPaper(r.id)">
              <b>#{{ r.id }}</b> {{ (r.title || '').slice(0, 52) }}
            </div>
          </div>
          <div class="dim hint" v-else>回车：纯数字=直接添加；文本=搜索</div>
        </div>
      </div>

      <!-- 对话流 -->
      <main class="ap-msgs" ref="msgsEl">
        <div v-if="!messages.length && !streaming" class="ap-welcome">
          <p>我是 <b>litread 图书管理员</b>，可以：</p>
          <p>· 帮你找/挑论文 ——「找几篇 GEDI 结合深度学习估生物量的」</p>
          <p>· 读原文回答 ——「#250 的 R² 和 RMSE 是多少」</p>
          <p>· 对比多篇 / 查库 ——「工作区这两篇方法有什么区别」「库里风电预测有多少篇」</p>
          <p class="dim">问答自动入库：常见问题下次直接命中。</p>
        </div>

        <div
          v-for="(m, i) in messages" :key="m.id || i"
          class="m" :class="m.role"
        >
          <div class="bub">
            <template v-if="m.role === 'user'">{{ m.content }}</template>
            <template v-else-if="m.role === 'system'">
              <div class="sys-note">📦 会话已归档，图书管理员正在整理本次对话、提取跨文献关系…</div>
            </template>
            <template v-else>
              <div class="tools" v-if="m.tool_log && m.tool_log.length">
                <div class="tool" v-for="(t, j) in m.tool_log" :key="j">
                  {{ toolIcon(t.name) }} {{ t.label }}
                </div>
              </div>
              <div class="txt" v-if="m.content">{{ m.content }}</div>
              <div class="cites" v-if="m.citations && m.citations.length">
                <a
                  class="cite" v-for="c in m.citations" :key="c.id"
                  @click="goPaper(c.id)"
                  :title="c.title || ''"
                >#{{ c.id }} {{ c.year || '—' }} {{ short(c.title) }}</a>
              </div>
              <div class="err" v-if="m.error">⚠ {{ m.error }}</div>
              <div class="thinking" v-if="m.streaming && !m.content && !m.tool_log.length">
                正在思考…
              </div>
            </template>
          </div>
        </div>
      </main>

      <footer class="ap-input">
        <textarea
          v-model="input" rows="2"
          :disabled="archived"
          :placeholder="archived
            ? '会话已归档，不可继续对话'
            : '问图书管理员：找论文 / 读原文 / 对比 / 统计…（Enter 发送，Shift+Enter 换行）'"
          @keydown.enter.exact.prevent="send"
        ></textarea>
        <button class="send" :disabled="archived || streaming || !input.trim()" @click="send">
          {{ archived ? '已归档' : (streaming ? '…' : '发送') }}
        </button>
      </footer>
    </aside>
  </transition>
</template>

<script setup>
import { ref, onMounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'
import { agentStore as agent } from '../agentStore.js'

const router = useRouter()

const threads = ref([])
const activeId = ref(null)
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const showHist = ref(false)
const archived = ref(false)
const showAdd = ref(false)
const addQ = ref('')
const addSugs = ref([])
const msgsEl = ref(null)

/* ── threads CRUD ── */
async function loadThreads() {
  try {
    const d = await api.agentThreads()
    threads.value = d.threads || []
  } catch (e) { /* 列表失败不阻断对话 */ }
}

function newThread() {
  activeId.value = null
  messages.value = []
  showHist.value = false
  archived.value = false
  agent.pendingIds = []
}

async function switchThread(id) {
  try {
    const t = await api.agentThread(id)
    activeId.value = t.id
    messages.value = t.messages || []
    agent.syncThread(t.paper_ids)
    archived.value = !!t.archived_at
    scrollTo(true)
  } catch (e) { alert(`加载会话失败：${e.message}`) }
}

async function removeThread(id) {
  if (!confirm('删除该会话（含全部问答记录）？')) return
  try {
    await api.agentThreadDelete(id)
    if (id === activeId.value) newThread()
    await loadThreads()
  } catch (e) { alert(`删除失败：${e.message}`) }
}

/* ── 归档：冻结会话 + 触发图书管理员整理对话提取超边 ── */
async function archiveThread() {
  if (!activeId.value) return
  if (!confirm('归档本会话？\n\n· 归档后不可继续对话\n· 图书管理员将整理本次对话，提取跨文献超边'))
    return
  try {
    const r = await api.agentThreadArchive(activeId.value)
    archived.value = true
    messages.value.push({ id: r.archived_msg_id, role: 'system', content: '' })
    await loadThreads()
  } catch (e) { alert(`归档失败：${e.message}`) }
}

/* ── 工作区 ── */
function rmPaper(id) {
  agent.removePaper(id)
}

function addPaper(id) {
  if (!agent.addPaper(id)) { alert('工作区最多 12 篇'); return }
  showAdd.value = false
  addQ.value = ''
  addSugs.value = []
}

function toggleAdd() {
  showAdd.value = !showAdd.value
  addSugs.value = []
}

async function addSearch() {
  const q = addQ.value.trim()
  if (!q) return
  if (/^\d+$/.test(q)) { addPaper(Number(q)); return }
  try {
    const d = await api.papers({ query: q, page_size: 6 })
    addSugs.value = d.items || []
    if (!addSugs.value.length) addQ.value = `${q}（无命中）`
  } catch (e) { /* ignore */ }
}

let addTimer = null
function onAddInput() {
  clearTimeout(addTimer)
  const q = addQ.value.trim()
  if (!q || /^\d+$/.test(q)) { addSugs.value = []; return }
  addTimer = setTimeout(async () => {
    try {
      const d = await api.papers({ query: q, page_size: 6 })
      addSugs.value = d.items || []
    } catch (e) { /* ignore */ }
  }, 350)
}

function goPaper(id) {
  router.push({ name: 'paper', params: { id } })
}

/* ── SSE 对话 ── */
function toolIcon(name) {
  return name === 'search_papers' ? '🔍' : name === 'read_paper' ? '📖'
    : name === 'get_stats' ? '📊' : '🔧'
}

function short(t) {
  if (!t) return ''
  return t.length > 30 ? t.slice(0, 30) + '…' : t
}

function scrollTo(force = false) {
  nextTick(() => {
    const el = msgsEl.value
    if (!el) return
    const near = force || el.scrollHeight - el.scrollTop - el.clientHeight < 160
    if (near) el.scrollTop = el.scrollHeight
  })
}

async function send() {
  const q = input.value.trim()
  if (!q || streaming.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: q })
  const am = { role: 'assistant', content: '', tool_log: [], citations: [], streaming: true }
  messages.value.push(am)
  streaming.value = true
  scrollTo()

  try {
    const resp = await fetch('/api/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: q,
        thread_id: activeId.value,
        paper_ids: agent.paperIds,
        pending_ids: agent.pendingIds,
      }),
    })
    if (!resp.ok || !resp.body) {
      throw new Error(`${resp.status} ${(await resp.text().catch(() => '')).slice(0, 200)}`)
    }
    const reader = resp.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      let i
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, i)
        buf = buf.slice(i + 2)
        let ev = ''
        let raw = ''
        for (const line of block.split('\n')) {
          if (line.startsWith('event: ')) ev = line.slice(7).trim()
          else if (line.startsWith('data: ')) raw += line.slice(6)
        }
        if (!ev || !raw) continue
        let d
        try { d = JSON.parse(raw) } catch { continue }
        if (ev === 'meta') {
          activeId.value = d.thread_id
        } else if (ev === 'tool') {
          am.tool_log.push({ name: d.name, label: d.label })
        } else if (ev === 'delta') {
          am.content += d.text || ''
        } else if (ev === 'done') {
          am.content = d.content || am.content      // 幂等收口：用完整文本替换流式累积
          am.citations = d.citations || []
          am.tool_log = d.tool_log || am.tool_log
          activeId.value = d.thread_id
          agent.consumePending()      // 本轮已发：新加论文转为「已知」（保留显示）
        } else if (ev === 'error') {
          am.error = d.message || '未知错误'
        }
        scrollTo()
      }
    }
    if (!am.content && !am.error) am.error = '（服务端无响应内容）'
  } catch (e) {
    am.error = String(e && e.message ? e.message : e)
  } finally {
    am.streaming = false
    streaming.value = false
    scrollTo()
    if (activeId.value) loadThreads()
  }
}

onMounted(loadThreads)

// 论文页「在智能体中讨论」时打开抽屉（paperIds 已由 store 推入）
watch(() => agent.open, (v) => { if (v) scrollTo(true) })
</script>

<style scoped>
/* ── 入口悬浮钮 ── */
.agent-fab {
  position: fixed;
  right: 18px;
  bottom: 22px;
  z-index: 60;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 10px 16px;
  border-radius: 999px;
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: var(--shadow);
  transition: transform var(--t-fast);
}
.agent-fab:hover { transform: translateY(-2px); }
.fab-ico { font-size: 16px; }

/* ── 抽屉 ── */
.agent-panel {
  position: fixed;
  top: 0;
  right: 0;
  width: 470px;
  max-width: 100vw;
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg);
  border-left: 1px solid var(--line);
  box-shadow: -12px 0 32px rgb(0 0 0 / 14%);
  z-index: 70;
}
.slide-enter-active, .slide-leave-active { transition: transform var(--t-med), opacity var(--t-med); }
.slide-enter-from, .slide-leave-to { transform: translateX(40px); opacity: 0; }

.ap-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--bg-panel);
}
.ap-title { font-size: 14.5px; font-weight: 700; }
.ap-acts { display: flex; gap: 6px; position: relative; }
.mini {
  font-size: 12.5px;
  padding: 4px 10px;
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}
.mini:hover { border-color: var(--accent); color: var(--accent); }
.mini.close { border: none; font-size: 14px; }

.hist-pop {
  position: absolute;
  top: 34px;
  right: 0;
  width: 300px;
  max-height: 320px;
  overflow: auto;
  background: var(--bg-elev);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  box-shadow: var(--shadow);
  z-index: 5;
}
.hist-empty { padding: 14px; font-size: 13px; text-align: center; }
.hist-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
  font-size: 13px;
}
.hist-item:last-child { border-bottom: none; }
.hist-item:hover, .hist-item.on { background: var(--bg-hover); }
.hist-item .ht { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hist-item .hn { font-size: 11.5px; flex: none; }
.hdel {
  border: none;
  background: none;
  color: var(--text-faint);
  cursor: pointer;
  font-size: 14px;
  padding: 0 2px;
}
.hdel:hover { color: var(--warn); }

/* ── 工作区条 ── */
.ap-ws {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--line);
  font-size: 12.5px;
  position: relative;
}
.ws-lbl { color: var(--text-faint); }
.ws-pid {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: 999px;
  padding: 2px 8px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.ws-pid i { font-style: normal; cursor: pointer; opacity: .65; }
.ws-pid i:hover { opacity: 1; }
.ws-pid.fresh { background: var(--accent); color: #fff; }
.ws-note { font-size: 11.5px; color: var(--accent); font-weight: 600; }
.add { margin-left: auto; }
.add-pop {
  position: absolute;
  top: 36px;
  right: 10px;
  width: 340px;
  background: var(--bg-elev);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  box-shadow: var(--shadow);
  padding: 9px;
  z-index: 5;
}
.add-pop input {
  width: 100%;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  background: var(--bg-panel);
  color: var(--text);
  font-size: 13px;
  outline: none;
}
.add-pop input:focus { border-color: var(--accent); }
.sugs { margin-top: 7px; max-height: 210px; overflow: auto; }
.sug {
  padding: 6px 7px;
  font-size: 12.6px;
  border-radius: var(--r-sm);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sug:hover { background: var(--bg-hover); }
.sug b { color: var(--accent); margin-right: 5px; }
.hint { font-size: 12px; margin-top: 7px; }

/* ── 消息流 ── */
.ap-msgs {
  flex: 1;
  overflow-y: auto;
  padding: 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ap-welcome {
  padding: 14px;
  border: 1px dashed var(--line-strong);
  border-radius: var(--r-md);
  font-size: 13.2px;
  line-height: 1.8;
  color: var(--text-dim);
}
.ap-welcome p { margin: 0; }
.ap-welcome b { color: var(--text); }

.m { display: flex; }
.m.user { justify-content: flex-end; }
.bub {
  max-width: 92%;
  padding: 9px 12px;
  border-radius: var(--r-md);
  font-size: 13.4px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.m.user .bub {
  background: var(--accent-soft);
  color: var(--text);
  border-bottom-right-radius: 4px;
}
.m.assistant .bub {
  background: var(--bg-panel);
  border: 1px solid var(--line);
  border-bottom-left-radius: 4px;
  width: 100%;
}
.tools { display: flex; flex-direction: column; gap: 4px; margin-bottom: 7px; }
.tool {
  font-size: 12.2px;
  color: var(--text-faint);
  background: var(--bg-hover);
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  padding: 3px 8px;
  font-family: var(--font-mono);
}
.txt { white-space: pre-wrap; }
.thinking { color: var(--text-faint); font-size: 12.8px; }
.err { color: var(--warn); font-size: 12.8px; margin-top: 5px; }
.cites { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.cite {
  font-size: 12px;
  padding: 3px 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--bg-hover);
  color: var(--text-dim);
  cursor: pointer;
  text-decoration: none;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cite:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }

/* ── 输入区 ── */
.ap-input {
  display: flex;
  gap: 9px;
  padding: 11px 13px;
  border-top: 1px solid var(--line);
  background: var(--bg-panel);
}
.ap-input textarea {
  flex: 1;
  resize: none;
  padding: 9px 11px;
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  background: var(--bg-panel);
  color: var(--text);
  font-size: 13.4px;
  font-family: inherit;
  line-height: 1.55;
  outline: none;
}
.ap-input textarea:focus { border-color: var(--accent); }
.send {
  align-self: flex-end;
  padding: 9px 18px;
  border: none;
  border-radius: var(--r-md);
  background: var(--accent);
  color: #fff;
  font-size: 13.5px;
  font-weight: 600;
  cursor: pointer;
}
.send:disabled { opacity: .45; cursor: not-allowed; }

/* ── 会话归档（2026-10-13）── */
.mini.arch { background: rgba(245,158,11,.15); border-color: rgba(245,158,11,.45); }
.mini.arch:hover { background: rgba(245,158,11,.28); }
.sys-note { font-size: 12.5px; color: #b45309;
  background: rgba(245,158,11,.10); border: 1px dashed rgba(245,158,11,.5);
  border-radius: 8px; padding: 8px 10px; }
.ap-input textarea:disabled { opacity: .5; cursor: not-allowed; }

@media (max-width: 560px) {
  .agent-panel { width: 100vw; border-left: none; }
}
</style>
