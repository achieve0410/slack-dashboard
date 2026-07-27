<script setup lang="ts">
import type { CategoryNode, KnowledgeDetail } from '~/types/api'

const props = defineProps<{
  itemId: number
  status: KnowledgeDetail['status']
  currentCategoryId?: number | null
}>()
const emit = defineEmits<{ classified: [item: KnowledgeDetail] }>()
const { request } = useApi()
const categories = ref<CategoryNode[]>([])
const selectedCategoryId = ref(props.currentCategoryId ? String(props.currentCategoryId) : '')
const reviewNote = ref('')
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const success = ref('')

const categoryOptions = computed(() => {
  const result: CategoryNode[] = []
  const visit = (nodes: CategoryNode[]) => nodes.forEach((node) => {
    result.push(node)
    visit(node.children)
  })
  visit(categories.value)
  return result
})

async function loadCategories() {
  loading.value = true
  error.value = ''
  try {
    const data = await request<{ results: CategoryNode[] }>('/api/categories/')
    categories.value = data.results
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '카테고리를 불러오지 못했습니다.'
  }
  finally {
    loading.value = false
  }
}

async function submit() {
  if (!selectedCategoryId.value) return
  saving.value = true
  error.value = ''
  success.value = ''
  try {
    const item = await request<KnowledgeDetail>(`/api/knowledge/${props.itemId}/classification/`, {
      method: 'PATCH',
      body: {
        category_id: Number(selectedCategoryId.value),
        review_note: reviewNote.value.trim() || '페이지에서 직접 분류',
      },
    })
    success.value = `${item.category_path}로 분류했습니다.`
    emit('classified', item)
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '분류를 저장하지 못했습니다.'
  }
  finally {
    saving.value = false
  }
}

onMounted(loadCategories)
</script>

<template>
  <section class="side-card classification-form-card">
    <p class="eyebrow">MANUAL CLASSIFICATION</p>
    <h2>{{ status === 'classified' ? '분류 변경' : '직접 분류' }}</h2>
    <p>카테고리를 선택하면 자동 분류 결과를 기다리지 않고 즉시 반영합니다.</p>
    <form @submit.prevent="submit">
      <label class="field-label" :for="`classification-category-${itemId}`">카테고리</label>
      <select :id="`classification-category-${itemId}`" v-model="selectedCategoryId" :disabled="loading || saving">
        <option value="">카테고리 선택</option>
        <option v-for="category in categoryOptions" :key="category.id" :value="String(category.id)">
          {{ category.path }}
        </option>
      </select>
      <label class="field-label" :for="`classification-note-${itemId}`">분류 메모 <small>선택</small></label>
      <textarea :id="`classification-note-${itemId}`" v-model="reviewNote" rows="3" maxlength="980" placeholder="분류 근거 또는 변경 사유" />
      <button class="submit-button" type="submit" :disabled="loading || saving || !selectedCategoryId">
        {{ saving ? '저장 중…' : status === 'classified' ? '분류 변경' : '분류 확정' }}
      </button>
    </form>
    <p v-if="error" class="classification-feedback error" role="alert">{{ error }}</p>
    <p v-else-if="success" class="classification-feedback success" role="status">{{ success }}</p>
  </section>
</template>
