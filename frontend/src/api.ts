export const API_BASE = 'http://127.0.0.1:8000'

export interface SearchResult {
  title: string
  title_id: number
}

export interface WikiLink {
  label: string
  target: string
  char_start?: number
  char_end?: number
}

export interface ArticleSection {
  section_key: string
  title_id: number
  heading_id: number
  heading: string
  level: number | null
  parent_id: number | null
  clean_text: string
  content_html: string
  links: WikiLink[]
  provenance: Record<string, unknown>
}

export interface ArticleCore {
  summary: string
  topic: Record<string, unknown>
  key_entities: Array<Record<string, unknown>>
  dated_spine: Array<Record<string, unknown>>
  model_version: string
  updated_at: string | null
}

export interface ArticleResponse {
  title: string
  title_id: number
  sections: ArticleSection[]
  core: ArticleCore | null
  cached: boolean
  warnings?: string[]
}

export interface RelatedItem {
  to_title_id: number
  to_title: string
  level: number
  score: number
  signals: Record<string, unknown>
  why_text: string
  why_source: string
  model_version: string
  summary: string
  agent_updated_at: string | null
  why: Record<string, unknown>
}

export interface RelatedResponse {
  section_key: string
  items: RelatedItem[]
  scoring_metrics?: Record<string, unknown>
}

export interface TimelineEvent {
  id: string
  title_id: number
  source_title_id: number
  source_title: string
  heading_id: number
  source_heading: string
  section_key: string
  heading: string
  time_ref_id: string
  time_kind: string
  label: string
  precision: 'day' | 'month' | 'season' | 'year' | string | null
  start_date: string | null
  end_date: string | null
  year: number | null
  month: number | null
  day: number | null
  season: string | null
  source: string
  confidence: number
  excerpt: string
  lane: string
  domain_lane: string
  level: number
  track: string
  relevance_score: number
  provenance: Record<string, unknown>
  attribution: Record<string, unknown>
  why: Record<string, unknown>
}

export interface TimelineResponse {
  title: string
  title_id: number
  events: TimelineEvent[]
  seeded: boolean
  enrichment_pending: boolean
  read_path_llm_used: boolean
  scoring_metrics?: Record<string, unknown>
}

export interface ArticleGraphNode {
  id: string
  kind: string
  label: string
  postgres_id: string | number | null
  title_id: number | null
  title: string | null
  section_key: string | null
  heading_id: number | null
  level: number | null
  coverage: Record<string, unknown>
  metrics: Record<string, unknown>
}

export interface ArticleGraphEdge {
  id: string
  kind: string
  source: string
  target: string
  level: number | null
  score: number | null
  why_source: string | null
  signals: Record<string, unknown>
}

export interface ArticleGraphResponse {
  title: string
  title_id: number
  nodes: ArticleGraphNode[]
  edges: ArticleGraphEdge[]
  synced_to_neo4j: boolean
}

export interface GraphCoverageArticle {
  title_id: number
  title: string
  level: number
  sections_cached: number
  sections_selected: number
  jobs_enqueued: Record<string, number>
  status: string
  error: string | null
}

export interface GraphCoverageResponse {
  title: string
  title_id: number
  frontier_considered: number
  articles_processed: number
  jobs_enqueued: Record<string, number>
  articles: GraphCoverageArticle[]
}

export interface GraphologyNodePayload {
  key: string
  attributes: Record<string, unknown>
}

export interface GraphologyEdgePayload {
  key: string
  source: string
  target: string
  attributes: Record<string, unknown>
}

export interface GraphologyGraphResponse {
  attributes: Record<string, unknown>
  nodes: GraphologyNodePayload[]
  edges: GraphologyEdgePayload[]
}

export interface TimelineExplainResponse {
  section_key: string
  time_ref_id: string
  why_text: string
  why_source: string
  model_version: string
  generated_at: string
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `Request failed with ${response.status}`)
  }
  return response.json() as Promise<T>
}

export function searchArticles(query: string, limit = 12): Promise<SearchResult[]> {
  const params = new URLSearchParams({ q: query, limit: String(limit) })
  return requestJson(`${API_BASE}/api/reader/search?${params}`)
}

export function fetchArticle(title: string, seed = false, enableInsights = false): Promise<ArticleResponse> {
  const params = new URLSearchParams({
    seed: String(seed),
    enable_insights: String(enableInsights),
    agent_temporal: String(enableInsights),
    agent_related: String(enableInsights),
    related_warmup_limit: '0',
  })
  return requestJson(`${API_BASE}/api/reader/article/${encodeURIComponent(title)}?${params}`)
}

export function refreshArticle(title: string, enableInsights = false): Promise<ArticleResponse> {
  const params = new URLSearchParams({
    seed: String(enableInsights),
    enable_insights: String(enableInsights),
    agent_temporal: String(enableInsights),
    agent_related: String(enableInsights),
    related_warmup_limit: '0',
  })
  return requestJson(`${API_BASE}/api/reader/article/${encodeURIComponent(title)}/refresh?${params}`, {
    method: 'POST',
  })
}

