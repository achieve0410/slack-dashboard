<script setup lang="ts">
import { apiErrorMessage } from '~/utils/apiError'

const route = useRoute()
const router = useRouter()
const headerQuery = ref('')
const sidebar = ref<HTMLElement | null>(null)
const menuToggle = ref<HTMLButtonElement | null>(null)
const sidebarOpen = ref(false)
const sidebarCollapsed = ref(false)
const isMobileSidebar = ref(false)
const { request } = useApi()
const { applyLocale, initializeLocale, locale, t } = useDashboardLocale()
const undoToken = useState<string>('bulk-undo-token', () => '')
const knowledgeReload = useState<number>('knowledge-reload', () => 0)
const undoError = ref('')
const loggingOut = ref(false)
let mobileSidebarQuery: MediaQueryList | undefined

watch(
  () => route.query.q,
  value => headerQuery.value = typeof value === 'string' ? value : '',
  { immediate: true },
)
watch(() => route.fullPath, () => {
  if (sidebarOpen.value && isMobileSidebar.value) void closeSidebar()
  else sidebarOpen.value = false
})

onMounted(() => {
  initializeLocale()
  sidebarCollapsed.value = localStorage.getItem('dashboard:sidebar-collapsed') === '1'
  mobileSidebarQuery = window.matchMedia('(max-width: 900px)')
  isMobileSidebar.value = mobileSidebarQuery.matches
  mobileSidebarQuery.addEventListener('change', handleMobileSidebarChange)
})

onBeforeUnmount(() => mobileSidebarQuery?.removeEventListener('change', handleMobileSidebarChange))

function handleMobileSidebarChange(event: MediaQueryListEvent) {
  isMobileSidebar.value = event.matches
  if (!event.matches) sidebarOpen.value = false
}

async function openSidebar() {
  sidebarOpen.value = true
  await nextTick()
  sidebar.value?.querySelector<HTMLElement>('a')?.focus()
}

async function closeSidebar() {
  sidebarOpen.value = false
  await nextTick()
  menuToggle.value?.focus()
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem('dashboard:sidebar-collapsed', sidebarCollapsed.value ? '1' : '0')
}

const activeSection = computed(() => {
  if (route.path === '/ask') return 'ask'
  if (route.path.startsWith('/quiz')) return 'quiz'
  if (route.path === '/schedule') return 'schedule'
  if (route.path === '/trash') return 'trash'
  if (route.path === '/api-tokens') return 'api-tokens'
  if (route.path === '/api-guide') return 'api-guide'
  if (route.path.startsWith('/knowledge/') || route.path.startsWith('/runs/')) return 'library'
  const view = typeof route.query.view === 'string' ? route.query.view : ''
  if (view === 'free-question') return 'free-question'
  if (view === 'operations') return 'operations'
  if (view === 'library' || route.query.category) return 'library'
  if (view === 'search') return 'search'
  return 'dashboard'
})

const pageTitle = computed(() => ({
  dashboard: t('dashboard'),
  library: t('library'),
  'free-question': t('freeQuestion'),
  ask: t('ask'),
  operations: t('operations'),
  quiz: t('quiz'),
  schedule: t('schedule'),
  trash: t('trash'),
  'api-tokens': t('apiTokens'),
  'api-guide': t('apiGuide'),
  search: t('search'),
}[activeSection.value] || 'Slack Dashboard'))

async function submitGlobalSearch() {
  const query = headerQuery.value.trim()
  if (!query) return
  await router.push({ path: '/', query: { view: 'search', q: query } })
}

async function undoBulkHide() {
  if (!undoToken.value) return
  undoError.value = ''
  try {
    await request('/api/knowledge/bulk/undo/', { method: 'POST', body: { token: undoToken.value } })
    undoToken.value = ''
    knowledgeReload.value += 1
  }
  catch (reason) {
    undoError.value = apiErrorMessage(reason, '복원 시간이 지났거나 복원할 수 없습니다.')
    undoToken.value = ''
  }
}

async function logout() {
  if (loggingOut.value) return
  loggingOut.value = true
  try {
    await request('/accounts/logout/', { method: 'POST' })
  }
  finally {
    window.location.assign('/accounts/login/')
  }
}
</script>

