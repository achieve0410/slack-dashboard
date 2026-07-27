interface KnowledgePresentationItem {
  source_type: 'cron' | 'slack_qa'
  status: string
  title: string
  summary?: string
  question?: string
}

function normalizeText(value: string | undefined): string {
  return (value || '').trim().replace(/\s+/g, ' ')
}

export function shouldShowKnowledgeSummary(item: KnowledgePresentationItem): boolean {
  if (!normalizeText(item.summary)) return false
  return item.source_type !== 'slack_qa' || item.status === 'classified'
}

export function shouldShowKnowledgeQuestion(item: KnowledgePresentationItem): boolean {
  const question = normalizeText(item.question)
  return Boolean(question) && question !== normalizeText(item.title)
}
