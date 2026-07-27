import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const [
  appSource,
  apiTypesSource,
  quizApiSource,
  quizIndexSource,
  quizSessionSource,
  quizResultSource,
  quizReviewSource,
  cssSource,
] = await Promise.all([
  readFile(new URL('../app/app.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/types/api.ts', import.meta.url), 'utf8'),
  readFile(new URL('../app/composables/useQuizApi.ts', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/quiz/index.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/quiz/session/[id].vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/quiz/result/[id].vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/quiz/review.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/assets/css/main.css', import.meta.url), 'utf8'),
])

assert.match(appSource, /route\.path\.startsWith\('\/quiz'\)/)
assert.match(appSource, /quiz: '퀴즈'/)
assert.match(appSource, /<NuxtLink to="\/quiz" title="퀴즈"/)
assert.match(appSource, /:aria-hidden="isMobileSidebar && !sidebarOpen \? 'true' : undefined"/)
assert.match(appSource, /:inert="isMobileSidebar && !sidebarOpen"/)
assert.match(appSource, /aria-controls="primary-sidebar"/)
assert.match(appSource, /:aria-expanded="sidebarOpen"/)

for (const literal of [
  '/api/quiz/catalog/',
  '/api/quiz/review/',
  '/api/quiz/sessions/',
  '/api/quiz/sessions/?limit=${limit}',
  '/api/quiz/sessions/${sessionId}/',
  '/api/quiz/sessions/${sessionId}/items/${itemId}/answer/',
  '/api/quiz/sessions/${sessionId}/result/',
  '/api/quiz/questions/${questionId}/wrong-note/',
]) {
  assert.ok(quizApiSource.includes(literal), `missing quiz API literal ${literal}`)
}

const currentItemType = interfaceBody(apiTypesSource, 'QuizCurrentItem')
assert.doesNotMatch(currentItemType, /correct|explanation|source|detail_url|source_key|source_hash|evidence/)
assert.match(apiTypesSource, /export interface QuizAnswerResponse[\s\S]*correct_choice_ids[\s\S]*explanation[\s\S]*source/)
assert.match(apiTypesSource, /export interface QuizResultItem[\s\S]*correct_choice_ids[\s\S]*explanation[\s\S]*source/)
const sessionResponseType = interfaceBody(apiTypesSource, 'QuizSessionResponse')
assert.doesNotMatch(sessionResponseType, /available_count/)
assert.match(apiTypesSource, /export interface QuizSessionStartResponse extends QuizSessionResponse \{[\s\S]*available_count: number[\s\S]*\}/)
assert.doesNotMatch(apiTypesSource, /available_count\?: number/)
assert.match(apiTypesSource, /export interface QuizSessionHistoryItem[\s\S]*session_id[\s\S]*answered_count[\s\S]*started_at[\s\S]*completed_at/)
assert.match(apiTypesSource, /export interface QuizSessionHistoryList[\s\S]*results: QuizSessionHistoryItem\[\]/)

assert.match(quizIndexSource, /quiz_pool_shortage/)
assert.match(quizIndexSource, /catalog\?\.empty_state/)
assert.match(quizIndexSource, /quizModeLabels\[filters\.mode\]/)
assert.match(quizIndexSource, /router\.push\(`\/quiz\/session\/\$\{session\.session_id\}`\)/)
assert.match(quizIndexSource, /quiz\.history\(20\)/)
assert.match(quizIndexSource, /최근 학습/)
assert.match(quizIndexSource, /item\.status === 'completed' \? `\/quiz\/result\/\$\{item\.session_id\}` : `\/quiz\/session\/\$\{item\.session_id\}`/)
assert.match(quizIndexSource, /이어풀기/)
assert.match(quizIndexSource, /결과 보기/)

assert.match(quizSessionSource, /quiz\.session\(String\(route\.params\.id\)\)/)
assert.match(quizSessionSource, /quiz\.answer\(session\.value\.session_id, currentItem\.value\.id, selectedChoiceIds\.value\)/)
assert.match(quizSessionSource, /feedback\.value = null/)
assert.match(quizSessionSource, /v-if="feedback"[\s\S]*feedback\.explanation[\s\S]*feedback\.source\.detail_url/)
assert.match(quizSessionSource, /feedback\.next_item \? '다음 문제' : '결과 보기'/)

const preFeedbackTemplate = quizSessionSource.split('<section v-if="feedback"')[0]
assert.doesNotMatch(preFeedbackTemplate, /correct_choice_ids|explanation|source\.detail_url|source_key|source_hash|evidence/)

assert.match(quizResultSource, /quiz\.result\(String\(route\.params\.id\)\)/)
assert.match(quizResultSource, /quiz\.wrongNote\(item\.question_id, nextValue\)/)
assert.match(quizResultSource, /item\.source\.detail_url/)
assert.match(quizResultSource, /오답노트 해제/)
assert.match(quizResultSource, /<article><span>마스터<\/span><strong>\{\{ result\.mastered_count \}\}<\/strong><small>장기 복습 완료<\/small><\/article>/)

assert.match(quizReviewSource, /dueOnly: true/)
assert.match(quizReviewSource, /quiz\.review\(/)
assert.match(quizReviewSource, /startMode\('wrong'/)
assert.match(quizReviewSource, /quiz\.wrongNote\(item\.question_id, nextValue\)/)
assert.match(quizReviewSource, /item\.source\.detail_url/)

assert.match(cssSource, /\/\* Quiz workspace \*\//)
assert.match(cssSource, /\.quiz-session-layout\s*\{[\s\S]*?grid-template-columns: 280px minmax\(0, 1fr\);/)
assert.match(cssSource, /@media \(max-width: 900px\)[\s\S]*?\.quiz-workbench,[\s\S]*?\.quiz-session-layout,[\s\S]*?\.quiz-review-controls \{ grid-template-columns: 1fr; \}/)
assert.match(cssSource, /@media \(max-width: 620px\)[\s\S]*?\.quiz-metrics \{ grid-template-columns: 1fr; \}/)
assert.match(cssSource, /\.workspace-topbar \.header-search \{[\s\S]*?grid-template-columns: auto minmax\(0, 1fr\) auto;[\s\S]*?min-width: 0;/)
assert.match(cssSource, /\.workspace-topbar \.header-search input \{ min-width: 0;[\s\S]*?\}/)
assert.match(cssSource, /\.workspace-topbar \.header-search button \{[\s\S]*?white-space: nowrap;[\s\S]*?\}/)
assert.match(cssSource, /@media \(max-width: 620px\)[\s\S]*?\.workspace-topbar \{ display: grid; grid-template-columns: minmax\(0, 1fr\); height: auto; padding-block: 10px; \}/)
assert.match(cssSource, /@media \(max-width: 620px\)[\s\S]*?\.workspace-topbar \.header-search \{ order: 2; width: 100%; max-width: 100%; justify-self: stretch; \}/)
assert.match(cssSource, /@media \(max-width: 620px\)[\s\S]*?\.page-command-bar \{ display: grid; grid-template-columns: minmax\(0, 1fr\);/)
assert.match(cssSource, /@media \(max-width: 620px\)[\s\S]*?\.quiz-header-actions \{ width: 100%; \}/)
assert.match(cssSource, /@media \(max-width: 620px\)[\s\S]*?\.quiz-header-actions > \* \{ flex: 1 1 0; min-width: 0; text-align: center; \}/)

function interfaceBody(source, name) {
  const match = source.match(new RegExp(`export interface ${name} \\{([\\s\\S]*?)\\n\\}`))
  assert.ok(match, `missing interface ${name}`)
  return match[1]
}

console.log('frontend quiz regression tests passed')
