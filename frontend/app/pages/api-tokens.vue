<script setup lang="ts">
import type {
  PlatformToken,
  PlatformTokenCollection,
  PlatformTokenIssueResponse,
} from '~/types/api'
import { apiErrorMessage } from '~/utils/apiError'

const { request } = useApi()
const data = ref<PlatformTokenCollection>({ tokens: [], agents: [], available_scopes: [] })
const loading = ref(true)
const saving = ref(false)
const revokingId = ref<number | null>(null)
const error = ref('')
const success = ref('')
const secret = ref('')
const secretAgentKey = ref('')
const form = reactive({
  agentKey: '',
  scopes: [] as string[],
  expiresDays: '90',
})

const scopeDescriptions: Record<string, string> = {
  'platform:read': '작업·아티팩트·승인·이벤트·에이전트·검색 조회',
  'inbox:write': '외부 원본 수집',
  'tasks:write': '작업 생성·수정·상태 전이',
  'artifacts:write': '불변 아티팩트 리비전 생성',
  'approvals:request': '아티팩트 승인 요청',
  'approvals:decide': '승인·거절·수정 요청 결정',
}

const sortedTokens = computed(() => [...data.value.tokens].sort((left, right) => {
  if (left.is_active !== right.is_active) return left.is_active ? -1 : 1
  return right.id - left.id
}))
const activeCount = computed(() => data.value.tokens.filter(token => token.is_active).length)
const expiringCount = computed(() => data.value.tokens.filter((token) => {
  if (!token.is_active || !token.expires_at) return false
  const remaining = new Date(token.expires_at).getTime() - Date.now()
  return remaining > 0 && remaining <= 30 * 24 * 60 * 60 * 1000
}).length)

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await request<PlatformTokenCollection>('/api/platform-tokens/')
    if (!form.agentKey && data.value.agents.length) {
      form.agentKey = data.value.agents[0]?.key || ''
      form.scopes = defaultScopes(form.agentKey)
    }
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '토큰 정보를 불러오지 못했습니다.')
  }
  finally {
    loading.value = false
  }
}

function defaultScopes(agentKey: string): string[] {
  const active = data.value.tokens.find(token => token.agent.key === agentKey && token.is_active)
  return active?.scopes.length
    ? [...active.scopes]
    : ['platform:read', 'tasks:write', 'artifacts:write', 'approvals:request']
}

function selectAgent() {
  form.scopes = defaultScopes(form.agentKey)
}

function editToken(token: PlatformToken) {
  form.agentKey = token.agent.key
  form.scopes = [...token.scopes]
  form.expiresDays = token.expires_at ? remainingDays(token.expires_at) : ''
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function remainingDays(value: string): string {
  const milliseconds = new Date(value).getTime() - Date.now()
  return String(Math.max(1, Math.ceil(milliseconds / (24 * 60 * 60 * 1000))))
}

function issuePayload() {
  return {
    agent_key: form.agentKey,
    scopes: form.scopes,
    expires_days: form.expiresDays ? Number(form.expiresDays) : null,
  }
}

async function issueToken() {
  if (!form.agentKey || !form.scopes.length) return
  const existing = data.value.tokens.find(token => token.agent.key === form.agentKey && token.is_active)
  if (
    existing
    && !window.confirm(`${existing.agent.name}의 현재 토큰을 폐기하고 새 토큰으로 교체할까요?`)
  ) return

  saving.value = true
  error.value = ''
  success.value = ''
  secret.value = ''
  try {
    const path = existing
      ? `/api/platform-tokens/${existing.id}/rotate/`
      : '/api/platform-tokens/'
    const body = existing
      ? { scopes: form.scopes, expires_days: form.expiresDays ? Number(form.expiresDays) : null }
      : issuePayload()
    const result = await request<PlatformTokenIssueResponse>(path, {
      method: 'POST',
      body,
    })
    secret.value = result.secret
    secretAgentKey.value = result.token.agent.key
    success.value = existing ? '토큰을 교체했습니다.' : '토큰을 발급했습니다.'
    await load()
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '토큰을 발급하지 못했습니다.')
  }
  finally {
    saving.value = false
  }
}

