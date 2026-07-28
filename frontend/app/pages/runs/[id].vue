<script setup lang="ts">
import type { KnowledgeDetail, KnowledgeNavigationResponse, KnowledgeVerification, RunDetail, RunState, UserResponse } from '~/types/api'
import {
  affectsKnowledgeMembership,
  canEditRunConsumptionState,
  updateKnowledgeRemoval,
} from '~/utils/knowledgeState'
import { knowledgeQueryParams } from '~/utils/knowledgeQuery'
import { apiErrorCode, apiErrorMessage } from '~/utils/apiError'

const route = useRoute()
const router = useRouter()
const { request } = useApi()
const run = ref<RunDetail | null>(null)
const loading = ref(true)
const error = ref('')
const responseText = ref('')
const questionKey = ref('')
const savingResponse = ref(false)
const savingState = ref(false)
const stateError = ref('')
const deleting = ref(false)
const deleteError = ref('')
const navigation = ref<KnowledgeNavigationResponse | null>(null)
const navigationError = ref('')
const knowledgeUpdates = useState<Record<number, KnowledgeDetail>>('knowledge-updates', () => ({}))
const knowledgeRemovals = useState<Record<number, boolean>>('knowledge-removals', () => ({}))
const canEditConsumptionState = computed(() => canEditRunConsumptionState(run.value))

async function load() {
  loading.value = true
  error.value = ''
  try {
    run.value = await request<RunDetail>(`/api/runs/${route.params.id}/`)
    loading.value = false
    await nextTick()
    if (run.value.knowledge_item && !run.value.state.read) {
      await updateState({ read: true })
    }
    else if (run.value.knowledge_item) await loadNavigation(run.value.knowledge_item.id)
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '콘텐츠를 불러오지 못했습니다.'
  }
  finally {
    loading.value = false
  }
}

async function loadNavigation(itemId: number) {
  navigation.value = null
  navigationError.value = ''
  try {
    navigation.value = await request<KnowledgeNavigationResponse>(
      `/api/knowledge/${itemId}/navigation/?${knowledgeQueryParams(route.query)}`,
    )
  }
  catch (reason) {
    navigationError.value = apiErrorCode(reason) === 'context_changed'
      ? '현재 항목이 목록 조건에서 벗어났습니다. 목록을 새로고침해 주세요.'
      : apiErrorMessage(reason, '목록 이동 정보를 불러오지 못했습니다.')
  }
}

async function updateState(patch: Partial<RunState>) {
  if (!run.value?.knowledge_item) return
  savingState.value = true
  stateError.value = ''
  try {
    run.value.state = await request<RunState>(`/api/runs/${run.value.id}/state/`, {
      method: 'PATCH',
      body: patch,
    })
    if (run.value.knowledge_item) {
      const updated = { ...run.value.knowledge_item, state: run.value.state }
      run.value.knowledge_item = updated
      knowledgeUpdates.value = { ...knowledgeUpdates.value, [updated.id]: updated }
      knowledgeRemovals.value = updateKnowledgeRemoval(
        knowledgeRemovals.value,
        updated.id,
        run.value.state.archived,
      )
    }
  }
  catch (reason) {
    stateError.value = reason instanceof Error ? reason.message : '소비 상태를 저장하지 못했습니다.'
  }
  finally {
    savingState.value = false
    if (run.value?.knowledge_item && affectsKnowledgeMembership(patch)) {
      await loadNavigation(run.value.knowledge_item.id)
    }
  }
}

async function submitResponse() {
  if (!run.value || !responseText.value.trim()) return
  savingResponse.value = true
  try {
    const saved = await request<UserResponse>(`/api/runs/${run.value.id}/responses/`, {
      method: 'POST',
      body: { question_key: questionKey.value, answer: responseText.value },
    })
    run.value.responses.unshift(saved)
    responseText.value = ''
    questionKey.value = ''
  }
  finally {
    savingResponse.value = false
  }
}

