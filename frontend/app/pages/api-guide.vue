<script setup lang="ts">
const currentBaseUrl = computed(() => (
  import.meta.client ? `${window.location.origin}/api/v1/` : ''
))
const scopes = [
  ['platform:read', '작업·아티팩트·승인·이벤트·에이전트·검색 조회'],
  ['inbox:write', '외부 원본 수집'],
  ['tasks:write', '작업 생성·수정·상태 전이'],
  ['artifacts:write', '불변 아티팩트 리비전 생성'],
  ['approvals:request', '아티팩트 승인 요청'],
  ['approvals:decide', '승인·거절·수정 요청 결정'],
]
const endpoints = [
  ['GET', '/', 'API 기능과 현재 토큰의 에이전트 정보'],
  ['GET', '/agents/', '활성 에이전트 목록'],
  ['GET · POST', '/inbox/', '수집 원본 조회·등록'],
  ['GET · POST', '/tasks/', '작업 조회·생성'],
  ['GET · PATCH', '/tasks/{task_id}/', '작업 상세·상태 변경'],
  ['GET', '/tasks/{task_id}/context/', '작업 전체 컨텍스트'],
  ['GET · POST', '/artifacts/', '불변 아티팩트 조회·생성'],
  ['GET · POST', '/approvals/', '승인 요청 조회·생성'],
  ['POST', '/approvals/{approval_id}/decision/', '승인·거절·수정 요청 결정'],
  ['GET', '/events/', '감사 이벤트 목록'],
  ['GET', '/workflows/{task_id}/', '워크플로 이력'],
  ['GET', '/search/?q=검색어', '작업과 기존 지식 통합 검색'],
]
const curlExample = computed(() => `curl -sS \\
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \\
  "${currentBaseUrl.value}tasks/"`)
const mutationExample = computed(() => `curl -sS -X POST \\
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -H "Content-Type: application/json" \\
  -d '{"title":"API 작업","description":"대시보드에서 생성"}' \\
  "${currentBaseUrl.value}tasks/"`)
</script>

<template>
  <div class="page-container platform-page api-guide-page">
    <header class="platform-page-header">
      <div>
        <p class="eyebrow">DASHBOARD PLATFORM API</p>
        <h1>API 가이드</h1>
        <p>다른 에이전트가 작업, 아티팩트, 승인과 감사 이력을 안전하게 공유하는 공식 API입니다.</p>
      </div>
      <NuxtLink class="action-button primary" to="/api-tokens">토큰 관리</NuxtLink>
    </header>

    <section class="platform-panel api-start-panel">
      <div>
        <span>현재 접속 주소 기준 API 베이스</span>
        <code>{{ currentBaseUrl }}</code>
      </div>
      <ul>
        <li>모든 변경 요청은 Bearer 토큰을 사용합니다. 리버스 프록시 뒤에 배포한다면 HTTPS를 사용하세요.</li>
        <li>브라우저 UI와 기존 <code>/api/*</code>는 소유자 로그인 세션을 사용하며, 이 가이드는 신규 연동용 <code>/api/v1/*</code>을 설명합니다.</li>
        <li>변경 요청에는 최대 128자의 <code>Idempotency-Key</code>가 필요합니다.</li>
        <li>토큰 원문을 프롬프트, Slack, 로그, Git 또는 URL에 넣지 않습니다.</li>
      </ul>
    </section>

    <section class="platform-panel">
      <div class="platform-panel-heading">
        <div><p class="eyebrow">AUTHENTICATION</p><h2>인증 호출</h2></div>
      </div>
      <p>토큰 파일을 환경 변수로 읽고 <code>Authorization: Bearer</code> 헤더에 전달합니다.</p>
      <pre><code>{{ curlExample }}</code></pre>
    </section>

    <section class="platform-panel">
      <div class="platform-panel-heading">
        <div><p class="eyebrow">MCP QUICK START</p><h2>에이전트 MCP 가이드</h2></div>
      </div>
      <p>
        에이전트 CLI(Claude Code, Codex 등)에서 <code>dashboard_platform</code> MCP를 연결한 뒤
        <code>read_mcp_guide</code>를 가장 먼저 호출합니다.
      </p>
      <p>
        리소스 읽기를 지원하는 클라이언트는
        <code>dashboard://guides/mcp</code>에서 같은 A–Z 문서를 읽을 수 있습니다.
      </p>
    </section>

    <section class="platform-panel">
      <div class="platform-panel-heading">
        <div><p class="eyebrow">ENDPOINTS</p><h2>제공 기능</h2></div>
      </div>
      <div class="platform-table-wrap" role="region" tabindex="0" aria-label="Dashboard Platform API 엔드포인트 표">
        <table class="platform-table api-endpoint-table">
          <thead><tr><th>메서드</th><th>경로</th><th>기능</th></tr></thead>
          <tbody>
            <tr v-for="[method, path, description] in endpoints" :key="path">
              <td><strong>{{ method }}</strong></td><td><code>{{ path }}</code></td><td>{{ description }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="platform-panel">
      <div class="platform-panel-heading">
        <div><p class="eyebrow">SCOPES</p><h2>권한 범위</h2></div>
      </div>
      <div class="api-scope-grid">
        <article v-for="[scope, description] in scopes" :key="scope">
          <code>{{ scope }}</code><p>{{ description }}</p>
        </article>
      </div>
      <p class="platform-footnote"><code>approvals:decide</code>는 관리자 에이전트에만 부여하는 것이 원칙입니다.</p>
    </section>

    <section class="platform-panel">
      <div class="platform-panel-heading">
        <div><p class="eyebrow">IDEMPOTENCY</p><h2>변경 요청</h2></div>
      </div>
      <p>같은 토큰·경로·키·본문을 반복하면 최초 응답을 재생합니다. 같은 키에 다른 본문을 사용하면 409를 반환합니다.</p>
      <pre><code>{{ mutationExample }}</code></pre>
    </section>

    <section class="platform-panel api-response-grid">
      <article><h2>목록 응답</h2><pre><code>{
  "data": [],
  "pagination": {
    "count": 0,
    "limit": 50,
    "offset": 0,
    "next_offset": null
  }
}</code></pre></article>
      <article><h2>오류 응답</h2><pre><code>{
  "error": {
    "code": "insufficient_scope",
    "message": "필요한 권한 범위가 없습니다."
  }
}</code></pre></article>
    </section>

    <section class="platform-safety-note" role="note">
      <strong>현재 제공하지 않는 기능</strong>
      <span><code>/sources</code>, <code>/ideas</code>, <code>/actions</code>, 외부 게시·주문 실행, 웹훅과 이벤트 스트림은 아직 구현되어 있지 않습니다.</span>
    </section>
  </div>
</template>
