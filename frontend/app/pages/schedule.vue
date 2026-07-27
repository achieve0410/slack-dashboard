<script setup lang="ts">
import type { AgendaGroup, ScheduleCategory, ScheduleEvent, ScheduleListResponse } from '~/types/api'
import { groupScheduleEvents, scheduleGroupTotal } from '~/utils/scheduleGroups'

const { request } = useApi()
const events = ref<ScheduleEvent[]>([])
const categories = ref<ScheduleCategory[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const editingId = ref<number | null>(null)
const form = reactive({
  itemType: 'schedule' as 'schedule' | 'todo',
  title: '',
  startsAt: '',
  endsAt: '',
  allDay: false,
  notes: '',
  categoryId: '',
})
const categoryForm = reactive({
  id: null as number | null,
  name: '',
  keywords: '',
})
const categorySaving = ref(false)
const groupCounts = ref<Record<AgendaGroup, number>>({
  completed: 0,
  today: 0,
  overdue_todo: 0,
  past_schedule: 0,
  upcoming: 0,
  undated: 0,
})
const agendaGroupOrder: AgendaGroup[] = ['completed', 'today', 'overdue_todo', 'past_schedule', 'upcoming', 'undated']
const agendaGroupLabels: Record<AgendaGroup, string> = {
  completed: '완료',
  today: '오늘',
  overdue_todo: '지연 TODO',
  past_schedule: '지난 일정',
  upcoming: '예정',
  undated: '기한 없음',
}
const agendaGroups = computed(() => groupScheduleEvents(
  events.value as Array<ScheduleEvent & { agenda_group: AgendaGroup }>,
  agendaGroupOrder,
))

function localInputValue(value: string, allDay: boolean): string {
  const date = new Date(value)
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  if (allDay) return `${year}-${month}-${day}`
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  return `${year}-${month}-${day}T${hour}:${minute}`
}

function defaultStart(): string {
  const date = new Date()
  date.setMinutes(0, 0, 0)
  date.setHours(date.getHours() + 1)
  return localInputValue(date.toISOString(), false)
}

function resetForm() {
  editingId.value = null
  form.itemType = 'schedule'
  form.title = ''
  form.startsAt = defaultStart()
  form.endsAt = ''
  form.allDay = false
  form.notes = ''
  form.categoryId = ''
}

function resetCategoryForm() {
  categoryForm.id = null
  categoryForm.name = ''
  categoryForm.keywords = ''
}

function eventPayload() {
  if (form.itemType === 'todo') {
    return {
      item_type: form.itemType,
      title: form.title.trim(),
      starts_at: form.startsAt ? `${form.startsAt}T00:00:00` : null,
      ends_at: null,
      all_day: Boolean(form.startsAt),
      notes: form.notes.trim(),
      todo_category_id: form.categoryId ? Number(form.categoryId) : null,
    }
  }
  let endsAt: string | null = form.endsAt || null
  if (endsAt && form.allDay) endsAt = `${endsAt}T23:59:59`
  return {
    item_type: form.itemType,
    title: form.title.trim(),
    starts_at: form.allDay ? `${form.startsAt}T00:00:00` : form.startsAt,
    ends_at: endsAt,
    all_day: form.allDay,
    notes: form.notes.trim(),
  }
}

function eventSubmitLabel(): string {
  if (saving.value) return '저장 중…'
  if (editingId.value) return '수정 저장'
  return form.itemType === 'todo' ? '할 일 추가' : '일정 추가'
}

function categorySubmitLabel(): string {
  if (categorySaving.value) return '저장 중…'
  return categoryForm.id ? '카테고리 수정' : '카테고리 추가'
}

async function loadEvents() {
  loading.value = true
  error.value = ''
  try {
    const [eventData, categoryData] = await Promise.all([
      request<ScheduleListResponse>('/api/schedule/?grouped=1'),
      request<{ results: ScheduleCategory[] }>('/api/schedule/categories/'),
    ])
    if (
      !eventData.group_counts
      || scheduleGroupTotal(eventData.group_counts) !== eventData.count
      || eventData.results.length !== eventData.count
    ) {
      throw new Error('일정 그룹 합계가 전체 개수와 일치하지 않습니다.')
    }
    if (eventData.results.some(event => !event.agenda_group || !agendaGroupOrder.includes(event.agenda_group))) {
      throw new Error('분류되지 않은 일정 그룹이 있습니다.')
    }
    events.value = eventData.results
    groupCounts.value = eventData.group_counts
    categories.value = categoryData.results
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '일정을 불러오지 못했습니다.'
  }
  finally {
    loading.value = false
  }
}

async function saveEvent() {
  if (!form.title.trim() || (form.itemType === 'schedule' && !form.startsAt)) return
  saving.value = true
  error.value = ''
  try {
    if (editingId.value) {
      await request<ScheduleEvent>(`/api/schedule/${editingId.value}/`, {
        method: 'PATCH',
        body: eventPayload(),
      })
    }
    else {
      await request<ScheduleEvent>('/api/schedule/', {
        method: 'POST',
        body: eventPayload(),
      })
    }
    resetForm()
    await loadEvents()
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '일정을 저장하지 못했습니다.'
  }
  finally {
    saving.value = false
  }
}

function editEvent(event: ScheduleEvent) {
  if (event.source_type === 'slack') return
  editingId.value = event.id
  form.itemType = event.item_type
  form.title = event.title
  form.startsAt = event.starts_at
    ? localInputValue(event.starts_at, event.item_type === 'todo' || event.all_day)
    : ''
  form.endsAt = event.ends_at ? localInputValue(event.ends_at, event.all_day) : ''
  form.allDay = event.all_day
  form.notes = event.notes
  form.categoryId = event.todo_category_manual && event.todo_category_id
    ? String(event.todo_category_id)
    : ''
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function editCategory(category: ScheduleCategory) {
  categoryForm.id = category.id
  categoryForm.name = category.name
  categoryForm.keywords = category.keywords.join(', ')
}

async function saveCategory() {
  if (!categoryForm.name.trim()) return
  categorySaving.value = true
  error.value = ''
  const body = {
    name: categoryForm.name.trim(),
    keywords: categoryForm.keywords.split(',').map(value => value.trim()).filter(Boolean),
  }
  try {
    if (categoryForm.id) {
      await request(`/api/schedule/categories/${categoryForm.id}/`, {
        method: 'PATCH',
        body,
      })
    }
    else {
      await request('/api/schedule/categories/', { method: 'POST', body })
    }
    resetCategoryForm()
    await loadEvents()
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '카테고리를 저장하지 못했습니다.'
  }
  finally {
    categorySaving.value = false
  }
}

async function deleteCategory(category: ScheduleCategory) {
  if (!window.confirm(`“${category.name}” 카테고리를 삭제할까요?`)) return
  error.value = ''
  try {
    await request(`/api/schedule/categories/${category.id}/`, { method: 'DELETE' })
    if (categoryForm.id === category.id) resetCategoryForm()
    await loadEvents()
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '카테고리를 삭제하지 못했습니다.'
  }
}

function changeItemType() {
  form.endsAt = ''
  form.allDay = false
  form.startsAt = form.itemType === 'schedule' ? defaultStart() : ''
}

async function toggleCompleted(event: ScheduleEvent) {
  error.value = ''
  try {
    await request<ScheduleEvent>(`/api/schedule/${event.id}/`, {
      method: 'PATCH',
      body: { completed: !event.completed },
    })
    await loadEvents()
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '일정 상태를 변경하지 못했습니다.'
  }
}

async function changeEventCategory(event: ScheduleEvent, value: Event) {
  const target = value.target as HTMLSelectElement
  error.value = ''
  try {
    await request<ScheduleEvent>(`/api/schedule/${event.id}/`, {
      method: 'PATCH',
      body: { todo_category_id: target.value ? Number(target.value) : null },
    })
    await loadEvents()
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '카테고리를 변경하지 못했습니다.'
  }
}

async function deleteEvent(event: ScheduleEvent) {
  if (!window.confirm(`“${event.title}” 항목을 삭제할까요?`)) return
  error.value = ''
  try {
    await request(`/api/schedule/${event.id}/`, { method: 'DELETE' })
    if (editingId.value === event.id) resetForm()
    await loadEvents()
  }
  catch (reason) {
    error.value = reason instanceof Error ? reason.message : '일정을 삭제하지 못했습니다.'
  }
}

function formatDate(event: ScheduleEvent): string {
  if (!event.starts_at) return '기한 없음'
  if (event.item_type === 'todo') {
    const dueDate = new Intl.DateTimeFormat('ko-KR', {
      year: 'numeric', month: 'long', day: 'numeric', weekday: 'short',
    }).format(new Date(event.starts_at))
    return `${dueDate}까지`
  }
  const options: Intl.DateTimeFormatOptions = event.all_day
    ? { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' }
    : { month: 'long', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit' }
  return new Intl.DateTimeFormat('ko-KR', options).format(new Date(event.starts_at))
}

resetForm()
onMounted(loadEvents)
</script>

<template>
  <div class="page-container schedule-page">
    <header class="page-command-bar">
      <div><p class="eyebrow">SCHEDULE & TODO</p><h1>일정·할 일 관리</h1><p>웹에서 직접 등록하거나 <code>SLACK_DASHBOARD_SCHEDULE_CHANNEL</code>로 지정한 Slack 채널에서 일정과 TODO를 관리합니다.</p></div>
      <button class="ghost-button" type="button" @click="loadEvents">새로고침</button>
    </header>

    <div v-if="error" class="notice error-notice" role="alert">{{ error }}</div>

    <div class="schedule-layout">
      <section class="schedule-form-panel" aria-labelledby="schedule-form-title">
        <div class="panel-heading"><div><p class="eyebrow">{{ editingId ? 'EDIT' : 'NEW ITEM' }}</p><h2 id="schedule-form-title">{{ editingId ? '항목 수정' : '새 일정·할 일' }}</h2></div></div>
        <form @submit.prevent="saveEvent">
          <label class="field-label">유형<select v-model="form.itemType" @change="changeItemType"><option value="schedule">일정</option><option value="todo">할 일</option></select></label>
          <label class="field-label">제목<input v-model="form.title" type="text" maxlength="200" required :placeholder="form.itemType === 'todo' ? '해야 할 일' : '일정 제목'"></label>
          <template v-if="form.itemType === 'schedule'">
            <label class="check-field"><input v-model="form.allDay" type="checkbox"><span>종일 일정</span></label>
            <div class="schedule-time-fields">
              <label class="field-label">시작<input v-model="form.startsAt" :type="form.allDay ? 'date' : 'datetime-local'" required></label>
              <label class="field-label">종료 <small>선택</small><input v-model="form.endsAt" :type="form.allDay ? 'date' : 'datetime-local'"></label>
            </div>
          </template>
          <label v-else class="field-label">기한 <small>선택</small><input v-model="form.startsAt" type="date"></label>
          <label v-if="form.itemType === 'todo'" class="field-label">카테고리<select v-model="form.categoryId"><option value="">제목으로 자동 분류</option><option v-for="category in categories" :key="category.id" :value="String(category.id)">{{ category.name }}</option></select></label>
          <label class="field-label">메모<textarea v-model="form.notes" rows="5" maxlength="5000" placeholder="준비할 내용이나 참고 사항" /></label>
          <div class="form-actions">
            <button class="submit-button" type="submit" :disabled="saving || !form.title.trim() || (form.itemType === 'schedule' && !form.startsAt)">{{ eventSubmitLabel() }}</button>
            <button v-if="editingId" class="text-button" type="button" @click="resetForm">취소</button>
          </div>
        </form>
      </section>

      <section class="schedule-list-panel" aria-labelledby="schedule-list-title">
        <div class="panel-heading"><div><p class="eyebrow">AGENDA</p><h2 id="schedule-list-title">전체 일정·할 일</h2></div><span>{{ events.length }}개</span></div>
        <div v-if="loading" class="notice" role="status">일정을 불러오는 중입니다.</div>
        <div v-else-if="events.length" class="agenda-groups">
          <details v-for="group in agendaGroupOrder" :key="group" class="agenda-group" :open="group === 'today' || group === 'overdue_todo' || group === 'upcoming'">
            <summary><span>{{ agendaGroupLabels[group] }}</span><strong>{{ groupCounts[group] }}개</strong></summary>
            <div v-if="agendaGroups[group]?.length" class="schedule-event-list">
              <article v-for="event in agendaGroups[group] || []" :key="event.id" :class="['schedule-event', { completed: event.completed }]">
                <button class="event-check" type="button" :aria-label="event.completed ? '완료 취소' : '항목 완료'" @click="toggleCompleted(event)">{{ event.completed ? '✓' : '' }}</button>
                <div class="event-content">
                  <div class="event-meta"><time :datetime="event.starts_at || undefined">{{ formatDate(event) }}<span v-if="event.item_type === 'schedule' && event.all_day"> · 종일</span></time><span :class="['schedule-source', event.source_type]">{{ event.source_label }} {{ event.item_type_label }}</span><span v-if="event.item_type === 'todo'" class="todo-category">{{ event.todo_category_label }}<small v-if="event.todo_category_manual"> · 직접 지정</small></span></div>
                  <h3>{{ event.title }}</h3><p v-if="event.notes">{{ event.notes }}</p>
                  <label v-if="event.item_type === 'todo'" class="event-category-control"><span>카테고리</span><select :value="event.todo_category_manual && event.todo_category_id ? String(event.todo_category_id) : ''" @change="changeEventCategory(event, $event)"><option value="">제목으로 자동 분류</option><option v-for="category in categories" :key="category.id" :value="String(category.id)">{{ category.name }}</option></select></label>
                </div>
                <div v-if="event.source_type === 'manual'" class="event-actions"><button type="button" @click="editEvent(event)">수정</button><button class="danger" type="button" @click="deleteEvent(event)">삭제</button></div>
                <div v-else class="event-source-help">Slack에서 수정</div>
              </article>
            </div>
            <p v-else class="agenda-empty">해당 항목이 없습니다.</p>
          </details>
        </div>
        <div v-else class="panel-empty">아직 등록된 일정이 없습니다.</div>
      </section>
    </div>

    <details class="schedule-category-panel">
      <summary><span><b>TODO 카테고리 관리</b><small>카테고리 이름과 제목 분류 키워드를 관리합니다.</small></span><strong>{{ categories.length }}개</strong></summary>
      <div class="schedule-category-content">
        <form class="schedule-category-form" @submit.prevent="saveCategory">
          <label class="field-label">카테고리 이름<input v-model="categoryForm.name" type="text" maxlength="50" required placeholder="예: 집안일"></label>
          <label class="field-label">키워드 <small>쉼표로 구분</small><input v-model="categoryForm.keywords" type="text" placeholder="예: 분리수거, 화분, 청소"></label>
          <div class="form-actions"><button class="submit-button" type="submit" :disabled="categorySaving || !categoryForm.name.trim()">{{ categorySubmitLabel() }}</button><button v-if="categoryForm.id" class="text-button" type="button" @click="resetCategoryForm">취소</button></div>
        </form>
        <div class="schedule-category-list">
          <article v-for="category in categories" :key="category.id">
            <div><strong>{{ category.name }}</strong><small>{{ category.keywords.length ? category.keywords.join(' · ') : '일치 항목이 없을 때 사용' }}</small><span>{{ category.usage_count }}개 항목</span></div>
            <div class="event-actions"><button type="button" @click="editCategory(category)">수정</button><button v-if="!category.is_fallback" class="danger" type="button" :disabled="category.usage_count > 0" :title="category.usage_count > 0 ? '사용 중인 카테고리는 삭제할 수 없습니다.' : ''" @click="deleteCategory(category)">삭제</button></div>
          </article>
        </div>
      </div>
    </details>
  </div>
</template>
