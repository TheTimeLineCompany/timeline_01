import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  LoaderCircle,
  Network,
  RefreshCw,
  Search,
  SlidersHorizontal,
} from 'lucide-react'
import {
  crawlArticleGraph,
  expandGraphologyArticle,
  fetchArticle,
  fetchGraphologyArticle,
  fetchProcessingStatus,
  fetchRelated,
  fetchTimeline,
  explainTimelineEvent,
  rerunRelatedAgent,
  refreshArticle,
  searchArticles,
  type ArticleResponse,
  type ArticleSection,
  type GraphologyGraphResponse,
  type ProcessingStatusResponse,
  type RelatedItem,
  type SearchResult,
  type TimelineEvent,
  type TimelineExplainResponse,
  type WikiLink,
} from './api'
import CytoscapeGraphView from './components/CytoscapeGraphView'

type ViewMode = 'reader' | 'timeline' | 'graph'

function firstUsableSection(article: ArticleResponse | null): string | null {
  if (!article?.sections.length) return null
  const section = article.sections.find((item) => item.clean_text.trim() || item.links.length) ?? article.sections[0]
  return section.section_key
}

function sectionTitle(section: ArticleSection): string {
  const heading = section.heading?.trim()
  if (!heading || heading.toLowerCase() === 'lead') return 'Introduction'
  return heading
}

function trimText(text: string, max = 320): string {
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (normalized.length <= max) return normalized
  return `${normalized.slice(0, max - 1).trim()}...`
}

function uniqueLinks(links: WikiLink[], limit: number): WikiLink[] {
  const seen = new Set<string>()
  const output: WikiLink[] = []
  for (const link of links) {
    const key = link.target.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    output.push(link)
    if (output.length >= limit) break
  }
  return output
}

function oneLineSectionSummary(section: ArticleSection): string {
  const clean = section.clean_text.replace(/\s+/g, ' ').trim()
  if (!clean) return 'No section text is cached yet.'
  const firstSentence = clean.match(/^.{40,220}?[.!?](?:\s|$)/)
  if (firstSentence?.[0]) return firstSentence[0].trim()
  if (clean.length <= 180) return clean
  return `${clean.slice(0, 180).trim().replace(/\s+\S*$/, '')}...`
}

function cleanAgentInsight(text: string): string {
  return text
    .replace(/^this\s+(article|candidate|item)\s+is\s+(relevant|useful)\s+because\s+/i, '')
    .replace(/^this\s+is\s+(relevant|useful)\s+because\s+/i, '')
    .replace(/^it\s+is\s+(relevant|useful)\s+because\s+/i, '')
    .trim()
}

