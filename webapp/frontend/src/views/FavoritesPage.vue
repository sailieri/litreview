<template>
  <div class="fav-page">
    <header class="topbar">
      <router-link class="btn ghost" to="/">← 返回检索</router-link>
      <div class="brand">
        <span class="logo">收藏夹</span>
        <span class="sub" v-if="totalPapers != null">去重后 {{ totalPapers }} 篇</span>
      </div>
      <span class="spacer"></span>
      <input
        v-model="newName"
        class="input"
        placeholder="新收藏夹名称"
        @keyup.enter="create"
      />
      <button class="btn primary" :disabled="!newName.trim() || busy" @click="create">＋ 新建</button>
    </header>

    <div v-if="error" class="state err">{{ error }}</div>
    <div v-if="loading" class="state"><span class="spinner"></span></div>

    <main class="grid" v-else>
      <div v-for="c in items" :key="c.id" class="fcard">
        <div class="fc-head">
          <span class="fc-name" v-if="editingId !== c.id" :title="c.name">{{ c.name }}</span>
          <input
            v-else
            class="input fc-rename"
            v-model="editName"
            @keyup.enter="saveRename(c)"
            @keyup.esc="editingId = 0"
            @blur="saveRename(c)"
          />
          <span class="badge" v-if="c.is_default">默认</span>
        </div>
        <div class="fc-count">
          <b>{{ c.count }}</b> 篇论文
        </div>
        <div class="fc-acts">
          <button class="btn primary sm" @click="open(c)">打开</button>
          <button class="btn ghost sm" @click="startRename(c)">改名</button>
          <button class="btn ghost sm danger" :disabled="c.is_default" :title="c.is_default ? '默认收藏夹不可删除（可改名）' : '删除收藏夹（论文不会删）'" @click="remove(c)">删除</button>
        </div>
      </div>
    </main>

    <p class="hint">
      同一篇论文可以同时放进多个收藏夹；删除收藏夹只解除归类，不会删除论文。
    </p>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'

const router = useRouter()
const items = ref([])
const totalPapers = ref(null)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const newName = ref('')
const editingId = ref(0)
const editName = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const d = await api.collections()
    items.value = d.items || []
    totalPapers.value = d.total_papers
  } catch (e) {
    error.value = `加载收藏夹失败：${e.message}`
  } finally {
    loading.value = false
  }
}

async function create() {
  const name = newName.value.trim()
  if (!name) return
  busy.value = true
  try {
    await api.collectionCreate(name)
    newName.value = ''
    await load()
  } catch (e) {
    error.value = `新建失败：${e.message}`
  } finally {
    busy.value = false
  }
}

function startRename(c) {
  editingId.value = c.id
  editName.value = c.name
}

async function saveRename(c) {
  if (editingId.value !== c.id) return
  const name = editName.value.trim()
  editingId.value = 0
  if (!name || name === c.name) return
  try {
    await api.collectionRename(c.id, name)
    await load()
  } catch (e) {
    error.value = `改名失败：${e.message}`
  }
}

async function remove(c) {
  if (c.is_default) return
  if (!window.confirm(`删除收藏夹「${c.name}」？\n其中 ${c.count} 篇论文不会被删除，只是解除归类。`)) return
  try {
    await api.collectionDelete(c.id)
    await load()
  } catch (e) {
    error.value = `删除失败：${e.message}`
  }
}

function open(c) {
  router.push({ path: '/', query: { collection: c.id } })
}

onMounted(load)
</script>

<style scoped>
.fav-page {
  max-width: 1180px;
  margin: 0 auto;
  padding: 26px 28px 60px;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 22px;
}
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand .logo {
  font-family: var(--font-serif);
  font-size: 24px;
  font-weight: 700;
  color: var(--text);
}
.brand .sub { color: var(--text-faint); font-size: 13px; }
.spacer { flex: 1; }
.topbar .input { width: 200px; height: 33px; }

.state { padding: 46px; text-align: center; color: var(--text-dim); }
.state.err { color: var(--warn); }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 14px;
}
.fcard {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 16px 13px;
  border-radius: 11px;
  background: var(--bg-elev);
  border: 1px solid var(--line);
}
.fcard:hover { border-color: var(--line-strong); }
.fc-head { display: flex; align-items: center; gap: 8px; }
.fc-name {
  flex: 1;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fc-rename { flex: 1; height: 28px; font-size: 14px; }
.badge {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
}
.fc-count { color: var(--text-faint); font-size: 12.5px; }
.fc-count b { color: var(--text); font-size: 17px; font-weight: 600; }
.fc-acts { display: flex; gap: 6px; }
.btn.sm { height: 29px; padding: 0 11px; font-size: 12.5px; }
.btn.danger:not(:disabled):hover { color: var(--warn); }
.hint {
  margin-top: 26px;
  color: var(--text-faint);
  font-size: 12.5px;
}
</style>
