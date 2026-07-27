<script setup lang="ts">
import type { QuizResultItem, QuizResultResponse } from '~/types/api'
import { apiErrorMessage } from '~/utils/apiError'

const route = useRoute()
const quiz = useQuizApi()
const result = ref<QuizResultResponse | null>(null)
const manualWrongNotes = reactive<Record<number, boolean>>({})
const togglingId = ref<number | null>(null)
const loading = ref(true)
const error = ref('')

const wrongItems = computed(() => result.value?.item_results.filter(item => !item.correct) || [])

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    result.value = await quiz.result(String(route.params.id))
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '퀴즈 결과를 불러오지 못했습니다.')
  }
  finally {
    loading.value = false
  }
}

async function toggleWrongNote(item: QuizResultItem) {
  togglingId.value = item.question_id
  error.value = ''
  try {
    const nextValue = !manualWrongNotes[item.question_id]
    await quiz.wrongNote(item.question_id, nextValue)
    manualWrongNotes[item.question_id] = nextValue
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '오답노트를 수정하지 못했습니다.')
  }
  finally {
    togglingId.value = null
  }
}
</script>

<template>
  <div class="page-container quiz-page">
    <header class="page-command-bar">
      <div>
        <p class="eyebrow">QUIZ RESULT</p>
        <h1>퀴즈 결과</h1>
        <p>틀린 문제를 오답노트에 남기고 관련 지식을 다시 확인합니다.</p>
      </div>
      <div class="quiz-header-actions">
        <NuxtLink class="ghost-button" to="/quiz">퀴즈 홈</NuxtLink>
        <NuxtLink class="ghost-button" to="/quiz/review">오답노트</NuxtLink>
      </div>
    </header>

    <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>
    <div v-if="loading" class="notice" role="status">결과를 불러오는 중입니다.</div>

    <template v-else-if="result">
      <section class="quiz-metrics" aria-label="결과 요약">
        <article><span>점수</span><strong>{{ result.score }} / {{ result.item_results.length }}</strong><small>총 풀이 수</small></article>
        <article><span>정답</span><strong>{{ result.correct_count }}</strong><small>맞힌 문제</small></article>
        <article><span>오답</span><strong>{{ result.incorrect_count }}</strong><small>다시 볼 문제</small></article>
        <article><span>마스터</span><strong>{{ result.mastered_count }}</strong><small>장기 복습 완료</small></article>
        <article><span>완료</span><strong>{{ result.status === 'completed' ? '완료' : '진행 중' }}</strong><small>{{ new Date(result.completed_at).toLocaleString('ko-KR') }}</small></article>
      </section>

      <section class="content-view quiz-result-panel" aria-labelledby="quiz-wrong-title">
        <div class="panel-heading"><div><p class="eyebrow">WRONG ANSWERS</p><h2 id="quiz-wrong-title">오답 정리</h2></div><span>{{ wrongItems.length }}개</span></div>
        <div v-if="wrongItems.length" class="quiz-result-list">
          <article v-for="item in wrongItems" :key="item.item_id">
            <div>
              <span>{{ item.position }}번</span>
              <strong>{{ item.prompt }}</strong>
              <p>{{ item.explanation }}</p>
              <NuxtLink :to="item.source.detail_url">{{ item.source.title }}</NuxtLink>
            </div>
            <button class="action-button" type="button" :disabled="togglingId === item.question_id" @click="toggleWrongNote(item)">
              {{ manualWrongNotes[item.question_id] ? '오답노트 해제' : '오답노트 추가' }}
            </button>
          </article>
        </div>
        <p v-else class="panel-empty">틀린 문제가 없습니다.</p>
      </section>
    </template>
  </div>
</template>