<template>
  <div :class="['app-shell', { 'sidebar-collapsed': sidebarCollapsed }]">
    <aside id="primary-sidebar" ref="sidebar" :class="['app-sidebar', { open: sidebarOpen, collapsed: sidebarCollapsed }]" aria-label="주요 메뉴" :aria-hidden="isMobileSidebar && !sidebarOpen ? 'true' : undefined" :inert="isMobileSidebar && !sidebarOpen" @keydown.esc="closeSidebar">
      <div class="sidebar-head">
        <NuxtLink to="/" class="sidebar-brand" aria-label="Slack Dashboard 홈">
          <span class="brand-mark">S</span>
          <span class="brand-copy"><strong>Slack Dashboard</strong><small>Knowledge Workspace</small></span>
        </NuxtLink>
        <button class="sidebar-collapse" type="button" :aria-label="sidebarCollapsed ? '메뉴 펼치기' : '메뉴 접기'" :title="sidebarCollapsed ? '메뉴 펼치기' : '메뉴 접기'" @click="toggleSidebar">
          {{ sidebarCollapsed ? '›' : '‹' }}
        </button>
      </div>

      <nav class="sidebar-nav">
        <p>WORKSPACE</p>
        <NuxtLink to="/" title="대시보드" :class="{ active: activeSection === 'dashboard' }"><span aria-hidden="true">⌂</span><b>{{ t('dashboard') }}</b></NuxtLink>
        <NuxtLink to="/?view=library" title="지식 라이브러리" :class="{ active: activeSection === 'library' }"><span aria-hidden="true">▤</span><b>{{ t('library') }}</b></NuxtLink>
        <NuxtLink to="/?view=free-question" title="자유 질문" :class="{ active: activeSection === 'free-question' }"><span aria-hidden="true">?</span><b>{{ t('freeQuestion') }}</b></NuxtLink>
        <NuxtLink to="/ask" title="지식에게 질문" :class="{ active: activeSection === 'ask' }"><span aria-hidden="true">⌕</span><b>{{ t('ask') }}</b></NuxtLink>
        <NuxtLink to="/quiz" title="퀴즈" :class="{ active: activeSection === 'quiz' }"><span aria-hidden="true">✓</span><b>{{ t('quiz') }}</b></NuxtLink>
        <NuxtLink to="/schedule" title="일정 관리" :class="{ active: activeSection === 'schedule' }"><span aria-hidden="true">□</span><b>{{ t('schedule') }}</b></NuxtLink>
        <NuxtLink to="/trash" title="휴지통" :class="{ active: activeSection === 'trash' }"><span aria-hidden="true">♲</span><b>{{ t('trash') }}</b></NuxtLink>

        <p>OPERATIONS</p>
        <NuxtLink to="/?view=operations" title="Cron 실행 상태" :class="{ active: activeSection === 'operations' }"><span aria-hidden="true">◉</span><b>{{ t('operations') }}</b></NuxtLink>
        <NuxtLink to="/api-tokens" title="API 토큰 관리" :class="{ active: activeSection === 'api-tokens' }"><span aria-hidden="true">◇</span><b>{{ t('apiTokens') }}</b></NuxtLink>
        <NuxtLink to="/api-guide" title="API 가이드" :class="{ active: activeSection === 'api-guide' }"><span aria-hidden="true">≡</span><b>{{ t('apiGuide') }}</b></NuxtLink>
      </nav>

      <div class="sidebar-footer">
        <div class="private-status"><span class="live-indicator" aria-hidden="true" /><span><b>Local workspace</b><small>Session-authenticated</small></span></div>
        <button class="sidebar-logout" type="button" :disabled="loggingOut" title="로그아웃" @click="logout"><span aria-hidden="true">↪</span><b>{{ loggingOut ? t('loggingOut') : t('logout') }}</b></button>
        <small>Slack Dashboard</small>
      </div>
    </aside>
    <button v-if="sidebarOpen" class="sidebar-scrim" type="button" :aria-label="t('menuClose')" @click="closeSidebar" />

    <div class="workspace-shell">
      <header class="workspace-topbar">
        <div class="topbar-title">
          <NuxtLink to="/" class="mobile-home-mark" aria-label="Slack Dashboard 홈">S</NuxtLink>
          <button ref="menuToggle" class="menu-toggle" type="button" :aria-label="t('menuOpen')" aria-controls="primary-sidebar" :aria-expanded="sidebarOpen" @click="openSidebar">☰</button>
          <div><span>Slack Dashboard</span><strong>{{ pageTitle }}</strong></div>
        </div>
        <form class="header-search" role="search" @submit.prevent="submitGlobalSearch">
          <label class="visually-hidden" for="global-search">{{ t('searchPlaceholder') }}</label>
          <span aria-hidden="true">⌕</span>
          <input id="global-search" v-model="headerQuery" type="search" maxlength="200" :placeholder="t('searchPlaceholder')">
          <button type="submit" :disabled="!headerQuery.trim()">{{ t('searchAction') }}</button>
        </form>
        <div class="locale-switch" role="group" aria-label="Language">
          <button type="button" :class="{ active: locale === 'ko' }" :aria-pressed="locale === 'ko'" @click="applyLocale('ko')">KO</button>
          <button type="button" :class="{ active: locale === 'en' }" :aria-pressed="locale === 'en'" @click="applyLocale('en')">EN</button>
        </div>
      </header>
      <main>
        <NuxtPage />
      </main>
    </div>
    <div v-if="undoToken" class="undo-toast" role="status" aria-live="polite"><span>항목을 휴지통으로 옮겼습니다.</span><button type="button" @click="undoBulkHide">10초 내 실행 취소</button></div>
    <div v-if="undoError" class="undo-toast error-notice" role="alert">{{ undoError }}</div>
  </div>
</template>
