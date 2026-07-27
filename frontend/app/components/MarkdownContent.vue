<script setup lang="ts">
import { renderMarkdownContent } from '~/utils/markdownContent'

const props = defineProps<{ content: string }>()
const rendered = computed(() => renderMarkdownContent(props.content))
</script>

<template>
  <article class="markdown-body" v-html="rendered" />
</template>
