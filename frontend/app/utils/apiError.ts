interface ApiErrorData {
  code?: unknown
  error?: unknown
}

interface ApiErrorLike {
  data?: ApiErrorData
  message?: unknown
}

export function apiErrorCode(reason: unknown): string {
  if (!reason || typeof reason !== 'object') return ''
  const code = (reason as ApiErrorLike).data?.code
  return typeof code === 'string' ? code : ''
}

export function apiErrorMessage(reason: unknown, fallback: string): string {
  if (!reason || typeof reason !== 'object') return fallback
  const error = (reason as ApiErrorLike).data?.error
  if (typeof error === 'string' && error) return error
  const message = (reason as ApiErrorLike).message
  return typeof message === 'string' && message ? message : fallback
}
