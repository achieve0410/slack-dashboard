export interface RunState {
  read: boolean
  bookmarked: boolean
  completed: boolean
  archived: boolean
  read_at: string | null
  bookmarked_at: string | null
  completed_at: string | null
  archived_at: string | null
  note: string
  created_at: string | null
  updated_at: string | null
}

export interface PlatformTokenAgent {
  key: string
  name: string
  capabilities?: string[]
}

export type PlatformTokenStatus = 'active' | 'inactive' | 'revoked' | 'expired' | 'agent_inactive'

export interface PlatformToken {
  id: number
  name: string
  agent: PlatformTokenAgent
  token_prefix: string
  scopes: string[]
  status: PlatformTokenStatus
  is_active: boolean
  expires_at: string | null
  last_used_at: string | null
  created_at: string
  revoked_at: string | null
  file_present: boolean
}

export interface PlatformTokenCollection {
  tokens: PlatformToken[]
  agents: PlatformTokenAgent[]
  available_scopes: string[]
}

export interface PlatformTokenIssueResponse {
  token: PlatformToken
  secret: string
  secret_visible_once: boolean
}

export interface RunJob {
  id: number
  external_id: string
  name: string
  category: string
  category_label: string
}

export interface RunSummary {
  id: number
  title: string
  status: 'success' | 'failed'
  generated_at: string
  excerpt: string
  job: RunJob
  state: RunState
  citation_count: number
}

export interface Citation {
  title: string
  url: string
  publisher: string
}

export interface UserResponse {
  id: number
  question_key: string
  answer: string
  feedback: string
  score: string | null
  created_at: string
}

export interface RunDetail extends RunSummary {
  body: string
  error: string
  structured_data: Record<string, unknown>
  model_name: string
  prompt_version: string
  citations: Citation[]
  responses: UserResponse[]
  knowledge_item: KnowledgeDetail | null
}

export interface CronJob {
  id: number
  external_id: string
  name: string
  category: string
  category_label: string
  schedule: string
  timezone: string
  enabled: boolean
  state: string
  last_status: string
  last_error: string
  last_run_at: string | null
  next_run_at: string | null
  thread_ts: string
  model_name: string
}

export interface Summary {
  jobs: { total: number, enabled: number, success: number, failed: number }
  progress: { completed: number, responses: number }
  knowledge: {
    classified: number
    generated_today: number
    awaiting_answer: number
    pending: number
    needs_review: number
    scheduled_today: number
  }
  categories: Array<{
    category: string
    label: string
    total: number
    healthy: number
    failed: number
  }>
  latest_runs: RunSummary[]
  recent_failures: RunSummary[]
  latest_knowledge: KnowledgeCard[]
  operations: OperationSummary
  backlog: OperationBacklog
}

export interface KnowledgeCategory {
  id: number
  name: string
  path: string
  depth: number
  parent_id: number | null
}

export interface CategoryNode extends KnowledgeCategory {
  classified_count: number
  children: CategoryNode[]
}

export type KnowledgeStatus = 'awaiting_answer' | 'pending' | 'classified' | 'needs_review'

export interface KnowledgeCard {
  id: number
  title: string
  summary: string
  generated_at: string
  classified_at?: string | null
  classification_stale_at?: string | null
  source_type: 'cron' | 'slack_qa'
  source_label: string
  status: KnowledgeStatus
  status_label: string
  category?: KnowledgeCategory | null
  category_path?: string
  detail_url: string
  content_run_id?: number | null
  state?: RunState | null
  question_excerpt?: string
  has_answer?: boolean
  tags: string[]
}

export interface KnowledgeListResponse {
  count: number
  limit: number
  offset: number
  next_offset: number | null
  canonical_query: string
  results: KnowledgeCard[]
}

export interface SavedKnowledgeView {
  id: number
  name: string
  filters: Record<string, string | number>
  sort: 'newest' | 'oldest'
  is_default: boolean
  created_at: string
  updated_at: string
}

export interface SavedKnowledgeViewList {
  count: number
  results: SavedKnowledgeView[]
}

