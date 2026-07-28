import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const [
  appSource,
  cssSource,
  indexSource,
  knowledgeDetailSource,
  runDetailSource,
  tokenManagementSource,
  apiGuideSource,
  apiComposableSource,
  askSource,
  localeSource,
  knowledgeCardSource,
  verificationPanelSource,
] = await Promise.all([
  readFile(new URL('../app/app.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/assets/css/main.css', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/index.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/knowledge/[id].vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/runs/[id].vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/api-tokens.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/api-guide.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/composables/useApi.ts', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/ask.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/composables/useDashboardLocale.ts', import.meta.url), 'utf8'),
  readFile(new URL('../app/components/KnowledgeCard.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/components/KnowledgeVerificationPanel.vue', import.meta.url), 'utf8'),
])

assert.match(cssSource, /\.widget-agenda > div \{[^}]*grid-template-columns: 64px minmax\(0, 1fr\) auto;/)
assert.match(cssSource, /\.widget-agenda > div > span \{ min-width: 0; \}/)
assert.doesNotMatch(cssSource, /@import\s+url\(['"]https?:\/\//)
assert.match(cssSource, /--font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;/)

assert.match(appSource, /:aria-hidden="isMobileSidebar && !sidebarOpen \? 'true' : undefined"/)
assert.match(appSource, /:inert="isMobileSidebar && !sidebarOpen"/)
assert.match(appSource, /aria-controls="primary-sidebar"/)
assert.match(appSource, /:aria-expanded="sidebarOpen"/)
assert.match(appSource, /menuToggle\.value\?\.focus\(\)/)
assert.match(appSource, /to="\/api-tokens"[\s\S]*?t\('apiTokens'\)/)
assert.match(appSource, /to="\/api-guide"[\s\S]*?t\('apiGuide'\)/)
assert.match(appSource, /to="\/ask"[\s\S]*?t\('ask'\)/)
assert.match(appSource, /initializeLocale\(\)/)
assert.match(appSource, /applyLocale\('en'\)/)
assert.match(localeSource, /localStorage\.setItem\('dashboard:locale'/)
assert.match(localeSource, /document\.documentElement\.lang = value/)
assert.match(appSource, /request\('\/accounts\/logout\/', \{ method: 'POST' \}\)/)
assert.match(apiComposableSource, /responseStatus\(reason\) === 401/)
assert.match(apiComposableSource, /\/accounts\/login\/\?next=/)
assert.match(tokenManagementSource, /\/api\/platform-tokens\//)
assert.match(tokenManagementSource, /\/api\/platform-tokens\/\$\{existing\.id\}\/rotate\//)
assert.match(tokenManagementSource, /발급 직후 한 번만 표시/)
assert.match(tokenManagementSource, /window\.confirm/)
assert.doesNotMatch(tokenManagementSource, /localStorage.*secret|sessionStorage.*secret/)
assert.match(apiGuideSource, /window\.location\.origin.*\/api\/v1\//)
assert.match(apiGuideSource, /Authorization: Bearer/)
assert.match(apiGuideSource, /Idempotency-Key/)
assert.match(apiGuideSource, /read_mcp_guide/)
assert.match(apiGuideSource, /dashboard:\/\/guides\/mcp/)
assert.match(cssSource, /\.platform-page/)
assert.match(cssSource, /\.token-secret-panel/)
assert.match(cssSource, /\.platform-panel,[\s\S]*?min-width: 0;/)
assert.match(cssSource, /\.platform-table-wrap \{[\s\S]*?min-width: 0;/)

assert.match(
  indexSource,
  /const knowledgeQueryKey = computed\(\(\) => knowledgeQueryParams\(route\.query\)\.toString\(\)\)/,
)
assert.match(indexSource, /const operationKinds: OperationKind\[\] = \['sync', 'tagging', 'classify'\]/)
assert.match(indexSource, /tagging: '태깅'/)
assert.match(indexSource, /수집·태깅·분류 정상/)
assert.match(indexSource, /동기화, 태깅, 분류 실행 기록 표/)
assert.match(indexSource, /class="dashboard-main-grid"/)
assert.match(indexSource, /class="dashboard-action-rail"/)
assert.match(indexSource, /class="dashboard-support-grid"/)
assert.match(indexSource, /\/api\/onboarding\//)
assert.match(indexSource, /class="dashboard-widget onboarding-panel"/)
assert.match(indexSource, /operationsData\.llm_usage/)
assert.match(indexSource, /operationsData\.data_policy/)
assert.match(indexSource, /job\.sync_cursor_ts/)
assert.match(indexSource, /v-model="filterDraft\.verification"/)
assert.match(indexSource, /item\.tags\.slice\(0, 2\)/)
assert.match(cssSource, /\.dashboard-kpis\s*\{[\s\S]*?border: 1px solid #dce2de;/)
assert.match(cssSource, /\.kpi-widget\s*\{[\s\S]*?border-right: 1px solid #e3e8e5;/)
assert.match(cssSource, /\.widget-schedule \.widget-empty\s*\{[\s\S]*?min-height: 92px;/)
assert.match(cssSource, /\.knowledge-feed a:first-child\s*\{[\s\S]*?grid-column: 1 \/ -1;/)
assert.match(indexSource, /watch\(\[activeView, knowledgeQueryKey\]/)
assert.match(indexSource, /class="content-view library-view"/)
assert.match(indexSource, /class="run-grid library-run-grid"/)
assert.doesNotMatch(indexSource, /class="selection-toolbar"/)
assert.doesNotMatch(indexSource, /class="card-selection"/)
assert.match(indexSource, /sessionStorage\.removeItem\(LIST_RETURN_KEY\)/)
assert.match(indexSource, /sessionStorage\.removeItem\(LIST_RETURN_POSITION_KEY\)/)
assert.match(indexSource, /sessionStorage\.removeItem\(scrollKey\(path\)\)/)
assert.match(indexSource, /Math\.min\(value, maxScroll\)/)
assert.match(indexSource, /performance\.now\(\) < deadline/)
assert.match(indexSource, /onBeforeRouteLeave\(\(to\) =>/)
assert.match(indexSource, /isDetailRoute\(to\.path\)/)
assert.match(indexSource, /sessionStorage\.setItem\(LIST_RETURN_POSITION_KEY, historyPosition\(\)\)/)
assert.match(indexSource, /sessionStorage\.getItem\(LIST_RETURN_POSITION_KEY\) === historyPosition\(\)/)
assert.match(indexSource, /window\.scrollTo\(\{ top: 0 \}\)/)
assert.match(cssSource, /\.library-view \.run-excerpt\s*\{[\s\S]*?display: none;/)
assert.match(cssSource, /\.content-code-block\s*\{[\s\S]*?overflow-x: auto;/)
assert.match(knowledgeDetailSource, /class="detail-page knowledge-page"/)
assert.match(knowledgeDetailSource, /<section v-if="showSummary"/)
assert.match(knowledgeDetailSource, /<section v-if="showQuestion"/)
assert.match(knowledgeDetailSource, /<MarkdownContent :content="item\.summary \|\| ''" \/>/)
assert.match(verificationPanelSource, /\/verification\//)
assert.match(verificationPanelSource, /\/feedback\//)
assert.match(verificationPanelSource, /class="verification-actions"/)
assert.match(knowledgeDetailSource, /<KnowledgeVerificationPanel/)
assert.match(runDetailSource, /<KnowledgeVerificationPanel/)
assert.match(knowledgeCardSource, /item\.verification\.status_label/)
assert.match(askSource, /request<KnowledgeAsk>\('\/api\/ask\/'/)
assert.match(askSource, /\/api\/ask\/\$\{result\.value\.id\}\/feedback\//)
assert.match(askSource, /class="ask-citations"/)
assert.match(askSource, /source\.detail_url/)
assert.match(cssSource, /\.ask-workbench/)
assert.match(cssSource, /\.onboarding-steps/)
assert.match(cssSource, /\.verification-actions/)
assert.doesNotMatch(knowledgeDetailSource, /class="qa-summary"/)
assert.match(cssSource, /\/\* Knowledge detail responsive pass: xs\/sm\/md\/lg\/xl \*\//)
assert.match(cssSource, /\.knowledge-page\s*\{[\s\S]*?width: min\(1240px, 100%\);[\s\S]*?\}/)
assert.doesNotMatch(cssSource.split('/* Knowledge detail responsive pass: xs/sm/md/lg/xl */')[1], /\.detail-page\s*\{/)
assert.match(cssSource, /\.qa-section > h2\s*\{/)
assert.match(cssSource, /\.knowledge-page \.knowledge-detail-header h1\s*\{[\s\S]*?font-size: var\(--text-xl\);[\s\S]*?\}/)
assert.match(cssSource, /\.knowledge-page \.summary-section \.markdown-body,[\s\S]*?\.knowledge-page \.summary-section \.markdown-body table\s*\{[\s\S]*?font-size: var\(--text-sm\);[\s\S]*?\}/)
assert.match(cssSource, /@media \(max-width: 1180px\)[\s\S]*?\.knowledge-page \.qa-detail-layout\s*\{[\s\S]*?minmax\(270px, 300px\)/)
assert.match(cssSource, /@media \(max-width: 900px\)[\s\S]*?\.knowledge-page \.detail-sidebar\s*\{[\s\S]*?repeat\(2, minmax\(0, 1fr\)\)/)
assert.match(cssSource, /@media \(max-width: 620px\)[\s\S]*?\.knowledge-page \.qa-section \.markdown-body\s*\{[\s\S]*?font-size: 13px;/)
assert.match(cssSource, /@media \(max-width: 420px\)[\s\S]*?\.knowledge-page \.knowledge-detail-header \.action-button\s*\{[\s\S]*?font-size: var\(--text-xs\);/)
assert.match(cssSource, /\.knowledge-page \.detail-neighbors strong\s*\{[\s\S]*?-webkit-line-clamp: 2;[\s\S]*?\}/)
assert.match(cssSource, /\.knowledge-page \.related-knowledge \.run-grid\s*\{[\s\S]*?grid-template-columns: repeat\(3, minmax\(0, 1fr\)\);[\s\S]*?\}/)

for (const detailSource of [knowledgeDetailSource, runDetailSource]) {
  assert.match(
    detailSource,
    /async function updateState\([\s\S]*?affectsKnowledgeMembership\(patch\)[\s\S]*?await loadNavigation\(/,
  )
  assert.match(detailSource, /async function handleClassification\([\s\S]*?await loadNavigation\(updated\.id\)/)
}

const contrastChecks = [
  ['.kpi-widget > div:nth-child(2) small', '#ffffff'],
  ['.operations-summary small', '#f3f6f4'],
  ['.widget-agenda small', '#ffffff'],
  ['.widget-header span', '#ffffff'],
  ['.knowledge-feed p', '#ffffff'],
  ['.knowledge-feed .feed-source', '#ffffff'],
  ['.sidebar-nav p', '#16251f'],
]

for (const [selector, background] of contrastChecks) {
  const color = declaration(selector, 'color')
  assert.ok(
    contrastRatio(color, background) >= 4.5,
    `${selector} contrast must be at least 4.5:1`,
  )
}

function declaration(selector, property) {
  const block = [...cssSource.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .find(([, selectorList, declarations]) => (
      selectorList.split(',').map(value => value.trim()).includes(selector)
      && new RegExp(`${property}:`).test(declarations)
    ))?.[2]
  assert.ok(block, `missing CSS rule for ${selector}`)
  const value = block.match(new RegExp(`${property}:\\s*(#[0-9a-f]{6})`, 'i'))?.[1]
  assert.ok(value, `missing ${property} declaration for ${selector}`)
  return value
}

function contrastRatio(foreground, background) {
  const lighter = Math.max(luminance(foreground), luminance(background))
  const darker = Math.min(luminance(foreground), luminance(background))
  return (lighter + 0.05) / (darker + 0.05)
}

function luminance(hex) {
  const channels = hex.slice(1).match(/.{2}/g).map(channel => Number.parseInt(channel, 16) / 255)
  const [red, green, blue] = channels.map(channel => (
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  ))
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

console.log('frontend dashboard regression tests passed')