function formatStatusUpdatedAt(value?: string | null): string {
  if (!value) return 'not recorded'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'not recorded'
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function compactStatusSource(value?: string): string {
  if (!value) return 'derived'
  return value.replace(/_/g, ' ')
}

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function metricNumber(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'n/a'
  if (Math.abs(value) >= 10) return String(Math.round(value))
  return value.toFixed(2)
}

function scoreComponents(why: Record<string, unknown>): Array<{ key: string; value: number }> {
  return asArray(why.top_components)
    .map((component) => {
      const record = asRecord(component)
      return {
        key: String(record.key ?? ''),
        value: typeof record.value === 'number' ? record.value : Number(record.value),
      }
    })
    .filter((component) => component.key && Number.isFinite(component.value))
    .slice(0, 4)
}

function whyReasons(why: Record<string, unknown>): string[] {
  const gate = asRecord(why.gate)
  return asArray(gate.reasons)
    .map((reason) => String(reason))
    .filter(Boolean)
    .slice(0, 3)
}

function compactCounts(value: unknown, limit = 4): Array<{ key: string; value: number }> {
  const record = asRecord(value)
  return Object.entries(record)
    .map(([key, raw]) => ({ key, value: typeof raw === 'number' ? raw : Number(raw) }))
    .filter((entry) => Number.isFinite(entry.value))
    .sort((a, b) => b.value - a.value)
    .slice(0, limit)
}

export default function App() {
  const [viewMode, setViewMode] = useState<ViewMode>('reader')
  const [articleTitle, setArticleTitle] = useState(() => localStorage.getItem('timeline-title') || 'Abraham Lincoln')
  const [article, setArticle] = useState<ArticleResponse | null>(null)
  const [selectedSectionKey, setSelectedSectionKey] = useState<string | null>(null)
  const [history, setHistory] = useState<string[]>(() => [localStorage.getItem('timeline-title') || 'Abraham Lincoln'])
  const [historyIndex, setHistoryIndex] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  const [isTimelineLoading, setIsTimelineLoading] = useState(false)
  const [isTimelineEnriching, setIsTimelineEnriching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [timelineEvents, setTimelineEvents] = useState<TimelineEvent[]>([])
  const [timelineScoringMetrics, setTimelineScoringMetrics] = useState<Record<string, unknown> | null>(null)
  const [relatedReloadToken, setRelatedReloadToken] = useState(0)
  const [articleGraph, setArticleGraph] = useState<GraphologyGraphResponse | null>(null)
  const [isGraphLoading, setIsGraphLoading] = useState(false)
  const [isGraphCrawling, setIsGraphCrawling] = useState(false)
  const [graphActionLabel, setGraphActionLabel] = useState<string | null>(null)
  const [graphError, setGraphError] = useState<string | null>(null)
  const [articleStatus, setArticleStatus] = useState<ProcessingStatusResponse | null>(null)
  const [sectionStatus, setSectionStatus] = useState<ProcessingStatusResponse | null>(null)
  const [insightsEnabled, setInsightsEnabled] = useState(
    () => localStorage.getItem('timeline-enable-insights') === 'true',
  )
  const articleRequestRef = useRef(0)
  const timelineRequestRef = useRef(0)
  const statusRequestRef = useRef(0)
  const graphRequestRef = useRef(0)

  const selectedSection = useMemo(
    () => article?.sections.find((section) => section.section_key === selectedSectionKey) ?? null,
    [article, selectedSectionKey],
  )

  useEffect(() => {
    let cancelled = false
    const requestId = articleRequestRef.current + 1
    articleRequestRef.current = requestId
    const requestedTitle = articleTitle
    setIsLoading(true)
    setError(null)
    setArticleStatus(null)
    setSectionStatus(null)
    setArticleGraph(null)
    setGraphActionLabel(null)
    setGraphError(null)
    setTimelineEvents([])
    setTimelineScoringMetrics(null)
    setIsTimelineEnriching(false)
    fetchArticle(articleTitle, false, insightsEnabled)
      .then((data) => {
        if (cancelled || articleRequestRef.current !== requestId || requestedTitle !== articleTitle) return
        setArticle(data)
        setArticleGraph(null)
        setGraphActionLabel(null)
        setGraphError(null)
        setTimelineEvents([])
        setTimelineScoringMetrics(null)
        setSelectedSectionKey((current) => {
          if (current && data.sections.some((section) => section.section_key === current)) return current
          return firstUsableSection(data)
        })
        localStorage.setItem('timeline-title', data.title)
      })
      .catch((err: Error) => {
        if (cancelled || articleRequestRef.current !== requestId) return
        setError(err.message || 'Failed to load article')
        setArticle(null)
      })
      .finally(() => {
        if (!cancelled && articleRequestRef.current === requestId) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [articleTitle, insightsEnabled])

  useEffect(() => {
    if (!article) {
      setTimelineEvents([])
      setTimelineScoringMetrics(null)
      setIsTimelineEnriching(false)
      return
    }
    let cancelled = false
    let timer: number | undefined
    const requestId = timelineRequestRef.current + 1
    timelineRequestRef.current = requestId
    const timelineTitle = article.title
    const loadTimeline = (showLoading: boolean) => {
      if (showLoading) setIsTimelineLoading(true)
      fetchTimeline(timelineTitle, true, insightsEnabled)
        .then((response) => {
          if (cancelled || timelineRequestRef.current !== requestId || response.title !== timelineTitle) return
          setTimelineEvents(response.events)
          setTimelineScoringMetrics(response.scoring_metrics ?? null)
          setIsTimelineEnriching(Boolean(response.enrichment_pending && insightsEnabled))
          if (response.enrichment_pending && insightsEnabled) {
            timer = window.setTimeout(() => loadTimeline(false), 7000)
          }
        })
        .catch(() => {
          if (!cancelled && timelineRequestRef.current === requestId) {
            setTimelineEvents([])
            setTimelineScoringMetrics(null)
            setIsTimelineEnriching(false)
          }
        })
        .finally(() => {
          if (!cancelled && timelineRequestRef.current === requestId && showLoading) setIsTimelineLoading(false)
        })
    }
    loadTimeline(true)
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [article, insightsEnabled])

  const loadArticleGraph = useCallback((title: string) => {
    const requestId = graphRequestRef.current + 1
    graphRequestRef.current = requestId
    setIsGraphLoading(true)
    setGraphError(null)
    setGraphActionLabel(null)
    fetchGraphologyArticle(title, 0, 1000)
      .then((response) => {
        if (graphRequestRef.current !== requestId || response.attributes.focus_title !== title) return
        setArticleGraph(response)
      })
      .catch((err: Error) => {
        if (graphRequestRef.current !== requestId) return
        setArticleGraph(null)
        setGraphActionLabel(null)
        setGraphError(err.message || 'Graph load failed')
      })
      .finally(() => {
        if (graphRequestRef.current === requestId) setIsGraphLoading(false)
      })
  }, [])

  useEffect(() => {
    if (!article) {
      setArticleGraph(null)
      setGraphError(null)
      return
    }
    loadArticleGraph(article.title)
  }, [article, relatedReloadToken, loadArticleGraph])

  const runGraphCoverageCrawler = useCallback(() => {
    if (!article || isGraphCrawling) return
    setIsGraphCrawling(true)
    setGraphActionLabel('Crawling graph coverage')
    setGraphError(null)
    crawlArticleGraph(article.title, 12, 4, false)
      .then(() => {
        loadArticleGraph(article.title)
        setRelatedReloadToken((token) => token + 1)
      })
      .catch((err: Error) => {
        setGraphError(err.message || 'Graph coverage crawl failed')
      })
      .finally(() => {
        setGraphActionLabel(null)
        setIsGraphCrawling(false)
      })
  }, [article, isGraphCrawling, loadArticleGraph])

  const lazyExpandGraph = useCallback((
    options: { sectionKey?: string; l1TitleId?: number | null; l1Title?: string | null },
  ) => {
    if (!article) return
    const requestId = graphRequestRef.current + 1
    graphRequestRef.current = requestId
    setIsGraphCrawling(true)
    setGraphActionLabel(
      options.sectionKey
        ? 'Loading L1 links for section'
        : options.l1Title
          ? `Loading L2 links for ${options.l1Title}`
          : 'Expanding graph links',
    )
    setGraphError(null)
    expandGraphologyArticle(article.title, options, 1000)
      .then((response) => {
        if (graphRequestRef.current !== requestId || response.attributes.focus_title !== article.title) return
        setArticleGraph(response)
      })
      .catch((err: Error) => {
        if (graphRequestRef.current !== requestId) return
        setGraphError(err.message || 'Graph expansion failed')
      })
      .finally(() => {
        if (graphRequestRef.current === requestId) {
          setGraphActionLabel(null)
          setIsGraphCrawling(false)
        }
      })
  }, [article])

  useEffect(() => {
    if (!article) {
      setArticleStatus(null)
      setSectionStatus(null)
      return
    }
    let cancelled = false
    let timeoutId: number | undefined
    const requestId = statusRequestRef.current + 1
    statusRequestRef.current = requestId
    const statusTitle = article.title
    const pollStatus = () => {
      Promise.all([
        fetchProcessingStatus(statusTitle),
        selectedSectionKey ? fetchProcessingStatus(statusTitle, selectedSectionKey) : Promise.resolve(null),
      ])
        .then(([articleResponse, sectionResponse]) => {
          if (cancelled || statusRequestRef.current !== requestId || articleResponse.title !== statusTitle) return
          setArticleStatus(articleResponse)
          setSectionStatus(sectionResponse)
          const active = [articleResponse, sectionResponse].some(
            (status) => status && !['completed', 'idle'].includes(status.overall_state),
          )
          timeoutId = window.setTimeout(pollStatus, active ? 2500 : 8000)
        })
        .catch(() => {
          if (!cancelled && statusRequestRef.current === requestId) timeoutId = window.setTimeout(pollStatus, 8000)
        })
    }
    pollStatus()
    return () => {
      cancelled = true
      if (timeoutId) window.clearTimeout(timeoutId)
    }
  }, [article, selectedSectionKey, relatedReloadToken, timelineEvents.length])

  const openArticle = useCallback((title: string) => {
    setArticleTitle(title)
    setSelectedSectionKey(null)
    setHistory((prev) => {
      const next = [...prev.slice(0, historyIndex + 1), title]
      setHistoryIndex(next.length - 1)
      return next
    })
  }, [historyIndex])

  const navigateHistory = useCallback((delta: number) => {
    setHistoryIndex((index) => {
      const next = Math.max(0, Math.min(history.length - 1, index + delta))
      const title = history[next]
      if (title) {
        setArticleTitle(title)
        setSelectedSectionKey(null)
      }
      return next
    })
  }, [history])

  const fullRefreshArticle = useCallback(() => {
    if (!article) return
    setIsLoading(true)
    setError(null)
    setTimelineEvents([])
    setIsTimelineEnriching(false)
    setArticleGraph(null)
    setGraphError(null)
    refreshArticle(article.title, insightsEnabled)
      .then((data) => {
        setArticle(data)
        setArticleGraph(null)
        setGraphError(null)
        setSelectedSectionKey((current) => {
          if (current && data.sections.some((section) => section.section_key === current)) return current
          return firstUsableSection(data)
        })
        setRelatedReloadToken((token) => token + 1)
        localStorage.setItem('timeline-title', data.title)
      })
      .catch((err: Error) => {
        setError(err.message || 'Failed to refresh article')
      })
      .finally(() => {
        setIsLoading(false)
      })
  }, [article, insightsEnabled])

  const toggleInsights = useCallback(() => {
    setInsightsEnabled((current) => {
      const next = !current
      localStorage.setItem('timeline-enable-insights', String(next))
      setTimelineEvents([])
      setTimelineScoringMetrics(null)
      setIsTimelineEnriching(false)
      setArticleGraph(null)
      setGraphError(null)
      setRelatedReloadToken((token) => token + 1)
      return next
    })
  }, [])

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark"><Clock3 size={20} /></div>
          <div>
            <h1>Timeline</h1>
            <span>cache-first article workspace</span>
          </div>
        </div>

        <div className="history-controls" aria-label="Article history">
          <button type="button" onClick={() => navigateHistory(-1)} disabled={historyIndex <= 0} title="Back">
            <ArrowLeft size={17} />
          </button>
          <button type="button" onClick={() => navigateHistory(1)} disabled={historyIndex >= history.length - 1} title="Forward">
            <ArrowRight size={17} />
          </button>
        </div>

        <ArticleSearch onSelect={openArticle} />

        <div className="view-tabs" role="tablist" aria-label="Views">
          <button
            type="button"
            className={viewMode === 'reader' ? 'active' : ''}
            onClick={() => setViewMode('reader')}
          >
            <BookOpen size={17} />
            <span>Reader</span>
          </button>
          <button
            type="button"
            className={viewMode === 'timeline' ? 'active' : ''}
            onClick={() => setViewMode('timeline')}
          >
            <CalendarDays size={17} />
            <span>Timeline</span>
          </button>
          <button
            type="button"
            className={viewMode === 'graph' ? 'active' : ''}
            onClick={() => setViewMode('graph')}
          >
            <Network size={17} />
            <span>Graph</span>
          </button>
        </div>

        <button
          type="button"
          className={`insights-toggle ${insightsEnabled ? 'enabled' : ''}`}
          onClick={toggleInsights}
          title={insightsEnabled ? 'Insights enabled' : 'Core mode: insights disabled'}
          aria-pressed={insightsEnabled}
        >
          <SlidersHorizontal size={16} />
          <span>{insightsEnabled ? 'Insights on' : 'Core mode'}</span>
        </button>
      </header>

      {error && (
        <div className="app-alert">
          <CircleAlert size={18} />
          <span>{error}</span>
        </div>
      )}

      <main className="workspace">
        {isLoading && !article ? (
          <div className="center-state">
            <LoaderCircle className="spin" size={28} />
            <span>Loading article</span>
          </div>
        ) : viewMode === 'reader' ? (
          <ReaderView
            article={article}
            selectedSectionKey={selectedSectionKey}
            onSelectSection={setSelectedSectionKey}
            onOpenArticle={openArticle}
            onReloadArticle={fullRefreshArticle}
            isReloading={isLoading && Boolean(article)}
            reloadToken={relatedReloadToken}
            selectedSection={selectedSection}
            articleStatus={articleStatus}
            sectionStatus={sectionStatus}
            insightsEnabled={insightsEnabled}
          />
        ) : viewMode === 'timeline' ? (
          <UnifiedTimelineView
            article={article}
            events={timelineEvents}
            scoringMetrics={timelineScoringMetrics}
            isLoading={isTimelineLoading}
            isEnriching={isTimelineEnriching}
            articleStatus={articleStatus}
            sectionStatus={sectionStatus}
            selectedSectionKey={selectedSectionKey}
            onSelectSection={setSelectedSectionKey}
            onOpenArticle={openArticle}
            insightsEnabled={insightsEnabled}
          />
        ) : (
          <GraphView
            article={article}
            selectedSection={selectedSection}
            selectedSectionKey={selectedSectionKey}
            articleGraph={articleGraph}
            isGraphLoading={isGraphLoading}
            isGraphCrawling={isGraphCrawling}
            graphActionLabel={graphActionLabel}
            graphError={graphError}
            onSelectSection={setSelectedSectionKey}
            onOpenArticle={openArticle}
            onCrawlGraph={runGraphCoverageCrawler}
            onLazyExpandGraph={lazyExpandGraph}
          />
        )}
      </main>
    </div>
  )
}

function ArticleSearch({ onSelect }: { onSelect: (title: string) => void }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [isLoading, setIsLoading] = useState(false)

  useEffect(() => {
    if (query.trim().length < 2) {
      setResults([])
      return
    }
    let cancelled = false
    const timeout = window.setTimeout(() => {
      setIsLoading(true)
      searchArticles(query.trim(), 8)
        .then((data) => { if (!cancelled) setResults(data) })
        .catch(() => { if (!cancelled) setResults([]) })
        .finally(() => { if (!cancelled) setIsLoading(false) })
    }, 260)
    return () => {
      cancelled = true
      window.clearTimeout(timeout)
    }
  }, [query])

  const choose = (title: string) => {
    onSelect(title)
    setQuery('')
    setResults([])
  }

  return (
    <div className="search-box">
      <Search className="search-icon" size={17} />
      <input
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && results[0]) choose(results[0].title)
        }}
        placeholder="Search article"
      />
      {isLoading && <LoaderCircle className="search-loading spin" size={16} />}
      {!!results.length && (
        <div className="search-results">
          {results.map((result) => (
            <button key={result.title_id} type="button" onClick={() => choose(result.title)}>
              <span>{result.title}</span>
              <small>{result.title_id}</small>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function ProcessingStatusStrip({
  status,
  compact = false,
  collapsible = false,
}: {
  status: ProcessingStatusResponse | null
  compact?: boolean
  collapsible?: boolean
}) {
  const [collapsed, setCollapsed] = useState(collapsible)
  if (!status) {
    return (
      <div className={`processing-status ${compact ? 'compact' : ''}`}>
        <div className="processing-overall state-idle">Status unavailable</div>
      </div>
    )
  }
  const orderedAreas = [...status.areas].sort((left, right) => statusPriority(left.state) - statusPriority(right.state))
  const metricChips = statusMetricChips(status.metrics)
  const isCollapsed = collapsible && collapsed
  return (
    <div className={`processing-status ${compact ? 'compact' : ''} ${isCollapsed ? 'collapsed' : ''}`}>
      <div className={`processing-overall state-${status.overall_state}`}>
        <span>{status.section_key ? 'Section status' : 'Article status'}</span>
        <strong>{statusLabel(status.overall_state)}</strong>
        {collapsible ? (
          <button
            type="button"
            className="processing-collapse-btn"
            onClick={() => setCollapsed((value) => !value)}
            title={collapsed ? 'Show article processing details' : 'Hide article processing details'}
          >
            {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          </button>
        ) : null}
      </div>
      {!isCollapsed && metricChips.length ? (
        <div className="processing-metrics">
          {metricChips.map((chip) => (
            <span key={chip.label}>
              <strong>{chip.label}</strong>
              {chip.value}
            </span>
          ))}
        </div>
      ) : null}
      {!isCollapsed ? <div className="processing-area-list">
        {orderedAreas.map((area) => (
          <div key={area.key} className={`processing-area state-${area.state}`}>
            <div className="processing-area-head">
              <span>{area.label}</span>
              <strong>
                {statusLabel(area.state)}{area.failed ? ` Â· ${area.failed} failed` : ''}
                {area.elapsed_seconds !== null && area.elapsed_seconds !== undefined ? (
                  <DurationBadge seconds={area.elapsed_seconds} />
                ) : null}
              </strong>
            </div>
            <div className="processing-area-body">
              <small title={area.last_error || area.reason || area.detail}>
                {area.detail || area.reason || `${area.completed}/${area.total || area.completed}`}
              </small>
              <small
                className="processing-audit"
                title={`source: ${area.source || 'derived'}; updated: ${area.updated_at || 'not recorded'}`}
              >
                {compactStatusSource(area.source)} - {formatStatusUpdatedAt(area.updated_at)}
              </small>
            </div>
            <strong>
              {statusLabel(area.state)}{area.failed ? ` Â· ${area.failed} failed` : ''}
              {area.elapsed_seconds !== null && area.elapsed_seconds !== undefined ? (
                <DurationBadge seconds={area.elapsed_seconds} />
              ) : null}
            </strong>
          </div>
        ))}
      </div> : null}
    </div>
  )
}

function statusMetricChips(metrics?: Record<string, unknown>): Array<{ label: string; value: string }> {
  if (!metrics) return []
  const chips: Array<{ label: string; value: string }> = []
  const usableCore = numericMetric(metrics.time_to_usable_core_seconds)
  const rich = numericMetric(metrics.time_to_rich_seconds)
  const deepAge = numericMetric(metrics.time_since_deep_start_seconds)
  const pending = numericMetric(metrics.deep_pending_jobs)
  const running = numericMetric(metrics.deep_running_jobs)
  if (usableCore !== null) chips.push({ label: 'Core', value: `${usableCore.toFixed(1)}s` })
  if (rich !== null) chips.push({ label: 'Rich', value: `${rich.toFixed(1)}s` })
  else if (deepAge !== null && (pending || running)) chips.push({ label: 'Deep age', value: `${deepAge.toFixed(0)}s` })
  if (pending || running) chips.push({ label: 'Queue', value: `${running ?? 0} run / ${pending ?? 0} queued` })
  return chips.slice(0, 3)
}

function numericMetric(value: unknown): number | null {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

function DurationBadge({ seconds }: { seconds: number }) {
  return (
    <span className="status-duration">
      {durationParts(seconds).map((part, index) => (
        <span key={`${part.text}:${index}`} className={part.kind === 'unit' ? 'duration-unit' : undefined}>
          {part.text}
        </span>
      ))}
    </span>
  )
}

function durationParts(seconds: number): Array<{ text: string; kind: 'value' | 'unit' | 'space' }> {
  if (!Number.isFinite(seconds)) return []
  if (seconds < 60) {
    return [
      { text: seconds.toFixed(seconds < 10 ? 1 : 0), kind: 'value' },
      { text: ' ', kind: 'space' },
      { text: 's', kind: 'unit' },
    ]
  }
  const minutes = Math.floor(seconds / 60)
  const remaining = Math.round(seconds % 60)
  if (minutes < 60) {
    return [
      { text: String(minutes), kind: 'value' },
      { text: ' ', kind: 'space' },
      { text: 'm', kind: 'unit' },
      { text: ' ', kind: 'space' },
      { text: String(remaining), kind: 'value' },
      { text: ' ', kind: 'space' },
      { text: 's', kind: 'unit' },
    ]
  }
  const hours = Math.floor(minutes / 60)
  return [
    { text: String(hours), kind: 'value' },
    { text: ' ', kind: 'space' },
    { text: 'h', kind: 'unit' },
    { text: ' ', kind: 'space' },
    { text: String(minutes % 60), kind: 'value' },
    { text: ' ', kind: 'space' },
    { text: 'm', kind: 'unit' },
  ]
}

function statusPriority(state: string): number {
  if (state === 'running') return 0
  if (state === 'pending') return 1
  if (state === 'incomplete' || state === 'partial' || state === 'stale') return 2
  if (state === 'attention') return 3
  if (state === 'completed') return 4
  if (state === 'disabled') return 5
  return 5
}

function statusLabel(state: string): string {
  if (state === 'running') return 'running'
  if (state === 'pending') return 'queued'
  if (state === 'incomplete' || state === 'partial') return 'incomplete'
  if (state === 'stale') return 'stale'
  if (state === 'completed') return 'completed'
  if (state === 'attention') return 'attention'
  if (state === 'disabled') return 'disabled'
  return 'idle'
}

function processingStatusActive(status: ProcessingStatusResponse | null): boolean {
  return Boolean(status && !['completed', 'idle'].includes(status.overall_state))
}

function ReaderView({
  article,
  selectedSectionKey,
  selectedSection,
  onSelectSection,
  onOpenArticle,
  onReloadArticle,
  isReloading,
  reloadToken,
  articleStatus,
  sectionStatus,
  insightsEnabled,
}: {
  article: ArticleResponse | null
  selectedSectionKey: string | null
  selectedSection: ArticleSection | null
  onSelectSection: (sectionKey: string) => void
  onOpenArticle: (title: string) => void
  onReloadArticle: () => void
  isReloading: boolean
  reloadToken: number
  articleStatus: ProcessingStatusResponse | null
  sectionStatus: ProcessingStatusResponse | null
  insightsEnabled: boolean
}) {
  return (
    <div className="reader-layout">
      <aside className="section-nav">
        <div className="pane-title">
          <span>Sections</span>
          <small>{article?.sections.length ?? 0}</small>
        </div>
        <div className="section-list">
          {article?.sections.map((section) => (
            <button
              key={section.section_key}
              type="button"
              className={section.section_key === selectedSectionKey ? 'active' : ''}
              style={{ paddingLeft: `${12 + Math.max(0, (section.level || 1) - 1) * 10}px` }}
              onClick={() => onSelectSection(section.section_key)}
            >
              <span>{sectionTitle(section)}</span>
              <small>{section.clean_text ? `${Math.round(section.clean_text.length / 100) / 10}k` : 'empty'}</small>
            </button>
          ))}
        </div>
      </aside>

      <article className="article-pane">
        {article ? (
          <>
            <div className="article-heading">
              <div>
                <h2>{article.title}</h2>
                <span>{article.sections.length} sections Â· title id {article.title_id}</span>
              </div>
              <div className="article-heading-actions">
                <button
                  type="button"
                  className="article-reload-btn"
                  onClick={onReloadArticle}
                  disabled={isReloading}
                  title="Full refresh: content, topics, connections, scoring, and timeline"
                >
                  <RefreshCw className={isReloading ? 'spin' : ''} size={15} />
                  <span>{isReloading ? 'Refreshing' : 'Full refresh'}</span>
                </button>
                <div className="cache-pill">{article.cached ? 'cached' : 'source'}</div>
              </div>
            </div>
            <ProcessingStatusStrip status={articleStatus} compact={false} collapsible />
            {article.core ? (
              <div className="article-core-card">
                <div>
                  <span className="article-core-label">Usable core</span>
                  <p>{article.core.summary}</p>
                </div>
                <div className="article-core-meta">
                  {(article.core.key_entities || []).slice(0, 5).map((entity, index) => (
                    <span key={`${String(entity.name || 'entity')}-${index}`}>
                      {String(entity.name || entity.label || 'Entity')}
                    </span>
                  ))}
                  {article.core.updated_at ? <small>{new Date(article.core.updated_at).toLocaleTimeString()}</small> : null}
                </div>
              </div>
            ) : null}
            <div className="article-sections">
              {article.sections.map((section) => (
                <section
                  key={section.section_key}
                  className={`article-section ${section.section_key === selectedSectionKey ? 'active' : ''}`}
                  onClick={() => onSelectSection(section.section_key)}
                >
                  <h3>{sectionTitle(section)}</h3>
                  {section.clean_text ? (
                    <div dangerouslySetInnerHTML={{ __html: section.content_html }} />
                  ) : (
                    <p className="muted">No section text cached.</p>
                  )}
                </section>
              ))}
            </div>
          </>
        ) : (
          <div className="center-state">Search for an article to begin.</div>
        )}
      </article>

      {insightsEnabled ? (
        <RelatedPanel
          section={selectedSection}
          onOpenArticle={onOpenArticle}
          reloadToken={reloadToken}
          sectionStatus={sectionStatus}
          insightsEnabled={insightsEnabled}
        />
      ) : (
        <CoreLinksPanel section={selectedSection} onOpenArticle={onOpenArticle} sectionStatus={sectionStatus} />
      )}
    </div>
  )
}

function CoreLinksPanel({
  section,
  onOpenArticle,
  sectionStatus,
}: {
  section: ArticleSection | null
  onOpenArticle: (title: string) => void
  sectionStatus: ProcessingStatusResponse | null
}) {
  const links = useMemo(() => uniqueLinks(section?.links ?? [], 80), [section])
  return (
    <aside className="related-pane">
      <div className="pane-title">
        <span>Core links</span>
        <Network size={16} />
      </div>
      {section ? (
        <>
          <ProcessingStatusStrip status={sectionStatus} compact />
          <div className="selected-card">
            <small>Selected section</small>
            <strong>{sectionTitle(section)}</strong>
            <p>{oneLineSectionSummary(section)}</p>
          </div>
          <div className="connected-box">
            <div className="related-header">
              <span>Direct L1 links</span>
              <small>{links.length}</small>
            </div>
            {!links.length ? <p className="muted">No direct article links in this section.</p> : null}
            {!!links.length && (
              <div className="connected-list core-link-list">
                {links.map((link) => (
                  <button key={`${link.target}:${link.char_start}`} type="button" onClick={() => onOpenArticle(link.target)}>
                    <span>{link.label || link.target}</span>
                    <small>{link.target}</small>
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="selected-card">
            <small>Core mode</small>
            <p>Insights, entity extraction, embeddings, temporal agents, and LLM jobs are disabled.</p>
          </div>
        </>
      ) : (
        <div className="empty-panel">Select a section.</div>
      )}
    </aside>
  )
}

function RelatedPanel({
  section,
  onOpenArticle,
  reloadToken,
  sectionStatus,
  insightsEnabled,
}: {
  section: ArticleSection | null
  onOpenArticle: (title: string) => void
  reloadToken: number
  sectionStatus: ProcessingStatusResponse | null
  insightsEnabled: boolean
}) {
  const [items, setItems] = useState<RelatedItem[]>([])
  const [scoringMetrics, setScoringMetrics] = useState<Record<string, unknown> | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [linkTab, setLinkTab] = useState<'l1' | 'l2'>('l1')
  const [selectedRelatedId, setSelectedRelatedId] = useState<number | null>(null)
  const [isRefreshingAgent, setIsRefreshingAgent] = useState(false)
  const relatedRequestRef = useRef(0)
  const sectionProcessingActive = processingStatusActive(sectionStatus)

  const l1Items = useMemo(() => items.filter((item) => item.level === 1), [items])
  const l2Items = useMemo(() => items.filter((item) => item.level === 2), [items])
  const activeLinkItems = linkTab === 'l1' ? l1Items : l2Items
  const selectedRelated =
    activeLinkItems.find((item) => item.to_title_id === selectedRelatedId) ?? activeLinkItems[0] ?? null
  const agentInsights = useMemo(
    () =>
      items
        .filter((item) => item.why_source === 'agent_related_v1' && item.model_version === 'agent-related-v7')
        .sort((a, b) => b.score - a.score)
        .slice(0, 6),
    [items],
  )
  const latestAgentUpdatedAt = useMemo(() => {
    const timestamps = agentInsights
      .map((item) => item.agent_updated_at)
      .filter((value): value is string => Boolean(value))
      .sort()
    return timestamps[timestamps.length - 1] ?? null
  }, [agentInsights])

  useEffect(() => {
    if (!section) {
      setItems([])
      setScoringMetrics(null)
      setSelectedRelatedId(null)
      return
    }
    let cancelled = false
    let timeoutId: number | undefined
    const requestId = relatedRequestRef.current + 1
    relatedRequestRef.current = requestId
    const sectionKey = section.section_key
    setItems([])
    setScoringMetrics(null)
    setSelectedRelatedId(null)
    setIsLoading(true)
    setError(null)

    const load = (attempt: number) => {
      fetchRelated(sectionKey, 40, reloadToken > 0 && attempt === 0, insightsEnabled)
        .then((response) => {
          if (cancelled || relatedRequestRef.current !== requestId || response.section_key !== sectionKey) return
          setItems(response.items)
          setScoringMetrics(response.scoring_metrics ?? null)
          const hasExplicitRefreshPending = response.items.some((item) => item.model_version === 'agent-refresh-pending')
          const shouldKeepPolling = sectionProcessingActive || hasExplicitRefreshPending
          if (shouldKeepPolling && attempt < 40) {
            timeoutId = window.setTimeout(() => load(attempt + 1), attempt < 2 ? 1800 : 3200)
          }
        })
        .catch((err: Error) => {
          if (!cancelled && relatedRequestRef.current === requestId) {
            setItems([])
            setScoringMetrics(null)
            setError(err.message || 'Related lookup failed')
          }
        })
        .finally(() => {
          if (!cancelled && relatedRequestRef.current === requestId) setIsLoading(false)
        })
    }

    load(0)
    return () => {
      cancelled = true
      if (timeoutId) window.clearTimeout(timeoutId)
    }
  }, [section, reloadToken, sectionProcessingActive])

  useEffect(() => {
    setSelectedRelatedId(activeLinkItems[0]?.to_title_id ?? null)
  }, [activeLinkItems, linkTab])

  const refreshAgentInsights = useCallback(() => {
    if (!section || isRefreshingAgent) return
    let cancelled = false
    const sectionKey = section.section_key
    const requestId = relatedRequestRef.current + 1
    relatedRequestRef.current = requestId
    setIsRefreshingAgent(true)
    setError(null)
    rerunRelatedAgent(sectionKey)
      .then(() => {
        if (cancelled || relatedRequestRef.current !== requestId) return
        setItems((current) =>
          current.map((item) =>
            item.why_source === 'agent_related_v1'
              ? { ...item, why_source: 'template', model_version: 'agent-refresh-pending', agent_updated_at: null }
              : item,
          ),
        )

        let attempts = 0
        const poll = () => {
          attempts += 1
          fetchRelated(sectionKey, 40)
            .then((response) => {
              if (cancelled || relatedRequestRef.current !== requestId || response.section_key !== sectionKey) return
              setItems(response.items)
              setScoringMetrics(response.scoring_metrics ?? null)
              const hasFresh = response.items.some((item) => item.model_version === 'agent-related-v7')
              if (!hasFresh && attempts < 20) {
                window.setTimeout(poll, attempts < 4 ? 1500 : 3000)
              } else {
                setIsRefreshingAgent(false)
              }
            })
            .catch((err: Error) => {
              if (!cancelled && relatedRequestRef.current === requestId) {
                setError(err.message || 'Agent refresh failed')
                setIsRefreshingAgent(false)
              }
            })
        }
        window.setTimeout(poll, 1200)
      })
      .catch((err: Error) => {
        if (!cancelled && relatedRequestRef.current === requestId) {
          setError(err.message || 'Agent refresh failed')
          setIsRefreshingAgent(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [isRefreshingAgent, section])

  return (
    <aside className="related-pane">
      <div className="pane-title">
        <span>Related</span>
        <Network size={16} />
      </div>
      {section ? (
        <>
          <ProcessingStatusStrip status={sectionStatus} compact />

          <div className="selected-card">
            <small>Selected section</small>
            <strong>{sectionTitle(section)}</strong>
            <p>{oneLineSectionSummary(section)}</p>
          </div>

          <div className="connected-box">
            <div className="related-header">
              <span>Connected articles</span>
              {isLoading && <LoaderCircle className="spin" size={15} />}
            </div>
            <ScoringMetricStrip metrics={scoringMetrics} compact />
            <div className="connected-tabs" role="tablist" aria-label="Connected article depth">
              <button type="button" className={linkTab === 'l1' ? 'active' : ''} onClick={() => setLinkTab('l1')}>
                L1 links
              </button>
              <button type="button" className={linkTab === 'l2' ? 'active' : ''} onClick={() => setLinkTab('l2')}>
                L2 links
              </button>
            </div>
            {error && <p className="muted">{error}</p>}
            {!isLoading && !activeLinkItems.length && !error && <p className="muted">No connected articles cached yet.</p>}
            {!!activeLinkItems.length && (
              <>
                <div className="connected-list">
                  {activeLinkItems.map((item) => (
                    <button
                      key={`${item.to_title_id}:${item.level}`}
                      type="button"
                      className={item.to_title_id === selectedRelated?.to_title_id ? 'active' : ''}
                      onClick={() => setSelectedRelatedId(item.to_title_id)}
                    >
                      <span>{item.to_title}</span>
                      <small>Relevance {item.score.toFixed(2)}</small>
                    </button>
                  ))}
                </div>
                {selectedRelated && (
                  <div className="connected-preview">
                    <strong>{selectedRelated.to_title}</strong>
                  <p>{selectedRelated.summary || 'No cached summary is available yet.'}</p>
                    <WhySignalStrip why={selectedRelated.why} />
                    <button type="button" onClick={() => onOpenArticle(selectedRelated.to_title)}>
                      Open article
                    </button>
                  </div>
                )}
              </>
            )}
          </div>

          <div className="related-list">
            <div className="related-header">
              <span>Agent insights</span>
              <button
                type="button"
                className="agent-refresh-btn"
                onClick={refreshAgentInsights}
                disabled={isRefreshingAgent}
              >
                {isRefreshingAgent && <LoaderCircle className="spin" size={13} />}
                <span>{isRefreshingAgent ? 'Refreshing' : 'Re-run'}</span>
              </button>
            </div>
            {latestAgentUpdatedAt && (
              <div className="agent-timestamp">
                Generated {new Date(latestAgentUpdatedAt).toLocaleString()}
              </div>
            )}
            {error && <p className="muted">{error}</p>}
            {!isLoading && !agentInsights.length && !error && <p className="muted">No agent insights yet.</p>}
            {agentInsights.map((item) => (
              <div key={`insight:${item.to_title_id}:${item.level}`} className="insight-card">
                <span>{item.to_title}</span>
                <small className="related-meta">
                  <span>L{item.level} Â· {item.score.toFixed(2)}</span>
                  <strong className={item.why_source === 'agent_related_v1' && item.model_version === 'agent-related-v7' ? 'agent-tag' : 'cache-tag'}>
                    {item.why_source === 'agent_related_v1' && item.model_version === 'agent-related-v7' ? 'Agent Powered' : 'Cache'}
                  </strong>
                </small>
                <div className={item.why_source === 'agent_related_v1' && item.model_version === 'agent-related-v7' ? 'agent-insight' : 'cache-insight'}>
                  <em>{item.why_source === 'agent_related_v1' && item.model_version === 'agent-related-v7' ? 'Agent insight' : 'Cache signal'}</em>
                  <p>{cleanAgentInsight(item.why_text)}</p>
                </div>
                <WhySignalStrip why={item.why} />
              </div>
            ))}
          </div>
        </>
      ) : (
        <div className="empty-panel">Select a section.</div>
      )}
    </aside>
  )
}

function GraphView({
  article,
  selectedSection,
  selectedSectionKey,
  articleGraph,
  isGraphLoading,
  isGraphCrawling,
  graphActionLabel,
  graphError,
  onSelectSection,
  onOpenArticle,
  onCrawlGraph,
  onLazyExpandGraph,
}: {
  article: ArticleResponse | null
  selectedSection: ArticleSection | null
  selectedSectionKey: string | null
  articleGraph: GraphologyGraphResponse | null
  isGraphLoading: boolean
  isGraphCrawling: boolean
  graphActionLabel: string | null
  graphError: string | null
  onSelectSection: (sectionKey: string) => void
  onOpenArticle: (title: string) => void
  onCrawlGraph: () => void
  onLazyExpandGraph: (options: { sectionKey?: string; l1TitleId?: number | null; l1Title?: string | null }) => void
}) {
  if (!article) {
    return <div className="center-state">Search for an article to build a graph.</div>
  }
  return (
    <div className="graph-view-layout">
      <aside className="graph-view-sidebar">
        <div className="pane-title">
          <span>Graph Sections</span>
          <small>{article.sections.length}</small>
        </div>
        <h2>{article.title}</h2>
        <p>Article spine with section-level L1/L2 connections.</p>
        <div className="graph-view-actions">
          <button type="button" onClick={onCrawlGraph} disabled={isGraphCrawling}>
            {isGraphCrawling ? 'Crawling coverage' : 'Cover graph'}
          </button>
        </div>
        <div className="section-list graph-section-list">
          {article.sections.map((section) => (
            <button
              key={section.section_key}
              type="button"
              className={section.section_key === selectedSectionKey ? 'active' : ''}
              style={{ paddingLeft: `${12 + Math.max(0, (section.level || 1) - 1) * 10}px` }}
              onClick={() => onSelectSection(section.section_key)}
            >
              <span>{sectionTitle(section)}</span>
              <small>{section.heading_id}</small>
            </button>
          ))}
        </div>
      </aside>
      <section className="graph-view-main">
        <CytoscapeGraphView
          articleTitle={article.title}
          payload={articleGraph}
          selectedSectionKey={selectedSection?.section_key ?? selectedSectionKey}
          onOpenArticle={onOpenArticle}
          onSelectSection={onSelectSection}
          isLoading={isGraphLoading}
          isCrawling={isGraphCrawling}
          actionLabel={graphActionLabel}
          error={graphError}
          onCrawlGraph={onCrawlGraph}
          onLazyExpandGraph={onLazyExpandGraph}
        />
      </section>
    </div>
  )
}

function ScoringMetricStrip({ metrics, compact = false }: { metrics: Record<string, unknown> | null; compact?: boolean }) {
  if (!metrics) return null
  const levelCounts = compactCounts(metrics.level_counts, 3)
  const componentCounts = compactCounts(metrics.top_component_distribution, 3)
  return (
    <div className={`score-metric-strip ${compact ? 'compact' : ''}`}>
      <span>Items {metricNumber(metrics.count)}</span>
      <span>Spread {metricNumber(metrics.spread)}</span>
      <span>Ties {metricNumber(metrics.tie_rate)}</span>
      {typeof metrics.embedding_available === 'number' && <span>Emb {metricNumber(metrics.embedding_available)}</span>}
      {levelCounts.length ? <span>{levelCounts.map((entry) => `L${entry.key}:${entry.value}`).join(' ')}</span> : null}
      {componentCounts.length ? <span>{componentCounts.map((entry) => `${entry.key}:${entry.value}`).join(' ')}</span> : null}
    </div>
  )
}

function WhySignalStrip({ why }: { why?: Record<string, unknown> }) {
  const safeWhy = asRecord(why)
  const components = scoreComponents(safeWhy)
  const reasons = whyReasons(safeWhy)
  if (!components.length && !reasons.length) return null
  return (
    <div className="why-signal-strip">
      {components.map((component) => (
        <span key={component.key}>
          {component.key.replace(/_/g, ' ')} {component.value.toFixed(2)}
        </span>
      ))}
      {reasons.map((reason) => (
        <span key={reason} className="why-reason">
          {reason.replace(/_/g, ' ')}
        </span>
      ))}
    </div>
  )
}

const DOMAIN_LANES = [
  'Politics & Government',
  'War & Military',
  'Science & Discovery',
  'Technology & Engineering',
  'Economy & Finance',
  'Arts & Culture',
  'Religion & Philosophy',
  'Society & People',
  'Law & Treaties',
  'Nature, Environment & Climate',
  'Health & Medicine',
  'Exploration & Geography',
  'Media & Portrayals',
]

function UnifiedTimelineView({
  article,
  events,
  scoringMetrics,
  isLoading,
  isEnriching,
  articleStatus,
  sectionStatus,
  selectedSectionKey,
  onSelectSection,
  onOpenArticle,
  insightsEnabled,
}: {
  article: ArticleResponse | null
  events: TimelineEvent[]
  scoringMetrics: Record<string, unknown> | null
  isLoading: boolean
  isEnriching: boolean
  articleStatus: ProcessingStatusResponse | null
  sectionStatus: ProcessingStatusResponse | null
  selectedSectionKey: string | null
  onSelectSection: (sectionKey: string) => void
  onOpenArticle: (title: string) => void
  insightsEnabled: boolean
}) {
  const [enabledDomains, setEnabledDomains] = useState<Set<string>>(() => new Set(DOMAIN_LANES))
  const [enabledLevels, setEnabledLevels] = useState<Set<number>>(() => new Set([0, 1, 2]))
  const [volumeThreshold, setVolumeThreshold] = useState(35)
  const [timelineExplanations, setTimelineExplanations] = useState<Record<string, TimelineExplainResponse>>({})
  const [timelineExplainLoading, setTimelineExplainLoading] = useState<string | null>(null)
  const [timelineExplainError, setTimelineExplainError] = useState<string | null>(null)
  const selectedSection = article?.sections.find((section) => section.section_key === selectedSectionKey) ?? null
  const articleCore = article?.core ?? null
  const coreTopic = typeof articleCore?.topic?.primary === 'string' ? articleCore.topic.primary : null
  const coreDomains = Array.isArray(articleCore?.topic?.domains)
    ? articleCore.topic.domains.map((domain) => String(domain)).slice(0, 4)
    : []

  const availableDomains = useMemo(() => {
    const seen = new Set(DOMAIN_LANES)
    for (const event of events) seen.add(event.domain_lane || 'Society & People')
    return Array.from(seen)
  }, [events])

  const activeEvents = useMemo(() => {
    const contextThreshold = volumeThreshold / 100
    return events.filter((event) => {
      const level = event.level ?? 0
      const domain = event.domain_lane || 'Society & People'
      if (!enabledLevels.has(level)) return false
      if (!enabledDomains.has(domain)) return false
      if (level > 0 && (event.relevance_score ?? 0) < contextThreshold) return false
      return true
    })
  }, [enabledDomains, enabledLevels, events, volumeThreshold])

  const firstYear = events.find((event) => event.year)?.year
  const lastYear = [...events].reverse().find((event) => event.year)?.year
  const yearSpan = firstYear && lastYear ? `${firstYear} - ${lastYear}` : 'No dates'

  const strengthClass = (event: TimelineEvent): string => {
    const score = event.relevance_score ?? 0
    if ((event.level ?? 0) <= 0) return 'strength-core'
    if (score >= 0.72) return 'strength-high'
    if (score >= 0.52) return 'strength-medium'
    return 'strength-low'
  }

  const timelineAttributionLabel = (event: TimelineEvent): string => {
    const status = String(event.attribution?.status || '')
    if (status === 'focus_core') return 'Focus core'
    if (status === 'section_attributed_unreviewed') return 'Section context'
    if (status) return status.replace(/_/g, ' ')
    return (event.level ?? 0) > 0 ? 'Section context' : 'Focus core'
  }

  const toggleDomain = (domain: string) => {
    setEnabledDomains((current) => {
      const next = new Set(current)
      if (next.has(domain)) next.delete(domain)
      else next.add(domain)
      return next
    })
  }

  const toggleLevel = (level: number) => {
    setEnabledLevels((current) => {
      const next = new Set(current)
      if (next.has(level)) next.delete(level)
      else next.add(level)
      return next
    })
  }

  const explainEvent = async (event: TimelineEvent) => {
    setTimelineExplainLoading(event.id)
    setTimelineExplainError(null)
    try {
      const response = await explainTimelineEvent(event)
      setTimelineExplanations((current) => ({ ...current, [event.id]: response }))
    } catch (error) {
      setTimelineExplainError(error instanceof Error ? error.message : 'Timeline explanation failed.')
    } finally {
      setTimelineExplainLoading(null)
    }
  }

  return (
    <div className="timeline-layout unified">
      <aside className="timeline-context">
        <div className="pane-title">
          <span>Unified Timeline</span>
          <small>{article?.title_id ?? ''}</small>
        </div>
        {article ? (
          <>
            <h2>{article.title}</h2>
            <ScoringMetricStrip metrics={scoringMetrics} />
            <p>
              {article.sections.length} sections Â· {insightsEnabled ? `${events.length} cached events Â· ${yearSpan}` : 'Core mode'}
            </p>

            {!insightsEnabled ? (
              <div className="timeline-core-card">
                <span>CPU timeline</span>
                <strong>LLM context disabled</strong>
                <p>Showing deterministic cached dates from the article. L1/L2 agent context and explanations are disabled.</p>
              </div>
            ) : null}

            {insightsEnabled && articleCore ? (
              <div className="timeline-core-card">
                <span>Usable core</span>
                <strong>{coreTopic || article.title}</strong>
                <p>{articleCore.summary}</p>
                <div className="timeline-core-tags">
                  {coreDomains.map((domain) => (
                    <small key={domain}>{domain}</small>
                  ))}
                  {(articleCore.key_entities || []).slice(0, 5).map((entity, index) => (
                    <small key={`${String(entity.name || entity.label || 'entity')}-${index}`}>
                      {String(entity.name || entity.label || 'Entity')}
                    </small>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="timeline-control-block">
              <span>Levels</span>
              <div className="timeline-level-controls">
                {[
                  [0, 'Main L0'],
                  [1, 'L1'],
                  [2, 'L2'],
                ].map(([level, label]) => (
                  <button
                    key={String(level)}
                    type="button"
                    className={enabledLevels.has(Number(level)) ? 'active' : ''}
                    onClick={() => toggleLevel(Number(level))}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            <div className="timeline-control-block">
              <span>Context volume</span>
              <input
                type="range"
                min="0"
                max="100"
                value={volumeThreshold}
                onChange={(event) => setVolumeThreshold(Number(event.target.value))}
              />
              <small>Context threshold {(volumeThreshold / 100).toFixed(2)}</small>
            </div>

            <div className="timeline-control-block">
              <span>Topic tags</span>
              <div className="timeline-domain-list">
                {availableDomains.map((domain) => (
                  <button
                    key={domain}
                    type="button"
                    className={enabledDomains.has(domain) ? 'active' : ''}
                    onClick={() => toggleDomain(domain)}
                  >
                    {domain}
                  </button>
                ))}
              </div>
            </div>

            <div className="timeline-section-picker">
              <span>Reader section sync</span>
              {article.sections.map((section) => (
                <button
                  key={section.section_key}
                  type="button"
                  className={section.section_key === selectedSectionKey ? 'active' : ''}
                  onClick={() => onSelectSection(section.section_key)}
                >
                  <span>{sectionTitle(section)}</span>
                  <ChevronRight size={14} />
                </button>
              ))}
            </div>
          </>
        ) : (
          <p className="muted">Search for an article to build a timeline.</p>
        )}
      </aside>

      <section className="timeline-main">
        <div className="timeline-toolbar">
          <div>
            <h2>{article?.title || 'Timeline'}</h2>
            <span>
              {isLoading ? 'Loading backend timeline...' : `${activeEvents.length} visible events in one continuous timeline`}
              {isEnriching ? ' Â· enriching L1/L2 context' : ''}
            </span>
          </div>
        </div>
        <ProcessingStatusStrip status={articleStatus} collapsible />

        <div className="timeline-track unified">
          {isLoading ? (
            <div className="center-state"><LoaderCircle className="spin" size={24} />Loading timeline</div>
          ) : activeEvents.length ? activeEvents.map((event) => (
            <div
              key={event.id}
              className={`timeline-event level-${event.level ?? 0} track-${event.track || 'core'} ${strengthClass(event)} ${event.section_key === selectedSectionKey ? 'active' : ''}`}
            >
              <div className="event-date">
                <strong>{event.label}</strong>
                <small>{event.precision || event.time_kind}</small>
              </div>
              <button
                type="button"
                className="event-body"
                onClick={() => onSelectSection(event.section_key)}
              >
                <div className="event-card-header">
                  <span>{event.heading || 'Introduction'}</span>
                  <small>L{event.level ?? 0} Â· {event.track || 'core'} Â· {event.source}</small>
                </div>
                <p>{event.excerpt}</p>
                <div className="event-tags">
                  <span>{event.domain_lane || 'Society & People'}</span>
                  <span>{event.source_title || article?.title} â†’ {event.source_heading || event.heading || 'Introduction'}</span>
                </div>
              </button>
              <aside className="event-meta-card" aria-label="Timeline event metadata">
                <div className="event-meta-topline">
                  <span>{event.source_title || article?.title}</span>
                  <strong>{event.source_heading || event.heading || 'Introduction'}</strong>
                </div>
                <dl>
                  <div>
                    <dt>Source</dt>
                    <dd>{event.source_title || article?.title}</dd>
                  </div>
                  <div>
                    <dt>Section</dt>
                    <dd>{event.source_heading || event.heading || 'Introduction'}</dd>
                  </div>
                  <div>
                    <dt>Topic</dt>
                    <dd>{event.domain_lane || 'Society & People'}</dd>
                  </div>
                  <div>
                    <dt>Score</dt>
                    <dd>{Math.round((event.relevance_score ?? 1) * 100)}%</dd>
                  </div>
                  <div>
                    <dt>Signals</dt>
                    <dd>L{event.level ?? 0} Â· {event.track || 'core'} Â· {event.source}</dd>
                  </div>
                  <div>
                    <dt>Confidence</dt>
                    <dd>{(event.confidence ?? 0).toFixed(2)}</dd>
                  </div>
                  <div>
                    <dt>Attribution</dt>
                    <dd>{timelineAttributionLabel(event)}</dd>
                  </div>
                </dl>
                <WhySignalStrip why={event.why} />
                {insightsEnabled ? (
                  <button
                    type="button"
                    className="event-explain-button"
                    onClick={() => explainEvent(event)}
                    disabled={timelineExplainLoading === event.id}
                  >
                    {timelineExplainLoading === event.id ? (
                      <><LoaderCircle className="spin" size={14} />Explaining</>
                    ) : (
                      'Explain this'
                    )}
                  </button>
                ) : null}
                {timelineExplanations[event.id] ? (
                  <div className="event-agent-note">
                    <span>Agent temporal context</span>
                    <p>{timelineExplanations[event.id].why_text}</p>
                    <small>
                      {timelineExplanations[event.id].why_source} Â· {new Date(timelineExplanations[event.id].generated_at).toLocaleTimeString()}
                    </small>
                  </div>
                ) : null}
              </aside>
            </div>
          )) : (
            <div className="center-state">No timeline events match the current filters.</div>
          )}
          {timelineExplainError ? <div className="timeline-inline-error">{timelineExplainError}</div> : null}
        </div>
      </section>

      <aside className="timeline-detail">
        <div className="pane-title">
          <span>Selected Source</span>
          <CalendarDays size={16} />
        </div>
        {selectedSection ? (
          <>
            <h3>{sectionTitle(selectedSection)}</h3>
            <p>{trimText(selectedSection.clean_text, 360)}</p>
            <div className="link-block">
              <span>Open linked article</span>
              {uniqueLinks(selectedSection.links, 10).map((link) => (
                <button key={`tl:${link.target}`} type="button" onClick={() => onOpenArticle(link.target)}>
                  {link.label || link.target}
                </button>
              ))}
            </div>
          </>
        ) : (
          <div className="empty-panel">Select a timeline event or reader section.</div>
        )}
      </aside>
    </div>
  )
}