function goBack() {
  if (import.meta.client && window.history.state?.back) {
    router.back()
    return
  }
  const fallback = import.meta.client
    ? sessionStorage.getItem('dashboard:list-return') || '/?view=library'
    : '/?view=library'
  router.push(fallback)
}

async function handleClassification(updated: KnowledgeDetail) {
  if (!run.value) return
  run.value.knowledge_item = updated
  knowledgeUpdates.value = { ...knowledgeUpdates.value, [updated.id]: updated }
  await loadNavigation(updated.id)
}

function handleTagsUpdated(tags: string[]) {
  if (!run.value?.knowledge_item) return
  const updated = { ...run.value.knowledge_item, tags }
  run.value.knowledge_item = updated
  knowledgeUpdates.value = { ...knowledgeUpdates.value, [updated.id]: updated }
}

function handleVerificationUpdated(verification: KnowledgeVerification) {
  if (!run.value?.knowledge_item) return
  const updated = { ...run.value.knowledge_item, verification }
  run.value.knowledge_item = updated
  knowledgeUpdates.value = { ...knowledgeUpdates.value, [updated.id]: updated }
}

async function deleteRun() {
  if (!run.value || !window.confirm('Cron 실행 원본은 유지됩니다. 이 콘텐츠를 대시보드에서 삭제할까요?')) return
  deleting.value = true
  deleteError.value = ''
  try {
    await request(`/api/runs/${run.value.id}/`, { method: 'DELETE' })
    if (run.value.knowledge_item) {
      knowledgeRemovals.value = {
        ...knowledgeRemovals.value,
        [run.value.knowledge_item.id]: true,
      }
    }
    goBack()
  }
  catch (reason) {
    deleteError.value = reason instanceof Error ? reason.message : '콘텐츠를 삭제하지 못했습니다.'
  }
  finally {
    deleting.value = false
  }
}

watch(() => route.params.id, load, { immediate: true })
</script>

