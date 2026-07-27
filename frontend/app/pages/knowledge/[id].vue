<script setup lang="ts">
import type { KnowledgeDetail, KnowledgeNavigationResponse, RunState } from '~/types/api'
import { affectsKnowledgeMembership, updateKnowledgeRemoval } from '~/utils/knowledgeState'
import { knowledgeQueryParams } from '~/utils/knowledgeQuery'
import { apiErrorCode, apiErrorMessage } from '~/utils/apiError'
import {
  shouldShowKnowledgeQuestion,
  shouldShowKnowledgeSummary,
} from '~/utils/knowledgePresentation'

const route = useRoute()
const router = useRouter()
const { request } = useApi()
const item = ref<KnowledgeDetail | null>(null)
const loading = ref(true)
const error = ref('')
const deleteError = ref('')
const deleting = ref(false)
const savingState = ref(false)
const stateError = ref('')
const navigation = ref<KnowledgeNavigationResponse | null>(null)
const navigationError = ref('')
const knowledgeUpdates = useState<Record<number, KnowledgeDetail>>('knowledge-updates', () => ({}))
const knowledgeRemovals = useState<Record<number, boolean>>('knowledge-removals', () => ({}))
const showSummary = computed(() => Boolean(
  item.value && shouldShowKnowledgeSummary(item.value),
))
const showQuestion = computed(() => Boolean(
  item.value && shouldShowKnowledgeQuestion(item.value),
))
const formattedGeneratedAt = computed(() => (
  item.value
    ? new Intl.DateTimeFormat('ko-KR', {
        dateStyle: 'long',
        timeStyle: 'short',
      }).format(new Date(item.value.generated_at))
    : ''
))

