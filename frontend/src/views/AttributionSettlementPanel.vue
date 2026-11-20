<template>
  <section class="attribution-settlement" data-testid="attribution-settlement">
    <header class="page-head">
      <div>
        <h3>转化归因结算</h3>
        <p class="page-desc">
          按归因窗口把触点权重结算到每一笔回访转化，再由留存分析按周期口径汇总渠道贡献。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" :disabled="loading" @click="reloadBills">
          刷新账单
        </button>
        <button class="btn primary" data-testid="run-settlement" type="button" :disabled="loading" @click="runSettlement">
          {{ loading ? '结算中…' : '重算转化归因' }}
        </button>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="runSettlement">
      <label>
        归因模型
        <select v-model.number="form.modelId" data-testid="settlement-model" @change="syncWindowFromModel">
          <option v-for="option in modelOptions" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </label>
      <label>
        归因窗口阈值（天）
        <input v-model.number="form.windowDays" data-testid="settlement-window" type="number" min="0" step="1" />
      </label>
      <label>
        结算周期口径
        <select v-model="form.settleBasis" data-testid="settlement-basis">
          <option value="return_week">运营按回访周复盘</option>
          <option value="touch_week">渠道按触达周结算</option>
        </select>
      </label>
      <label>
        留存周期（天）
        <input v-model.number="form.periodDays" data-testid="settlement-period" type="number" min="1" step="1" />
      </label>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="result">
      <div class="analysis-summary">
        <article class="analysis-card">
          <span class="stat-label">归属权重合计</span>
          <strong class="stat-value">{{ result.reconcile.attribution_weight_total }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">矩阵权重合计</span>
          <strong class="stat-value">{{ result.reconcile.cohort_weight_total }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">同期群分母</span>
          <strong class="stat-value">{{ result.reconcile.denominators.join(' / ') }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">重复回访（只记一次）</span>
          <strong class="stat-value">{{ result.settlement.totals.duplicate_submissions }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">无效触点</span>
          <strong class="stat-value">{{ result.settlement.totals.invalid_touchpoints }}</strong>
        </article>
      </div>

      <p class="caliber-line" data-testid="settlement-caliber">
        模型条件：{{ result.reconcile.model_conditions.model }}
        ｜窗口 {{ displayWindow }} ｜周期 {{ result.cohort.periods.period_unit }}
        ｜{{ basisLabel }} ｜分母 {{ result.reconcile.denominator_rule }}
        <span v-if="result.settlement.bill_id">｜账单 {{ result.settlement.bill_id }}</span>
      </p>

      <h4>渠道归属周期矩阵（行=同期群，列=留存周期）</h4>
      <div class="matrix-wrap">
        <table class="data-table" data-testid="cohort-attribution-matrix">
          <thead>
            <tr>
              <th>同期群</th>
              <th>分母</th>
              <th v-for="label in result.cohort.periods.period_labels" :key="label">{{ label }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in result.cohort.cohort_attribution_matrix" :key="row.cohort_id">
              <td>{{ row.cohort_id }}</td>
              <td>{{ row.denominator }}</td>
              <td v-for="cell in row.periods" :key="cell.period_label">
                <template v-if="cell.total_weight">
                  <strong>{{ cell.total_weight }}</strong>
                  <span class="matrix-sub">{{ cell.conversion_rate_pct }}%</span>
                  <span v-for="(weight, channel) in cell.channels" :key="channel" class="matrix-chip">
                    {{ channel }} {{ weight }}
                  </span>
                </template>
                <span v-else class="matrix-empty">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <h4>转化归因明细</h4>
      <table class="data-table" data-testid="attribution-detail">
        <thead>
          <tr>
            <th>转化</th>
            <th>渠道</th>
            <th>触达周期</th>
            <th>转化周期</th>
            <th>结算周期</th>
            <th>权重</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in result.settlement.conversions" :key="`${row.conversion_id}-${row.channel}-${index}`">
            <td>{{ row.conversion_id }}</td>
            <td>{{ row.channel }}</td>
            <td>{{ row.touch_period_label }}</td>
            <td>{{ row.conversion_period_label }}</td>
            <td>{{ row.settlement_period_label }}</td>
            <td>{{ row.weight }}</td>
          </tr>
          <tr v-if="!result.settlement.conversions.length">
            <td colspan="6" class="empty-state">窗口内没有可归因的转化</td>
          </tr>
        </tbody>
      </table>

      <h4>无效触点与未归因转化</h4>
      <table class="data-table" data-testid="attribution-exclusions">
        <thead>
          <tr><th>对象</th><th>渠道 / 原因</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in result.settlement.excluded_touchpoints" :key="`ex-${index}`">
            <td>{{ row.conversion_id }}</td>
            <td>{{ row.channel }}</td>
            <td>{{ row.reason }}</td>
          </tr>
          <tr v-for="(row, index) in result.settlement.unattributed" :key="`un-${index}`">
            <td>{{ row.conversion_id }}</td>
            <td>{{ row.user_id }}</td>
            <td>{{ row.reason }}</td>
          </tr>
        </tbody>
      </table>

      <h4>渠道贡献</h4>
      <table class="data-table" data-testid="channel-contributions">
        <thead>
          <tr><th>渠道</th><th>累计权重</th><th>占比</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in result.cohort.channel_contributions" :key="row.channel">
            <td>{{ row.channel }}</td>
            <td>{{ row.total_weight }}</td>
            <td>{{ row.share_pct }}%</td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-if="bills.length">
      <h4>结算账单（随账单保留模型条件）</h4>
      <table class="data-table" data-testid="attribution-bills">
        <thead>
          <tr><th>账单</th><th>模型</th><th>模型条件</th><th>权重合计</th></tr>
        </thead>
        <tbody>
          <tr v-for="bill in bills" :key="bill.bill_id">
            <td>{{ bill.bill_id }}</td>
            <td>{{ bill.model_name }}</td>
            <td>
              {{ bill.model_conditions.model }} / 窗口 {{ bill.model_conditions.window_days }} 天 /
              {{ bill.model_conditions.settle_basis }}
            </td>
            <td>{{ bill.totals.attributed_weight }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  loadAttributionBills,
  loadAttributionCaliber,
  settleAttribution,
  type AttributionBill,
  type AttributionCaliber,
  type AttributionSettlementResponse,
} from '@/api/attribution'

const modelOptions = ref<{ value: number; label: string }[]>([])
const modelWindows = ref<Record<number, number | null>>({})

const form = reactive({
  modelId: 1,
  windowDays: 30,
  settleBasis: 'return_week' as 'touch_week' | 'return_week',
  periodDays: 7,
})

const result = ref<AttributionSettlementResponse | null>(null)
const bills = ref<AttributionBill[]>([])
const loading = ref(false)
const errorMessage = ref('')

const basisLabel = computed(() =>
  result.value?.cohort.periods.settlement_basis_label ?? '运营按回访周复盘',
)
const displayWindow = computed(() => {
  const windowDays = result.value?.reconcile.model_conditions.window_days
  return windowDays === null || windowDays === undefined ? '不限' : `${windowDays} 天`
})

async function runSettlement() {
  loading.value = true
  errorMessage.value = ''
  try {
    result.value = await settleAttribution({
      model_id: form.modelId,
      config_id: 1,
      model_overrides: {
        window_days: form.windowDays,
        settle_basis: form.settleBasis,
        period_days: form.periodDays,
      },
    })
    if (!result.value.ok) throw new Error(result.value.message ?? '结算未返回结果')
    await reloadBills()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结算失败'
  } finally {
    loading.value = false
  }
}

async function reloadBills() {
  try {
    const payload = await loadAttributionBills(10)
    bills.value = payload.bills
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '账单读取失败'
  }
}

async function loadModels() {
  try {
    const payload = await listEntries('attribution_model')
    const options: { value: number; label: string }[] = []
    for (const row of payload.items) {
      const id = Number(row.id)
      const windowText = String(row['归因窗口'] ?? '不限')
      options.push({ value: id, label: `${id} · ${String(row['模型名称'] ?? '')}（${windowText}）` })
      const caliber: AttributionCaliber = (
        await loadAttributionCaliber(id)
      ).caliber
      modelWindows.value[id] = caliber.window_days
    }
    modelOptions.value = options
    if (options.length && !options.some((option) => option.value === form.modelId)) {
      form.modelId = options[0].value
    }
    syncWindowFromModel()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归因模型读取失败'
  }
}

function syncWindowFromModel() {
  const windowDays = modelWindows.value[form.modelId]
  if (windowDays !== null && windowDays !== undefined) {
    form.windowDays = windowDays
  }
}

onMounted(async () => {
  await loadModels()
  await runSettlement()
})
</script>

<style scoped>
.attribution-settlement { margin-top: 24px; border-top: 1px solid var(--border); padding-top: 16px; }
.caliber-line { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; font-size: 12px; color: var(--muted); }
.matrix-wrap { overflow-x: auto; }
.matrix-sub { display: block; color: var(--muted); font-size: 11px; }
.matrix-chip { display: block; font-size: 11px; color: var(--brand); }
.matrix-empty { color: var(--muted); }
</style>
