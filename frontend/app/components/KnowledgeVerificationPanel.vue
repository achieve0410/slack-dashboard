<script setup lang="ts">
import type {
  KnowledgeVerification,
  KnowledgeVerificationStatus,
} from '~/types/api'
import { apiErrorMessage } from '~/utils/apiError'

const props = defineProps<{
  itemId: number
  verification: KnowledgeVerification
}>()
const emit = defineEmits<{
  updated: [verification: KnowledgeVerification]
}>()
const { request } = useApi()
const { intlLocale } = useDashboardLocale()
const note = ref(props.verification.note)
const saving = ref(false)
const error = ref('')

watch(
  () => props.verification.note,
  value => note.value = value,
)

function formatDate(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat(intlLocale.value, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

async function updateStatus(status: KnowledgeVerificationStatus) {
  if (saving.value) return
  saving.value = true
  error.value = ''
  try {
    const verification = await request<KnowledgeVerification>(
      `/api/knowledge/${props.itemId}/verification/`,
      {
        method: 'PATCH',
        body: { status, note: note.value.trim() },
      },
    )
    note.value = verification.note
    emit('updated', verification)
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '검증 상태를 저장하지 못했습니다.')
  }
  finally {
    saving.value = false
  }
}

async function sendFeedback(kind: 'helpful' | 'incorrect' | 'outdated') {
  if (saving.value) return
  saving.value = true
  error.value = ''
  try {
    const response = await request<{ verification: KnowledgeVerification }>(
      `/api/knowledge/${props.itemId}/feedback/`,
      { method: 'POST', body: { kind } },
    )
    note.value = response.verification.note
    emit('updated', response.verification)
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '지식 피드백을 저장하지 못했습니다.')
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <section :class="['side-card', 'verification-card', `verification-${verification.status}`]">
    <div class="verification-heading">
      <div><p class="eyebrow">TRUST</p><h2>지식 검증</h2></div>
      <span class="verification-badge">{{ verification.status_label }}</span>
    </div>
    <dl class="audit-list">
      <div><dt>검증자</dt><dd>{{ verification.owner?.username || '—' }}</dd></div>
      <div><dt>검증 시각</dt><dd>{{ formatDate(verification.verified_at) }}</dd></div>
      <div><dt>다음 검토</dt><dd>{{ formatDate(verification.review_due_at) }}</dd></div>
    </dl>
    <label class="field-label" :for="`knowledge-verification-note-${itemId}`">검증 메모</label>
    <textarea
      :id="`knowledge-verification-note-${itemId}`"
      v-model="note"
      rows="3"
      maxlength="1000"
      placeholder="원문 확인 범위나 변경 이력을 기록하세요."
    />
    <div class="verification-actions">
      <button type="button" :disabled="saving" @click="updateStatus('verified')">✓ 검증 완료</button>
      <button type="button" :disabled="saving" @click="updateStatus('stale')">재검토 필요</button>
      <button type="button" :disabled="saving" @click="updateStatus('unverified')">미검증</button>
    </div>
    <div class="verification-feedback" role="group" aria-label="지식 품질 피드백">
      <button type="button" :disabled="saving" @click="sendFeedback('helpful')">도움됨 {{ verification.feedback_counts.helpful }}</button>
      <button type="button" :disabled="saving" @click="sendFeedback('incorrect')">틀림 {{ verification.feedback_counts.incorrect }}</button>
      <button type="button" :disabled="saving" @click="sendFeedback('outdated')">오래됨 {{ verification.feedback_counts.outdated }}</button>
    </div>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
  </section>
</template>
