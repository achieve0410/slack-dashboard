<script setup lang="ts">
import type {
  CategoryNode,
  CronJob,
  KnowledgeCard as KnowledgeCardType,
  KnowledgeDetail,
  KnowledgeListResponse,
  OperationKind,
  OperationRunKind,
  OperationStatus,
  OperationsResponse,
  SavedKnowledgeView,
  SavedKnowledgeViewApply,
  SavedKnowledgeViewList,
  ScheduleEvent,
  ScheduleListResponse,
  Summary,
} from '~/types/api'
import {
  applyPage,
  createPageState,
  failPage,
  remainingItems,
  resetPage,
  startPageRequest,
} from '~/utils/pagination'
import { isFreeQuestionItem, mergeKnowledgeUpdates } from '~/utils/knowledgeState'
import { canonicalKnowledgeQuery, knowledgeQueryParams } from '~/utils/knowledgeQuery'
import { apiErrorCode, apiErrorMessage } from '~/utils/apiError'

definePageMeta({ keepalive: true })

const route = useRoute()
const router = useRouter()
const { request } = useApi()
const summary = ref<Summary | null>(null)
const jobs = ref<CronJob[]>([])
const categories = ref<CategoryNode[]>([])
const todayEvents = ref<ScheduleEvent[]>([])
const overdueEvents = ref<ScheduleEvent[]>([])
const knowledgePage = reactive(createPageState<KnowledgeCardType>())
const freeQuestionPage = reactive(createPageState<KnowledgeCardType>())
const searchPage = reactive(createPageState<KnowledgeCardType>())
const knowledgeItems = computed(() => knowledgePage.items)
const freeQuestionItems = computed(() => freeQuestionPage.items)
const searchItems = computed(() => searchPage.items)
const knowledgeRemaining = computed(() => remainingItems(knowledgePage))
const freeQuestionRemaining = computed(() => remainingItems(freeQuestionPage))
const searchRemaining = computed(() => remainingItems(searchPage))
const baseLoading = ref(true)
const baseError = ref('')
const knowledgeError = ref('')
const freeQuestionError = ref('')
const searchError = ref('')
const scheduleError = ref('')
const scheduleLoading = ref(true)
const savedViews = ref<SavedKnowledgeView[]>([])
const savedViewsError = ref('')
const savedViewName = ref('')
const savedViewDefault = ref(false)
const savingSavedView = ref(false)
const knowledgeReload = useState<number>('knowledge-reload', () => 0)
const operationsData = ref<OperationsResponse | null>(null)
const operationsError = ref('')
const operationKinds: OperationKind[] = ['sync', 'tagging', 'classify']
const operationLabels: Record<OperationRunKind, string> = { sync: '동기화', tagging: '태깅', classify: '분류', quiz: '퀴즈' }
const activeViewEyebrows = {
  dashboard: 'DAILY DESK',
  operations: 'SYSTEM STATUS',
  search: 'GLOBAL SEARCH',
  'free-question': 'INBOX',
  library: 'KNOWLEDGE',
} as const
const routeReady = ref(false)
const filterInput = ref('')
const filterDraft = reactive({
  status: 'all',
  sourceType: 'all',
  read: 'all',
  bookmarked: false,
  completed: false,
  archived: 'exclude',
  period: 'all',
  from: '',
  to: '',
  sort: 'newest',
})
const knowledgeUpdates = useState<Record<number, KnowledgeDetail>>('knowledge-updates', () => ({}))
const knowledgeRemovals = useState<Record<number, boolean>>('knowledge-removals', () => ({}))
const LIST_RETURN_KEY = 'dashboard:list-return'
const LIST_RETURN_POSITION_KEY = 'dashboard:list-return-position'

function queryValue(value: unknown): string {
  if (Array.isArray(value)) return String(value[0] || '')
  return typeof value === 'string' ? value : ''
}

function scrollKey(path: string): string {
  return `dashboard:scroll:${path}`
}

function clearSavedScroll(path: string) {
  sessionStorage.removeItem(LIST_RETURN_KEY)
  sessionStorage.removeItem(LIST_RETURN_POSITION_KEY)
  sessionStorage.removeItem(scrollKey(path))
}

function isDetailRoute(path: string): boolean {
  return path.startsWith('/knowledge/') || path.startsWith('/runs/')
}

function historyPosition(): string {
  return String(window.history.state?.position ?? '')
}

function canRestoreSavedScroll(): boolean {
  return sessionStorage.getItem(LIST_RETURN_KEY) === route.fullPath
    && sessionStorage.getItem(LIST_RETURN_POSITION_KEY) === historyPosition()
}

async function restoreSavedScroll(): Promise<boolean> {
  if (!import.meta.client) return false
  const path = route.fullPath
  if (!canRestoreSavedScroll()) return false
  const value = Number(sessionStorage.getItem(scrollKey(path)))
  clearSavedScroll(path)
  if (!Number.isFinite(value) || value < 1) return false
  await nextTick()
  const deadline = performance.now() + 3000
  const applyScroll = () => {
    const maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight)
    if (maxScroll < value && performance.now() < deadline) {
      requestAnimationFrame(applyScroll)
      return
    }
    window.scrollTo({ top: Math.min(value, maxScroll) })
  }
  requestAnimationFrame(applyScroll)
  return true
}

