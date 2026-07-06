import { useEffect, useMemo, useRef, useState } from 'react'
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape'
import Graph from 'graphology'
import { Maximize2, Minimize2 } from 'lucide-react'
import { fetchArticle, type GraphologyEdgePayload, type GraphologyGraphResponse, type GraphologyNodePayload } from '../api'

interface CytoscapeGraphViewProps {
  payload: GraphologyGraphResponse | null
  articleTitle: string
  selectedSectionKey: string | null
  isLoading?: boolean
  isCrawling?: boolean
  actionLabel?: string | null
  error?: string | null
  onSelectSection: (sectionKey: string) => void
  onOpenArticle: (title: string) => void
  onCrawlGraph: () => void
  onLazyExpandGraph: (options: { sectionKey?: string; l1TitleId?: number | null; l1Title?: string | null }) => void
}

type CanvasMode = 'core' | 'promoted'

interface GraphConnection {
  edge: GraphologyEdgePayload
  node: GraphologyNodePayload
  sourceId: string
  sourceIds: string[]
  targetId: string
  level: number
  score: number
  strength: string
  visibility: string
  processingState: string
  linkState: string
  linkCompleted: number
  linkExpected: number
  title: string
  titleId: number | null
  domain: string
  viaTitle: string
  viaSourceId: string
  viaSourceIds: string[]
  routeCount: number
  entityPreview: NodePreview
  timelinePreview: NodePreview
}

interface SectionRow {
  key: string
  sectionKey: string
  title: string
  order: number
  processingState: string
  linkState: string
  linkCompleted: number
  linkExpected: number
  connectionCount: number
  l1Count: number
  l2Count: number
  entityPreview: NodePreview
  timelinePreview: NodePreview
}

interface BrowserState {
  section: SectionRow | null
  l1: GraphConnection | null
  l2: GraphConnection | null
}

interface NodePreview {
  count: number
  items: Array<Record<string, unknown>>
  truncated: number
}

interface RouteAnchor {
  source: string
  target: string
  level: number
  angle: number
  score: number
  weight: number
  visibility: string
}

interface NodeLayoutPlan {
  position: { x: number; y: number }
  level: number
  score: number
  routeCount: number
}

interface PlannedRouteTarget {
  key: string
  angle: number
  level: number
  score: number
  routes: RouteAnchor[]
  band: string
}

const DOMAIN_COLORS: Record<string, string> = {
  'Politics & Government': '#C0392B',
  'War & Military': '#7F8C8D',
  'Science & Discovery': '#2980B9',
  'Technology & Engineering': '#16A085',
  'Economy & Finance': '#27AE60',
  'Arts & Culture': '#8E44AD',
  'Religion & Philosophy': '#D4AC0D',
  'Society & People': '#E67E22',
  'Law & Treaties': '#2C3E50',
  'Nature, Environment & Climate': '#1ABC9C',
  'Health & Medicine': '#E74C3C',
  'Exploration & Geography': '#3498DB',
  'Media & Portrayals': '#95A5A6',
}

const RELATION_COLORS: Record<string, string> = {
  has_section: '#CBD5E1',
  wikilink: '#64748B',
  shared_entity: '#7C3AED',
  shared_tag: '#8B5CF6',
  temporal_overlap: '#0F766E',
  embedding_sim: '#94A3B8',
  composite: '#475569',
  l1_to_l2: '#0F766E',
}

const COVERAGE_COLORS: Record<string, string> = {
  missing: '#9AA0A6',
  incomplete: '#64748B',
  running: '#3B82F6',
  done: '#22C55E',
  stale: '#F59E0B',
  failed: '#EF4444',
}

const GRAPH_LAYOUT_RADII = {
  l0: 170,
  bandMultiplier: 1.2,
}

const GRAPH_SCORE_BAND_COUNT = 4

function attrString(attributes: Record<string, unknown>, key: string, fallback = ''): string {
  const value = attributes[key]
  return typeof value === 'string' ? value : fallback
}

