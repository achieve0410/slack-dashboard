<script setup lang="ts">
import type { QuizCatalogResponse, QuizDifficulty, QuizDomain, QuizMode, QuizReviewResponse, QuizSessionHistoryItem } from '~/types/api'
import { apiErrorCode, apiErrorMessage } from '~/utils/apiError'

const router = useRouter()
const quiz = useQuizApi()
const catalog = ref<QuizCatalogResponse | null>(null)
const review = ref<QuizReviewResponse | null>(null)
const history = ref<QuizSessionHistoryItem[]>([])
const loading = ref(true)
const starting = ref(false)
const error = ref('')
const startError = ref('')
const filters = reactive<{ domain: QuizDomain, difficulty: QuizDifficulty, mode: QuizMode }>({
  domain: 'english',
  difficulty: 'beginner',
  mode: 'new',
})

const domains: QuizDomain[] = ['english', 'japanese', 'aws_saa']
const difficulties: QuizDifficulty[] = ['beginner', 'intermediate', 'advanced']
const modes: QuizMode[] = ['new', 'review', 'wrong']

const availableCount = computed(() => catalog.value?.available_counts[quizAvailableKey(filters.domain, filters.difficulty)] || 0)
const canStart = computed(() => availableCount.value >= 10 && !starting.value)
const duePreview = computed(() => review.value?.items.slice(0, 5) || [])

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  startError.value = ''
  try {
    const [catalogData, reviewData, historyData] = await Promise.all([
      quiz.catalog(),
      quiz.review({ dueOnly: false }),
      quiz.history(20),
    ])
    catalog.value = catalogData
    review.value = reviewData
    history.value = historyData.results
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '퀴즈 정보를 불러오지 못했습니다.')
  }
  finally {
    loading.value = false
  }
}

async function start() {
  if (starting.value) return
  starting.value = true
  startError.value = ''
  try {
    const session = await quiz.startSession(filters)
    await router.push(`/quiz/session/${session.session_id}`)
  }
  catch (reason) {
    if (apiErrorCode(reason) === 'quiz_pool_shortage') {
      startError.value = `${quizDomainLabels[filters.domain]} ${quizDifficultyLabels[filters.difficulty]} 문제는 현재 ${availableCount.value}개입니다. 10개 이상 생성된 뒤 시작할 수 있습니다.`
    }
    else {
      startError.value = apiErrorMessage(reason, '퀴즈 세션을 시작하지 못했습니다.')
    }
  }
  finally {
    starting.value = false
  }
}

