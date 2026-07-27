<script setup lang="ts">
import type { KnowledgeCard } from '~/types/api'
import { detailUrlWithKnowledgeContext } from '~/utils/knowledgeQuery'

interface NavigationItem {
  id: number
  title: string
  detail_url: string
}

defineProps<{
  previous: NavigationItem | null
  next: NavigationItem | null
  position: number
  total: number
  related: KnowledgeCard[]
}>()

const route = useRoute()
const link = (item: NavigationItem) => detailUrlWithKnowledgeContext(item.detail_url, route.query)
</script>

<template>
  <section class="knowledge-navigation" aria-labelledby="knowledge-navigation-title">
    <div class="detail-neighbors">
      <h2 id="knowledge-navigation-title" class="visually-hidden">현재 목록 안에서 이동</h2>
      <NuxtLink v-if="previous" :to="link(previous)" rel="prev">
        <span>← 이전</span>
        <strong>{{ previous.title }}</strong>
      </NuxtLink>
      <span v-else class="disabled-neighbor" aria-disabled="true">첫 번째 항목</span>
      <p aria-live="polite">{{ position }} / {{ total }}</p>
      <NuxtLink v-if="next" :to="link(next)" rel="next">
        <span>다음 →</span>
        <strong>{{ next.title }}</strong>
      </NuxtLink>
      <span v-else class="disabled-neighbor" aria-disabled="true">마지막 항목</span>
    </div>

    <div v-if="related.length" class="related-knowledge" aria-labelledby="related-knowledge-title">
      <div class="panel-heading">
        <div><p class="eyebrow">RELATED</p><h2 id="related-knowledge-title">관련 지식</h2></div>
        <span>{{ related.length }}개</span>
      </div>
      <div class="run-grid">
        <KnowledgeCard v-for="item in related" :key="item.id" :item="item" />
      </div>
    </div>
  </section>
</template>
