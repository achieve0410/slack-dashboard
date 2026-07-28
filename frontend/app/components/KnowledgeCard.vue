<script setup lang="ts">
import type { KnowledgeCard } from '~/types/api'
import { detailUrlWithKnowledgeContext } from '~/utils/knowledgeQuery'

const props = defineProps<{
  item: KnowledgeCard
  selectedTag?: string
}>()
const emit = defineEmits<{
  tagClick: [tag: string]
}>()
const route = useRoute()
const { intlLocale } = useDashboardLocale()

const detailLink = computed(() => detailUrlWithKnowledgeContext(props.item.detail_url, route.query))

const formattedDate = computed(() => new Intl.DateTimeFormat(intlLocale.value, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
}).format(new Date(props.item.generated_at)))

const excerpt = computed(() => (
  props.item.summary
  || props.item.question_excerpt
  || (props.item.has_answer ? '질문과 답변을 확인하세요.' : '답변을 기다리고 있습니다.')
))
const categoryLabel = computed(() => (
  props.item.category_path
  || (props.item.source_type === 'slack_qa' ? '자유 질문' : '미분류')
))

function handleTagClick(tag: string) {
  emit('tagClick', tag)
}
</script>

<template>
  <article
    :class="['run-card', 'knowledge-card', `source-${item.source_type}`, `status-${item.status}`]"
  >
    <NuxtLink :to="detailLink" class="run-card-main">
      <div class="run-card-topline">
        <span class="category-pill" :title="categoryLabel">
          {{ categoryLabel }}
        </span>
        <time class="run-date" :datetime="item.generated_at">{{ formattedDate }}</time>
      </div>
      <h3>{{ item.title }}</h3>
      <p class="run-excerpt">{{ excerpt }}</p>
    </NuxtLink>
    <div v-if="item.tags.length" class="knowledge-tag-row" aria-label="지식 태그">
      <button
        v-for="tag in item.tags"
        :key="tag"
        type="button"
        :class="['knowledge-tag-chip', { active: selectedTag === tag }]"
        :aria-pressed="selectedTag === tag"
        @click="handleTagClick(tag)"
      >
        #{{ tag }}
      </button>
    </div>
    <footer class="run-card-footer">
      <span>
        <span :class="['knowledge-status', `status-${item.status}`]">{{ item.status_label }}</span>
        <span :class="['verification-badge', `verification-${item.verification.status}`]">{{ item.verification.status_label }}</span>
      </span>
      <span class="run-signals">
        <span>{{ item.source_label }}</span>
        <span v-if="!item.state?.read">새 항목</span>
        <span v-if="item.state?.completed">✓ 완료</span>
        <span v-if="item.state?.bookmarked">★ 저장</span>
        <span v-if="item.has_answer === false">답변 대기 중</span>
      </span>
    </footer>
  </article>
</template>