function formatDate(value: string | null): string {
  if (!value) return '예정 없음'
  return new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function historyProgress(item: QuizSessionHistoryItem): string {
  if (item.status === 'completed') return `${item.score} / ${item.total_count}`
  return `${item.answered_count} / ${item.total_count}`
}
</script>

<template>
  <div class="page-container quiz-page">
    <header class="page-command-bar">
      <div>
        <p class="eyebrow">KNOWLEDGE QUIZ</p>
        <h1>지식 기반 퀴즈</h1>
        <p>영어·일본어·AWS SAA 지식을 10문제 단위로 풀고 오답을 복습합니다.</p>
      </div>
      <div class="quiz-header-actions">
        <NuxtLink class="ghost-button" to="/quiz/review">오답노트</NuxtLink>
        <button class="ghost-button" type="button" :disabled="loading" @click="load">새로고침</button>
      </div>
    </header>

    <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>

    <template v-if="!loading && !error">
      <section class="quiz-metrics" aria-label="학습 지표">
        <article>
          <span>오늘 목표</span>
          <strong>{{ review?.today_goal.completed || 0 }} / {{ review?.today_goal.target || 10 }}</strong>
          <small>{{ review?.today_goal.remaining || 0 }}문제 남음</small>
        </article>
        <article>
          <span>연속 학습</span>
          <strong>{{ review?.streak.current_days || 0 }}일</strong>
          <small>완료 세션 기준</small>
        </article>
        <article>
          <span>복습 예정</span>
          <strong>{{ review?.due_count || 0 }}</strong>
          <small>오늘 다시 볼 문제</small>
        </article>
        <article>
          <span>선택 문제 은행</span>
          <strong>{{ availableCount }}</strong>
          <small>세션 시작 기준 10문제 필요</small>
        </article>
      </section>

      <section class="quiz-workbench">
        <article class="content-view quiz-start-panel" aria-labelledby="quiz-start-title">
          <div class="panel-heading">
            <div><p class="eyebrow">SESSION</p><h2 id="quiz-start-title">학습 세션 시작</h2></div>
            <span>{{ quizModeLabels[filters.mode] }}</span>
          </div>

          <div v-if="catalog?.empty_state" class="panel-empty">
            아직 게시된 퀴즈 문제가 없습니다. 퀴즈 생성 배치가 게시된 뒤 시작할 수 있습니다.
          </div>

          <form v-else class="quiz-start-form" @submit.prevent="start">
            <fieldset>
              <legend>분야</legend>
              <button v-for="domain in domains" :key="domain" type="button" :class="['filter-chip', { active: filters.domain === domain }]" @click="filters.domain = domain">{{ quizDomainLabels[domain] }}</button>
            </fieldset>
            <fieldset>
              <legend>난이도</legend>
              <button v-for="difficulty in difficulties" :key="difficulty" type="button" :class="['filter-chip', { active: filters.difficulty === difficulty }]" @click="filters.difficulty = difficulty">{{ quizDifficultyLabels[difficulty] }}</button>
            </fieldset>
            <fieldset>
              <legend>모드</legend>
              <button v-for="mode in modes" :key="mode" type="button" :class="['filter-chip', { active: filters.mode === mode }]" @click="filters.mode = mode">{{ quizModeLabels[mode] }}</button>
            </fieldset>

            <div class="quiz-start-summary">
              <span>현재 조건</span>
              <strong>{{ quizDomainLabels[filters.domain] }} · {{ quizDifficultyLabels[filters.difficulty] }} · {{ quizModeLabels[filters.mode] }}</strong>
              <small>사용 가능 {{ availableCount }}문제</small>
            </div>
            <p v-if="startError" class="notice error-notice" role="alert">{{ startError }}</p>
            <button class="action-button primary" type="submit" :disabled="!canStart">{{ starting ? '시작 중…' : '10문제 시작' }}</button>
          </form>
        </article>

        <aside class="content-view quiz-review-panel" aria-labelledby="quiz-review-preview-title">
          <div class="panel-heading">
            <div><p class="eyebrow">REVIEW</p><h2 id="quiz-review-preview-title">복습 큐</h2></div>
            <span>{{ review?.items.length || 0 }}개</span>
          </div>
          <div v-if="duePreview.length" class="quiz-review-list compact">
            <article v-for="item in duePreview" :key="item.question_id">
              <span>{{ quizDomainLabels[item.domain] }} · {{ quizDifficultyLabels[item.difficulty] }}</span>
              <strong>{{ item.source.title }}</strong>
              <small>{{ item.next_review_at ? formatDate(item.next_review_at) : '수동 오답노트' }} · {{ item.stage }}</small>
            </article>
          </div>
          <p v-else class="panel-empty">지금 복습할 문제가 없습니다.</p>
          <NuxtLink class="action-button" to="/quiz/review">오답노트 자세히 보기</NuxtLink>
        </aside>
      </section>

      <section class="content-view quiz-history-panel" aria-labelledby="quiz-history-title">
        <div class="panel-heading">
          <div><p class="eyebrow">HISTORY</p><h2 id="quiz-history-title">최근 학습</h2></div>
          <span>{{ history.length }}개</span>
        </div>
        <div v-if="history.length" class="quiz-history-list">
          <NuxtLink
            v-for="item in history"
            :key="item.session_id"
            :to="item.status === 'completed' ? `/quiz/result/${item.session_id}` : `/quiz/session/${item.session_id}`"
          >
            <div>
              <span>{{ quizDomainLabels[item.domain] }} · {{ quizDifficultyLabels[item.difficulty] }} · {{ quizModeLabels[item.mode] }}</span>
              <strong>{{ item.status === 'completed' ? '결과 보기' : '이어풀기' }}</strong>
              <small>{{ item.status === 'completed' ? formatDate(item.completed_at) : formatDate(item.started_at) }}</small>
            </div>
            <b>{{ historyProgress(item) }}</b>
          </NuxtLink>
        </div>
        <p v-else class="panel-empty">아직 시작한 퀴즈 세션이 없습니다.</p>
      </section>
    </template>

    <div v-else-if="loading" class="notice" role="status">퀴즈 정보를 불러오는 중입니다.</div>
  </div>
</template>
