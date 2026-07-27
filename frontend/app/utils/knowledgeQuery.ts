export const KNOWLEDGE_CONTEXT_KEYS = [
  'q',
  'tag',
  'category',
  'status',
  'sort',
  'period',
  'from',
  'to',
  'source_type',
  'read',
  'bookmarked',
  'completed',
  'archived',
] as const

export type KnowledgeContextKey = typeof KNOWLEDGE_CONTEXT_KEYS[number]

function firstQueryValue(value: unknown): string {
  if (Array.isArray(value)) return firstQueryValue(value[0])
  return typeof value === 'string' ? value.trim() : ''
}

export function canonicalKnowledgeQuery(
  query: Record<string, unknown>,
): Partial<Record<KnowledgeContextKey, string>> {
  const result: Partial<Record<KnowledgeContextKey, string>> = {}
  for (const key of KNOWLEDGE_CONTEXT_KEYS) {
    const value = firstQueryValue(query[key])
    if (value) result[key] = value
  }
  return result
}

export function knowledgeQueryParams(query: Record<string, unknown>): URLSearchParams {
  return new URLSearchParams(canonicalKnowledgeQuery(query))
}

export function detailUrlWithKnowledgeContext(
  detailUrl: string,
  query: Record<string, unknown>,
): string {
  const url = new URL(detailUrl, 'http://dashboard.local')
  for (const [key, value] of knowledgeQueryParams(query)) url.searchParams.set(key, value)
  return `${url.pathname}${url.search}${url.hash}`
}
