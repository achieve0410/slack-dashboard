<script setup lang="ts">
import type { KnowledgeTagsUpdateRequest, KnowledgeTagsUpdateResponse } from '~/types/api'
import { canonicalKnowledgeQuery } from '~/utils/knowledgeQuery'
import { apiErrorMessage } from '~/utils/apiError'

const props = defineProps<{
  itemId: number
  tags: string[]
}>()
const emit = defineEmits<{
  updated: [tags: string[]]
}>()

const route = useRoute()
const router = useRouter()
const { request } = useApi()
const draft = ref('')
const saving = ref(false)
const error = ref('')
const success = ref('')

watch(
  () => props.tags,
  tags => draft.value = tags.join(', '),
  { immediate: true },
)

function parseTags(value: string): string[] {
  const seen = new Set<string>()
  return value
    .split(/[,\n]/)
    .map(tag => tag.trim())
    .filter((tag) => {
      if (!tag || seen.has(tag)) return false
      seen.add(tag)
      return true
    })
}

async function filterByTag(tag: string) {
  const query: Record<string, string> = {
    view: 'library',
    ...canonicalKnowledgeQuery(route.query),
    tag,
  }
  await router.push({ path: '/', query })
}

async function saveTags() {
  const tags = parseTags(draft.value)
  success.value = ''
  error.value = ''
  if (tags.length < 3) {
    error.value = '태그는 최소 3개 이상 입력해야 합니다.'
    return
  }
  saving.value = true
  try {
    const response = await request<KnowledgeTagsUpdateResponse>(
      `/api/knowledge/${props.itemId}/tags/`,
      {
        method: 'PATCH',
        body: { tags } satisfies KnowledgeTagsUpdateRequest,
      },
    )
    draft.value = response.tags.join(', ')
    emit('updated', response.tags)
    success.value = '태그를 저장했습니다.'
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '태그를 저장하지 못했습니다.')
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="side-card knowledge-tags-panel" aria-labelledby="knowledge-tags-title">
    <p class="eyebrow">TAGS</p>
    <h2 id="knowledge-tags-title">태그</h2>
    <div v-if="tags.length" class="knowledge-tag-row detail-tag-row" aria-label="현재 지식 태그">
      <button
        v-for="tag in tags"
        :key="tag"
        type="button"
        class="knowledge-tag-chip"
        @click="filterByTag(tag)"
      >
        #{{ tag }}
      </button>
    </div>
    <p v-else class="tag-empty-state">아직 표시할 태그가 없습니다.</p>
    <p class="tag-recompute-notice">태그는 다음 야간 재계산 시 변경될 수 있습니다.</p>
    <form class="knowledge-tags-editor" @submit.prevent="saveTags">
      <label class="field-label" for="knowledge-tags-input">태그 편집</label>
      <textarea
        id="knowledge-tags-input"
        v-model="draft"
        rows="5"
        maxlength="3000"
        placeholder="쉼표 또는 줄바꿈으로 태그를 입력하세요."
        :disabled="saving"
      />
      <button type="submit" class="submit-button" :aria-busy="saving" :disabled="saving">
        {{ saving ? '저장 중…' : '태그 저장' }}
      </button>
      <p v-if="error" class="classification-feedback error" role="alert">{{ error }}</p>
      <p v-if="success" class="classification-feedback success" role="status">{{ success }}</p>
    </form>
  </section>
</template>