export function fetchRelated(
  sectionKey: string,
  limit = 8,
  refresh = false,
  enableInsights = false,
): Promise<RelatedResponse> {
  const params = new URLSearchParams({
    limit: String(limit),
    refresh: String(refresh),
    agent_related: String(enableInsights),
  })
  return requestJson(`${API_BASE}/api/reader/section/${encodeURIComponent(sectionKey)}/related?${params}`)
}

export interface AgentEnqueueResponse {
  title: string
  title_id: number
  job_type: string
  sections_considered: number
  jobs_enqueued: number
  force: boolean
}

export interface ProcessingAreaStatus {
  key: string
  label: string
  state: 'idle' | 'pending' | 'running' | 'incomplete' | 'partial' | 'stale' | 'completed' | 'attention' | string
  pending: number
  running: number
  failed: number
  completed: number
  total: number
  detail: string
  reason: string
  last_error: string | null
  updated_at?: string | null
  started_at?: string | null
  completed_at?: string | null
  elapsed_seconds?: number | null
  source?: string
}

export interface ProcessingStatusResponse {
  title: string | null
  title_id: number | null
  section_key: string | null
  overall_state: string
  areas: ProcessingAreaStatus[]
  metrics?: Record<string, unknown>
}

export function fetchProcessingStatus(title: string, sectionKey?: string | null): Promise<ProcessingStatusResponse> {
  const params = new URLSearchParams()
  if (sectionKey) params.set('section_key', sectionKey)
  const suffix = params.toString() ? `?${params}` : ''
  return requestJson(`${API_BASE}/api/reader/article/${encodeURIComponent(title)}/status${suffix}`)
}

export function rerunRelatedAgent(sectionKey: string): Promise<AgentEnqueueResponse> {
  return requestJson(
    `${API_BASE}/api/reader/section/${encodeURIComponent(sectionKey)}/agent/related?force=true`,
    { method: 'POST' },
  )
}

export function explainRelatedItem(sectionKey: string, toTitleId: number): Promise<AgentEnqueueResponse> {
  return requestJson(
    `${API_BASE}/api/reader/section/${encodeURIComponent(sectionKey)}/agent/related/${toTitleId}?force=true`,
    { method: 'POST' },
  )
}

export function fetchTimeline(title: string, seedMissing = true, enrichContext = true): Promise<TimelineResponse> {
  const params = new URLSearchParams({
    seed_missing: String(seedMissing),
    enrich_context: String(enrichContext),
  })
  return requestJson(`${API_BASE}/api/reader/article/${encodeURIComponent(title)}/timeline?${params}`)
}

export function fetchArticleGraph(title: string, relatedLimit = 240, syncNeo4j = true): Promise<ArticleGraphResponse> {
  const params = new URLSearchParams({
    related_limit: String(relatedLimit),
    sync_neo4j: String(syncNeo4j),
  })
  return requestJson(`${API_BASE}/api/reader/article/${encodeURIComponent(title)}/graph?${params}`)
}

export function crawlArticleGraph(
  title: string,
  maxArticles = 12,
  maxSectionsPerArticle = 4,
  force = false,
): Promise<GraphCoverageResponse> {
  const params = new URLSearchParams({
    max_articles: String(maxArticles),
    max_sections_per_article: String(maxSectionsPerArticle),
    force: String(force),
  })
  return requestJson(`${API_BASE}/api/reader/article/${encodeURIComponent(title)}/graph/crawl?${params}`, {
    method: 'POST',
  })
}

export function fetchGraphologyArticle(title: string, minRelevance = 0, maxNodes = 1000): Promise<GraphologyGraphResponse> {
  const params = new URLSearchParams({
    levels: '2',
    min_relevance: String(minRelevance),
    include_back_edges: 'true',
    include_cross_edges: 'true',
    max_nodes: String(maxNodes),
  })
  return requestJson(`${API_BASE}/api/graph/article/${encodeURIComponent(title)}?${params}`)
}

export function expandGraphologyArticle(
  title: string,
  options: { sectionKey?: string; l1TitleId?: number | null; l1Title?: string | null },
  maxNodes = 1000,
): Promise<GraphologyGraphResponse> {
  const params = new URLSearchParams({ max_nodes: String(maxNodes) })
  if (options.sectionKey) params.set('section_key', options.sectionKey)
  if (options.l1TitleId) params.set('l1_title_id', String(options.l1TitleId))
  if (options.l1Title) params.set('l1_title', options.l1Title)
  return requestJson(`${API_BASE}/api/graph/article/${encodeURIComponent(title)}/expand?${params}`, {
    method: 'POST',
  })
}

export function explainTimelineEvent(event: TimelineEvent): Promise<TimelineExplainResponse> {
  return requestJson(`${API_BASE}/api/reader/timeline/explain`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      section_key: event.section_key,
      time_ref_id: event.time_ref_id,
      label: event.label,
      domain_lane: event.domain_lane || 'Society & People',
      level: event.level ?? 0,
      track: event.track || 'core',
      relevance_score: event.relevance_score ?? 1,
    }),
  })
}
