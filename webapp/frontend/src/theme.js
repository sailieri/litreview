export function toggleTheme() {
  const el = document.documentElement
  const next = el.dataset.theme === 'dark' ? 'light' : 'dark'
  el.dataset.theme = next
  try {
    localStorage.setItem('litread-theme', next)
  } catch (e) {}
  return next
}