const activeView = computed<'dashboard' | 'library' | 'free-question' | 'search' | 'operations'>(() => {
  const view = queryValue(route.query.view)
  if (view === 'free-question') return 'free-question'
  if (view === 'search') return 'search'
  if (view === 'operations') return 'operations'
  if (view === 'library' || queryValue(route.query.category)) return 'library'
  return 'dashboard'
})
const searchTerm = computed(() => queryValue(route.query.q).trim())
const selectedTag = computed(() => queryValue(route.query.tag).trim())
const sortOrder = computed<'newest' | 'oldest'>(() => (
  queryValue(route.query.sort) === 'oldest' ? 'oldest' : 'newest'
))
const todayOnly = computed(() => queryValue(route.query.period) === 'today')
const statusFilter = computed<'all' | 'classified' | 'pending' | 'awaiting_answer' | 'needs_review'>(() => {
  const value = queryValue(route.query.status)
  if (value === 'classified' || value === 'pending' || value === 'awaiting_answer' || value === 'needs_review') return value
  return 'all'
})
const knowledgeQueryKey = computed(() => knowledgeQueryParams(route.query).toString())

watch(searchTerm, value => filterInput.value = value, { immediate: true })
watch(
  () => route.fullPath,
  () => {
    filterDraft.status = statusFilter.value
    filterDraft.sourceType = queryValue(route.query.source_type) || 'all'
    filterDraft.read = queryValue(route.query.read) || 'all'
    filterDraft.bookmarked = queryValue(route.query.bookmarked) === '1'
    filterDraft.completed = queryValue(route.query.completed) === '1'
    filterDraft.archived = queryValue(route.query.archived) || 'exclude'
    filterDraft.period = queryValue(route.query.period) || 'all'
    filterDraft.from = queryValue(route.query.from)
    filterDraft.to = queryValue(route.query.to)
    filterDraft.sort = sortOrder.value
  },
  { immediate: true },
)

const selectedCategoryId = computed<number | null>(() => {
  const value = Number(queryValue(route.query.category))
  return Number.isInteger(value) && value > 0 ? value : null
})

const categoryById = computed(() => {
  const result = new Map<number, CategoryNode>()
  const visit = (nodes: CategoryNode[]) => {
    nodes.forEach((node) => {
      result.set(node.id, node)
      visit(node.children)
    })
  }
  visit(categories.value)
  return result
})
const selectedCategory = computed(() => (
  selectedCategoryId.value ? categoryById.value.get(selectedCategoryId.value) || null : null
))
const selectedTrail = computed(() => {
  const trail: CategoryNode[] = []
  let current = selectedCategory.value
  while (current) {
    trail.unshift(current)
    current = current.parent_id ? categoryById.value.get(current.parent_id) || null : null
  }
  return trail
})
const activeRoot = computed(() => selectedTrail.value[0] || null)
const activeSecondLevel = computed(() => selectedTrail.value[1] || null)
const secondLevelCategories = computed(() => activeRoot.value?.children || [])
const thirdLevelCategories = computed(() => activeSecondLevel.value?.children || [])
const libraryHeading = computed(() => selectedCategory.value?.path || '전체 지식')
const attentionTotal = computed(() => summary.value
  ? summary.value.knowledge.awaiting_answer + summary.value.knowledge.pending + summary.value.knowledge.needs_review
  : 0)

function localDateKey(date = new Date()): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

const todayCronJobs = computed(() => jobs.value.filter((job) => {
  if (!job.next_run_at) return false
  return localDateKey(new Date(job.next_run_at)) === localDateKey()
}))
const dashboardDate = computed(() => new Intl.DateTimeFormat('ko-KR', {
  month: 'long',
  day: 'numeric',
  weekday: 'long',
}).format(new Date()))

