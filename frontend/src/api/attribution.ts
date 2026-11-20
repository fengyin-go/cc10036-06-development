import { request } from './client'

/** 转化归因结算与同期群渠道贡献的数据契约，与后端 /api/analytics/attribution-settlement 对齐。 */

export type AttributionCaliber = {
  model: string
  window_days: number | null
  decay_half_life_days: number
  position_first_weight: number
  position_last_weight: number
  period_days: number
  settle_basis: 'touch_week' | 'return_week'
  window_rule?: string
  settlement_basis_label?: string
  dedup_rule?: string
}

export type AttributionConversion = {
  conversion_id: string
  user_id: string
  conversion_time: number
  conversion_period: number
  conversion_period_label: string
  channel: string
  touch_time: number
  touch_period: number
  touch_period_label: string
  settlement_period: number
  settlement_period_label: string
  weight: number
  model: string
  window_days: number | null
  settle_basis: string
}

export type AttributionTotals = {
  submitted_conversions: number
  unique_conversions: number
  attributed_conversions: number
  unattributed_conversions: number
  duplicate_submissions: number
  touchpoints: number
  duplicate_touchpoints: number
  invalid_touchpoints: number
  attributed_weight: number
}

export type AttributionSettlement = {
  ok: boolean
  message?: string
  caliber: AttributionCaliber
  conversions: AttributionConversion[]
  unattributed: { conversion_id: string; user_id: string; reason: string }[]
  excluded_touchpoints: {
    conversion_id: string
    channel: string
    touch_time: number | null
    reason: string
  }[]
  period_summary: {
    period_index: number
    period_label: string
    conversion_count: number
    total_weight: number
    channels: Record<string, number>
    channel_share: Record<string, number>
  }[]
  totals: AttributionTotals
  bill_id?: string
  bill?: {
    bill_id: string
    model_id: number
    model_name: string
    model_conditions: Record<string, unknown>
    overrides: Record<string, unknown>
    totals: AttributionTotals
  }
  model?: { model_id: number; model_name: string; attribution_window: string }
  overrides: Record<string, unknown>
}

export type CohortAttributionPeriod = {
  period_label: string
  period_offset: number
  total_weight: number
  conversion_rate_pct: number
  channels: Record<string, number>
  channel_rates_pct: Record<string, number>
}

export type CohortAttributionRow = {
  cohort_id: string
  cohort_index: number
  denominator: number
  periods: CohortAttributionPeriod[]
}

export type CohortAttribution = {
  ok: boolean
  message?: string
  config_name: string
  init_event: string
  return_event: string
  periods: {
    period_days: number
    period_labels: string[]
    settlement_basis: string
    settlement_basis_label: string
    denominator: string
    rate_formula: string
    model: string
    window_days: number | null
    period_unit: string
  }
  attribution_caliber: AttributionCaliber
  attribution_model: {
    model_id: number
    model_name: string
    attribution_window: string
    overrides: Record<string, unknown>
  }
  attribution_totals: AttributionTotals
  conversions: AttributionConversion[]
  unattributed: AttributionSettlement['unattributed']
  excluded_touchpoints: AttributionSettlement['excluded_touchpoints']
  attribution_periods: AttributionSettlement['period_summary']
  channel_contributions: { channel: string; total_weight: number; share_pct: number }[]
  cohort_ids: string[]
  denominators: number[]
  cohort_attribution_matrix: CohortAttributionRow[]
  retention_matrix: number[][]
  retention_period_labels: string[]
  unmatched_weight: number
  outside_range_weight: number
}

export type AttributionSettlementResponse = {
  ok: boolean
  message?: string
  pipeline: string
  settlement: AttributionSettlement
  cohort: CohortAttribution
  reconcile: {
    attribution_weight_total: number
    cohort_weight_total: number
    denominators: number[]
    denominator_rule: string
    settlement_basis: string
    model_conditions: {
      model_id: number
      model: string
      window_days: number | null
      period_days: number
      overrides: Record<string, unknown>
    }
  }
}

export type AttributionBill = {
  bill_id: string
  model_id: number
  model_name: string
  model_conditions: Record<string, unknown>
  overrides: Record<string, unknown>
  totals: AttributionTotals
  period_summary: AttributionSettlement['period_summary']
}

export type AttributionSettlementRequest = {
  model_id: number
  config_id: number
  model_overrides: Record<string, unknown>
  events?: Record<string, unknown>[]
  touchpoints?: Record<string, unknown>[]
  max_periods?: number
}

export async function settleAttribution(
  payload: AttributionSettlementRequest,
): Promise<AttributionSettlementResponse> {
  const response = await request('/api/analytics/attribution-settlement', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('转化归因结算失败')
  return (await response.json()) as AttributionSettlementResponse
}

export async function loadAttributionBills(limit = 20): Promise<{
  ok: boolean
  total: number
  bills: AttributionBill[]
}> {
  const response = await request(`/api/analytics/attribution/bills?limit=${limit}`)
  if (!response.ok) throw new Error('结算账单读取失败')
  return await response.json()
}

export async function loadAttributionCaliber(modelId: number): Promise<{
  ok: boolean
  caliber: AttributionCaliber
}> {
  const response = await request(`/api/analytics/attribution/caliber/${modelId}`)
  if (!response.ok) throw new Error('归因口径读取失败')
  return await response.json()
}
