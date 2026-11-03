<template>
  <div class="facet" ref="root">
    <button
      type="button"
      class="trigger"
      :class="{ active: modelValue.length, open }"
      @click.stop="open = !open"
    >
      <span class="t">{{ label }}</span>
      <span v-if="modelValue.length" class="badge">{{ modelValue.length }}</span>
      <span class="caret">▾</span>
    </button>

    <div v-if="open" class="pop" @click.stop>
      <input
        v-if="searchable"
        class="q"
        v-model="q"
        :placeholder="`搜索${label}…`"
        @keydown.esc="open = false"
      />
      <div class="list">
        <label v-for="o in filtered" :key="o.value" class="opt" :title="o.value">
          <input type="checkbox" :value="o.value" v-model="local" />
          <span class="v">{{ o.value }}</span>
          <span class="c">{{ o.count }}</span>
        </label>
        <div v-if="!filtered.length" class="empty">无匹配选项</div>
      </div>
      <div class="foot">
        <span class="dim">已选 {{ modelValue.length }} / {{ options.length }}</span>
        <span class="acts">
          <button v-if="modelValue.length" type="button" class="link" @click="local = []">清空</button>
          <button type="button" class="link ok" @click="open = false">完成</button>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  options: { type: Array, default: () => [] },   // [{value, count}]
  modelValue: { type: Array, default: () => [] },
  searchable: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const q = ref('')
const root = ref(null)

// 选中即生效（父组件 watch 会触发重新检索）
const local = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const filtered = computed(() => {
  const s = q.value.trim().toLowerCase()
  if (!s) return props.options
  return props.options.filter((o) => String(o.value).toLowerCase().includes(s))
})

function onDocClick(e) {
  if (root.value && !root.value.contains(e.target)) open.value = false
}
onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
watch(open, (v) => { if (v) q.value = '' })
</script>

<style scoped>
.facet { position: relative; }
.trigger {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 11px;
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  background: var(--bg-panel);
  color: var(--text-dim);
  font-size: 13px;
  cursor: pointer;
  transition: border-color var(--t-fast), color var(--t-fast);
}
.trigger:hover { border-color: var(--line-strong); color: var(--text); }
.trigger.active { border-color: var(--accent); color: var(--text); background: var(--accent-soft); }
.trigger.open .caret { transform: rotate(180deg); }
.badge {
  min-width: 17px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--accent);
  color: #fff;
  font-size: 11.5px;
  line-height: 17px;
  text-align: center;
}
.caret { font-size: 10px; transition: transform var(--t-fast); }

.pop {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  z-index: 30;
  width: 320px;
  max-width: 84vw;
  background: var(--bg-elev);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  box-shadow: var(--shadow);
  overflow: hidden;
}
.q {
  width: 100%;
  padding: 9px 11px;
  border: none;
  border-bottom: 1px solid var(--line);
  background: transparent;
  color: var(--text);
  font-size: 13px;
  outline: none;
}
.list { max-height: 264px; overflow-y: auto; padding: 5px 0; }
.opt {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px;
  cursor: pointer;
  font-size: 13px;
}
.opt:hover { background: var(--bg-hover); }
.opt input { accent-color: var(--accent); cursor: pointer; }
.opt .v {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.opt .c {
  color: var(--text-faint);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.empty { padding: 18px; text-align: center; color: var(--text-faint); font-size: 13px; }
.foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 12px;
  border-top: 1px solid var(--line);
  font-size: 12.5px;
}
.acts { display: flex; gap: 12px; }
.link {
  border: none;
  background: none;
  color: var(--accent);
  font-size: 12.5px;
  cursor: pointer;
  padding: 0;
}
.link:hover { text-decoration: underline; }
.link.ok { color: var(--ok); }
.dim { color: var(--text-faint); }
</style>