async function revokeToken(token: PlatformToken) {
  if (!window.confirm(`${token.agent.name}의 ${token.token_prefix} 토큰을 즉시 폐기할까요?`)) return
  revokingId.value = token.id
  error.value = ''
  success.value = ''
  try {
    await request(`/api/platform-tokens/${token.id}/revoke/`, {
      method: 'POST',
      body: {},
    })
    success.value = '토큰을 폐기했습니다.'
    if (secretAgentKey.value === token.agent.key) {
      secret.value = ''
      secretAgentKey.value = ''
    }
    await load()
  }
  catch (reason) {
    error.value = apiErrorMessage(reason, '토큰을 폐기하지 못했습니다.')
  }
  finally {
    revokingId.value = null
  }
}

async function copySecret() {
  if (!secret.value) return
  await navigator.clipboard.writeText(secret.value)
  success.value = '토큰 원문을 클립보드에 복사했습니다.'
}

function downloadSecret() {
  if (!secret.value) return
  const url = URL.createObjectURL(new Blob([`${secret.value}\n`], { type: 'text/plain' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${secretAgentKey.value}.token`
  anchor.click()
  URL.revokeObjectURL(url)
}

function closeSecret() {
  secret.value = ''
  secretAgentKey.value = ''
}

function toggleScope(scope: string) {
  form.scopes = form.scopes.includes(scope)
    ? form.scopes.filter(value => value !== scope)
    : [...form.scopes, scope]
}

function statusLabel(status: PlatformToken['status']): string {
  return {
    active: '활성',
    inactive: '비활성',
    revoked: '폐기됨',
    expired: '만료됨',
    agent_inactive: '에이전트 비활성',
  }[status]
}

function formatDate(value: string | null): string {
  if (!value) return '없음'
  return new Intl.DateTimeFormat('ko-KR', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}
</script>

<template>
  <div class="page-container platform-page">
    <header class="platform-page-header">
      <div>
        <p class="eyebrow">API OPERATIONS</p>
        <h1>API 토큰 관리</h1>
        <p>다른 에이전트가 Dashboard Platform API를 호출할 때 사용하는 자격증명입니다.</p>
      </div>
      <NuxtLink class="action-button" to="/api-guide">API 가이드 보기</NuxtLink>
    </header>

    <div class="platform-safety-note" role="note">
      <strong>현재 접근 경계: 로그인 세션</strong>
      <span>이 화면은 로그인한 사용자만 접근할 수 있습니다. 인터넷에 공개하기 전에 <code>DASHBOARD_AUTH_REQUIRED=1</code>과 HTTPS 리버스 프록시를 설정하세요.</span>
    </div>

    <div v-if="error" class="notice error-notice" role="alert">{{ error }}</div>
    <div v-if="success" class="notice platform-success" role="status" aria-live="polite">{{ success }}</div>

    <section v-if="secret" class="token-secret-panel" aria-labelledby="issued-token-title">
      <div>
        <p class="eyebrow">ONE-TIME SECRET</p>
        <h2 id="issued-token-title">발급된 토큰 원문</h2>
        <p>발급 직후 한 번만 표시됩니다. 화면을 닫으면 서버에서 다시 조회할 수 없습니다.</p>
      </div>
      <code>{{ secret }}</code>
      <div class="platform-actions">
        <button class="action-button primary" type="button" @click="copySecret">복사</button>
        <button class="action-button" type="button" @click="downloadSecret">파일 다운로드</button>
        <button class="action-button" type="button" @click="closeSecret">확인 후 닫기</button>
      </div>
    </section>

    <section class="platform-summary-grid" aria-label="토큰 상태 요약">
      <article><span>활성 토큰</span><strong>{{ activeCount }}</strong></article>
      <article><span>30일 안에 만료</span><strong>{{ expiringCount }}</strong></article>
      <article><span>등록 에이전트</span><strong>{{ data.agents.length }}</strong></article>
    </section>

    <section class="platform-panel token-issue-panel" aria-labelledby="token-issue-title">
      <div class="platform-panel-heading">
        <div>
          <p class="eyebrow">ISSUE OR ROTATE</p>
          <h2 id="token-issue-title">토큰 발급·교체</h2>
        </div>
        <span>원문 파일은 서버의 비공개 토큰 디렉터리에도 권한 0600으로 저장됩니다.</span>
      </div>
      <form class="platform-form" @submit.prevent="issueToken">
        <label>
          <span>에이전트</span>
          <select v-model="form.agentKey" required @change="selectAgent">
            <option v-for="agent in data.agents" :key="agent.key" :value="agent.key">{{ agent.name }} · {{ agent.key }}</option>
          </select>
        </label>
        <label>
          <span>유효 기간</span>
          <select v-model="form.expiresDays">
            <option value="30">30일</option>
            <option value="90">90일</option>
            <option value="180">180일</option>
            <option value="365">1년</option>
            <option value="">만료 없음</option>
          </select>
        </label>
        <fieldset>
          <legend>권한 범위</legend>
          <label v-for="scope in data.available_scopes" :key="scope" class="scope-option">
            <input type="checkbox" :checked="form.scopes.includes(scope)" @change="toggleScope(scope)">
            <span><code>{{ scope }}</code><small>{{ scopeDescriptions[scope] }}</small></span>
          </label>
        </fieldset>
        <button class="action-button primary" type="submit" :disabled="saving || !form.agentKey || !form.scopes.length">
          {{ saving ? '처리 중…' : '발급 또는 교체' }}
        </button>
      </form>
    </section>

    <section class="platform-panel" aria-labelledby="token-list-title">
      <div class="platform-panel-heading">
        <div>
          <p class="eyebrow">TOKEN INVENTORY</p>
          <h2 id="token-list-title">발급 이력</h2>
        </div>
        <button class="action-button" type="button" :disabled="loading" @click="load">새로고침</button>
      </div>
      <div v-if="loading" class="notice" role="status">토큰 정보를 불러오는 중입니다.</div>
      <div v-else class="platform-table-wrap" role="region" tabindex="0" aria-label="API 토큰 발급 이력 표">
        <table class="platform-table">
          <thead>
            <tr><th>에이전트</th><th>상태</th><th>토큰</th><th>권한</th><th>만료</th><th>마지막 사용</th><th>관리</th></tr>
          </thead>
          <tbody>
            <tr v-for="token in sortedTokens" :key="token.id">
              <td><strong>{{ token.agent.name }}</strong><small>{{ token.agent.key }}</small></td>
              <td><span :class="['token-status', token.status]">{{ statusLabel(token.status) }}</span></td>
              <td><code>{{ token.token_prefix }}</code><small>{{ token.file_present ? '파일 연결됨' : '파일 없음' }}</small></td>
              <td><div class="scope-list"><code v-for="scope in token.scopes" :key="scope">{{ scope }}</code></div></td>
              <td>{{ formatDate(token.expires_at) }}</td>
              <td>{{ formatDate(token.last_used_at) }}</td>
              <td>
                <div class="platform-row-actions">
                  <button class="text-button" type="button" @click="editToken(token)">설정 사용</button>
                  <button v-if="token.is_active" class="text-button danger-text" type="button" :disabled="revokingId === token.id" @click="revokeToken(token)">
                    {{ revokingId === token.id ? '폐기 중…' : '폐기' }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="platform-footnote">토큰을 교체하거나 폐기한 뒤 장기 실행 중인 에이전트 프로세스는 새 파일을 읽도록 재시작해야 합니다.</p>
    </section>
  </div>
</template>