function attrNumber(attributes: Record<string, unknown>, key: string, fallback = 0): number {
  const value = attributes[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function attrBool(attributes: Record<string, unknown>, key: string, fallback = false): boolean {
  const value = attributes[key]
  return typeof value === 'boolean' ? value : fallback
}

function coverageState(attributes: Record<string, unknown>): string {
  const explicit = attrString(attributes, 'processing_state')
  if (explicit) return explicit
  const coverage = attributes.coverage
  if (!coverage || typeof coverage !== 'object') return 'missing'
  const states = Object.values(coverage as Record<string, { state?: unknown }>).map((entry) => String(entry?.state || 'missing'))
  if (states.length === 0 || states.every((state) => state === 'missing')) return 'missing'
  if (states.includes('failed')) return 'failed'
  if (states.includes('running')) return 'running'
  if (states.includes('stale')) return 'stale'
  if (states.every((state) => state === 'done')) return 'done'
  return 'incomplete'
}

function coverageSummary(attributes: Record<string, unknown>): string {
  const coverage = attributes.coverage
  if (!coverage || typeof coverage !== 'object') return 'No coverage recorded'
  return Object.entries(coverage as Record<string, { state?: unknown; version?: unknown }>)
    .map(([layer, state]) => `${layer}: ${String(state?.state || 'missing')}`)
    .join(' | ')
}

function linkExtraction(attributes: Record<string, unknown>): { state: string; completed: number; expected: number } {
  const value = attributes.link_extraction
  if (!value || typeof value !== 'object') {
    return {
      state: attrString(attributes, 'link_extraction_state', 'missing'),
      completed: 0,
      expected: 0,
    }
  }
  const record = value as Record<string, unknown>
  return {
    state: typeof record.state === 'string' ? record.state : 'missing',
    completed: typeof record.completed === 'number' ? record.completed : Number(record.completed || 0),
    expected: typeof record.expected === 'number' ? record.expected : Number(record.expected || 0),
  }
}

function nodePreview(attributes: Record<string, unknown>, key: 'entity_preview' | 'timeline_preview'): NodePreview {
  const value = attributes[key]
  if (!value || typeof value !== 'object') return { count: 0, items: [], truncated: 0 }
  const record = value as Record<string, unknown>
  const items = Array.isArray(record.items)
    ? record.items.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    : []
  return {
    count: typeof record.count === 'number' ? record.count : items.length,
    items,
    truncated: typeof record.truncated === 'number' ? record.truncated : 0,
  }
}

function previewText(item: Record<string, unknown>, kind: 'entity' | 'time'): string {
  if (kind === 'entity') return String(item.surface || item.label || item.entity_id || '').trim()
  return String(item.label || item.time_ref_id || '').trim()
}

function previewMeta(item: Record<string, unknown>, kind: 'entity' | 'time'): string {
  if (kind === 'entity') return String(item.type || item.domain || item.source || '').trim()
  return String(item.precision || item.kind || item.source || '').trim()
}

function nodePreviewItems(data: Record<string, unknown>, key: 'entity_preview' | 'timeline_preview'): NodePreview {
  const value = data[key]
  if (!value || typeof value !== 'object') return { count: 0, items: [], truncated: 0 }
  const record = value as Record<string, unknown>
  const items = Array.isArray(record.items)
    ? record.items.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    : []
  return {
    count: typeof record.count === 'number' ? record.count : items.length,
    items,
    truncated: typeof record.truncated === 'number' ? record.truncated : 0,
  }
}

function selectedNodeSummary(data: Record<string, unknown>): string {
  const kind = String(data.kind || 'node')
  const level = typeof data.level === 'number' ? data.level : 0
  const state = String(data.processing_state || data.coverage_state || 'missing')
  const linkState = String(data.link_extraction_state || 'missing')
  const domain = String(data.domain || 'Society & People')
  const relevance = typeof data.relevance === 'number' ? data.relevance : typeof data.score === 'number' ? data.score : 0
  const routeCount = typeof data.route_count === 'number' ? data.route_count : 0
  const entityPreview = nodePreviewItems(data, 'entity_preview')
  const timelinePreview = nodePreviewItems(data, 'timeline_preview')

  if (kind === 'section') {
    return `Article section with ${entityPreview.count} extracted entit${entityPreview.count === 1 ? 'y' : 'ies'} and ${timelinePreview.count} timeline item${timelinePreview.count === 1 ? '' : 's'}. Link extraction is ${linkState}; processing is ${state}.`
  }
  if (kind === 'article' && level > 0) {
    return `L${level} viewpoint article in ${domain}. Relevance is ${Math.round(relevance * 100)}% with ${routeCount} visible route${routeCount === 1 ? '' : 's'} back to the focused article. Processing is ${state}.`
  }
  if (kind === 'article') {
    return `Focused article node. Its surrounding section nodes anchor the graph, while L1/L2 article levels are computed from this focus.`
  }
  return `Graph node with processing state ${state}.`
}

function selectedNodeContentKey(data: Record<string, unknown> | null): string {
  if (!data) return ''
  return String(data.id || data.section_key || data.title_id || data.full_label || data.label || '')
}

function selectedArticleTitle(data: Record<string, unknown> | null, focusTitle: string): string {
  if (!data) return focusTitle
  const kind = String(data.kind || '')
  if (kind === 'section') return focusTitle
  return String(data.title || data.full_label || data.label || focusTitle)
}

function compactIntroText(text: string, maxChars = 1250): string {
  const normalized = text.split(/\s+/).filter(Boolean).join(' ')
  if (normalized.length <= maxChars) return normalized
  const clipped = normalized.slice(0, maxChars).replace(/\s+\S*$/, '').replace(/[,:;]$/, '')
  return `${clipped}...`
}

function importGraphology(payload: GraphologyGraphResponse | null): Graph {
  const graph = new Graph({ multi: true, type: 'mixed', allowSelfLoops: false })
  if (!payload) return graph
  graph.import({ attributes: payload.attributes, nodes: payload.nodes, edges: payload.edges })
  return graph
}

function radialPoint(center: { x: number; y: number }, radius: number, angle: number): { x: number; y: number } {
  return { x: center.x + Math.cos(angle) * radius, y: center.y + Math.sin(angle) * radius }
}

function shouldShowNode(attributes: Record<string, unknown>, mode: CanvasMode): boolean {
  const visibility = attrString(attributes, 'visibility', 'hidden')
  if (visibility === 'core') return true
  return mode === 'promoted' && visibility === 'promoted'
}

function routeScore(attributes: Record<string, unknown>): number {
  return Math.max(
    attrNumber(attributes, 'score', 0),
    attrNumber(attributes, 'weight', 0),
    attrNumber(attributes, 'relevance_to_focus', 0),
  )
}

function routeLevel(edgeAttributes: Record<string, unknown>, targetAttributes: Record<string, unknown>): number {
  const edgeLevel = attrNumber(edgeAttributes, 'level', 0)
  if (edgeLevel > 0) return edgeLevel
  return Math.max(1, attrNumber(targetAttributes, 'level', 1))
}

function circularMean(routes: RouteAnchor[]): number {
  if (!routes.length) return -Math.PI / 2
  let x = 0
  let y = 0
  routes.forEach((route) => {
    x += Math.cos(route.angle) * route.weight
    y += Math.sin(route.angle) * route.weight
  })
  return Math.atan2(y, x)
}

function radialBandRadius(level: number, score: number): number {
  const l1FirstBand = GRAPH_LAYOUT_RADII.l0 * GRAPH_LAYOUT_RADII.bandMultiplier
  const l1LastBand = l1FirstBand * Math.pow(GRAPH_LAYOUT_RADII.bandMultiplier, GRAPH_SCORE_BAND_COUNT - 1)
  const l2FirstBand = l1LastBand * GRAPH_LAYOUT_RADII.bandMultiplier
  const firstBand = level === 1 ? l1FirstBand : l2FirstBand
  let bandIndex = 3
  if (score >= 0.72) bandIndex = 0
  else if (score >= 0.62) bandIndex = 1
  else if (score >= 0.52) bandIndex = 2
  return firstBand * Math.pow(GRAPH_LAYOUT_RADII.bandMultiplier, bandIndex)
}

function radialBandKey(level: number, score: number): string {
  if (score >= 0.72) return `${level}:high`
  if (score >= 0.62) return `${level}:strong`
  if (score >= 0.52) return `${level}:medium`
  return `${level}:low`
}

function angularDistance(left: number, right: number): number {
  const diff = Math.abs(left - right) % (Math.PI * 2)
  return diff > Math.PI ? (Math.PI * 2) - diff : diff
}

function thresholdAllowsRoute(attributes: Record<string, unknown>, score: number, thresholds: { l1: number; l2: number }): boolean {
  const source = attrString(attributes, 'score_source')
  const visibility = attrString(attributes, 'visibility')
  const level = routeLevel(attributes, {})
  const threshold = level === 2 ? thresholds.l2 : thresholds.l1
  return source === 'related_cache' && (visibility === 'promoted' || score >= threshold)
}

function cytoscapeElements(payload: GraphologyGraphResponse | null, mode: CanvasMode, scoreThresholds: { l1: number; l2: number }): ElementDefinition[] {
  if (!payload) return []
  const graph = importGraphology(payload)
  const sectionKeys = graph
    .nodes()
    .filter((key) => attrString(graph.getNodeAttributes(key), 'kind') === 'section')
    .sort((left, right) => attrNumber(graph.getNodeAttributes(left), 'order', 9999) - attrNumber(graph.getNodeAttributes(right), 'order', 9999))
  const center = { x: 760, y: 560 }
  const sectionRadius = GRAPH_LAYOUT_RADII.l0
  const l1Radius = radialBandRadius(1, 1)
  const l2Radius = radialBandRadius(2, 1)
  const sectionAngles = new Map<string, number>()
  const sectionPositions = new Map<string, { x: number; y: number }>()
  const startAngle = -Math.PI / 2
  const fullTurn = Math.PI * 2
  const angleStep = sectionKeys.length > 1 ? fullTurn / sectionKeys.length : 0
  sectionKeys.forEach((key, index) => {
    const angle = startAngle + index * angleStep
    sectionAngles.set(key, angle)
    sectionPositions.set(key, radialPoint(center, sectionRadius, angle))
  })

  const visibleNodeIds = new Set<string>()
  graph.forEachNode((key, attributes) => {
    if (shouldShowNode(attributes, mode)) visibleNodeIds.add(key)
  })

  const routeAnchorsByTarget = new Map<string, RouteAnchor[]>()
  graph.forEachEdge((_key, attributes, source, target) => {
    if (!visibleNodeIds.has(source)) return
    const relation = attrString(attributes, 'relation', 'wikilink')
    if (relation !== 'related_to') return
    const sourceAttrs = graph.getNodeAttributes(source)
    const targetAttrs = graph.getNodeAttributes(target)
    if (attrString(sourceAttrs, 'kind') !== 'section' || attrString(targetAttrs, 'kind') !== 'article') return
    const sourceAngle = sectionAngles.get(source)
    if (sourceAngle === undefined) return
    const visibility = attrString(attributes, 'visibility', attrString(targetAttrs, 'visibility', 'hidden'))
    const score = Math.max(routeScore(attributes), attrNumber(targetAttrs, 'relevance_to_focus', 0))
    if (mode !== 'promoted' || !thresholdAllowsRoute(attributes, score, scoreThresholds)) return
    visibleNodeIds.add(target)
    const weight = Math.max(
      0.08,
      score * (visibility === 'promoted' ? 1.35 : visibility === 'context' ? 1.0 : 0.45),
    )
    const routes = routeAnchorsByTarget.get(target) ?? []
    routes.push({
      source,
      target,
      level: routeLevel(attributes, targetAttrs),
      angle: sourceAngle,
      score,
      weight,
      visibility,
    })
    routeAnchorsByTarget.set(target, routes)
  })

  const layoutPlans = new Map<string, NodeLayoutPlan>()
  const groupedTargets = new Map<string, PlannedRouteTarget[]>()
  routeAnchorsByTarget.forEach((routes, key) => {
    const promotedRoutes = routes.filter((route) => route.visibility === 'promoted')
    const usableRoutes = promotedRoutes.length ? promotedRoutes : routes
    const level = usableRoutes.reduce((best, route) => (route.score > best.score ? route : best), usableRoutes[0]).level
    const score = Math.max(...usableRoutes.map((route) => route.score), 0)
    const angle = circularMean(usableRoutes)
    const bucket = radialBandKey(level, score)
    const items = groupedTargets.get(bucket) ?? []
    items.push({ key, angle, level, score, routes: usableRoutes, band: bucket })
    groupedTargets.set(bucket, items)
  })

  groupedTargets.forEach((items) => {
    const ordered = items.sort((left, right) => (
      left.angle - right.angle
      || right.score - left.score
      || attrString(graph.getNodeAttributes(left.key), 'label').localeCompare(attrString(graph.getNodeAttributes(right.key), 'label'))
    ))
    const baseRadius = radialBandRadius(ordered[0]?.level ?? 1, ordered[0]?.score ?? 0)
    const minDistance = (ordered[0]?.level ?? 1) === 1 ? 44 : 36
    const capacity = Math.max(6, Math.floor((Math.PI * 2 * baseRadius) / minDistance))
    ordered.forEach((item, index) => {
        const subRing = Math.floor(index / capacity)
        const subRingStart = subRing * capacity
        const subRingItems = ordered.slice(subRingStart, subRingStart + capacity)
        const within = index - subRingStart
        const ringRadius = baseRadius * Math.pow(GRAPH_LAYOUT_RADII.bandMultiplier, subRing)
        const needsEvenDistribution = ordered.length > capacity || subRingItems.some((other, otherIndex) => (
          otherIndex !== within && angularDistance(other.angle, item.angle) * ringRadius < minDistance
        ))
        const circumferenceAngle = startAngle + ((within + 0.5) / Math.max(1, subRingItems.length)) * fullTurn
        const angle = needsEvenDistribution ? circumferenceAngle : item.angle
        const routeJitter = Math.min(34, Math.max(0, item.routes.length - 1) * (item.level === 1 ? -8 : -10))
        layoutPlans.set(item.key, {
          position: radialPoint(center, ringRadius + routeJitter, angle),
          level: item.level,
          score: item.score,
          routeCount: item.routes.length,
        })
      })
  })

  const fallbackArticleIndex = new Map<number, number>()
  const elements: ElementDefinition[] = []
  graph.forEachNode((key, attributes) => {
    if (!visibleNodeIds.has(key)) return
    const kind = attrString(attributes, 'kind')
    const level = attrNumber(attributes, 'level')
    const order = attrNumber(attributes, 'order', 0)
    const plan = layoutPlans.get(key)
    const relevance = Math.max(attrNumber(attributes, 'relevance_to_focus', 0.4), plan?.score ?? 0)
    const domain = attrString(attributes, 'primary_domain', 'Society & People')
    const isSection = kind === 'section'
    const isFocus = kind === 'article' && level === 0
    const visualLevel = plan?.level ?? level
    const shouldLabelArticle = kind === 'article' && (Boolean(plan) || attrString(attributes, 'visibility', 'hidden') === 'promoted')
    const fallbackIndex = fallbackArticleIndex.get(visualLevel) ?? 0
    if (!isFocus && !isSection && !plan) fallbackArticleIndex.set(visualLevel, fallbackIndex + 1)
    const fallbackAngle = startAngle + (fallbackIndex / Math.max(1, graph.order)) * fullTurn
    const position = isFocus
      ? center
      : isSection
        ? (sectionPositions.get(key) ?? radialPoint(center, sectionRadius, startAngle + order * 0.2))
        : (plan?.position ?? radialPoint(center, visualLevel === 1 ? l1Radius : l2Radius, fallbackAngle))
    elements.push({
      group: 'nodes',
      data: {
        id: key,
        label: isFocus
          ? attrString(attributes, 'label', key)
          : isSection
            ? (sectionKeys.length <= 24 || order % 2 === 0 ? attrString(attributes, 'label', key) : '')
            : shouldLabelArticle
              ? attrString(attributes, 'label', key)
              : '',
        full_label: attrString(attributes, 'label', key),
        kind,
        level,
        title: attrString(attributes, 'label', key),
        title_id: attributes.title_id,
        section_key: attributes.section_key,
        heading_id: attributes.heading_id,
        domain,
        fill: isFocus ? '#0F172A' : isSection ? '#F59E0B' : visualLevel === 1 ? '#2563EB' : '#94A3B8',
        domain_color: DOMAIN_COLORS[domain] || DOMAIN_COLORS['Society & People'],
        coverage_state: coverageState(attributes),
        link_extraction_state: linkExtraction(attributes).state,
        coverage_color: COVERAGE_COLORS[coverageState(attributes)] || COVERAGE_COLORS.missing,
        coverage_summary: coverageSummary(attributes),
        intro_excerpt: attrString(attributes, 'intro_excerpt'),
        entity_preview: nodePreview(attributes, 'entity_preview'),
        timeline_preview: nodePreview(attributes, 'timeline_preview'),
        relevance,
        score: attrNumber(attributes, 'score', relevance),
        strength: attrString(attributes, 'strength', 'low'),
        visibility: attrString(attributes, 'visibility', 'hidden'),
        processing_state: coverageState(attributes),
        route_count: plan?.routeCount ?? 0,
        size: isFocus ? 13 : isSection ? 4.5 : Math.max(4.5, Math.min(8, 4 + relevance * 3.5 + attrNumber(attributes, 'specificity') + Math.min(1.5, (plan?.routeCount ?? 0) * 0.16))),
        is_focus: isFocus,
      },
      position,
    })
  })

  graph.forEachEdge((key, attributes, source, target) => {
    if (!visibleNodeIds.has(source) || !visibleNodeIds.has(target)) return
    const relation = attrString(attributes, 'relation', 'wikilink')
    const weight = Math.max(0.05, Math.min(1, attrNumber(attributes, 'weight', 0.2)))
    elements.push({
      group: 'edges',
      data: {
        id: key,
        source,
        target,
        relation,
        weight,
        confidence: attrNumber(attributes, 'confidence', 0.7),
        color: attrBool(attributes, 'back_to_l0') ? '#F59E0B' : RELATION_COLORS[relation] || RELATION_COLORS.wikilink,
        directed: attrBool(attributes, 'directed'),
        back_to_l0: attrBool(attributes, 'back_to_l0'),
      },
    })
  })
  return elements
}

function payloadMaps(payload: GraphologyGraphResponse | null): {
  nodes: Map<string, GraphologyNodePayload>
  edges: GraphologyEdgePayload[]
} {
  return {
    nodes: new Map((payload?.nodes ?? []).map((node) => [node.key, node])),
    edges: payload?.edges ?? [],
  }
}

function allConnectionRows(payload: GraphologyGraphResponse | null): GraphConnection[] {
  if (!payload) return []
  const { nodes, edges } = payloadMaps(payload)
  const l1SourceByTarget = new Map<string, { sourceId: string }>()
  const l2ParentByTarget = new Map<string, { sourceId: string; viaTitle: string }>()
  for (const edge of edges) {
    const relation = attrString(edge.attributes, 'relation')
    if (relation !== 'l1_to_l2') {
      const target = nodes.get(edge.target)
      if (target && attrNumber(target.attributes, 'level', attrNumber(edge.attributes, 'level', 0)) === 1) {
        l1SourceByTarget.set(edge.target, { sourceId: edge.source })
      }
      continue
    }
    const source = nodes.get(edge.source)
    const viaTitle = source ? attrString(source.attributes, 'label', edge.source) : attrString(edge.attributes, 'via_title')
    l2ParentByTarget.set(edge.target, { sourceId: edge.source, viaTitle })
  }
  const rows: GraphConnection[] = []
  for (const edge of edges) {
    const relation = attrString(edge.attributes, 'relation')
    if (relation === 'has_section') continue
    const target = nodes.get(edge.target)
    if (!target) continue
    const score = attrNumber(edge.attributes, 'score', attrNumber(edge.attributes, 'weight', 0))
    const edgeLevel = attrNumber(edge.attributes, 'level', 0)
    const targetLevel = attrNumber(target.attributes, 'level', 0)
    const routeLevel = edgeLevel > 0 ? edgeLevel : targetLevel
    const parent = relation === 'l1_to_l2'
      ? {
          sourceId: edge.source,
          viaTitle: attrString(nodes.get(edge.source)?.attributes ?? {}, 'label', attrString(edge.attributes, 'via_title')),
        }
      : l2ParentByTarget.get(edge.target)
    const l1Source = parent ? l1SourceByTarget.get(parent.sourceId) : null
    rows.push({
      edge,
      node: target,
      sourceId: relation === 'l1_to_l2' && l1Source ? l1Source.sourceId : edge.source,
      sourceIds: [relation === 'l1_to_l2' && l1Source ? l1Source.sourceId : edge.source].filter(Boolean),
      targetId: edge.target,
      level: routeLevel,
      score,
      strength: attrString(edge.attributes, 'strength', score >= 0.72 ? 'high' : score >= 0.52 ? 'medium' : 'low'),
      visibility: attrString(edge.attributes, 'visibility', attrString(target.attributes, 'visibility', 'hidden')),
      processingState: coverageState(target.attributes),
      linkState: linkExtraction(target.attributes).state,
      linkCompleted: linkExtraction(target.attributes).completed,
      linkExpected: linkExtraction(target.attributes).expected,
      title: attrString(target.attributes, 'label', target.key),
      titleId: typeof target.attributes.title_id === 'number' ? target.attributes.title_id : null,
      domain: attrString(target.attributes, 'primary_domain', 'Society & People'),
      viaTitle: parent?.viaTitle || attrString(edge.attributes, 'via_title'),
      viaSourceId: parent?.sourceId || '',
      viaSourceIds: [parent?.sourceId || ''].filter(Boolean),
      routeCount: 1,
      entityPreview: nodePreview(target.attributes, 'entity_preview'),
      timelinePreview: nodePreview(target.attributes, 'timeline_preview'),
    })
  }
  return rows.sort((a, b) => b.score - a.score || a.title.localeCompare(b.title))
}

function sectionRows(payload: GraphologyGraphResponse | null, rows: GraphConnection[]): SectionRow[] {
  if (!payload) return []
  const bySection = new Map<string, GraphConnection[]>()
  for (const row of rows) {
    const group = bySection.get(row.sourceId) ?? []
    group.push(row)
    bySection.set(row.sourceId, group)
  }
  return payload.nodes
    .filter((node) => attrString(node.attributes, 'kind') === 'section')
    .map((node) => {
      const sectionConnections = bySection.get(node.key) ?? []
      const linkState = linkExtraction(node.attributes)
      return {
        key: node.key,
        sectionKey: attrString(node.attributes, 'section_key', node.key.replace(/^sec:/, '')),
        title: attrString(node.attributes, 'label', node.key),
        order: attrNumber(node.attributes, 'order', 9999),
        processingState: coverageState(node.attributes),
        linkState: linkState.state,
        linkCompleted: linkState.completed,
        linkExpected: linkState.expected,
        connectionCount: sectionConnections.length,
        l1Count: sectionConnections.filter((row) => row.level === 1).length,
        l2Count: sectionConnections.filter((row) => row.level === 2).length,
        entityPreview: nodePreview(node.attributes, 'entity_preview'),
        timelinePreview: nodePreview(node.attributes, 'timeline_preview'),
      }
    })
    .sort((a, b) => a.order - b.order || a.title.localeCompare(b.title))
}

interface GraphLevelStatusCounts {
  total: number
  promoted: number
  processed: number
  active: number
  incomplete: number
  missing: number
}

interface GraphCounts {
  sections: number
  l1: GraphLevelStatusCounts
  l2: GraphLevelStatusCounts
  promoted: number
  hidden: number
  active: number
  incomplete: number
}

function emptyLevelCounts(): GraphLevelStatusCounts {
  return { total: 0, promoted: 0, processed: 0, active: 0, incomplete: 0, missing: 0 }
}

function levelCounts(payload: GraphologyGraphResponse | null): GraphCounts {
  const counts: GraphCounts = {
    sections: 0,
    l1: emptyLevelCounts(),
    l2: emptyLevelCounts(),
    promoted: 0,
    hidden: 0,
    active: 0,
    incomplete: 0,
  }
  for (const node of payload?.nodes ?? []) {
    const kind = attrString(node.attributes, 'kind')
    const level = attrNumber(node.attributes, 'level', 0)
    const visibility = attrString(node.attributes, 'visibility', 'hidden')
    const state = coverageState(node.attributes)
    if (kind === 'section') counts.sections += 1
    const levelBucket = kind === 'article' && level === 1 ? counts.l1 : kind === 'article' && level === 2 ? counts.l2 : null
    if (levelBucket) {
      levelBucket.total += 1
      if (visibility === 'promoted') levelBucket.promoted += 1
      if (state === 'done') levelBucket.processed += 1
      if (state === 'running') levelBucket.active += 1
      if (state === 'stale' || state === 'incomplete') levelBucket.incomplete += 1
      if (state === 'missing') levelBucket.missing += 1
    }
    if (visibility === 'promoted') counts.promoted += 1
    if (visibility === 'hidden') counts.hidden += 1
    if (state === 'running') counts.active += 1
    if (state === 'stale' || state === 'incomplete') counts.incomplete += 1
  }
  return counts
}

function levelStatusSummary(label: string, counts: GraphLevelStatusCounts): string {
  return `${label} ${counts.total}: ${counts.promoted} promoted, ${counts.processed} processed, ${counts.active} active, ${counts.incomplete} incomplete, ${counts.missing} missing`
}

function stateClass(state: string): string {
  if (state === 'done') return 'done'
  if (state === 'failed') return 'failed'
  if (state === 'running') return 'running'
  if (state === 'stale') return 'stale'
  if (state === 'incomplete') return 'stale'
  return 'missing'
}

function relationshipKey(row: GraphConnection): string {
  const identity = row.titleId != null ? String(row.titleId) : normalizedTitle(row.title)
  return `L${row.level}:${identity}`
}

function normalizedTitle(title: string): string {
  return title.replace(/_/g, ' ').replace(/\s+/g, ' ').trim().toLocaleLowerCase()
}

function sectionKeysForConnection(rows: GraphConnection[], row: GraphConnection): Set<string> {
  if (row.level === 1) return new Set((row.sourceIds.length ? row.sourceIds : [row.sourceId]).filter(Boolean))
  if (row.sourceIds.length > 1) return new Set(row.sourceIds.filter(Boolean))
  return new Set(
    rows
      .filter((candidate) => (
        candidate.level === 1
        && (candidate.targetId === row.viaSourceId || normalizedTitle(candidate.title) === normalizedTitle(row.viaTitle))
      ))
      .map((candidate) => candidate.sourceId)
      .filter(Boolean),
  )
}

function dedupeRelationshipRows(rows: GraphConnection[]): GraphConnection[] {
  const byIdentity = new Map<string, GraphConnection>()
  for (const row of rows) {
    const key = relationshipKey(row)
    const existing = byIdentity.get(key)
    if (!existing) {
      byIdentity.set(key, {
        ...row,
        sourceIds: Array.from(new Set(row.sourceIds.length ? row.sourceIds : [row.sourceId].filter(Boolean))),
        viaSourceIds: Array.from(new Set(row.viaSourceIds.length ? row.viaSourceIds : [row.viaSourceId].filter(Boolean))),
        routeCount: Math.max(1, row.routeCount || 1),
      })
      continue
    }
    const score = Math.max(existing.score, row.score)
    const better = row.score > existing.score ? row : existing
    byIdentity.set(key, {
      ...better,
      score,
      strength: score >= 0.72 ? 'high' : score >= 0.52 ? 'medium' : 'low',
      visibility: existing.visibility === 'promoted' || row.visibility === 'promoted' ? 'promoted' : better.visibility,
      sourceIds: Array.from(new Set([...existing.sourceIds, ...row.sourceIds, existing.sourceId, row.sourceId].filter(Boolean))),
      viaSourceIds: Array.from(new Set([...existing.viaSourceIds, ...row.viaSourceIds, existing.viaSourceId, row.viaSourceId].filter(Boolean))),
      routeCount: (existing.routeCount || 1) + (row.routeCount || 1),
      linkCompleted: Math.max(existing.linkCompleted, row.linkCompleted),
      linkExpected: Math.max(existing.linkExpected, row.linkExpected),
      entityPreview: existing.entityPreview.count >= row.entityPreview.count ? existing.entityPreview : row.entityPreview,
      timelinePreview: existing.timelinePreview.count >= row.timelinePreview.count ? existing.timelinePreview : row.timelinePreview,
    })
  }
  return Array.from(byIdentity.values()).sort((a, b) => b.score - a.score || b.routeCount - a.routeCount || a.title.localeCompare(b.title))
}

function filteredRows(rows: GraphConnection[], state: BrowserState): { l1: GraphConnection[]; l2: GraphConnection[] } {
  const weightedRows = rows.filter((row) => attrString(row.edge.attributes, 'relation') === 'related_to')
  const baseRows = weightedRows.length ? weightedRows : rows
  let scopedRows = state.section ? baseRows.filter((row) => row.sourceId === state.section?.key || row.sourceIds.includes(state.section?.key ?? '')) : baseRows
  if (state.l2) {
    scopedRows = scopedRows.filter((row) => (
      row.level === 2
        ? relationshipKey(row) === relationshipKey(state.l2 as GraphConnection)
        : row.targetId === state.l2?.viaSourceId || normalizedTitle(row.title) === normalizedTitle(state.l2?.viaTitle ?? '')
    ))
  }
  const l1 = scopedRows.filter((row) => row.level === 1)
  const l2Base = state.l1
    ? baseRows.filter((row) => row.level === 2)
    : scopedRows.filter((row) => row.level === 2)
  const l2 = state.l1
    ? l2Base.filter((row) => (
        row.viaSourceId
          ? row.viaSourceId === state.l1?.targetId || row.viaSourceIds.includes(state.l1?.targetId ?? '')
          : normalizedTitle(row.viaTitle) === normalizedTitle(state.l1?.title ?? '')
      ))
    : l2Base
  return { l1: dedupeRelationshipRows(l1), l2: dedupeRelationshipRows(l2) }
}

function filteredSections(sections: SectionRow[], rows: GraphConnection[], state: BrowserState): SectionRow[] {
  if (state.l2) {
    const keys = sectionKeysForConnection(rows, state.l2)
    return sections.filter((section) => keys.has(section.key))
  }
  if (state.l1) {
    const keys = sectionKeysForConnection(rows, state.l1)
    return sections.filter((section) => keys.has(section.key))
  }
  return sections
}

function NodePreviewBlock({
  title,
  preview,
  kind,
}: {
  title: string
  preview: NodePreview
  kind: 'entity' | 'time'
}) {
  const items = preview.items.slice(0, 8)
  return (
    <div className="cy-node-preview">
      <span>{title} <small>{preview.count}</small></span>
      {items.length ? (
        <div className="cy-node-preview-list">
          {items.map((item, index) => {
            const text = previewText(item, kind)
            const meta = previewMeta(item, kind)
            if (!text) return null
            return (
              <small key={`${text}-${index}`} title={meta || text}>
                {text}
                {meta ? <em>{meta}</em> : null}
              </small>
            )
          })}
          {preview.truncated > 0 ? <small>+{preview.truncated}</small> : null}
        </div>
      ) : (
        <p>None extracted yet</p>
      )}
    </div>
  )
}

function SectionPanel({
  rows,
  selected,
  onSelect,
}: {
  rows: SectionRow[]
  selected: SectionRow | null
  onSelect: (row: SectionRow) => void
}) {
  return (
    <section className="cy-relationship-panel">
      <header>
        <h3>L0 Sections</h3>
        <span>{rows.length}</span>
      </header>
      <ul className="cy-relationship-list">
        {rows.map((row) => (
          <li
            key={row.key}
            className={`cy-relationship-card cy-section-card ${selected?.key === row.key ? 'selected' : ''}`}
            onClick={() => onSelect(row)}
            role="button"
            tabIndex={0}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onSelect(row)
              }
            }}
          >
            <button type="button" className="cy-relationship-select" tabIndex={-1}>
              <strong>{row.title}</strong>
              <span className={`cy-state-chip state-${stateClass(row.linkState)}`}>links {row.linkState}</span>
            </button>
            <div className="cy-relationship-meta">
              <span className={`cy-state-chip state-${stateClass(row.processingState)}`}>node {row.processingState}</span>
              <span>{row.connectionCount} links</span>
              <span>{row.linkCompleted}/{row.linkExpected}</span>
              <span>{row.l1Count} L1</span>
              <span>{row.l2Count} L2</span>
            </div>
            <NodePreviewBlock title="Entities" preview={row.entityPreview} kind="entity" />
            <NodePreviewBlock title="Timeline" preview={row.timelinePreview} kind="time" />
          </li>
        ))}
      </ul>
    </section>
  )
}