function formattedEventTime(event: ScheduleEvent): string {
  if (!event.starts_at) return '기한 없음'
  if (event.item_type === 'todo') return '할 일'
  if (event.all_day) return '종일'
  return new Intl.DateTimeFormat('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(event.starts_at))
}

function eventDescription(event: ScheduleEvent): string {
  if (event.notes) return event.notes
  if (event.item_type === 'todo') return event.todo_category_label
  return event.all_day ? '종일 일정' : '개인 일정'
}

function compactDate(value: string): string {
  return new Intl.DateTimeFormat('ko-KR', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function operationStatusLabel(status: OperationStatus | undefined): string {
  if (!status) return '기록 없음'
  return ({ running: '실행 중', success: '성공', failed: '실패', skipped: '건너뜀' } as const)[status]
}

function operationSummaryText(summary: Record<string, number | boolean | null>): string {
  const entries = Object.entries(summary).filter(([, value]) => value !== null)
  return entries.length ? entries.map(([key, value]) => `${key} ${String(value)}`).join(' · ') : '요약 없음'
}

async function loadBase() {
  baseLoading.value = true
  baseError.value = ''
  try {
    const [summaryData, jobsData, categoryData, savedViewData] = await Promise.all([
      request<Summary>('/api/summary/'),
      request<{ results: CronJob[] }>('/api/jobs/'),
      request<{ results: CategoryNode[] }>('/api/categories/'),
      request<SavedKnowledgeViewList>('/api/saved-knowledge-views/'),
    ])
    summary.value = summaryData
    jobs.value = jobsData.results
    categories.value = categoryData.results
    savedViews.value = savedViewData.results
  }
  catch (reason) {
    baseError.value = reason instanceof Error ? reason.message : '대시보드를 불러오지 못했습니다.'
  }
  finally {
    baseLoading.value = false
  }
}

async function loadOperations() {
  operationsError.value = ''
  try {
    operationsData.value = await request<OperationsResponse>('/api/operations/?limit=20')
  }
  catch (reason) {
    operationsError.value = apiErrorMessage(reason, '운영 상태를 불러오지 못했습니다.')
  }
}

async function loadTodayEvents() {
  scheduleLoading.value = true
  scheduleError.value = ''
  try {
    const data = await request<ScheduleListResponse>('/api/schedule/?grouped=1')
    todayEvents.value = data.results.filter(event => event.agenda_group === 'today')
    overdueEvents.value = data.results.filter(event => event.agenda_group === 'overdue_todo')
  }
  catch (reason) {
    scheduleError.value = reason instanceof Error ? reason.message : '오늘 일정을 불러오지 못했습니다.'
  }
  finally {
    scheduleLoading.value = false
  }
}

function listParams(offset: number): URLSearchParams {
  const params = knowledgeQueryParams(route.query)
  params.set('limit', '100')
  params.set('offset', String(offset))
  if (!params.has('sort')) params.set('sort', sortOrder.value)
  return params
}

async function loadKnowledge(append = false) {
  const pageRequest = startPageRequest(knowledgePage, append)
  if (!pageRequest) return
  knowledgeError.value = ''
  try {
    const params = listParams(pageRequest.offset)
    if (selectedCategoryId.value) params.set('category', String(selectedCategoryId.value))
    if (todayOnly.value) params.set('period', 'today')
    if (statusFilter.value !== 'all') params.set('status', statusFilter.value)
    const data = await request<KnowledgeListResponse>(`/api/knowledge/?${params}`)
    applyPage(knowledgePage, pageRequest, data)
  }
  catch (reason) {
    if (failPage(knowledgePage, pageRequest)) {
      knowledgeError.value = reason instanceof Error ? reason.message : '지식 목록을 불러오지 못했습니다.'
    }
  }
}

async function loadFreeQuestion(append = false) {
  const pageRequest = startPageRequest(freeQuestionPage, append)
  if (!pageRequest) return
  freeQuestionError.value = ''
  try {
    const data = await request<KnowledgeListResponse>(`/api/free-question/?${listParams(pageRequest.offset)}`)
    applyPage(freeQuestionPage, pageRequest, data)
  }
  catch (reason) {
    if (failPage(freeQuestionPage, pageRequest)) {
      freeQuestionError.value = reason instanceof Error ? reason.message : '자유 질문을 불러오지 못했습니다.'
    }
  }
}

async function loadSearch(append = false) {
  if (!searchTerm.value) return
  const pageRequest = startPageRequest(searchPage, append)
  if (!pageRequest) return
  searchError.value = ''
  try {
    const data = await request<KnowledgeListResponse>(`/api/search/?${listParams(pageRequest.offset)}`)
    applyPage(searchPage, pageRequest, data)
  }
  catch (reason) {
    if (failPage(searchPage, pageRequest)) {
      searchError.value = reason instanceof Error ? reason.message : '검색 결과를 불러오지 못했습니다.'
    }
  }
}

async function selectLibrary(categoryId: number | null = null) {
  const query: Record<string, string> = {
    view: 'library',
    ...canonicalKnowledgeQuery(route.query),
  }
  if (categoryId) query.category = String(categoryId)
  else delete query.category
  await router.push({ query })
}

async function applyFilters() {
  const query: Record<string, string> = { sort: filterDraft.sort }
  if (selectedTag.value) query.tag = selectedTag.value
  if (activeView.value === 'library') {
    if (selectedCategoryId.value) query.category = String(selectedCategoryId.value)
    else query.view = 'library'
    if (filterDraft.status !== 'all') query.status = filterDraft.status
    if (filterDraft.sourceType !== 'all') query.source_type = filterDraft.sourceType
    if (filterDraft.read !== 'all') query.read = filterDraft.read
    if (filterDraft.bookmarked) query.bookmarked = '1'
    if (filterDraft.completed) query.completed = '1'
    if (filterDraft.archived !== 'exclude') query.archived = filterDraft.archived
    if (filterDraft.period !== 'all') query.period = filterDraft.period
    if (filterDraft.period === 'custom') {
      if (filterDraft.from) query.from = filterDraft.from
      if (filterDraft.to) query.to = filterDraft.to
    }
  }
  else if (activeView.value === 'free-question') query.view = 'free-question'
  else query.view = 'search'
  if (filterInput.value.trim()) query.q = filterInput.value.trim()
  await router.push({ query })
}

async function applyTagFilter(tag: string) {
  const query: Record<string, string> = {
    view: 'library',
    ...canonicalKnowledgeQuery(route.query),
    tag,
  }
  await router.push({ path: '/', query })
}

async function clearTagFilter() {
  const query: Record<string, string> = {
    view: 'library',
    ...canonicalKnowledgeQuery(route.query),
  }
  delete query.tag
  await router.push({ path: '/', query })
}

async function changeSort(value: Event) {
  const target = value.target as HTMLSelectElement
  await router.push({ query: { ...route.query, sort: target.value } })
}

function savedViewPayload() {
  const filters = canonicalKnowledgeQuery(route.query) as Record<string, string>
  const sort = filters.sort === 'oldest' ? 'oldest' : 'newest'
  delete filters.sort
  return { filters, sort }
}

async function createSavedView() {
  if (!savedViewName.value.trim()) return
  savingSavedView.value = true
  savedViewsError.value = ''
  try {
    const created = await request<SavedKnowledgeView>('/api/saved-knowledge-views/', {
      method: 'POST',
      body: {
        name: savedViewName.value.trim(),
        ...savedViewPayload(),
        is_default: savedViewDefault.value,
      },
    })
    savedViews.value = [
      ...savedViews.value.map(view => ({ ...view, is_default: created.is_default ? false : view.is_default })),
      created,
    ].sort((left, right) => left.name.localeCompare(right.name, 'ko'))
    savedViewName.value = ''
    savedViewDefault.value = false
  }
  catch (reason) {
    savedViewsError.value = apiErrorMessage(reason, '보기를 저장하지 못했습니다.')
  }
  finally {
    savingSavedView.value = false
  }
}

async function applySavedView(view: SavedKnowledgeView) {
  savedViewsError.value = ''
  try {
    const applied = await request<SavedKnowledgeViewApply>(`/api/saved-knowledge-views/${view.id}/apply/`)
    const query = Object.fromEntries(new URLSearchParams(applied.canonical_query))
    await router.push({ query: { view: 'library', ...query } })
  }
  catch (reason) {
    savedViewsError.value = apiErrorCode(reason) === 'stale_category'
      ? '이 보기는 비활성 카테고리를 참조합니다. 이름을 바꾸거나 삭제할 수 있습니다.'
      : apiErrorMessage(reason, '저장된 보기를 적용하지 못했습니다.')
  }
}

async function renameSavedView(view: SavedKnowledgeView) {
  const name = window.prompt('새 보기 이름', view.name)?.trim()
  if (!name || name === view.name) return
  savedViewsError.value = ''
  try {
    const updated = await request<SavedKnowledgeView>(`/api/saved-knowledge-views/${view.id}/`, {
      method: 'PATCH', body: { name },
    })
    savedViews.value = savedViews.value.map(item => item.id === view.id ? updated : item)
  }
  catch (reason) {
    savedViewsError.value = apiErrorMessage(reason, '보기 이름을 바꾸지 못했습니다.')
  }
}

async function updateSavedViewFilters(view: SavedKnowledgeView) {
  savedViewsError.value = ''
  try {
    const updated = await request<SavedKnowledgeView>(`/api/saved-knowledge-views/${view.id}/`, {
      method: 'PATCH', body: savedViewPayload(),
    })
    savedViews.value = savedViews.value.map(item => item.id === view.id ? updated : item)
  }
  catch (reason) {
    savedViewsError.value = apiErrorMessage(reason, '현재 조건으로 보기를 수정하지 못했습니다.')
  }
}

async function toggleSavedViewDefault(view: SavedKnowledgeView) {
  savedViewsError.value = ''
  try {
    const updated = await request<SavedKnowledgeView>(`/api/saved-knowledge-views/${view.id}/`, {
      method: 'PATCH', body: { is_default: !view.is_default },
    })
    savedViews.value = savedViews.value.map((item) => {
      let isDefault = item.is_default
      if (item.id === view.id) isDefault = updated.is_default
      else if (updated.is_default) isDefault = false
      return { ...item, is_default: isDefault }
    })
  }
  catch (reason) {
    savedViewsError.value = apiErrorMessage(reason, '기본 보기를 변경하지 못했습니다.')
  }
}

async function deleteSavedView(view: SavedKnowledgeView) {
  if (!window.confirm(`“${view.name}” 보기를 삭제할까요?`)) return
  savedViewsError.value = ''
  try {
    await request(`/api/saved-knowledge-views/${view.id}/`, { method: 'DELETE' })
    savedViews.value = savedViews.value.filter(item => item.id !== view.id)
  }
  catch (reason) {
    savedViewsError.value = apiErrorMessage(reason, '보기를 삭제하지 못했습니다.')
  }
}

async function refreshActiveView() {
  resetPage(knowledgePage)
  resetPage(freeQuestionPage)
  resetPage(searchPage)
  const requests: Promise<unknown>[] = [loadBase()]
  if (activeView.value === 'dashboard') requests.push(loadTodayEvents())
  if (activeView.value === 'library') requests.push(loadKnowledge())
  if (activeView.value === 'free-question') requests.push(loadFreeQuestion())
  if (activeView.value === 'search') requests.push(loadSearch())
  if (activeView.value === 'operations') requests.push(loadOperations())
  await Promise.all(requests)
}

function applyKnowledgeUpdates() {
  const updates = knowledgeUpdates.value
  const updateIds = new Set(Object.keys(updates).map(Number))
  const removalIds = new Set(Object.keys(knowledgeRemovals.value).map(Number))
  if (!updateIds.size && !removalIds.size) return false

  const removeItems = (page: typeof knowledgePage) => {
    const previousCount = page.items.length
    page.items = page.items.filter(item => !removalIds.has(item.id))
    page.total = Math.max(0, page.total - (previousCount - page.items.length))
  }
  removeItems(knowledgePage)
  removeItems(freeQuestionPage)
  removeItems(searchPage)

  const freeQuestionCount = freeQuestionPage.items.length
  freeQuestionPage.items = mergeKnowledgeUpdates(freeQuestionPage.items, updates)
    .filter(isFreeQuestionItem)
  freeQuestionPage.total = Math.max(0, freeQuestionPage.total - (freeQuestionCount - freeQuestionPage.items.length))

  const selectedPath = selectedCategory.value?.path || ''
  const knowledgeCount = knowledgePage.items.length
  knowledgePage.items = mergeKnowledgeUpdates(knowledgePage.items, updates).filter((item) => {
    if (updateIds.has(item.id) && statusFilter.value !== 'all' && item.status !== statusFilter.value) return false
    if (!updateIds.has(item.id) || !selectedPath) return true
    return item.category_path === selectedPath || item.category_path?.startsWith(`${selectedPath}/`)
  })
  knowledgePage.total = Math.max(0, knowledgePage.total - (knowledgeCount - knowledgePage.items.length))
  searchPage.items = mergeKnowledgeUpdates(searchPage.items, updates)
  knowledgeUpdates.value = {}
  knowledgeRemovals.value = {}
  return true
}

watch([activeView, knowledgeQueryKey], () => {
  if (!routeReady.value) return
  resetPage(knowledgePage)
  resetPage(freeQuestionPage)
  resetPage(searchPage)
  if (activeView.value === 'dashboard') loadTodayEvents()
  if (activeView.value === 'library') loadKnowledge()
  if (activeView.value === 'free-question') loadFreeQuestion()
  if (activeView.value === 'search') loadSearch()
  if (activeView.value === 'operations') loadOperations()
})

onMounted(async () => {
  window.history.scrollRestoration = 'manual'
  const restoreAfterLoad = canRestoreSavedScroll()
  if (!restoreAfterLoad) {
    clearSavedScroll(route.fullPath)
    window.scrollTo({ top: 0 })
  }
  await refreshActiveView()
  applyKnowledgeUpdates()
  routeReady.value = true
  if (restoreAfterLoad) await restoreSavedScroll()
})

onActivated(async () => {
  if (!routeReady.value) return
  if (applyKnowledgeUpdates()) await loadBase()
  await restoreSavedScroll()
})

watch(knowledgeReload, () => {
  if (routeReady.value && activeView.value === 'library') loadKnowledge(false)
})


onBeforeRouteLeave((to) => {
  if (!import.meta.client) return
  if (!isDetailRoute(to.path)) {
    clearSavedScroll(route.fullPath)
    return
  }
  sessionStorage.setItem(LIST_RETURN_KEY, route.fullPath)
  sessionStorage.setItem(LIST_RETURN_POSITION_KEY, historyPosition())
  sessionStorage.setItem(scrollKey(route.fullPath), String(window.scrollY))
})
</script>

<template>
  <div class="page-container dashboard-workspace">
    <header class="page-command-bar">
      <div>
        <p class="eyebrow">{{ activeViewEyebrows[activeView] }}</p>
        <h1 v-if="activeView === 'dashboard'">오늘의 브리핑</h1>
        <h1 v-else-if="activeView === 'library'">{{ libraryHeading }}</h1>
        <h1 v-else-if="activeView === 'free-question'">자유 질문</h1>
        <h1 v-else-if="activeView === 'operations'">Cron 운영 상태</h1>
        <h1 v-else>“{{ searchTerm }}” 검색 결과</h1>
        <p v-if="activeView === 'dashboard'">{{ dashboardDate }} · 지식, 일정과 운영 신호를 우선순위대로 정리했습니다.</p>
        <p v-else-if="activeView === 'library'">모든 상태의 질문·답변과 Cron 콘텐츠를 탐색합니다.</p>
        <p v-else-if="activeView === 'free-question'">답변과 분류를 기다리는 대화를 확인합니다.</p>
        <p v-else-if="activeView === 'operations'">수집 작업의 최근 상태와 다음 실행 시각을 확인합니다.</p>
        <p v-else>분류 지식과 자유 질문을 함께 검색했습니다.</p>
      </div>
      <button class="ghost-button" type="button" @click="refreshActiveView">새로고침</button>
    </header>

    <div v-if="baseError" class="notice error-notice dashboard-error" role="alert">
      <p>{{ baseError }}</p>
      <button class="ghost-button" type="button" @click="loadBase">다시 시도</button>
    </div>

    <template v-if="activeView === 'dashboard'">
      <section class="dashboard-kpis" aria-label="오늘 핵심 지표" :aria-busy="baseLoading || scheduleLoading">
        <NuxtLink to="/?view=library&period=today" class="kpi-widget">
          <div class="kpi-icon green" aria-hidden="true">▤</div>
          <div><span>오늘 생성된 지식</span><strong>{{ baseLoading ? '—' : summary?.knowledge.generated_today || 0 }}</strong><small>전체 {{ summary?.knowledge.classified || 0 }}개</small></div>
          <b aria-hidden="true">→</b>
        </NuxtLink>
        <NuxtLink to="/?view=free-question" class="kpi-widget">
          <div class="kpi-icon amber" aria-hidden="true">!</div>
          <div><span>확인 필요</span><strong>{{ baseLoading ? '—' : attentionTotal }}</strong><small>답변·분류·검토 대기</small></div>
          <b aria-hidden="true">→</b>
        </NuxtLink>
        <NuxtLink to="/schedule" class="kpi-widget">
          <div class="kpi-icon blue" aria-hidden="true">□</div>
          <div><span>금일 일정·할 일</span><strong>{{ scheduleLoading ? '—' : todayEvents.length }}</strong><small>오늘 예정 항목</small></div>
          <b aria-hidden="true">→</b>
        </NuxtLink>
        <NuxtLink to="/?view=operations" class="kpi-widget" :class="{ danger: (summary?.jobs.failed || 0) > 0 }">
          <div class="kpi-icon red" aria-hidden="true">◉</div>
          <div><span>시스템 경고</span><strong>{{ baseLoading ? '—' : summary?.jobs.failed || 0 }}</strong><small>{{ summary?.jobs.success || 0 }}개 작업 정상</small></div>
          <b aria-hidden="true">→</b>
        </NuxtLink>
      </section>

      <section class="dashboard-main-grid">
        <article class="dashboard-widget widget-knowledge">
          <header class="widget-header"><div><span>LATEST KNOWLEDGE</span><h2>최근 도착한 지식</h2></div><NuxtLink to="/?view=library">전체 보기 →</NuxtLink></header>
          <div v-if="summary?.latest_knowledge.length" class="knowledge-feed">
            <NuxtLink v-for="(item, index) in summary.latest_knowledge.slice(0, 6)" :key="item.id" :to="item.detail_url">
              <span class="feed-topline">
                <span class="feed-source">{{ index === 0 ? '가장 최근 · ' : '' }}{{ item.category_path || item.source_label }}</span>
                <time>{{ compactDate(item.generated_at) }}</time>
              </span>
              <strong>{{ item.title }}</strong>
              <p>{{ item.summary }}</p>
              <span v-if="item.tags.length" class="feed-tags" aria-label="태그">
                <span v-for="tag in item.tags.slice(0, 2)" :key="tag">#{{ tag }}</span>
              </span>
            </NuxtLink>
          </div>
          <div v-else-if="baseLoading" class="widget-loading" role="status">최근 지식을 불러오는 중입니다.</div>
          <div v-else-if="!baseLoading" class="widget-empty"><strong>새로 등록된 지식이 없습니다.</strong></div>
        </article>

        <div class="dashboard-action-rail">
          <article class="dashboard-widget widget-schedule">
            <header class="widget-header"><div><span>TODAY</span><h2>금일 일정·할 일</h2></div><NuxtLink to="/schedule">자세히 보기 →</NuxtLink></header>
            <p v-if="scheduleError" class="inline-error">{{ scheduleError }}</p>
            <div v-else-if="scheduleLoading" class="widget-loading" role="status">오늘 일정을 불러오는 중입니다.</div>
            <div v-else-if="todayEvents.length" class="widget-agenda">
              <div v-for="event in todayEvents.slice(0, 5)" :key="event.id" :class="{ completed: event.completed }">
                <time>{{ formattedEventTime(event) }}</time>
                <span><strong>{{ event.title }}</strong><small>{{ eventDescription(event) }}</small></span>
                <i>{{ event.completed ? '완료' : '예정' }}</i>
              </div>
            </div>
            <div v-else class="widget-empty"><span aria-hidden="true">□</span><strong>오늘 등록된 일정이나 할 일이 없습니다.</strong><NuxtLink to="/schedule">항목 추가하기</NuxtLink></div>
            <footer v-if="todayCronJobs.length" class="widget-footer-note"><span>자동 실행 예정</span><b>{{ todayCronJobs.length }}개 수집 작업</b></footer>
            <footer v-if="overdueEvents.length" class="widget-footer-note overdue"><span>지연 TODO</span><b>{{ overdueEvents.length }}개 확인 필요</b></footer>
          </article>

          <article class="dashboard-widget widget-attention">
            <header class="widget-header"><div><span>ACTION REQUIRED</span><h2>확인할 항목</h2></div><NuxtLink to="/?view=free-question">인박스 →</NuxtLink></header>
            <dl v-if="summary" class="status-breakdown">
              <div><dt><span class="status-bullet blue" />답변 대기</dt><dd>{{ summary.knowledge.awaiting_answer }}</dd></div>
              <div><dt><span class="status-bullet amber" />분류 대기</dt><dd>{{ summary.knowledge.pending }}</dd></div>
              <div><dt><span class="status-bullet red" />검토 필요</dt><dd>{{ summary.knowledge.needs_review }}</dd></div>
            </dl>
            <div v-else class="widget-loading" role="status">확인할 항목을 불러오는 중입니다.</div>
            <div v-if="summary?.recent_failures.length" class="compact-failures">
              <span>최근 실패</span>
              <NuxtLink v-for="run in summary.recent_failures.slice(0, 3)" :key="run.id" :to="`/runs/${run.id}`"><strong>{{ run.title }}</strong><time>{{ compactDate(run.generated_at) }}</time></NuxtLink>
            </div>
            <div v-else-if="summary" class="widget-success"><span aria-hidden="true">✓</span><p><strong>긴급한 오류가 없습니다.</strong><small>최근 Cron 실행이 정상입니다.</small></p></div>
          </article>
        </div>
      </section>

      <section class="dashboard-support-grid">
        <article class="dashboard-widget widget-operations">
          <header class="widget-header"><div><span>OPERATIONS</span><h2>수집 작업 상태</h2></div><NuxtLink to="/?view=operations">자세히 보기 →</NuxtLink></header>
          <div class="operations-summary"><div><span class="live-indicator" />{{ operationKinds.some(kind => summary?.operations[kind].stale) ? '신선도 확인 필요' : '수집·태깅·분류 정상' }}</div><small>backlog {{ summary?.backlog.total || 0 }}개</small></div>
          <div class="compact-job-list">
            <div v-for="kind in operationKinds" :key="kind">
              <span><i :class="summary?.operations[kind].stale ? 'error' : 'success'" />{{ operationLabels[kind] }}</span>
              <b>{{ summary?.operations[kind].stale ? '지연' : operationStatusLabel(summary?.operations[kind].last_attempt?.status) }}</b>
              <time>{{ summary?.operations[kind].last_success_at ? compactDate(summary.operations[kind].last_success_at) : '성공 없음' }}</time>
            </div>
          </div>
        </article>

        <article class="dashboard-widget widget-quicklinks">
          <header class="widget-header"><div><span>SHORTCUTS</span><h2>바로가기</h2></div></header>
          <nav class="quicklink-grid" aria-label="대시보드 바로가기">
            <NuxtLink to="/?view=library"><span>▤</span><b>지식 탐색</b><small>카테고리별 보기</small></NuxtLink>
            <NuxtLink to="/?view=free-question"><span>?</span><b>질문 확인</b><small>대기 인박스</small></NuxtLink>
            <NuxtLink to="/schedule"><span>□</span><b>일정 등록</b><small>오늘 할 일</small></NuxtLink>
            <NuxtLink to="/?view=operations"><span>◉</span><b>운영 상태</b><small>Cron 모니터링</small></NuxtLink>
          </nav>
        </article>
      </section>
    </template>

    <section v-else-if="activeView === 'library'" class="content-view library-view">
      <nav class="library-root-tabs" aria-label="지식 상위 카테고리">
        <button type="button" :class="{ active: !selectedCategoryId }" @click="selectLibrary()">전체 지식</button>
        <button v-for="category in categories" :key="category.id" type="button" :class="{ active: activeRoot?.id === category.id }" @click="selectLibrary(category.id)">{{ category.name }} <span>{{ category.classified_count }}</span></button>
      </nav>
      <nav v-if="selectedTrail.length" class="category-breadcrumb" aria-label="현재 카테고리 경로">
        <button type="button" @click="selectLibrary()">전체 지식</button>
        <template v-for="category in selectedTrail" :key="category.id"><span aria-hidden="true">/</span><button type="button" :aria-current="selectedCategoryId === category.id ? 'page' : undefined" @click="selectLibrary(category.id)">{{ category.name }}</button></template>
      </nav>
      <section class="saved-view-panel" aria-labelledby="saved-view-title">
        <div class="panel-heading"><div><p class="eyebrow">SAVED VIEWS</p><h2 id="saved-view-title">저장된 보기</h2></div><span>{{ savedViews.length }}개</span></div>
        <form class="saved-view-create" @submit.prevent="createSavedView">
          <label><span>현재 조건 이름</span><input v-model="savedViewName" maxlength="100" placeholder="예: 읽지 않은 Slack 질문" required></label>
          <label class="check-field"><input v-model="savedViewDefault" type="checkbox"><span>기본 보기로 저장</span></label>
          <button class="action-button primary" type="submit" :disabled="savingSavedView || !savedViewName.trim()">{{ savingSavedView ? '저장 중…' : '현재 조건 저장' }}</button>
        </form>
        <p v-if="savedViewsError" class="inline-error" role="alert">{{ savedViewsError }}</p>
        <div v-if="savedViews.length" class="saved-view-list">
          <article v-for="view in savedViews" :key="view.id">
            <button class="saved-view-apply" type="button" @click="applySavedView(view)"><strong>{{ view.name }}</strong><small>{{ view.is_default ? '기본 보기 · ' : '' }}{{ view.sort === 'oldest' ? '오래된 순' : '최신순' }}</small></button>
            <div class="saved-view-actions">
              <button type="button" :aria-pressed="view.is_default" @click="toggleSavedViewDefault(view)">{{ view.is_default ? '기본 해제' : '기본' }}</button>
              <button type="button" @click="updateSavedViewFilters(view)">현재 조건 저장</button>
              <button type="button" @click="renameSavedView(view)">이름 변경</button>
              <button class="danger" type="button" @click="deleteSavedView(view)">삭제</button>
            </div>
          </article>
        </div>
      </section>
      <div v-if="secondLevelCategories.length" class="taxonomy-filters">
        <div class="filter-group"><span>세부 분류</span><div class="filter-row"><button v-for="category in secondLevelCategories" :key="category.id" type="button" :class="['filter-chip', { active: activeSecondLevel?.id === category.id }]" @click="selectLibrary(category.id)">{{ category.name }} · {{ category.classified_count }}</button></div></div>
        <div v-if="thirdLevelCategories.length" class="filter-group"><span>하위 분류</span><div class="filter-row"><button v-for="category in thirdLevelCategories" :key="category.id" type="button" :class="['filter-chip', { active: selectedCategoryId === category.id }]" @click="selectLibrary(category.id)">{{ category.name }} · {{ category.classified_count }}</button></div></div>
      </div>
      <form class="board-tools library-tools" role="search" @submit.prevent="applyFilters">
        <label><span>현재 범위 검색</span><input v-model="filterInput" type="search" maxlength="200" :placeholder="`${libraryHeading}에서 검색`"></label>
        <label><span>분류 상태</span><select v-model="filterDraft.status"><option value="all">전체 상태</option><option value="classified">분류 완료</option><option value="pending">분류 대기</option><option value="awaiting_answer">답변 대기</option><option value="needs_review">검토 필요</option></select></label>
        <label><span>출처</span><select v-model="filterDraft.sourceType"><option value="all">모든 출처</option><option value="cron">Cron</option><option value="slack_qa">Slack 질문</option></select></label>
        <label><span>읽음 상태</span><select v-model="filterDraft.read"><option value="all">읽음 전체</option><option value="unread">읽지 않음</option><option value="read">읽음</option></select></label>
        <label><span>보관 상태</span><select v-model="filterDraft.archived"><option value="exclude">보관 제외</option><option value="include">보관 포함</option><option value="only">보관만</option></select></label>
        <label><span>생성일</span><select v-model="filterDraft.period"><option value="all">전체 기간</option><option value="today">오늘</option><option value="7d">최근 7일</option><option value="30d">최근 30일</option><option value="custom">직접 지정</option></select></label>
        <label v-if="filterDraft.period === 'custom'"><span>시작일</span><input v-model="filterDraft.from" type="date" required></label>
        <label v-if="filterDraft.period === 'custom'"><span>종료일</span><input v-model="filterDraft.to" type="date" required></label>
        <label><span>날짜 정렬</span><select v-model="filterDraft.sort"><option value="newest">최신순</option><option value="oldest">오래된 순</option></select></label>
        <label class="check-field"><input v-model="filterDraft.bookmarked" type="checkbox"><span>저장한 항목만</span></label>
        <label class="check-field"><input v-model="filterDraft.completed" type="checkbox"><span>완료한 항목만</span></label>
        <button class="action-button primary" type="submit">적용</button>
      </form>
      <div v-if="selectedTag" class="selected-tag-filter" role="status">
        <span>태그 필터</span>
        <b>#{{ selectedTag }}</b>
        <button type="button" class="text-button" @click="clearTagFilter">해제</button>
      </div>
      <div v-if="knowledgeError" class="notice error-notice" role="alert"><p>{{ knowledgeError }}</p><button class="ghost-button" type="button" @click="loadKnowledge(false)">다시 시도</button></div>
      <div v-else-if="(knowledgePage.loading || baseLoading) && !knowledgeItems.length" class="loading-grid" role="status"><div v-for="index in 6" :key="index" class="skeleton-card" /></div>
      <template v-else-if="knowledgeItems.length">
        <div class="run-grid library-run-grid">
          <KnowledgeCard v-for="item in knowledgeItems" :key="item.id" :item="item" :selected-tag="selectedTag" @tag-click="applyTagFilter" />
        </div>
        <div class="board-pagination"><span>{{ knowledgeItems.length }} / {{ knowledgePage.total }}개 표시<span v-if="knowledgeRemaining"> · {{ knowledgeRemaining }}개 남음</span></span><button v-if="knowledgePage.nextOffset !== null" class="ghost-button" type="button" :disabled="knowledgePage.loading" @click="loadKnowledge(true)">{{ knowledgePage.loading ? '불러오는 중…' : '더 보기' }}</button></div>
      </template>
      <div v-else class="notice">조건에 맞는 지식이 없습니다.</div>
    </section>

    <section v-else-if="activeView === 'free-question'" class="content-view free-question-section">
      <form class="board-tools" role="search" @submit.prevent="applyFilters"><label><span>질문 검색</span><input v-model="filterInput" type="search" maxlength="200" placeholder="질문과 답변에서 검색"></label><label><span>날짜 정렬</span><select :value="sortOrder" @change="changeSort"><option value="newest">최신순</option><option value="oldest">오래된 순</option></select></label><button class="action-button primary" type="submit">적용</button></form>
      <div v-if="freeQuestionError" class="notice error-notice" role="alert">{{ freeQuestionError }}</div>
      <div v-else-if="freeQuestionPage.loading && !freeQuestionItems.length" class="loading-grid"><div v-for="index in 3" :key="index" class="skeleton-card" /></div>
      <template v-else-if="freeQuestionItems.length"><div class="run-grid"><KnowledgeCard v-for="item in freeQuestionItems" :key="item.id" :item="item" :selected-tag="selectedTag" @tag-click="applyTagFilter" /></div><div class="board-pagination"><span>{{ freeQuestionItems.length }} / {{ freeQuestionPage.total }}개 표시<span v-if="freeQuestionRemaining"> · {{ freeQuestionRemaining }}개 남음</span></span><button v-if="freeQuestionPage.nextOffset !== null" class="ghost-button" type="button" :disabled="freeQuestionPage.loading" @click="loadFreeQuestion(true)">더 보기</button></div></template>
      <div v-else class="notice">{{ searchTerm ? '검색 결과가 없습니다.' : '현재 분류를 기다리는 자유 질문이 없습니다.' }}</div>
    </section>

    <section v-else-if="activeView === 'operations'" class="content-view operations-view">
      <p v-if="operationsError" class="notice error-notice" role="alert">{{ operationsError }}</p>
      <template v-if="operationsData">
        <div class="operations-overview"><div><span>분류 대기</span><strong>{{ operationsData.backlog.pending }}</strong></div><div><span>검토 필요</span><strong>{{ operationsData.backlog.review }}</strong></div><div :class="{ warning: operationsData.backlog.total > 0 }"><span>전체 backlog</span><strong>{{ operationsData.backlog.total }}</strong></div></div>
        <div class="freshness-grid">
          <article v-for="kind in operationKinds" :key="kind" :class="['freshness-card', { stale: operationsData.operations[kind].stale }]">
            <header><div><p class="eyebrow">{{ kind.toUpperCase() }}</p><h2>{{ operationLabels[kind] }}</h2></div><span>{{ operationsData.operations[kind].stale ? '신선도 지연' : '신선도 정상' }}</span></header>
            <dl><div><dt>주기</dt><dd>{{ operationsData.operations[kind].schedule_label }}</dd></div><div><dt>최근 성공</dt><dd>{{ operationsData.operations[kind].last_success_at ? compactDate(operationsData.operations[kind].last_success_at) : '성공 기록 없음' }}</dd></div><div><dt>최근 시도</dt><dd>{{ operationsData.operations[kind].last_attempt ? compactDate(operationsData.operations[kind].last_attempt.started_at) : '시도 기록 없음' }}</dd></div><div><dt>상태</dt><dd>{{ operationStatusLabel(operationsData.operations[kind].last_attempt?.status) }}</dd></div></dl>
          </article>
        </div>
        <div class="jobs-table-wrap" role="region" tabindex="0" aria-label="동기화, 태깅, 분류 실행 기록 표, 가로로 스크롤할 수 있습니다"><table class="jobs-table"><caption class="visually-hidden">동기화·태깅·분류 최근 실행 기록</caption><thead><tr><th>작업</th><th>상태</th><th>시작</th><th>종료</th><th>요약</th><th>오류 코드</th></tr></thead><tbody><tr v-for="run in operationsData.results" :key="run.id"><td>{{ operationLabels[run.kind] }}</td><td><span :class="['status-dot', run.status]">{{ operationStatusLabel(run.status) }}</span></td><td>{{ compactDate(run.started_at) }}</td><td>{{ run.finished_at ? compactDate(run.finished_at) : '실행 중' }}</td><td>{{ operationSummaryText(run.summary) }}</td><td><code>{{ run.error_code || '—' }}</code></td></tr></tbody></table></div>
      </template>
      <div v-else-if="!operationsError" class="notice" role="status">운영 상태를 불러오는 중입니다.</div>
    </section>

    <section v-else class="content-view search-results-section">
      <form class="board-tools" role="search" @submit.prevent="applyFilters"><label><span>검색어</span><input v-model="filterInput" type="search" maxlength="200" placeholder="전체 지식 검색"></label><label><span>날짜 정렬</span><select :value="sortOrder" @change="changeSort"><option value="newest">최신순</option><option value="oldest">오래된 순</option></select></label><button class="action-button primary" type="submit">검색</button></form>
      <div v-if="searchError" class="notice error-notice" role="alert">{{ searchError }}</div>
      <div v-else-if="searchPage.loading && !searchItems.length" class="loading-grid"><div v-for="index in 6" :key="index" class="skeleton-card" /></div>
      <template v-else-if="searchItems.length"><div class="run-grid"><KnowledgeCard v-for="item in searchItems" :key="item.id" :item="item" :selected-tag="selectedTag" @tag-click="applyTagFilter" /></div><div class="board-pagination"><span>{{ searchItems.length }} / {{ searchPage.total }}개 표시<span v-if="searchRemaining"> · {{ searchRemaining }}개 남음</span></span><button v-if="searchPage.nextOffset !== null" class="ghost-button" type="button" :disabled="searchPage.loading" @click="loadSearch(true)">더 보기</button></div></template>
      <div v-else class="notice">검색 결과가 없습니다.</div>
    </section>
  </div>
</template>
