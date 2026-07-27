export function groupScheduleEvents<T extends { agenda_group: string }>(
  events: T[],
  groupOrder: readonly string[],
): Record<string, T[]> {
  const groups = Object.fromEntries(groupOrder.map(group => [group, [] as T[]]))
  for (const event of events) {
    const group = groups[event.agenda_group]
    if (!group) throw new Error(`지원하지 않는 일정 그룹입니다: ${event.agenda_group}`)
    group.push(event)
  }
  return groups
}

export function scheduleGroupTotal(counts: Record<string, number>): number {
  return Object.values(counts).reduce((total, count) => total + count, 0)
}
