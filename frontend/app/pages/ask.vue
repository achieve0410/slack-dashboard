<script setup lang="ts">
import type {
  KnowledgeAsk,
  KnowledgeAskHistory,
  KnowledgeVerificationStatus,
} from '~/types/api'
import { apiErrorMessage } from '~/utils/apiError'

const { request } = useApi()
const { intlLocale, locale, t } = useDashboardLocale()
const question = ref('')
const sourceType = ref('')
const verification = ref<KnowledgeVerificationStatus | ''>('verified')
const channelId = ref('')
const result = ref<KnowledgeAsk | null>(null)
const history = ref<KnowledgeAsk[]>([])
const loading = ref(true)
const submitting = ref(false)
const feedbackSaving = ref(false)
const error = ref('')

onMounted(loadHistory)

async function loadHistory() {
  loading.value = true
  error.value = ''
  try {
    const data = await request<KnowledgeAskHistory>('/api/ask/?limit=20')
    history.value = data.results
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '최근 질문을 불러오지 못했습니다.')
  }
  finally {
    loading.value = false
  }
}

async function ask() {
  if (!question.value.trim() || submitting.value) return
  submitting.value = true
  error.value = ''
  const filters: Record<string, string> = {}
  if (sourceType.value) filters.source_type = sourceType.value
  if (verification.value) filters.verification = verification.value
  if (channelId.value.trim()) filters.channel_id = channelId.value.trim()
  try {
    result.value = await request<KnowledgeAsk>('/api/ask/', {
      method: 'POST',
      body: {
        question: question.value.trim(),
        locale: locale.value,
        filters,
      },
    })
    history.value = [
      result.value,
      ...history.value.filter(item => item.id !== result.value?.id),
    ].slice(0, 20)
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '답변을 생성하지 못했습니다.')
  }
  finally {
    submitting.value = false
  }
}

async function saveFeedback(value: 'helpful' | 'unhelpful') {
  if (!result.value || feedbackSaving.value) return
  feedbackSaving.value = true
  error.value = ''
  try {
    result.value = await request<KnowledgeAsk>(
      `/api/ask/${result.value.id}/feedback/`,
      { method: 'PATCH', body: { feedback: value } },
    )
    history.value = history.value.map(item => (
      item.id === result.value?.id ? result.value : item
    )).filter((item): item is KnowledgeAsk => Boolean(item))
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '피드백을 저장하지 못했습니다.')
  }
  finally {
    feedbackSaving.value = false
  }
}

function selectHistory(item: KnowledgeAsk) {
  result.value = item
  question.value = item.question
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(intlLocale.value, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}
</script>

<template>
  <div class="page-container ask-page">
    <header class="page-command-bar">
      <div>
        <p class="eyebrow">CITED KNOWLEDGE</p>
        <h1>{{ t('askTitle') }}</h1>
        <p>{{ t('askDescription') }}</p>
      </div>
    </header>

    <section class="ask-workbench">
      <form class="content-view ask-form" @submit.prevent="ask">
        <label for="knowledge-question">{{ t('ask') }}</label>
        <textarea
          id="knowledge-question"
          v-model="question"
          rows="5"
          maxlength="1000"
          required
          :placeholder="t('askPlaceholder')"
        />
        <div class="ask-filters">
          <label>
            <span>{{ t('allSources') }}</span>
            <select v-model="sourceType">
              <option value="">{{ t('allSources') }}</option>
              <option value="cron">Slack knowledge</option>
              <option value="slack_qa">Slack Q&amp;A</option>
            </select>
          </label>
          <label>
            <span>{{ t('verifiedOnly') }}</span>
            <select v-model="verification">
              <option value="">{{ t('allVerification') }}</option>
              <option value="verified">{{ t('verifiedOnly') }}</option>
              <option value="unverified">Unverified</option>
              <option value="stale">Needs review</option>
            </select>
          </label>
          <label>
            <span>Slack channel ID</span>
            <input v-model="channelId" maxlength="50" placeholder="C0123456789">
          </label>
        </div>
        <button class="action-button primary" type="submit" :disabled="submitting || !question.trim()">
          {{ submitting ? t('askSubmitting') : t('askSubmit') }}
        </button>
      </form>

      <article v-if="result" :class="['content-view', 'ask-answer', { insufficient: result.insufficient_evidence }]">
        <header>
          <div>
            <p class="eyebrow">ANSWER</p>
            <h2>{{ result.question }}</h2>
          </div>
          <span v-if="result.insufficient_evidence">{{ t('insufficient') }}</span>
        </header>
        <MarkdownContent :content="result.answer" />
        <section v-if="result.sources.length" class="ask-citations" :aria-label="t('citations')">
          <h3>{{ t('citations') }}</h3>
          <ol>
            <li v-for="source in result.sources" :key="`${result.id}:${source.knowledge_item_id}:${source.title}`">
              <NuxtLink v-if="source.detail_url" :to="source.detail_url">{{ source.title }}</NuxtLink>
              <a v-else-if="source.source_url" :href="source.source_url" target="_blank" rel="noopener noreferrer">{{ source.title }}</a>
              <strong v-else>{{ source.title }}</strong>
              <p>{{ source.excerpt }}</p>
            </li>
          </ol>
        </section>
        <footer>
          <span>{{ formatDate(result.created_at) }}</span>
          <div role="group" aria-label="Answer feedback">
            <button type="button" :class="{ active: result.feedback === 'helpful' }" :disabled="feedbackSaving" @click="saveFeedback('helpful')">✓ {{ t('helpful') }}</button>
            <button type="button" :class="{ active: result.feedback === 'unhelpful' }" :disabled="feedbackSaving" @click="saveFeedback('unhelpful')">× {{ t('unhelpful') }}</button>
          </div>
        </footer>
      </article>
    </section>

    <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>

    <section class="content-view ask-history" aria-labelledby="ask-history-title">
      <div class="panel-heading">
        <div><p class="eyebrow">HISTORY</p><h2 id="ask-history-title">{{ t('askHistory') }}</h2></div>
        <span>{{ history.length }}</span>
      </div>
      <div v-if="history.length" class="ask-history-list">
        <button v-for="item in history" :key="item.id" type="button" @click="selectHistory(item)">
          <span>{{ item.insufficient_evidence ? t('insufficient') : `${item.sources.length} ${t('citations')}` }}</span>
          <strong>{{ item.question }}</strong>
          <small>{{ formatDate(item.created_at) }}</small>
        </button>
      </div>
      <p v-else-if="loading" class="panel-empty" role="status">Loading…</p>
      <p v-else class="panel-empty">No questions yet.</p>
    </section>
  </div>
</template>
