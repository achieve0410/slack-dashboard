<script setup lang="ts">
import type { QuizCatalogResponse, QuizDifficulty, QuizDomain, QuizReviewItem, QuizReviewResponse } from '~/types/api'
import { apiErrorMessage } from '~/utils/apiError'

const router = useRouter()
const quiz = useQuizApi()
const catalog = ref<QuizCatalogResponse | null>(null)
const domains = computed<Array<QuizDomain | ''>>(() => ['', ...(catalog.value?.domains || [])])
const difficulties: Array<QuizDifficulty | ''> = ['', 'beginner', 'intermediate', 'advanced']
const filters = reactive<{ domain: QuizDomain | '', difficulty: QuizDifficulty | '', dueOnly: boolean }>({
  domain: '',
  difficulty: '',
  dueOnly: true,
})
const review = ref<QuizReviewResponse | null>(null)
const loading = ref(true)
const starting = ref(false)
const togglingId = ref<number | null>(null)
const error = ref('')

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [catalogData, reviewData] = await Promise.all([
      quiz.catalog(),
      quiz.review({
        domain: filters.domain || undefined,
        difficulty: filters.difficulty || undefined,
        dueOnly: filters.dueOnly,
      }),
    ])
    catalog.value = catalogData
    review.value = reviewData
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '오답노트를 불러오지 못했습니다.')
  }
  finally {
    loading.value = false
  }
}

async function startMode(mode: 'review' | 'wrong', item?: QuizReviewItem) {
  if (starting.value) return
  starting.value = true
  error.value = ''
  const domain = item?.domain || filters.domain || catalog.value?.domains[0] || 'english'
  const difficulty = item?.difficulty || filters.difficulty || 'beginner'
  try {
    const session = await quiz.startSession({ domain, difficulty, mode })
    await router.push(`/quiz/session/${session.session_id}`)
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '복습 세션을 시작하지 못했습니다.')
  }
  finally {
    starting.value = false
  }
}

async function toggleWrongNote(item: QuizReviewItem) {
  togglingId.value = item.question_id
  error.value = ''
  try {
    const nextValue = !item.manual_wrong_note_at
    const updated = await quiz.wrongNote(item.question_id, nextValue)
    item.manual_wrong_note_at = updated.progress.manual_wrong_note_at
    item.next_review_at = updated.progress.next_review_at
    item.wrong_count = updated.progress.wrong_count
    item.stage = updated.progress.stage
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '오답노트를 수정하지 못했습니다.')
  }
  finally {
    togglingId.value = null
  }
}

function dateLabel(value: string | null): string {
  if (!value) return '복습일 없음'
  return new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}
</script>

<template>
  <div class="page-container quiz-page">
    <header class="page-command-bar">
      <div>
        <p class="eyebrow">WRONG NOTE</p>
        <h1>오답노트</h1>
        <p>틀린 문제와 복습 주기를 확인하고 바로 복습 세션을 시작합니다.</p>
      </div>
      <NuxtLink class="ghost-button" to="/quiz">퀴즈 홈</NuxtLink>
    </header>

    <section class="content-view quiz-review-controls" aria-label="오답노트 필터">
      <label><span>분야</span><select v-model="filters.domain" @change="load"><option v-for="domain in domains" :key="domain || 'all'" :value="domain">{{ domain ? quiz.domainLabel(domain) : '전체' }}</option></select></label>
      <label><span>난이도</span><select v-model="filters.difficulty" @change="load"><option v-for="difficulty in difficulties" :key="difficulty || 'all'" :value="difficulty">{{ difficulty ? quizDifficultyLabels[difficulty] : '전체' }}</option></select></label>
      <label class="check-field"><input v-model="filters.dueOnly" type="checkbox" @change="load"><span>오늘 복습할 문제만</span></label>
      <button class="action-button" type="button" :disabled="loading" @click="load">적용</button>
    </section>

    <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>

    <section class="quiz-metrics" aria-label="복습 지표">
      <article><span>오늘 목표</span><strong>{{ review?.today_goal.completed || 0 }} / {{ review?.today_goal.target || 10 }}</strong><small>{{ review?.today_goal.remaining || 0 }}문제 남음</small></article>
      <article><span>연속 학습</span><strong>{{ review?.streak.current_days || 0 }}일</strong><small>완료 세션 기준</small></article>
      <article><span>오늘 복습</span><strong>{{ review?.due_count || 0 }}</strong><small>due-only 기준</small></article>
      <article><span>마지막 풀이</span><strong>{{ review?.last_reviewed_at ? '있음' : '없음' }}</strong><small>{{ dateLabel(review?.last_reviewed_at || null) }}</small></article>
    </section>

    <div class="quiz-review-actions">
      <button class="action-button primary" type="button" :disabled="starting" @click="startMode('review')">{{ starting ? '시작 중…' : '복습 세션 시작' }}</button>
      <button class="ghost-button" type="button" :disabled="starting" @click="startMode('wrong')">오답 즉시 다시 풀기</button>
    </div>

    <div v-if="loading" class="notice" role="status">오답노트를 불러오는 중입니다.</div>
    <section v-else-if="review?.items.length" class="quiz-review-list" aria-label="오답노트 항목">
      <article v-for="item in review.items" :key="item.question_id">
        <div>
          <span>{{ quiz.domainLabel(item.domain) }} · {{ quizDifficultyLabels[item.difficulty] }} · {{ item.stage }}</span>
          <strong>{{ item.source.title }}</strong>
          <small>다음 복습 {{ dateLabel(item.next_review_at) }} · 오답 {{ item.wrong_count }}회 · 연속 정답 {{ item.correct_streak }}회</small>
          <NuxtLink :to="item.source.detail_url">원본 지식 열기</NuxtLink>
        </div>
        <div class="quiz-review-card-actions">
          <button class="ghost-button" type="button" :disabled="starting" @click="startMode('wrong', item)">다시 풀기</button>
          <button class="action-button" type="button" :disabled="togglingId === item.question_id" @click="toggleWrongNote(item)">
            {{ item.manual_wrong_note_at ? '오답노트 해제' : '오답노트 추가' }}
          </button>
        </div>
      </article>
    </section>
    <p v-else class="panel-empty">현재 조건에 해당하는 오답노트 항목이 없습니다.</p>
  </div>
</template>
