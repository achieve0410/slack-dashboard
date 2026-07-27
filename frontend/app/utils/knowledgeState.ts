import type { KnowledgeCard, KnowledgeDetail, KnowledgeStatus, RunDetail, RunState } from '../types/api'

const FREE_QUESTION_STATUSES: ReadonlySet<KnowledgeStatus> = new Set([
  'awaiting_answer',
  'pending',
  'needs_review',
])

const KNOWLEDGE_MEMBERSHIP_STATE_KEYS: ReadonlySet<keyof RunState> = new Set([
  'read',
  'bookmarked',
  'completed',
  'archived',
])

export function mergeKnowledgeUpdates(
  items: KnowledgeCard[],
  updates: Record<number, KnowledgeDetail>,
): KnowledgeCard[] {
  return items.map((card) => {
    const updated = updates[card.id]
    if (!updated) return card
    return {
      ...card,
      status: updated.status,
      status_label: updated.status_label,
      category: updated.category,
      category_path: updated.category_path,
      classified_at: updated.classified_at,
      state: updated.state,
      tags: updated.tags,
    }
  })
}

export function isFreeQuestionItem(item: Pick<KnowledgeCard, 'status'>): boolean {
  return FREE_QUESTION_STATUSES.has(item.status)
}

export function updateKnowledgeRemoval(
  removals: Record<number, boolean>,
  itemId: number,
  removed: boolean,
): Record<number, boolean> {
  if (removed) return { ...removals, [itemId]: true }
  return Object.fromEntries(
    Object.entries(removals).filter(([id]) => Number(id) !== itemId),
  )
}

export function canEditRunConsumptionState(
  run: Pick<RunDetail, 'knowledge_item'> | null,
): boolean {
  return Boolean(run?.knowledge_item)
}

export function affectsKnowledgeMembership(patch: Partial<RunState>): boolean {
  return Object.keys(patch).some(key => (
    KNOWLEDGE_MEMBERSHIP_STATE_KEYS.has(key as keyof RunState)
  ))
}
