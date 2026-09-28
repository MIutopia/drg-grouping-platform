// Unified API wrapper: token kept in localStorage, auto-redirect to login on 401
const TOKEN_KEY = 'drg_token'
export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (t) => localStorage.setItem(TOKEN_KEY, t)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)

export async function api(path, { method = 'GET', body } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (getToken()) headers.Authorization = 'Bearer ' + getToken()
  const res = await fetch('/api' + path, {
    method, headers, body: body ? JSON.stringify(body) : undefined
  })
  if (res.status === 401) {
    clearToken()
    if (location.hash !== '#/login') location.hash = '#/login'
    throw new Error('登录已失效')
  }
  if (!res.ok) {
    const j = await res.json().catch(() => ({}))
    throw new Error(j.detail || res.statusText)
  }
  return res.json()
}

// Report export: fetch the Excel stream and trigger a browser download. The filename
// comes from the backend Content-Disposition (UTF-8 names need decodeURIComponent here).
export async function downloadExport(kind) {
  const headers = {}
  if (getToken()) headers.Authorization = 'Bearer ' + getToken()
  const res = await fetch('/api/export/' + kind, { headers })
  if (res.status === 401) {
    clearToken()
    if (location.hash !== '#/login') location.hash = '#/login'
    throw new Error('登录已失效')
  }
  if (!res.ok) {
    const j = await res.json().catch(() => ({}))
    throw new Error(j.detail || res.statusText)
  }
  const blob = await res.blob()
  const cd = res.headers.get('Content-Disposition') || ''
  let fname = kind + '.xlsx'
  const m = cd.match(/filename\*=UTF-8''([^;]+)/)
  if (m) fname = decodeURIComponent(m[1])
  else {
    const m2 = cd.match(/filename="?([^";]+)"?/)
    if (m2) fname = m2[1]
  }
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fname
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
