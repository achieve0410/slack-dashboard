export const MAX_BULK_IDS = 1000

export function normalizeBulkIds(ids: Iterable<number>): number[] {
  const normalized = [...new Set(ids)]
    .filter(id => Number.isInteger(id) && id > 0)
    .sort((left, right) => left - right)
  if (normalized.length > MAX_BULK_IDS) {
    throw new RangeError(`한 번에 최대 ${MAX_BULK_IDS}개를 선택할 수 있습니다.`)
  }
  return normalized
}

export function toggleBulkId(ids: number[], id: number, selected: boolean): number[] {
  const next = new Set(ids)
  if (selected) next.add(id)
  else next.delete(id)
  return normalizeBulkIds(next)
}

export function toggleVisibleBulkIds(
  ids: number[],
  visibleIds: Iterable<number>,
  selected: boolean,
): number[] {
  const next = new Set(ids)
  for (const id of visibleIds) {
    if (selected) next.add(id)
    else next.delete(id)
  }
  return normalizeBulkIds(next)
}