function RelationshipPanel({
  title,
  rows,
  selected,
  emptyText,
  onSelect,
  onOpenArticle,
}: {
  title: string
  rows: GraphConnection[]
  selected: GraphConnection | null
  emptyText: string
  onSelect: (row: GraphConnection) => void
  onOpenArticle: (title: string) => void
}) {
  return (
    <section className="cy-relationship-panel">
      <header>
        <h3>{title}</h3>
        <span>{rows.length}</span>
      </header>
      {rows.length ? (
        <ul className="cy-relationship-list">
      {rows.slice(0, 120).map((row) => (
        <li
          key={relationshipKey(row)}
          className={`cy-relationship-card strength-${row.strength} ${selected && relationshipKey(selected) === relationshipKey(row) ? 'selected' : ''}`}
          onClick={() => onSelect(row)}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              onSelect(row)
            }
          }}
        >
          <button type="button" className="cy-relationship-select" tabIndex={-1}>
            <strong>{row.title}</strong>
            <span className={`cy-state-chip state-${stateClass(row.linkState)}`}>links {row.linkState}</span>
          </button>
          <div className="cy-relationship-meta">
            <span className={`cy-state-chip state-${stateClass(row.processingState)}`}>node {row.processingState}</span>
            <span>L{row.level}</span>
            <span>{row.linkCompleted}/{row.linkExpected}</span>
            <span>{row.strength}</span>
            <span>{row.visibility}</span>
            <span>Score {row.score.toFixed(2)}</span>
            {row.routeCount > 1 ? <span>{row.routeCount} routes</span> : null}
            {row.viaTitle ? <span>via {row.viaTitle}</span> : null}
          </div>
          <NodePreviewBlock title="Entities" preview={row.entityPreview} kind="entity" />
          <NodePreviewBlock title="Timeline" preview={row.timelinePreview} kind="time" />
          <div className="cy-relationship-footer">
            <small>{row.domain}</small>
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                if (row.title) onOpenArticle(row.title)
              }}
            >
              Open article
            </button>
          </div>
        </li>
      ))}
        </ul>
      ) : (
        <p className="cy-empty-connections">{emptyText}</p>
      )}
    </section>
  )
}

