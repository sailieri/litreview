import { reactive } from 'vue'

/** 图书管理员抽屉的全局状态：任何页面（列表/论文页）都可打开并投喂论文。 */
export const agentStore = reactive({
  open: false,
  paperIds: [],        // 工作区论文 id（切换会话时由线程快照覆盖）——「智能体已知」
  pendingIds: [],      // 本轮新加入、智能体尚未阅读的论文——「下一轮先看」，用后清空
  openPanel() {
    this.open = true
  },
  closePanel() {
    this.open = false
  },
  /**
   * 加入工作区。返回 false 表示超上限（调用方负责提示）。
   * 新加入的会同时记入 pendingIds → 下一轮提问时智能体会被要求先读它。
   */
  addPaper(id) {
    const n = Number(id)
    if (!n) return false
    if (!this.paperIds.includes(n) && this.paperIds.length >= 12) return false
    if (!this.paperIds.includes(n)) this.paperIds.push(n)
    if (!this.pendingIds.includes(n)) this.pendingIds.push(n)
    return true
  },
  /** 移出工作区（同时清掉待读标记） */
  removePaper(id) {
    const n = Number(id)
    this.paperIds = this.paperIds.filter((x) => x !== n)
    this.pendingIds = this.pendingIds.filter((x) => x !== n)
  },
  /** 论文页入口：打开抽屉并把该篇加入工作区 */
  openWithPaper(id) {
    this.addPaper(id)
    this.open = true
  },
  /** 本轮提问已发出：待读标记清空（工作区保留，UI 转为「已知」灰显） */
  consumePending() {
    this.pendingIds = []
  },
  /** 切换/新建会话：工作区快照由线程数据覆盖，待读标记清空 */
  syncThread(ids) {
    this.paperIds = Array.isArray(ids) ? ids.map(Number) : []
    this.pendingIds = []
  },
})
