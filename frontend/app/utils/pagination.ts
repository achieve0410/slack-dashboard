export interface PageState<T extends { id: number }> {
  items: T[]
  total: number
  nextOffset: number | null
  loading: boolean
  requestId: number
}

export interface PageRequest {
  requestId: number
  offset: number
  append: boolean
}

export interface PageResponse<T> {
  count: number
  next_offset: number | null
  results: T[]
}

export function createPageState<T extends { id: number }>(): PageState<T> {
  return {
    items: [],
    total: 0,
    nextOffset: null,
    loading: false,
    requestId: 0,
  }
}

export function resetPage<T extends { id: number }>(state: PageState<T>): void {
  state.items = []
  state.total = 0
  state.nextOffset = null
  state.loading = false
  state.requestId += 1
}

export function startPageRequest<T extends { id: number }>(
  state: PageState<T>,
  append: boolean,
): PageRequest | null {
  const offset = append ? state.nextOffset : 0
  if (offset === null) return null
  state.requestId += 1
  state.loading = true
  return {
    requestId: state.requestId,
    offset,
    append,
  }
}

export function isCurrentPageRequest<T extends { id: number }>(
  state: PageState<T>,
  request: PageRequest,
): boolean {
  return state.requestId === request.requestId
}

export function applyPage<T extends { id: number }>(
  state: PageState<T>,
  request: PageRequest,
  response: PageResponse<T>,
): boolean {
  if (!isCurrentPageRequest(state, request)) return false
  const items = request.append ? [...state.items, ...response.results] : response.results
  state.items = [...new Map(items.map(item => [item.id, item])).values()]
  state.total = response.count
  state.nextOffset = response.next_offset
  state.loading = false
  return true
}

export function failPage<T extends { id: number }>(
  state: PageState<T>,
  request: PageRequest,
): boolean {
  if (!isCurrentPageRequest(state, request)) return false
  state.loading = false
  return true
}

export function remainingItems<T extends { id: number }>(state: PageState<T>): number {
  return Math.max(0, state.total - state.items.length)
}
