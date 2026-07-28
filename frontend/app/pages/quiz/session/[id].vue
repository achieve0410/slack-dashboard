<script setup lang="ts">
import type { QuizAnswerResponse, QuizCurrentItem, QuizSessionResponse } from '~/types/api'
import { apiErrorMessage } from '~/utils/apiError'

const route = useRoute()
const router = useRouter()
const quiz = useQuizApi()
const session = ref<QuizSessionResponse | null>(null)
const feedback = ref<QuizAnswerResponse | null>(null)
const selectedChoiceIds = ref<string[]>([])
const loading = ref(true)
const submitting = ref(false)
const error = ref('')

const currentItem = computed(() => session.value?.current_item || null)
const isMultiple = computed(() => currentItem.value?.question_type === 'multiple_select')
const canSubmit = computed(() => Boolean(currentItem.value) && selectedChoiceIds.value.length > 0 && !submitting.value && !feedback.value)

onMounted(load)

watch(() => route.params.id, load)

async function load() {
  loading.value = true
  error.value = ''
  feedback.value = null
  selectedChoiceIds.value = []
  try {
    const [, data] = await Promise.all([
      quiz.catalog(),
      quiz.session(String(route.params.id)),
    ])
    session.value = data
    if (data.status === 'completed') await router.replace(`/quiz/result/${data.session_id}`)
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '퀴즈 세션을 불러오지 못했습니다.')
  }
  finally {
    loading.value = false
  }
}

function toggleChoice(choiceId: string) {
  if (feedback.value) return
  if (!isMultiple.value) {
    selectedChoiceIds.value = [choiceId]
    return
  }
  selectedChoiceIds.value = selectedChoiceIds.value.includes(choiceId)
    ? selectedChoiceIds.value.filter(id => id !== choiceId)
    : [...selectedChoiceIds.value, choiceId]
}

async function submitAnswer() {
  if (!canSubmit.value || !currentItem.value || !session.value) return
  submitting.value = true
  error.value = ''
  try {
    feedback.value = await quiz.answer(session.value.session_id, currentItem.value.id, selectedChoiceIds.value)
    session.value.progress.answered_count = feedback.value.session_summary.answered_count
    session.value.review_summary.wrong_count = feedback.value.session_summary.incorrect_count
    session.value.items = session.value.items.map(item => (
      item.id === currentItem.value?.id
        ? { ...item, answered: true, correct: feedback.value?.correct || false }
        : item
    ))
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '답안을 제출하지 못했습니다.')
  }
  finally {
    submitting.value = false
  }
}

async function nextQuestion() {
  if (!session.value || !feedback.value) return
  if (!feedback.value.next_item || feedback.value.session_summary.completed) {
    await router.push(`/quiz/result/${session.value.session_id}`)
    return
  }
  session.value.current_item = feedback.value.next_item
  feedback.value = null
  selectedChoiceIds.value = []
}

function choiceInputType(item: QuizCurrentItem): 'checkbox' | 'radio' {
  return item.question_type === 'multiple_select' ? 'checkbox' : 'radio'
}
</script>

<template>
  <div class="page-container quiz-page">
    <header class="page-command-bar">
      <div>
        <p class="eyebrow">QUIZ SESSION</p>
        <h1>{{ session ? `${quiz.domainLabel(session.domain)} · ${quizDifficultyLabels[session.difficulty]}` : '퀴즈 세션' }}</h1>
        <p>{{ session ? `${quizModeLabels[session.mode]} 모드 · ${session.required_count}문제` : '세션을 불러오는 중입니다.' }}</p>
      </div>
      <NuxtLink class="ghost-button" to="/quiz">퀴즈 홈</NuxtLink>
    </header>

    <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>
    <div v-if="loading" class="notice" role="status">세션을 불러오는 중입니다.</div>

    <section v-else-if="session && currentItem" class="quiz-session-layout">
      <aside class="content-view quiz-session-side" aria-label="풀이 진행 상태">
        <div class="panel-heading"><div><p class="eyebrow">PROGRESS</p><h2>진행률</h2></div><span>{{ session.progress.answered_count }} / {{ session.progress.total_count }}</span></div>
        <ol class="quiz-step-list">
          <li v-for="item in session.items" :key="item.id" :class="{ done: item.answered, current: item.id === currentItem.id }">
            <span>{{ item.position }}</span>
            <b>{{ item.answered ? (item.correct ? '정답' : '오답') : item.id === currentItem.id ? '풀이 중' : '대기' }}</b>
          </li>
        </ol>
      </aside>

      <article class="content-view quiz-question-card" aria-labelledby="quiz-current-question">
        <div class="quiz-question-meta">
          <span>{{ currentItem.position }} / {{ session.required_count }}</span>
          <b>{{ currentItem.question_type === 'multiple_select' ? '복수 선택' : '단일 선택' }}</b>
        </div>
        <h2 id="quiz-current-question">{{ currentItem.prompt }}</h2>

        <form class="quiz-choice-list" @submit.prevent="submitAnswer">
          <label v-for="choice in currentItem.choices" :key="choice.id" :class="{ selected: selectedChoiceIds.includes(choice.id), locked: Boolean(feedback) }">
            <input
              :type="choiceInputType(currentItem)"
              name="quiz-choice"
              :value="choice.id"
              :checked="selectedChoiceIds.includes(choice.id)"
              :disabled="Boolean(feedback)"
              @change="toggleChoice(choice.id)"
            >
            <span>{{ choice.label }}</span>
          </label>
          <button class="action-button primary" type="submit" :disabled="!canSubmit">{{ submitting ? '채점 중…' : '답안 제출' }}</button>
        </form>

        <section v-if="feedback" class="quiz-feedback" :class="{ correct: feedback.correct }" aria-live="polite">
          <div><span>{{ feedback.correct ? '정답' : '오답' }}</span><strong>{{ feedback.correct ? '좋습니다. 다음 문제로 넘어갑니다.' : '오답노트에 자동 반영했습니다.' }}</strong></div>
          <p>{{ feedback.explanation }}</p>
          <NuxtLink :to="feedback.source.detail_url">관련 지식 열기</NuxtLink>
          <button class="action-button" type="button" @click="nextQuestion">
            {{ feedback.next_item ? '다음 문제' : '결과 보기' }}
          </button>
        </section>
      </article>
    </section>

    <p v-else-if="session" class="panel-empty">진행할 문제가 없습니다.</p>
  </div>
</template>