const confidenceLabel = computed(() => {
  if (!item.value?.classification_confidence) return ''
  return `${Math.round(Number(item.value.classification_confidence) * 100)}%`
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    item.value = await request<KnowledgeDetail>(`/api/knowledge/${route.params.id}/`)
    loading.value = false
    await nextTick()
    if (!item.value.state.read) await updateState({ read: true })
    else await loadNavigation(item.value.id)
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '문답을 불러오지 못했습니다.'
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
  if (!item.value) return
  savingState.value = true
  stateError.value = ''
  try {
    item.value.state = await request<RunState>(
      `/api/knowledge/${item.value.id}/state/`,
      { method: 'PATCH', body: patch },
    )
    knowledgeUpdates.value = {
      ...knowledgeUpdates.value,
      [item.value.id]: item.value,
    }
    knowledgeRemovals.value = updateKnowledgeRemoval(
      knowledgeRemovals.value,
      item.value.id,
      item.value.state.archived,
    )
  }
  catch (reason) {
    stateError.value = reason instanceof Error ? reason.message : '소비 상태를 저장하지 못했습니다.'
  }
  finally {
    savingState.value = false
    if (affectsKnowledgeMembership(patch)) await loadNavigation(item.value.id)
  }
}

function formatAuditDate(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('ko-KR', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
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
  item.value = updated
  knowledgeUpdates.value = { ...knowledgeUpdates.value, [updated.id]: updated }
  await loadNavigation(updated.id)
}

function handleTagsUpdated(tags: string[]) {
  if (!item.value) return
  item.value = { ...item.value, tags }
  knowledgeUpdates.value = { ...knowledgeUpdates.value, [item.value.id]: item.value }
}

async function deleteItem() {
  if (!item.value || !window.confirm('원본 Slack/Cron 기록은 유지됩니다. 이 항목을 대시보드에서 삭제할까요?')) return
  deleting.value = true
  deleteError.value = ''
  try {
    await request(`/api/knowledge/${item.value.id}/`, { method: 'DELETE' })
    knowledgeRemovals.value = { ...knowledgeRemovals.value, [item.value.id]: true }
    goBack()
  }
  catch (reason) {
    deleteError.value = reason instanceof Error ? reason.message : '항목을 삭제하지 못했습니다.'
  }
  finally {
    deleting.value = false
  }
}

watch(() => route.params.id, load, { immediate: true })
</script>

<template>
  <div class="detail-page knowledge-page">
    <button type="button" class="back-link" @click="goBack">← 이전 목록</button>
    <div v-if="deleteError" class="notice error-notice" role="alert">{{ deleteError }}</div>
    <div v-if="stateError" class="notice error-notice" role="alert">{{ stateError }}</div>
    <div v-if="loading" class="notice" role="status" aria-live="polite">문답을 불러오는 중입니다.</div>
    <div v-else-if="error || !item" class="notice error-notice detail-error" role="alert">
      <p>{{ error || '문답을 찾을 수 없습니다.' }}</p>
      <button class="ghost-button" type="button" @click="load">다시 시도</button>
    </div>
    <template v-else>
      <header class="detail-header knowledge-detail-header" aria-labelledby="knowledge-title">
        <div>
          <div class="detail-labels">
            <span class="category-pill" :title="item.category_path || '자유 질문'">
              {{ item.category_path || '자유 질문' }}
            </span>
            <span :class="['knowledge-status', `status-${item.status}`]">{{ item.status_label }}</span>
          </div>
          <h1 id="knowledge-title">{{ item.title }}</h1>
          <p>
            {{ item.source_label }} · {{ formattedGeneratedAt }}
            <template v-if="item.slack?.source_url">
              · <a :href="item.slack.source_url" class="inline-link" target="_blank" rel="noopener noreferrer">Slack 원문</a>
            </template>
          </p>
        </div>
        <div class="detail-actions">
          <button
            type="button"
            class="action-button"
            :aria-pressed="item.state.bookmarked"
            :aria-busy="savingState"
            :disabled="savingState"
            @click="updateState({ bookmarked: !item.state.bookmarked })"
          >
            {{ item.state.bookmarked ? '★ 저장됨' : '☆ 저장' }}
          </button>
          <button
            type="button"
            class="action-button primary"
            :aria-pressed="item.state.completed"
            :aria-busy="savingState"
            :disabled="savingState"
            @click="updateState({ completed: !item.state.completed })"
          >
            {{ item.state.completed ? '완료 취소' : '✓ 완료하기' }}
          </button>
          <button
            type="button"
            class="action-button"
            :aria-pressed="item.state.archived"
            :aria-busy="savingState"
            :disabled="savingState"
            @click="updateState({ archived: !item.state.archived })"
          >
            {{ item.state.archived ? '보관 취소' : '보관' }}
          </button>
          <button type="button" class="action-button danger" :disabled="deleting" @click="deleteItem">{{ deleting ? '삭제 중…' : '목록에서 삭제' }}</button>
        </div>
      </header>

      <div class="detail-layout qa-detail-layout">
        <div class="qa-content">
          <section v-if="showSummary" class="content-paper qa-section summary-section" aria-labelledby="summary-heading">
            <p class="eyebrow">SUMMARY</p>
            <h2 id="summary-heading">요약</h2>
            <MarkdownContent :content="item.summary || ''" />
          </section>

          <section v-if="showQuestion" class="content-paper qa-section" aria-labelledby="question-heading">
            <p class="eyebrow">QUESTION</p>
            <h2 id="question-heading">질문</h2>
            <MarkdownContent :content="item.question || ''" />
          </section>

          <section v-if="item.answer" class="content-paper qa-section answer-section" aria-labelledby="answer-heading">
            <p class="eyebrow">ANSWER</p>
            <h2 id="answer-heading">답변</h2>
            <MarkdownContent :content="item.answer" />
          </section>
          <div v-else-if="item.source_type === 'slack_qa'" class="notice answer-waiting">
            답변을 기다리고 있습니다.
          </div>

          <div v-if="item.source_type === 'cron'" class="notice">
            본문과 출처, 메모 및 응답은
            <NuxtLink :to="item.detail_url" class="inline-link">Cron 실행 상세</NuxtLink>에서 확인하세요.
          </div>
        </div>

        <aside class="detail-sidebar" aria-label="분류 정보">
          <KnowledgeTagsPanel
            :item-id="item.id"
            :tags="item.tags"
            @updated="handleTagsUpdated"
          />

          <section class="side-card note-card">
            <p class="eyebrow">PRIVATE NOTE</p>
            <label class="field-label" for="knowledge-private-note">나만의 메모</label>
            <textarea id="knowledge-private-note" v-model="item.state.note" rows="5" maxlength="5000" placeholder="나만의 메모" />
            <button type="button" class="text-button" :aria-busy="savingState" :disabled="savingState" @click="updateState({ note: item.state.note })">메모 저장</button>
          </section>

          <section :class="['side-card', 'audit-card', `status-${item.status}`]">
            <p class="eyebrow">CLASSIFICATION</p>
            <dl class="audit-list">
              <div>
                <dt>카테고리</dt>
                <dd>{{ item.category_path || '미분류' }}</dd>
              </div>
              <div>
                <dt>상태</dt>
                <dd>{{ item.status_label }}</dd>
              </div>
              <div v-if="item.classification_model">
                <dt>분류 모델</dt>
                <dd>{{ item.classification_model }}</dd>
              </div>
              <div v-if="confidenceLabel">
                <dt>신뢰도</dt>
                <dd>{{ confidenceLabel }}</dd>
              </div>
              <div>
                <dt>분류 시각</dt>
                <dd>{{ formatAuditDate(item.classified_at) }}</dd>
              </div>
              <div v-if="item.reviewed_by">
                <dt>검토자</dt>
                <dd>{{ item.reviewed_by.username }}</dd>
              </div>
              <div v-if="item.reviewed_at">
                <dt>검토 시각</dt>
                <dd>{{ formatAuditDate(item.reviewed_at) }}</dd>
              </div>
            </dl>
            <p v-if="item.classification_stale_at" class="classification-stale" role="status">
              최신 답변 리비전이 추가되어 현재 분류의 재확인이 필요합니다.
            </p>
          </section>

          <section v-if="item.classification_reason" class="side-card">
            <p class="eyebrow">CLASSIFICATION NOTE</p>
            <p class="classification-note">{{ item.classification_reason }}</p>
          </section>

          <ClassificationForm
            v-if="item.status !== 'awaiting_answer'"
            :item-id="item.id"
            :status="item.status"
            :current-category-id="item.category?.id"
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
