type ApiOptions = Parameters<typeof $fetch>[1]

interface FetchErrorLike {
  response?: { status?: unknown }
  status?: unknown
  statusCode?: unknown
}

function csrfCookie(): string {
  if (!import.meta.client) return ''
  const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/)
  return match ? decodeURIComponent(match[1] || '') : ''
}

function responseStatus(reason: unknown): number {
  if (!reason || typeof reason !== 'object') return 0
  const error = reason as FetchErrorLike
  const status = error.response?.status ?? error.statusCode ?? error.status
  return typeof status === 'number' ? status : 0
}

function redirectToLogin() {
  if (!import.meta.client) return
  const next = `${window.location.pathname}${window.location.search}${window.location.hash}`
  window.location.assign(`/accounts/login/?next=${encodeURIComponent(next)}`)
}

export function useApi() {
  async function request<T>(path: string, options: ApiOptions = {}): Promise<T> {
    const method = String(options.method || 'GET').toUpperCase()
    const headers = new Headers(options.headers as HeadersInit | undefined)
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      await $fetch('/api/csrf/', { credentials: 'include' })
      headers.set('X-CSRFToken', csrfCookie())
    }
    try {
      return await $fetch<T>(path, {
        ...options,
        headers,
        credentials: 'include',
      })
    }
    catch (reason) {
      if (responseStatus(reason) === 401) redirectToLogin()
      throw reason
    }
  }

  return { request }
}
