import type {
  QuizAnswerResponse,
  QuizCatalogResponse,
  QuizDifficulty,
  QuizDomain,
  QuizDomainConfig,
  QuizMode,
  QuizResultResponse,
  QuizReviewResponse,
  QuizSessionHistoryList,
  QuizSessionResponse,
  QuizSessionStartResponse,
  QuizWrongNoteResponse,
} from '~/types/api'

export const quizDomainLabels: Record<string, string> = {
  english: '영어',
  japanese: '일본어',
  aws_saa: 'AWS SAA',
}

export function quizDomainLabel(domain: QuizDomain, configs: QuizDomainConfig[] = []): string {
  return configs.find(config => config.slug === domain)?.label
    || quizDomainLabels[domain]
    || domain.replaceAll('_', ' ')
}

export const quizDifficultyLabels: Record<QuizDifficulty, string> = {
  beginner: '초급',
  intermediate: '중급',
  advanced: '고급',
}

export const quizModeLabels: Record<QuizMode, string> = {
  new: '새 문제',
  review: '복습',
  wrong: '오답',
}

export function quizAvailableKey(domain: QuizDomain, difficulty: QuizDifficulty): string {
  return `${domain}:${difficulty}`
}

export function useQuizApi() {
  const { request } = useApi()
  const domainConfigs = useState<QuizDomainConfig[]>('quiz-domain-configs', () => [])

  async function catalog() {
    const response = await request<QuizCatalogResponse>('/api/quiz/catalog/')
    domainConfigs.value = response.domain_configs
    return response
  }

  function domainLabel(domain: QuizDomain) {
    return quizDomainLabel(domain, domainConfigs.value)
  }

  function review(params: { domain?: QuizDomain, difficulty?: QuizDifficulty, dueOnly?: boolean } = {}) {
    const query = new URLSearchParams()
    if (params.domain) query.set('domain', params.domain)
    if (params.difficulty) query.set('difficulty', params.difficulty)
    if (params.dueOnly !== undefined) query.set('due_only', params.dueOnly ? '1' : '0')
    const suffix = query.toString()
    return request<QuizReviewResponse>(`/api/quiz/review/${suffix ? `?${suffix}` : ''}`)
  }

  function startSession(payload: { domain: QuizDomain, difficulty: QuizDifficulty, mode: QuizMode }) {
    return request<QuizSessionStartResponse>('/api/quiz/sessions/', { method: 'POST', body: payload })
  }

  function history(limit = 20) {
    return request<QuizSessionHistoryList>(`/api/quiz/sessions/?limit=${limit}`)
  }

  function session(sessionId: string) {
    return request<QuizSessionResponse>(`/api/quiz/sessions/${sessionId}/`)
  }

  function answer(sessionId: string, itemId: number, choiceIds: string[]) {
    return request<QuizAnswerResponse>(`/api/quiz/sessions/${sessionId}/items/${itemId}/answer/`, {
      method: 'POST',
      body: { choice_ids: choiceIds },
    })
  }

  function result(sessionId: string) {
    return request<QuizResultResponse>(`/api/quiz/sessions/${sessionId}/result/`)
  }

  function wrongNote(questionId: number, manualWrongNote: boolean, note = '') {
    return request<QuizWrongNoteResponse>(`/api/quiz/questions/${questionId}/wrong-note/`, {
      method: 'PATCH',
      body: { manual_wrong_note: manualWrongNote, note },
    })
  }

  return {
    answer,
    catalog,
    domainConfigs,
    domainLabel,
    history,
    result,
    review,
    session,
    startSession,
    wrongNote,
  }
}
