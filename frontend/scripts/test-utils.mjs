import assert from 'node:assert/strict'

import { renderMarkdownContent } from '../app/utils/markdownContent.ts'
import {
  affectsKnowledgeMembership,
  canEditRunConsumptionState,
  isFreeQuestionItem,
  mergeKnowledgeUpdates,
  updateKnowledgeRemoval,
} from '../app/utils/knowledgeState.ts'
import {
  canonicalKnowledgeQuery,
  detailUrlWithKnowledgeContext,
  knowledgeQueryParams,
} from '../app/utils/knowledgeQuery.ts'
import {
  MAX_BULK_IDS,
  normalizeBulkIds,
  toggleBulkId,
  toggleVisibleBulkIds,
} from '../app/utils/bulkSelection.ts'
import { groupScheduleEvents, scheduleGroupTotal } from '../app/utils/scheduleGroups.ts'
import { apiErrorCode, apiErrorMessage } from '../app/utils/apiError.ts'
import {
  shouldShowKnowledgeQuestion,
  shouldShowKnowledgeSummary,
} from '../app/utils/knowledgePresentation.ts'
import {
  applyPage,
  createPageState,
  remainingItems,
  resetPage,
  startPageRequest,
} from '../app/utils/pagination.ts'

const state = createPageState()
const first = startPageRequest(state, false)
assert.ok(first)
assert.equal(applyPage(state, first, {
  count: 120,
  next_offset: 100,
  results: Array.from({ length: 100 }, (_, index) => ({ id: index + 1 })),
}), true)
assert.equal(remainingItems(state), 20)

const second = startPageRequest(state, true)
assert.ok(second)
assert.equal(second.offset, 100)
assert.equal(applyPage(state, second, {
  count: 120,
  next_offset: null,
  results: Array.from({ length: 21 }, (_, index) => ({ id: index + 100 })),
}), true)
assert.equal(state.items.length, 120)
assert.equal(new Set(state.items.map(item => item.id)).size, 120)
assert.equal(remainingItems(state), 0)

const stale = startPageRequest(state, false)
assert.ok(stale)
resetPage(state)
assert.equal(applyPage(state, stale, { count: 1, next_offset: null, results: [{ id: 999 }] }), false)
assert.deepEqual(state.items, [])