export interface SavedKnowledgeViewApply extends SavedKnowledgeView {
  canonical_query: string
}

export interface KnowledgeNavigationItem {
  id: number
  title: string
  detail_url: string
}

export interface KnowledgeNavigationResponse {
  previous: KnowledgeNavigationItem | null
  next: KnowledgeNavigationItem | null
  position: number
  total: number
  canonical_query: string
  related: KnowledgeCard[]
}

export type BulkAction = 'read' | 'bookmarked' | 'completed' | 'archived' | 'category' | 'hide'

export interface BulkPreviewResponse {
  token: string
  count: number
  ineligible: number
  expires_at: string
}

export interface BulkExecuteResponse {
  count: number
  affected_ids: number[]
  undo_expires_at?: string
}

export interface TrashKnowledgeCard extends KnowledgeCard {
  hidden_at: string
}

export interface TrashKnowledgeListResponse {
  count: number
  limit: number
  offset: number
  next_offset: number | null
  results: TrashKnowledgeCard[]
}

export type AgendaGroup = 'completed' | 'today' | 'overdue_todo' | 'past_schedule' | 'upcoming' | 'undated'

export interface ScheduleEvent {
  id: number
  title: string
  item_type: 'schedule' | 'todo'
  item_type_label: string
  todo_category_id: number | null
  todo_category_label: string
  todo_category_manual: boolean
  starts_at: string | null
  ends_at: string | null
  all_day: boolean
  notes: string
  completed: boolean
  source_type: 'manual' | 'slack'
  source_label: string
  slack_channel_id: string | null
  created_at: string
  updated_at: string
  agenda_group?: AgendaGroup
}

export interface ScheduleCategory {
  id: number
  name: string
  keywords: string[]
  is_fallback: boolean
  usage_count: number
}

export interface ScheduleListResponse {
  count: number
  results: ScheduleEvent[]
  group_counts?: Record<AgendaGroup, number>
}

export type OperationKind = 'sync' | 'tagging' | 'classify'
export type OperationRunKind = OperationKind | 'quiz'
export type OperationStatus = 'running' | 'success' | 'failed' | 'skipped'

export interface OperationRun {
  id: number
  kind: OperationRunKind
  status: OperationStatus
  error_code: string
  summary: Record<string, number | boolean | null>
  started_at: string
  finished_at: string | null
}

export interface OperationFreshness {
  last_attempt: OperationRun | null
  last_success_at: string | null
  stale: boolean
  threshold_seconds: number
  schedule_label: string
}

export interface OperationSummary {
  sync: OperationFreshness
  tagging: OperationFreshness
  classify: OperationFreshness
}

export interface OperationBacklog {
  pending: number
  review: number
  total: number
}

export interface OperationsResponse {
  operations: OperationSummary
  backlog: OperationBacklog
  results: OperationRun[]
}

export interface KnowledgeReviewer {
  id: number
  username: string
}

export interface KnowledgeSlackSource {
  channel_id: string
  thread_ts: string
  source_url: string
}

export interface KnowledgeDetail {
  id: number
  title: string
  summary: string
  generated_at: string
  classified_at: string | null
  classification_stale_at: string | null
  source_type: 'cron' | 'slack_qa'
  source_label: string
  status: KnowledgeStatus
  status_label: string
  category: KnowledgeCategory | null
  category_path: string
  detail_url: string
  classification_model: string
  classification_confidence: string | null
  classification_reason: string
  reviewed_by: KnowledgeReviewer | null
  reviewed_at: string | null
  state: RunState
  question?: string
  answer?: string
  slack?: KnowledgeSlackSource
  content_run_id?: number
  tags: string[]
}

export interface KnowledgeTagsUpdateRequest {
  tags: string[]
}

export interface KnowledgeTagsUpdateResponse {
  id: number
  tags: string[]
}

export type QuizDomain = 'english' | 'japanese' | 'aws_saa'
export type QuizDifficulty = 'beginner' | 'intermediate' | 'advanced'
export type QuizMode = 'new' | 'review' | 'wrong'
export type QuizQuestionType = 'single_choice' | 'multiple_select'
export type QuizSessionStatus = 'active' | 'completed'

