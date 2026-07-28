export type DashboardLocale = 'ko' | 'en'

const messages = {
  ko: {
    dashboard: '대시보드',
    library: '지식 라이브러리',
    freeQuestion: '자유 질문',
    ask: '지식에게 질문',
    quiz: '퀴즈',
    schedule: '일정 관리',
    trash: '휴지통',
    operations: '운영 상태',
    apiTokens: '토큰 관리',
    apiGuide: 'API 가이드',
    search: '통합 검색',
    searchPlaceholder: '전체 지식 검색',
    searchAction: '검색',
    logout: '로그아웃',
    loggingOut: '로그아웃 중',
    menuOpen: '메뉴 열기',
    menuClose: '메뉴 닫기',
    askTitle: '근거와 함께 답변 받기',
    askDescription: '분류된 지식만 사용하며, 답변을 뒷받침하는 출처를 함께 표시합니다.',
    askPlaceholder: '예: 배포 전에 확인해야 할 절차는 무엇인가요?',
    askSubmit: '질문하기',
    askSubmitting: '근거 찾는 중…',
    askHistory: '최근 질문',
    citations: '근거 출처',
    helpful: '도움됨',
    unhelpful: '도움 안 됨',
    insufficient: '근거 부족',
    allSources: '모든 출처',
    verifiedOnly: '검증된 지식만',
    allVerification: '모든 검증 상태',
  },
  en: {
    dashboard: 'Dashboard',
    library: 'Knowledge library',
    freeQuestion: 'Question inbox',
    ask: 'Ask knowledge',
    quiz: 'Quiz',
    schedule: 'Schedule',
    trash: 'Trash',
    operations: 'Operations',
    apiTokens: 'API tokens',
    apiGuide: 'API guide',
    search: 'Search',
    searchPlaceholder: 'Search all knowledge',
    searchAction: 'Search',
    logout: 'Log out',
    loggingOut: 'Logging out',
    menuOpen: 'Open menu',
    menuClose: 'Close menu',
    askTitle: 'Get an answer with evidence',
    askDescription: 'Answers use only classified knowledge and cite the supporting sources.',
    askPlaceholder: 'For example: What should we check before a deployment?',
    askSubmit: 'Ask',
    askSubmitting: 'Finding evidence…',
    askHistory: 'Recent questions',
    citations: 'Sources',
    helpful: 'Helpful',
    unhelpful: 'Not helpful',
    insufficient: 'Insufficient evidence',
    allSources: 'All sources',
    verifiedOnly: 'Verified knowledge only',
    allVerification: 'All verification states',
  },
} as const

type MessageKey = keyof typeof messages.ko

export function useDashboardLocale() {
  const locale = useState<DashboardLocale>('dashboard-locale', () => 'ko')
  const intlLocale = computed(() => locale.value === 'ko' ? 'ko-KR' : 'en-US')

  function applyLocale(value: DashboardLocale) {
    locale.value = value
    if (!import.meta.client) return
    localStorage.setItem('dashboard:locale', value)
    document.documentElement.lang = value
  }

  function initializeLocale() {
    if (!import.meta.client) return
    const stored = localStorage.getItem('dashboard:locale')
    applyLocale(stored === 'en' ? 'en' : 'ko')
  }

  function t(key: MessageKey): string {
    return messages[locale.value][key]
  }

  return { applyLocale, initializeLocale, intlLocale, locale, t }
}