export default function CytoscapeGraphView({
  payload,
  articleTitle,
  selectedSectionKey,
  isLoading = false,
  isCrawling = false,
  actionLabel = null,
  error = null,
  onSelectSection,
  onOpenArticle,
  onCrawlGraph,
  onLazyExpandGraph,
}: CytoscapeGraphViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const cyRef = useRef<Core | null>(null)
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null)
  const [canvasMode, setCanvasMode] = useState<CanvasMode>('promoted')
  const [scoreThresholds, setScoreThresholds] = useState({ l1: 0.42, l2: 0.34 })
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [isGraphFullscreen, setIsGraphFullscreen] = useState(false)
  const [browserState, setBrowserState] = useState<BrowserState>({ section: null, l1: null, l2: null })
  const [selectedIntro, setSelectedIntro] = useState<{ key: string; text: string; loading: boolean; error: string | null }>({
    key: '',
    text: '',
    loading: false,
    error: null,
  })
  const elements = useMemo(() => cytoscapeElements(payload, canvasMode, scoreThresholds), [payload, canvasMode, scoreThresholds])
  const counts = useMemo(() => levelCounts(payload), [payload])
  const relationshipRows = useMemo(() => allConnectionRows(payload), [payload])
  const sections = useMemo(() => sectionRows(payload, relationshipRows), [payload, relationshipRows])
  const visibleSections = useMemo(() => filteredSections(sections, relationshipRows, browserState), [sections, relationshipRows, browserState])
  const visibleRelationships = useMemo(() => filteredRows(relationshipRows, browserState), [relationshipRows, browserState])

  function selectGraphNode(data: Record<string, unknown>): void {
    const kind = String(data.kind || '')
    const level = typeof data.level === 'number' ? data.level : 0
    const nodeId = String(data.id || '')
    if (data.section_key) {
      const section = sections.find((row) => row.key === nodeId || row.sectionKey === String(data.section_key))
      if (!section) return
      onSelectSection(section.sectionKey)
      onLazyExpandGraph({ sectionKey: section.sectionKey })
      setBrowserState({ section, l1: null, l2: null })
      return
    }
    if (kind !== 'article' || level === 0) return

    const titleId = typeof data.title_id === 'number' ? data.title_id : null
    const title = String(data.title || data.full_label || data.label || '')
    const matchesNode = (row: GraphConnection) => (
      row.targetId === nodeId
      || (titleId !== null && row.titleId === titleId)
      || normalizedTitle(row.title) === normalizedTitle(title)
    )

    if (level === 1) {
      const [row] = dedupeRelationshipRows(relationshipRows.filter((candidate) => candidate.level === 1 && matchesNode(candidate)))
      if (!row) return
      onLazyExpandGraph({ l1TitleId: row.titleId, l1Title: row.title })
      setBrowserState({ section: null, l1: row, l2: null })
      return
    }

    if (level === 2) {
      const [row] = dedupeRelationshipRows(relationshipRows.filter((candidate) => candidate.level === 2 && matchesNode(candidate)))
      if (!row) return
      const upstreamL1 = dedupeRelationshipRows(
        relationshipRows.filter((candidate) => (
          candidate.level === 1
          && (
            row.viaSourceIds.includes(candidate.targetId)
            || candidate.targetId === row.viaSourceId
            || normalizedTitle(candidate.title) === normalizedTitle(row.viaTitle)
          )
        )),
      )[0] ?? null
      const upstreamSection = upstreamL1
        ? sections.find((section) => upstreamL1.sourceIds.includes(section.key) || section.key === upstreamL1.sourceId) ?? null
        : null
      setBrowserState({ section: upstreamSection, l1: upstreamL1, l2: row })
    }
  }

  useEffect(() => {
    if (!containerRef.current) return
    const cy = cytoscape({
      container: containerRef.current,
      elements,
      layout: { name: 'preset', fit: true, padding: 36 },
      minZoom: 0.28,
      maxZoom: 2.5,
      wheelSensitivity: 1.6,
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'background-color': 'data(fill)',
            width: 'data(size)',
            height: 'data(size)',
            'border-width': 0,
            'border-color': 'transparent',
            'font-size': 7,
            'font-weight': 700,
            color: '#0f172a',
            'text-outline-color': '#f8fafc',
            'text-outline-width': 2,
            'text-valign': 'bottom',
            'text-halign': 'center',
            'text-margin-y': 3,
          },
        },
        { selector: 'node[kind = "section"]', style: { 'font-size': 6, 'text-margin-y': 3, color: '#1e293b' } },
        { selector: 'node[level = 2]', style: { 'font-size': 6, color: '#334155' } },
        { selector: 'node[visibility = "promoted"]', style: { 'font-size': 8, color: '#111827', 'text-outline-width': 3, 'z-index': 60 } },
        { selector: 'node[is_focus]', style: { width: 13, height: 13, 'border-width': 1.4, 'border-color': '#F59E0B', 'font-size': 8, color: '#0f172a', 'text-halign': 'center', 'text-valign': 'bottom', 'text-margin-y': 4 } },
        { selector: 'node.hovered, node:selected', style: { label: 'data(full_label)', 'font-size': 9, 'text-outline-width': 4, 'z-index': 999 } },
        { selector: 'node:selected', style: { 'border-width': 2, 'border-color': '#F59E0B' } },
        { selector: 'edge', style: { width: 'mapData(weight, 0, 1, 0.45, 1.8)', 'line-color': 'data(color)', 'target-arrow-color': 'data(color)', 'target-arrow-shape': 'none', opacity: 0.16, 'curve-style': 'unbundled-bezier', 'control-point-distance': 34, 'control-point-weight': 0.5 } },
        { selector: 'edge[relation = "has_section"]', style: { width: 0.55, 'target-arrow-shape': 'none', opacity: 0.14, 'line-color': '#94A3B8' } },
        { selector: 'edge[back_to_l0]', style: { width: 'mapData(weight, 0, 1, 0.8, 2.4)', 'line-color': '#F59E0B', 'target-arrow-color': '#F59E0B', opacity: 0.24 } },
      ],
    })
    cyRef.current = cy
    cy.on('tap', 'node', (event) => {
      const node = event.target
      const data = node.data()
      setSelected(data)
      selectGraphNode(data)
    })
    cy.on('mouseover', 'node', (event) => event.target.addClass('hovered'))
    cy.on('mouseout', 'node', (event) => event.target.removeClass('hovered'))
    cy.on('dbltap', 'node', (event) => {
      const data = event.target.data()
      if (data.kind === 'article' && data.level !== 0 && data.title) onOpenArticle(String(data.title))
    })
    return () => {
      cy.destroy()
      cyRef.current = null
    }
  }, [elements, onOpenArticle, onSelectSection, onLazyExpandGraph, relationshipRows, sections])

  useEffect(() => {
    if (!selectedSectionKey || !cyRef.current) return
    const node = cyRef.current.nodes().filter((candidate) => candidate.data('section_key') === selectedSectionKey).first()
    if (node.nonempty()) {
      cyRef.current.nodes().unselect()
      node.select()
      setSelected(node.data())
    }
  }, [selectedSectionKey, elements])

  useEffect(() => {
    cyRef.current?.resize()
    const timers = [80, 260].map((delay) => window.setTimeout(() => {
      cyRef.current?.resize()
      cyRef.current?.fit(undefined, 36)
    }, delay))
    return () => timers.forEach((timer) => window.clearTimeout(timer))
  }, [isFullscreen, isGraphFullscreen])

  useEffect(() => {
    const key = selectedNodeContentKey(selected)
    if (!isGraphFullscreen || !selected || !key) {
      setSelectedIntro({ key: '', text: '', loading: false, error: null })
      return
    }

    const fallbackText = compactIntroText(String(selected.intro_excerpt || ''))
    const kind = String(selected.kind || '')
    const title = selectedArticleTitle(selected, articleTitle)
    if (!title) {
      setSelectedIntro({ key, text: fallbackText, loading: false, error: null })
      return
    }

    let cancelled = false
    setSelectedIntro({ key, text: fallbackText, loading: true, error: null })
    fetchArticle(title, false, false)
      .then((article) => {
        if (cancelled) return
        const selectedSectionKey = String(selected.section_key || '')
        const section = kind === 'section' && selectedSectionKey
          ? article.sections.find((candidate) => candidate.section_key === selectedSectionKey)
          : null
        const intro = section
          ?? article.sections.find((candidate) => (candidate.level ?? 0) === 0 || ['lead', 'introduction', 'overview'].includes(candidate.heading.toLowerCase()))
          ?? article.sections[0]
        const text = compactIntroText(intro?.clean_text || fallbackText, kind === 'section' ? 1800 : 1250)
        setSelectedIntro({ key, text, loading: false, error: null })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setSelectedIntro({
          key,
          text: fallbackText,
          loading: false,
          error: error instanceof Error ? error.message : 'Could not load article intro.',
        })
      })
    return () => {
      cancelled = true
    }
  }, [selected, isGraphFullscreen, articleTitle])

  const nodeCount = payload?.nodes.length ?? 0
  const edgeCount = payload?.edges.length ?? 0
  const selectedTitle = selected ? String(selected.full_label || selected.label || selected.id || 'Selected node') : 'Select a section'
  const activeContext = browserState.l2
    ? `L2: ${browserState.l2.title}`
    : browserState.l1
      ? `L1: ${browserState.l1.title}`
      : browserState.section
        ? `Section: ${browserState.section.title}`
        : 'All sections'

  function toggleSection(row: SectionRow): void {
    setBrowserState((current) => {
      const same = current.section?.key === row.key
      if (same) return { section: null, l1: null, l2: null }
      onSelectSection(row.sectionKey)
      onLazyExpandGraph({ sectionKey: row.sectionKey })
      return { section: row, l1: null, l2: null }
    })
  }

  function toggleRelationship(level: 1 | 2, row: GraphConnection): void {
    setBrowserState((current) => {
      if (level === 1) {
        const same = current.l1 && relationshipKey(current.l1) === relationshipKey(row)
        if (same) return { ...current, l1: null, l2: null }
        onLazyExpandGraph({ l1TitleId: row.titleId, l1Title: row.title })
        return { ...current, l1: row, l2: null }
      }
      const same = current.l2 && relationshipKey(current.l2) === relationshipKey(row)
      if (same) return { ...current, l2: null }
      const upstreamL1 = relationshipRows.find((candidate) => (
        candidate.level === 1
        && (candidate.targetId === row.viaSourceId || normalizedTitle(candidate.title) === normalizedTitle(row.viaTitle))
        && (!current.section || candidate.sourceId === current.section.key)
      )) ?? current.l1
      const upstreamSection = upstreamL1
        ? sections.find((section) => section.key === upstreamL1.sourceId) ?? current.section
        : current.section
      return { section: upstreamSection, l1: upstreamL1, l2: row }
    })
  }

  return (
    <div className={`cy-graph-shell ${isFullscreen ? 'fullscreen' : ''}`}>
      <header className="cy-graph-toolbar">
        <div>
          <h2>{articleTitle || String(payload?.attributes.focus_title || 'Graph')}</h2>
          {isLoading ? (
            <span>Loading graph...</span>
          ) : (
            <div className="cy-graph-stats">
              {isCrawling && actionLabel ? <span>{actionLabel}</span> : null}
              <small>{levelStatusSummary('L1', counts.l1)} | {levelStatusSummary('L2', counts.l2)}</small>
            </div>
          )}
          {error ? <strong>{error}</strong> : null}
        </div>
        <div className="cy-graph-actions">
          <div className="cy-mode-toggle" aria-label="Canvas visibility">
            <button type="button" className={canvasMode === 'core' ? 'active' : ''} onClick={() => setCanvasMode('core')}>L0 only</button>
            <button type="button" className={canvasMode === 'promoted' ? 'active' : ''} onClick={() => setCanvasMode('promoted')}>Promoted</button>
          </div>
          <button type="button" onClick={onCrawlGraph} disabled={isCrawling}>{isCrawling ? (actionLabel || 'Working') : 'Cover graph'}</button>
          <button
            type="button"
            onClick={() => {
              setIsGraphFullscreen(false)
              setIsFullscreen((value) => !value)
            }}
          >
            {isFullscreen ? 'Exit full screen' : 'Full screen'}
          </button>
        </div>
      </header>
      <div className="cy-graph-body">
        <section className={`cy-graph-left ${isGraphFullscreen ? 'graph-fullscreen' : ''} ${isGraphFullscreen && selected ? 'with-node-panel' : ''}`}>
          <div className="cy-graph-overlay-controls">
            <label className="cy-threshold-control">
              <span>L1 {Math.round(scoreThresholds.l1 * 100)}%</span>
              <input
                type="range"
                min="0.20"
                max="0.85"
                step="0.005"
                value={scoreThresholds.l1}
                disabled={canvasMode === 'core'}
                onChange={(event) => {
                  setCanvasMode('promoted')
                  setScoreThresholds((current) => ({ ...current, l1: Number(event.target.value) }))
                }}
              />
            </label>
            <label className="cy-threshold-control">
              <span>L2 {Math.round(scoreThresholds.l2 * 100)}%</span>
              <input
                type="range"
                min="0.15"
                max="0.75"
                step="0.005"
                value={scoreThresholds.l2}
                disabled={canvasMode === 'core'}
                onChange={(event) => {
                  setCanvasMode('promoted')
                  setScoreThresholds((current) => ({ ...current, l2: Number(event.target.value) }))
                }}
              />
            </label>
          </div>
          <button
            type="button"
            className="cy-graph-corner-toggle"
            title={isGraphFullscreen ? 'Minimize graph' : 'Maximize graph'}
            aria-label={isGraphFullscreen ? 'Minimize graph' : 'Maximize graph'}
            onClick={() => {
              if (isGraphFullscreen) {
                setIsGraphFullscreen(false)
                return
              }
              setIsFullscreen(false)
              setIsGraphFullscreen(true)
            }}
          >
            {isGraphFullscreen ? <Minimize2 size={16} strokeWidth={2.4} /> : <Maximize2 size={16} strokeWidth={2.4} />}
          </button>
          {isGraphFullscreen && selected ? (
            <aside className="cy-graph-node-panel" aria-live="polite">
              <header>
                <span>{String(selected.kind || 'node').toUpperCase()}{typeof selected.level === 'number' && selected.level > 0 ? ` L${selected.level}` : ''}</span>
                <strong title={selectedTitle}>{selectedTitle}</strong>
              </header>
              <p>{selectedNodeSummary(selected)}</p>
              <div className="cy-node-card-intro">
                <span>
                  {selectedIntro.loading
                    ? (String(selected.kind || '') === 'section' ? 'Loading section content' : 'Loading article intro')
                    : (String(selected.kind || '') === 'section' ? 'Section content' : 'Article intro')}
                </span>
                {selectedIntro.text ? (
                  <p>{selectedIntro.text}</p>
                ) : (
                  <p className="muted">{selectedIntro.loading ? 'Fetching content from article cache...' : 'No content available yet.'}</p>
                )}
                {selectedIntro.error ? <small>{selectedIntro.error}</small> : null}
              </div>
              <div className="cy-node-card-metrics">
                <small>{String(selected.processing_state || selected.coverage_state || 'missing')}</small>
                {typeof selected.relevance === 'number' ? <small>{Math.round(selected.relevance * 100)}% relevance</small> : null}
                {typeof selected.route_count === 'number' && selected.route_count > 0 ? <small>{selected.route_count} route{selected.route_count === 1 ? '' : 's'}</small> : null}
                {selected.domain ? <small>{String(selected.domain)}</small> : null}
              </div>
              {nodePreviewItems(selected, 'entity_preview').items.length ? (
                <div className="cy-node-card-preview">
                  <span>Entities</span>
                  <p>
                    {nodePreviewItems(selected, 'entity_preview').items.slice(0, 8).map((item) => previewText(item, 'entity')).filter(Boolean).join(', ')}
                  </p>
                </div>
              ) : null}
              {nodePreviewItems(selected, 'timeline_preview').items.length ? (
                <div className="cy-node-card-preview">
                  <span>Timeline</span>
                  <p>
                    {nodePreviewItems(selected, 'timeline_preview').items.slice(0, 6).map((item) => previewText(item, 'time')).filter(Boolean).join(', ')}
                  </p>
                </div>
              ) : null}
              {String(selected.kind || '') === 'article' && selectedTitle ? (
                <button type="button" onClick={() => onOpenArticle(selectedTitle)}>Open article</button>
              ) : null}
            </aside>
          ) : null}
          <div ref={containerRef} className="cy-graph-canvas" />
          <div className="cy-graph-selection-strip">
            <span>{selected ? String(selected.kind || 'node').toUpperCase() : 'GRAPH'}</span>
            <strong>{selectedTitle}</strong>
            {selected ? <small>{String(selected.processing_state || selected.coverage_state || 'missing')}</small> : <small>{activeContext}</small>}
          </div>
        </section>
        <aside className="cy-relationship-browser">
          <div className="cy-browser-header">
            <div>
              <span>{isCrawling && actionLabel ? 'Relationship Browser - running' : 'Relationship Browser'}</span>
              <strong>{activeContext}</strong>
            </div>
            {(browserState.section || browserState.l1 || browserState.l2) ? (
              <button type="button" onClick={() => setBrowserState({ section: null, l1: null, l2: null })}>Clear</button>
            ) : null}
          </div>
          <div className="cy-relationship-columns">
            <SectionPanel rows={visibleSections} selected={browserState.section} onSelect={toggleSection} />
            <RelationshipPanel
              title="L1"
              rows={visibleRelationships.l1}
              selected={browserState.l1}
              emptyText={browserState.section ? 'No L1 articles for this section yet.' : 'No L1 relationships loaded.'}
              onSelect={(row) => toggleRelationship(1, row)}
              onOpenArticle={onOpenArticle}
            />
            <RelationshipPanel
              title="L2 Articles"
              rows={visibleRelationships.l2}
              selected={browserState.l2}
              emptyText={browserState.section ? 'No L2 articles for this section yet.' : 'No L2 relationships loaded.'}
              onSelect={(row) => toggleRelationship(2, row)}
              onOpenArticle={onOpenArticle}
            />
          </div>
        </aside>
      </div>
    </div>
  )
}