const rendered = renderMarkdownContent(
  'MDLINKPLACEHOLDER0TOKEN @@MDLINKO@@ *강조* '
  + '<https://example.com/path_with_under_scores|표시_링크> '
  + '<https://example.com/" onmouseover="alert(1)|안전> '
  + '<javascript:alert(1)|bad>',
)
assert.match(rendered, /href="https:\/\/example\.com\/path_with_under_scores"/)
assert.match(rendered, />표시_링크<\/a>/)
assert.match(rendered, /MDLINKPLACEHOLDER0TOKEN @@MDLINKO@@/)
assert.match(rendered, /<strong>강조<\/strong>/)
assert.match(rendered, /href="https:\/\/example\.com\/&quot; onmouseover=&quot;alert\(1\)"/)
assert.doesNotMatch(rendered, /" onmouseover="/)
assert.doesNotMatch(rendered, /href="javascript:/)

const markdownRendered = renderMarkdownContent(
  '## 제목\n\n- **요약** [공식 문서](https://example.com/docs)\n\nBot Answer',
)
assert.match(markdownRendered, /<h2>제목<\/h2>/)
assert.match(markdownRendered, /<div class="content-gap"><\/div>/)
assert.match(markdownRendered, /<strong>요약<\/strong>/)
assert.match(markdownRendered, /href="https:\/\/example\.com\/docs"/)
assert.match(markdownRendered, />공식 문서<\/a>/)

const codeBlockRendered = renderMarkdownContent(
  '아래 코드를 참고하세요.\n```ts\nconst value = 1 < 2\nconsole.log(value)\n```\n끝',
)
assert.match(codeBlockRendered, /<pre class="content-code-block"><code class="language-ts">/)
assert.match(codeBlockRendered, /const value = 1 &lt; 2/)
assert.match(codeBlockRendered, /console\.log\(value\)/)
assert.doesNotMatch(codeBlockRendered, /<p>```ts<\/p>/)

const tableRendered = renderMarkdownContent(
  '|구간|항공사|실제 출발·도착 시각|직항 여부|\n'
  + '|---|---|---|---|\n'
  + '|출국 12/23(수)|제주항공|ICN 07:15 → CTS 10:00|직항, 2시간 45분|\n'
  + '|귀국 12/27(일)|**제주항공**|CTS 16:00 → ICN 19:25|직항, 3시간 25분|',
)
assert.match(tableRendered, /<div class="content-table-wrap"><table>/)
assert.match(tableRendered, /<th scope="col">구간<\/th>/)
assert.match(tableRendered, /<td>출국 12\/23\(수\)<\/td>/)
assert.match(tableRendered, /<td><strong>제주항공<\/strong><\/td>/)
assert.doesNotMatch(tableRendered, /<p>\|구간/)

const looseTableRendered = renderMarkdownContent(
  '|구간|항공사|실제 출발·도착 시각|직항 여부|\n'
  + '|---|---|---|---|\n'
  + '|출국 12/23(수)|제주항공|ICN 07:15 → CTS 10:00|직항, 2시간 45분|\n'
  + '|귀국 12/27(일)|제주항공|CTS 16:00 → ICN 19:25|직항, 3시간 25분',
)
assert.match(looseTableRendered, /<td>귀국 12\/27\(일\)<\/td><td>제주항공<\/td><td>CTS 16:00 → ICN 19:25<\/td><td>직항, 3시간 25분<\/td>/)
assert.doesNotMatch(looseTableRendered, /<td><\/td><td>귀국/)


const alignedTableRendered = renderMarkdownContent(
  '| # | 종목 / 시장 / 코드 | 당일 종가 | 당일 등락률 | 최근 1주 평균·추세 | 카테고리·이유 / 출처 |\n'
  + '|---:|---|---:|---:|---|---|\n'
  + '| 1 | 선정 없음 / 휴장 | - | - | 산출 불가 | 주식 거래 불가 - 일간 등락률·주간 추세 갱신 없음 / [이투데이](https://www.etoday.co.kr/news/view/2604259) |',
)
assert.match(alignedTableRendered, /<th scope="col">#<\/th>/)
assert.match(alignedTableRendered, /<th scope="col">당일 등락률<\/th>/)
assert.match(alignedTableRendered, /<td>선정 없음 \/ 휴장<\/td>/)
assert.match(alignedTableRendered, /href="https:\/\/www\.etoday\.co\.kr\/news\/view\/2604259"/)
assert.doesNotMatch(alignedTableRendered, /<p>\| # \|/)
assert.doesNotMatch(alignedTableRendered, /<p>#<\/p>/)

const flattenedSummaryRendered = renderMarkdownContent(
  '인천(ICN) ↔ 삿포로 신치토세(CTS) 항공권 확인 여행 조건: 성인 1명 ## 추천 왕복 조합 |구간|항공사|시각| |---|---|---| |출국|제주항공|ICN 07:15 → CTS 10:00| |귀국|제주항공|CTS 16:00 → ICN 19:25|',
)
assert.match(flattenedSummaryRendered, /<p>인천\(ICN\)/)
assert.match(flattenedSummaryRendered, /<h2>추천 왕복 조합<\/h2>/)
assert.match(flattenedSummaryRendered, /<th scope="col">구간<\/th>/)
assert.match(flattenedSummaryRendered, /<td>귀국<\/td>/)
assert.doesNotMatch(flattenedSummaryRendered, /<p>.*\|---\|/s)

const freeQuestionCard = {
  id: 42,
  status: 'pending',
  status_label: '분류 대기',
  category: null,
  category_path: '',
  state: { read: false },
}
const automaticReadUpdate = {
  ...freeQuestionCard,
  classified_at: null,
  state: { read: true },
}
const freeQuestionAfterAutomaticRead = mergeKnowledgeUpdates(
  [freeQuestionCard],
  { 42: automaticReadUpdate },
).filter(isFreeQuestionItem)
assert.equal(freeQuestionAfterAutomaticRead.length, 1)
assert.equal(freeQuestionAfterAutomaticRead[0].state.read, true)

const freeQuestionAfterClassification = mergeKnowledgeUpdates(
  [freeQuestionCard],
  { 42: { ...automaticReadUpdate, status: 'classified' } },
).filter(isFreeQuestionItem)
assert.deepEqual(freeQuestionAfterClassification, [])

const archivedRemovals = updateKnowledgeRemoval({}, 42, true)
assert.deepEqual(archivedRemovals, { 42: true })
assert.deepEqual(updateKnowledgeRemoval(archivedRemovals, 42, false), {})

assert.equal(canEditRunConsumptionState({ knowledge_item: null }), false)
assert.equal(canEditRunConsumptionState({ knowledge_item: automaticReadUpdate }), true)
assert.equal(affectsKnowledgeMembership({ read: true }), true)
assert.equal(affectsKnowledgeMembership({ bookmarked: true }), true)
assert.equal(affectsKnowledgeMembership({ completed: true }), true)
assert.equal(affectsKnowledgeMembership({ archived: true }), true)
assert.equal(affectsKnowledgeMembership({ note: 'unchanged membership' }), false)

const canonicalQuery = canonicalKnowledgeQuery({
  view: 'library',
  q: ['  exact query  ', 'ignored'],
  category: '7',
  source_type: 'slack_qa',
  archived: 'only',
  unknown: 'not-forwarded',
})
assert.deepEqual(canonicalQuery, {
  q: 'exact query',
  category: '7',
  source_type: 'slack_qa',
  archived: 'only',
})
assert.equal(
  knowledgeQueryParams(canonicalQuery).toString(),
  'q=exact+query&category=7&source_type=slack_qa&archived=only',
)
assert.equal(
  detailUrlWithKnowledgeContext('/knowledge/42/?existing=kept#content', canonicalQuery),
  '/knowledge/42/?existing=kept&q=exact+query&category=7&source_type=slack_qa&archived=only#content',
)

assert.deepEqual(normalizeBulkIds([3, 1, 3, 0, -1, 2.5, 2]), [1, 2, 3])
assert.deepEqual(toggleBulkId([1, 3], 2, true), [1, 2, 3])
assert.deepEqual(toggleBulkId([1, 2, 3], 2, false), [1, 3])
assert.deepEqual(toggleVisibleBulkIds([1, 4], [2, 3], true), [1, 2, 3, 4])
assert.deepEqual(toggleVisibleBulkIds([1, 2, 3, 4], [2, 3], false), [1, 4])
assert.throws(
  () => normalizeBulkIds(Array.from({ length: MAX_BULK_IDS + 1 }, (_, index) => index + 1)),
  RangeError,
)

const groupedSchedule = groupScheduleEvents([
  { id: 1, agenda_group: 'today' },
  { id: 2, agenda_group: 'completed' },
  { id: 3, agenda_group: 'today' },
], ['completed', 'today'])
assert.deepEqual(groupedSchedule, {
  completed: [{ id: 2, agenda_group: 'completed' }],
  today: [
    { id: 1, agenda_group: 'today' },
    { id: 3, agenda_group: 'today' },
  ],
})
assert.equal(scheduleGroupTotal({ completed: 1, today: 2 }), 3)
assert.throws(
  () => groupScheduleEvents([{ id: 4, agenda_group: 'unknown' }], ['today']),
  /지원하지 않는 일정 그룹/,
)
assert.equal(apiErrorCode({ data: { code: 'context_changed' } }), 'context_changed')
assert.equal(apiErrorCode(new Error('plain failure')), '')
assert.equal(apiErrorMessage({ data: { error: '서버 오류' } }, 'fallback'), '서버 오류')
assert.equal(apiErrorMessage(new Error('network failure'), 'fallback'), 'network failure')

assert.equal(shouldShowKnowledgeSummary({
  source_type: 'slack_qa',
  status: 'pending',
  title: '질문',
  summary: '답변 앞부분',
}), false)
assert.equal(shouldShowKnowledgeSummary({
  source_type: 'slack_qa',
  status: 'classified',
  title: '분류 제목',
  summary: '분류된 요약',
}), true)
assert.equal(shouldShowKnowledgeSummary({
  source_type: 'cron',
  status: 'classified',
  title: 'Cron 제목',
  summary: 'Cron 요약',
}), true)
assert.equal(shouldShowKnowledgeQuestion({
  source_type: 'slack_qa',
  status: 'pending',
  title: '같은 질문',
  question: '  같은   질문  ',
}), false)
assert.equal(shouldShowKnowledgeQuestion({
  source_type: 'slack_qa',
  status: 'classified',
  title: '짧은 분류 제목',
  question: '사용자가 입력한 전체 질문',
}), true)

console.log('frontend utility tests passed')