<template>
  <div class="detail-page">
    <button type="button" class="back-link" @click="goBack">← 이전 목록</button>
    <div v-if="deleteError" class="notice error-notice" role="alert">{{ deleteError }}</div>
    <div v-if="stateError" class="notice error-notice" role="alert">{{ stateError }}</div>
    <div v-if="loading" class="notice" role="status" aria-live="polite">콘텐츠를 불러오는 중입니다.</div>
    <div v-else-if="error || !run" class="notice error-notice detail-error" role="alert">
      <p>{{ error || '콘텐츠가 없습니다.' }}</p>
      <button class="ghost-button" type="button" @click="load">다시 시도</button>
    </div>
    <template v-else>
      <header class="detail-header" :class="`category-${run.job.category}`" aria-labelledby="run-title">
        <div>
          <span class="category-pill">{{ run.job.category_label }}</span>
          <h1 id="run-title">{{ run.title }}</h1>
          <p>{{ new Intl.DateTimeFormat('ko-KR', { dateStyle: 'long', timeStyle: 'short' }).format(new Date(run.generated_at)) }}</p>
        </div>
        <div class="detail-actions">
          <button
            v-if="canEditConsumptionState"
            type="button"
            class="action-button"
            :aria-pressed="run.state.bookmarked"
            :aria-busy="savingState"
            :disabled="savingState"
            @click="updateState({ bookmarked: !run.state.bookmarked })"
          >
            {{ run.state.bookmarked ? '★ 저장됨' : '☆ 저장' }}
          </button>
          <button
            v-if="canEditConsumptionState"
            type="button"
            class="action-button primary"
            :aria-pressed="run.state.completed"
            :aria-busy="savingState"
            :disabled="savingState"
            @click="updateState({ completed: !run.state.completed })"
          >
            {{ run.state.completed ? '완료 취소' : '✓ 완료하기' }}
          </button>
          <button
            v-if="canEditConsumptionState"
            type="button"
            class="action-button"
            :aria-pressed="run.state.archived"
            :aria-busy="savingState"
            :disabled="savingState"
            @click="updateState({ archived: !run.state.archived })"
          >
            {{ run.state.archived ? '보관 취소' : '보관' }}
          </button>
          <button type="button" class="action-button danger" :disabled="deleting" @click="deleteRun">{{ deleting ? '삭제 중…' : '목록에서 삭제' }}</button>
        </div>
      </header>

      <div class="detail-layout">
        <section class="content-paper">
          <div v-if="run.status === 'failed'" class="notice error-notice"><MarkdownContent :content="run.error" /></div>
          <MarkdownContent v-else :content="run.body" />
        </section>

        <aside class="detail-sidebar" aria-label="콘텐츠 작업 도구">
          <KnowledgeVerificationPanel
            v-if="run.knowledge_item"
            :item-id="run.knowledge_item.id"
            :verification="run.knowledge_item.verification"
            @updated="handleVerificationUpdated"
          />

          <KnowledgeTagsPanel
            v-if="run.knowledge_item"
            :item-id="run.knowledge_item.id"
            :tags="run.knowledge_item.tags"
            @updated="handleTagsUpdated"
          />

          <section class="side-card response-card">
            <p class="eyebrow">YOUR RESPONSE</p>
            <h2>직접 답해보기</h2>
            <p id="response-help">문제 번호나 역할을 지정하고 답변을 기록하세요.</p>
            <label class="field-label" for="response-question-key">문제 또는 역할</label>
            <input id="response-question-key" v-model="questionKey" type="text" placeholder="예: 퀴즈 3번, Role B" maxlength="200" aria-describedby="response-help">
            <label class="field-label" for="response-answer">답변</label>
            <textarea id="response-answer" v-model="responseText" rows="7" placeholder="답변을 입력하세요." />
            <button
              type="button"
              class="submit-button"
              :aria-busy="savingResponse"
              :disabled="savingResponse || !responseText.trim()"
              @click="submitResponse"
            >
              {{ savingResponse ? '저장 중…' : '답변 저장' }}
            </button>
          </section>

          <section v-if="canEditConsumptionState" class="side-card note-card">
            <p class="eyebrow">PRIVATE NOTE</p>
            <label class="field-label" for="private-note">나만의 메모</label>
            <textarea id="private-note" v-model="run.state.note" rows="5" maxlength="5000" placeholder="나만의 메모" />
            <button type="button" class="text-button" :aria-busy="savingState" :disabled="savingState" @click="updateState({ note: run.state.note })">메모 저장</button>
          </section>

          <section v-if="run.citations.length" class="side-card">
            <p class="eyebrow">SOURCES</p>
            <a v-for="citation in run.citations" :key="citation.url" :href="citation.url" target="_blank" rel="noopener noreferrer" class="citation-link">
              <span>{{ citation.title }}</span><b>↗</b>
            </a>
          </section>

          <section v-if="run.responses.length" class="side-card">
            <p class="eyebrow">RESPONSE HISTORY</p>
            <div v-for="response in run.responses" :key="response.id" class="response-history">
              <strong>{{ response.question_key || '일반 답변' }}</strong>
              <p>{{ response.answer }}</p>
              <small>{{ new Intl.DateTimeFormat('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(response.created_at)) }}</small>
            </div>
          </section>

          <ClassificationForm
            v-if="run.knowledge_item && run.knowledge_item.status !== 'awaiting_answer'"
            :item-id="run.knowledge_item.id"
            :status="run.knowledge_item.status"
            :current-category-id="run.knowledge_item.category?.id"
            @classified="handleClassification"
          />
        </aside>
      </div>
      <div v-if="navigationError" class="notice error-notice" role="alert">{{ navigationError }}</div>
      <KnowledgeNavigation
        v-if="navigation"
        :previous="navigation.previous"
        :next="navigation.next"
        :position="navigation.position"
        :total="navigation.total"
        :related="navigation.related"
      />
    </template>
  </div>
</template>