export interface QuizCatalogResponse {
  domains: QuizDomain[]
  difficulty_levels: QuizDifficulty[]
  question_types: QuizQuestionType[]
  available_counts: Record<string, number>
  allowlist_version: string
  published_at: string | null
  empty_state: boolean
}

export interface QuizChoice {
  id: string
  label: string
}

export interface QuizCurrentItem {
  id: number
  position: number
  question_type: QuizQuestionType
  prompt: string
  choices: QuizChoice[]
  domain: QuizDomain
  difficulty: QuizDifficulty
}

export interface QuizSessionItemSummary {
  id: number
  position: number
  answered: boolean
  correct: boolean | null
}

export interface QuizSessionSummary {
  status: QuizSessionStatus
  score: number
  correct_count: number
  incorrect_count: number
  answered_count: number
  total_count: number
  completed: boolean
}

export interface QuizReviewSummary {
  wrong_count: number
  answered_count: number
}

export interface QuizSessionProgressCount {
  answered_count: number
  total_count: number
}

export interface QuizSessionResponse {
  session_id: string
  status: QuizSessionStatus
  domain: QuizDomain
  difficulty: QuizDifficulty
  mode: QuizMode
  required_count: number
  current_item: QuizCurrentItem | null
  result: QuizSessionSummary | null
  review_summary: QuizReviewSummary
  progress: QuizSessionProgressCount
  items: QuizSessionItemSummary[]
}

export interface QuizSessionStartResponse extends QuizSessionResponse {
  available_count: number
}

export interface QuizSessionHistoryItem {
  session_id: string
  status: QuizSessionStatus
  domain: QuizDomain
  difficulty: QuizDifficulty
  mode: QuizMode
  answered_count: number
  total_count: number
  score: number
  started_at: string
  completed_at: string | null
}

export interface QuizSessionHistoryList {
  results: QuizSessionHistoryItem[]
}

export interface QuizProgress {
  stage: string
  wrong_count: number
  correct_streak: number
  next_review_at: string | null
  last_answered_at: string | null
  mastered_at: string | null
  manual_wrong_note_at: string | null
}

export interface QuizSourceLink {
  title: string
  detail_url: string
  source_type?: string
  source_key?: string
}

export interface QuizAnswerResponse {
  accepted_choice_ids: string[]
  correct: boolean
  correct_choice_ids: string[]
  explanation: string
  source: QuizSourceLink
  progress: QuizProgress
  next_item: QuizCurrentItem | null
  session_summary: QuizSessionSummary
}

export interface QuizResultItem {
  item_id: number
  position: number
  question_id: number
  prompt: string
  accepted_choice_ids: string[]
  correct: boolean
  correct_choice_ids: string[]
  explanation: string
  source: QuizSourceLink
}

export interface QuizResultResponse {
  session_id: string
  status: QuizSessionStatus
  score: number
  correct_count: number
  incorrect_count: number
  mastered_count: number
  item_results: QuizResultItem[]
  review_summary: QuizReviewSummary
  completed_at: string
}

export interface QuizTodayGoal {
  target: number
  completed: number
  remaining: number
}

export interface QuizStreak {
  current_days: number
}

export interface QuizPriorFeedback {
  correct: boolean
  answered_at: string
  session_id: string
}

export interface QuizReviewItem {
  question_id: number
  question_type: QuizQuestionType
  domain: QuizDomain
  difficulty: QuizDifficulty
  stage: string
  wrong_count: number
  correct_streak: number
  next_review_at: string | null
  last_answered_at: string | null
  mastered_at: string | null
  manual_wrong_note_at: string | null
  prior_feedback: QuizPriorFeedback | null
  source: QuizSourceLink
}

export interface QuizReviewResponse {
  items: QuizReviewItem[]
  due_count: number
  stage_counts: Record<string, number>
  today_goal: QuizTodayGoal
  streak: QuizStreak
  last_reviewed_at: string | null
}

export interface QuizWrongNoteResponse {
  question_id: number
  progress: QuizProgress
}
