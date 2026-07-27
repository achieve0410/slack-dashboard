import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const [
  cardSource,
  indexSource,
  detailSource,
  runDetailSource,
  tagsPanelSource,
  typesSource,
  stateSource,
  cssSource,
] = await Promise.all([
  readFile(new URL('../app/components/KnowledgeCard.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/index.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/knowledge/[id].vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/pages/runs/[id].vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/components/KnowledgeTagsPanel.vue', import.meta.url), 'utf8'),
  readFile(new URL('../app/types/api.ts', import.meta.url), 'utf8'),
  readFile(new URL('../app/utils/knowledgeState.ts', import.meta.url), 'utf8'),
  readFile(new URL('../app/assets/css/main.css', import.meta.url), 'utf8'),
])

assert.match(typesSource, /export interface KnowledgeCard \{[\s\S]*?tags: string\[\]/)
assert.match(typesSource, /export interface KnowledgeDetail \{[\s\S]*?tags: string\[\]/)
assert.match(typesSource, /export interface KnowledgeTagsUpdateRequest \{[\s\S]*?tags: string\[\]/)
assert.match(typesSource, /export interface KnowledgeTagsUpdateResponse \{[\s\S]*?id: number[\s\S]*?tags: string\[\]/)
assert.match(stateSource, /tags: updated\.tags/)

assert.match(cardSource, /<article[\s\S]*class="\['run-card', 'knowledge-card'/)
assert.match(cardSource, /<NuxtLink :to="detailLink" class="run-card-main">/)
const nuxtLinkBody = cardSource.slice(cardSource.indexOf('<NuxtLink'), cardSource.indexOf('</NuxtLink>'))
assert.doesNotMatch(nuxtLinkBody, /<button\b/)
assert.match(cardSource, /class="\['knowledge-tag-chip', \{ active: selectedTag === tag \}\]"/)
assert.match(cardSource, /:aria-pressed="selectedTag === tag"/)
assert.match(cardSource, /@click="handleTagClick\(tag\)"/)

assert.match(indexSource, /const selectedTag = computed\(\(\) => queryValue\(route\.query\.tag\)\.trim\(\)\)/)
assert.match(indexSource, /async function applyTagFilter\(tag: string\)[\s\S]*view: 'library'[\s\S]*\.\.\.canonicalKnowledgeQuery\(route\.query\)[\s\S]*tag/)
assert.match(indexSource, /async function clearTagFilter\(\)[\s\S]*delete query\.tag[\s\S]*router\.push\(\{ path: '\/', query \}\)/)
assert.match(indexSource, /if \(selectedTag\.value\) query\.tag = selectedTag\.value/)
assert.match(indexSource, /class="selected-tag-filter"/)
assert.match(indexSource, /:selected-tag="selectedTag" @tag-click="applyTagFilter"/)

assert.match(detailSource, /<KnowledgeTagsPanel[\s\S]*:item-id="item\.id"[\s\S]*:tags="item\.tags"[\s\S]*@updated="handleTagsUpdated"/)
assert.match(runDetailSource, /<KnowledgeTagsPanel[\s\S]*v-if="run\.knowledge_item"[\s\S]*:item-id="run\.knowledge_item\.id"[\s\S]*:tags="run\.knowledge_item\.tags"[\s\S]*@updated="handleTagsUpdated"/)
assert.match(tagsPanelSource, /태그는 다음 야간 재계산 시 변경될 수 있습니다\./)
assert.match(tagsPanelSource, /\.split\(\s*\/\[,\\n\]\/\s*\)/)
assert.match(tagsPanelSource, /tags\.length < 3/)
assert.match(tagsPanelSource, /method: 'PATCH'/)
assert.match(tagsPanelSource, /`\/api\/knowledge\/\$\{props\.itemId\}\/tags\/`/)
assert.match(tagsPanelSource, /emit\('updated', response\.tags\)/)

assert.match(cssSource, /\.knowledge-tag-row\s*\{[\s\S]*?min-width: 0;[\s\S]*?flex-wrap: wrap;/)
assert.match(cssSource, /\.knowledge-tag-chip\s*\{[\s\S]*?min-width: 0;[\s\S]*?overflow-wrap: anywhere;/)
assert.match(cssSource, /\.knowledge-tag-chip:focus-visible\s*\{[\s\S]*?outline: 3px solid/)
assert.match(cssSource, /@media \(max-width: 700px\)[\s\S]*?\.knowledge-tag-chip \{ min-height: 44px;/)
assert.match(cssSource, /\.selected-tag-filter\s*\{[\s\S]*?flex-wrap: wrap;/)
assert.match(cssSource, /\.selected-tag-filter b\s*\{[\s\S]*?overflow-wrap: anywhere;/)
assert.match(cssSource, /\.run-card h3\s*\{[\s\S]*?overflow-wrap: anywhere;/)

console.log('frontend knowledge tag regression tests passed')
