<script setup lang="ts">
import type { TrashKnowledgeCard, TrashKnowledgeListResponse } from '~/types/api'
import { apiErrorMessage } from '~/utils/apiError'

const { request } = useApi()
const items = ref<TrashKnowledgeCard[]>([])
const total = ref(0)
const nextOffset = ref<number | null>(0)
const loading = ref(false)
const restoringId = ref<number | null>(null)
const error = ref('')
const restoredMessage = ref('')

async function load(append = false) {
  if (loading.value || (append && nextOffset.value === null)) return
  loading.value = true
  error.value = ''
  const offset = append ? nextOffset.value || 0 : 0
  try {
    const data = await request<TrashKnowledgeListResponse>(`/api/knowledge/trash/?limit=50&offset=${offset}`)
    items.value = append ? [...items.value, ...data.results] : data.results
    total.value = data.count
    nextOffset.value = data.next_offset
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '휴지통을 불러오지 못했습니다.')
  }
  finally {
    loading.value = false
  }
}

async function restore(item: TrashKnowledgeCard) {
  restoringId.value = item.id
  error.value = ''
  restoredMessage.value = ''
  try {
    await request(`/api/knowledge/${item.id}/restore/`, { method: 'POST' })
    items.value = items.value.filter(candidate => candidate.id !== item.id)
    total.value = Math.max(0, total.value - 1)
    restoredMessage.value = item.state?.archived
      ? '복원했습니다. 보관된 항목이므로 보관 보기에서 확인할 수 있습니다.'
      : '복원했습니다. 지식 라이브러리에서 다시 확인할 수 있습니다.'
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '항목을 복원하지 못했습니다.')
  }
  finally {
    restoringId.value = null
  }
}

function hiddenDate(value: string): string {
  return new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

onMounted(() => load())
</script>

<template>
  <div class="page-container trash-page">
    <header class="page-command-bar">
      <div><p class="eyebrow">TRASH</p><h1>휴지통</h1><p>원본과 개인 상태를 보존한 채 숨긴 항목을 복원합니다.</p></div>
      <button class="ghost-button" type="button" :disabled="loading" @click="load(false)">새로고침</button>
    </header>
    <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>
    <p v-if="restoredMessage" class="notice" role="status" aria-live="polite">{{ restoredMessage }}</p>
    <section class="content-view" aria-labelledby="trash-list-title">
      <div class="panel-heading"><div><p class="eyebrow">HIDDEN ITEMS</p><h2 id="trash-list-title">삭제한 항목</h2></div><span>{{ items.length }} / {{ total }}개</span></div>
      <div v-if="loading && !items.length" class="notice" role="status">휴지통을 불러오는 중입니다.</div>
      <div v-else-if="items.length" class="trash-list">
        <article v-for="item in items" :key="item.id">
          <div><span>{{ item.category_path || item.source_label }}</span><h3>{{ item.title }}</h3><p>{{ item.summary }}</p><small>{{ hiddenDate(item.hidden_at) }}에 삭제됨<span v-if="item.state?.archived"> · 보관 상태 유지</span></small></div>
          <button class="action-button" type="button" :disabled="restoringId === item.id" @click="restore(item)">{{ restoringId === item.id ? '복원 중…' : '복원' }}</button>
        </article>
      </div>
      <p v-else class="panel-empty">휴지통이 비어 있습니다.</p>
      <div v-if="nextOffset !== null" class="board-pagination"><span>{{ total - items.length }}개 남음</span><button class="ghost-button" type="button" :disabled="loading" @click="load(true)">더 보기</button></div>
    </section>
  </div>
</template>
