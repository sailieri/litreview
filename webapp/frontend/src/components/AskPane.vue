<template>
  <div class="ask-pane">
    <div class="scope">
      <span class="agent-tag">论文问答助手</span>
      <span class="faint" style="font-size: 12px">
        围绕本篇原文与超图连续提问；需要其它论文证据时自动跨库检索
      </span>
    </div>

    <div class="msgs" ref="scroller" @click="onCiteClick">
      <div v-if="!messages.length" class="empty">
        <p class="dim">
          关于本篇论文随便问——回答基于原文全文与超图，数值可溯源。
          回答中的 <span class="cite">[S1]</span> 是跨库证据编号，点击可展开原文与出处。
        </p>
        <div class="suggests">
          <button class="chip" v-for="s in suggests" :key="s" @click="send(s)">{{ s }}</button>
        </div>
      </div>

      <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
        <div class="who">
          {{ m.role === 'user' ? '我' : '论文问答助手' }}
          <span v-if="m.tool_used" class="tool-badge" title="本轮触发了跨文献检索">跨库检索</span>
        </div>
        <div class="body">
          <template v-if="m.pending">
            <span class="spinner"></span>
            <span class="dim pending-tip">思考中…（若触发跨库检索约需 1–3 分钟）</span>
          </template>
          <template v-else-if="m.error">
            <span class="err">{{ m.error }}</span>
            <button class="btn ghost" @click="retry(i)">重试</button>
          </template>
          <template v-else>
            <div class="answer prose" v-html="renderText(m.text)"></div>
            <div class="cites" v-if="m.citations?.length">
              <div class="cite-head dim">跨库证据（{{ m.citations.length }}）</div>
              <details v-for="c in m.citations" :key="c.id" class="cite-item" :data-sid="c.id">
                <summary>
                  <span class="cite">{{ c.id }}</span>
                  <span class="c-type">{{ typeLabel(c) }}</span>
                  <span class="c-title">
                    <a v-if="c.metadata?.paper_id" :href="`/paper/${c.metadata.paper_id}`" target="_blank" @click.stop>{{ c.metadata.paper_title || `#${c.metadata.paper_id}` }}</a>
                    <span v-else>{{ c.metadata?.label || c.metadata?.sql || '跨文献证据' }}</span>
                  </span>
                </summary>
                <pre class="cite-body">{{ c.content }}</pre>
              </details>
            </div>
          </template>
        </div>
      </div>
    </div>

    <div class="composer">
      <textarea
        class="input"
        rows="2"
        v-model="draft"
        placeholder="围绕本篇连续提问，Enter 发送 / Shift+Enter 换行"
        @keydown.enter.exact.prevent="send()"
      ></textarea>
      <button class="btn primary" :disabled="busy || !draft.trim()" @click="send()">
        {{ busy ? '…' : '发送' }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'
import { api } from '../api.js'

const props = defineProps({ paperId: { type: Number, default: null } })

const draft = ref('')
const busy = ref(false)
const messages = ref([])
const scroller = ref(null)

const suggests = [
  '这篇论文的核心方法是什么？',
  '主要数据集和实验设置是怎样的？',
  '关键结论与精度指标有哪些？',
  '这项工作相比前人方法有什么创新？',
]

// 换论文清空对话（历史只存内存，刷新即清空）
watch(() => props.paperId, () => { messages.value = [] })

function typeLabel(c) {
  return { chunk: '正文', section: '章节', section_hit: '章节命中', hyperedge: '超边', sql: 'SQL' }[c.type] || c.type
}

function esc(s) {
  return (s || '').replace(/[&<>]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }))
}

function renderText(t) {
  return esc(t)
    .replace(/\[跨库检索\]/g, '<span class="tool-flag">[跨库检索]</span>')
    .replace(/\[([SE]\d+)\]/g, '<span class="cite" data-cite="$1">[$1]</span>')
}

function onCiteClick(e) {
  const el = e.target && e.target.closest ? e.target.closest('[data-cite]') : null
  if (!el) return
  const sid = el.dataset.cite
  const box = scroller.value
  if (!box) return
  const det = box.querySelector(`.cite-item[data-sid="${sid}"]`)
  if (det) {
    det.open = true
    det.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }
}

async function scrollBottom() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

async function send(override) {
  const q = (override ?? draft.value).trim()
  if (!q || busy.value || !props.paperId) return
  if (!override) draft.value = ''

  // 历史 = 已完成的 user/ai 消息（不含 pending/error）
  const history = messages.value
    .filter((m) => !m.pending && !m.error && m.text)
    .map((m) => ({ role: m.role === 'ai' ? 'assistant' : 'user', content: m.text }))

  messages.value.push({ role: 'user', text: q })
  const idx = messages.value.length
  messages.value.push({ role: 'ai', pending: true, text: '', citations: [] })
  busy.value = true
  scrollBottom()
  try {
    const d = await api.paperChat(props.paperId, q, history)
    messages.value[idx] = {
      role: 'ai', text: d.text, citations: d.citations || [],
      tool_used: !!d.tool_used,
    }
  } catch (e) {
    messages.value[idx] = { role: 'ai', error: `失败：${e.message}`, text: '', citations: [], q }
  } finally {
    busy.value = false
    scrollBottom()
  }
}

function retry(i) {
  const q = messages.value[i].q
  messages.value.splice(i, 1)
  send(q)
}
</script>

<style scoped>
.ask-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.scope {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 8px 2px;
  font-size: 13px;
}
.agent-tag {
  flex: none;
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 3px 10px;
  border-radius: 999px;
}
.msgs {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 6px 2px 14px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.empty { color: var(--text-dim); padding: 18px 4px; }
.suggests { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.suggests .chip { cursor: pointer; }
.suggests .chip:hover { filter: brightness(1.15); }
.who {
  font-size: 12px;
  color: var(--text-faint);
  margin-bottom: 4px;
  display: flex;
  gap: 8px;
  align-items: center;
}
.tool-badge {
  font-size: 10.5px;
  font-weight: 600;
  color: var(--warn);
  border: 1px solid var(--warn);
  border-radius: 999px;
  padding: 1px 7px;
}
.msg.user .body {
  background: var(--bg-hover);
  border-left: 2px solid var(--line-strong);
  padding: 8px 12px;
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  white-space: pre-wrap;
}
.answer { font-size: 15px; }
.pending-tip { font-size: 13px; margin-left: 8px; }
.tool-flag { color: var(--warn); font-weight: 600; }
.err { color: var(--warn); }
.cites { margin-top: 12px; border-top: 1px dashed var(--line); padding-top: 8px; }
.cite-head { font-size: 12px; margin-bottom: 6px; }
.cite-item { border: 1px solid var(--line); border-radius: var(--r-sm); margin-bottom: 6px; background: var(--bg-panel); }
.cite-item summary {
  cursor: pointer;
  padding: 6px 10px;
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: 13px;
  list-style: none;
}
.cite-item summary::-webkit-details-marker { display: none; }
.c-type { color: var(--text-faint); font-size: 12px; }
.c-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cite-body {
  margin: 0;
  padding: 10px 12px;
  border-top: 1px solid var(--line);
  font-family: var(--font-serif);
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--text-dim);
  white-space: pre-wrap;
  max-height: 260px;
  overflow-y: auto;
}
.composer {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}
.composer textarea { flex: 1; resize: none; font-size: 14px; }
</style>
