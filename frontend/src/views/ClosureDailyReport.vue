<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>结案日报</h2>
        <p class="page-desc">
          只展示依据完整的实验结果；存在未处置差异的轮次会被跳过，财务复算单独列出，不与结案口径重复计数。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="reload-report" type="button" @click="reload">刷新</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">可用实验结果</span>
        <strong class="stat-value">{{ report?.experiments.length ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">财务更正订单</span>
        <strong class="stat-value">{{ report?.corrections.length ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">跳过批次</span>
        <strong class="stat-value">{{ report?.skipped.length ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">日报依据</span>
        <strong class="stat-value">{{ report?.complete ? '完整' : '待补审' }}</strong>
      </article>    </div>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <h3>实验结案口径</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次编号</th><th>实验</th><th>轮次</th><th>停收时间</th>
          <th>分母（入组人数）</th><th>分子（有效购买）</th><th>转化率</th><th>批次状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in report?.experiments ?? []" :key="item.batch_id" :data-testid="`report-${item.batch_id}`">
          <td>{{ item.batch_id }}</td>
          <td>{{ item.experiment_id }}</td>
          <td>{{ item.round_no }}</td>
          <td>{{ item.cutoff_at }}</td>
          <td>{{ item.denominator }}</td>
          <td>{{ item.numerator }}</td>
          <td>{{ item.conversion_rate }}</td>
          <td>{{ item.batch_status }}</td>
        </tr>
        <tr v-if="!(report?.experiments.length)">
          <td colspan="8" class="empty-state">暂无依据完整的实验结果</td>
        </tr>
      </tbody>
    </table>

    <h3>财务复算更正</h3>
    <table class="data-table">
      <thead>
        <tr><th>原订单</th><th>实验</th><th>批次</th><th>轮次</th><th>用户</th><th>处置</th><th>归属</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in report?.corrections ?? []" :key="item.original_order_id">
          <td>{{ item.original_order_id }}</td>
          <td>{{ item.experiment_id }}</td>
          <td>{{ item.batch_id }}</td>
          <td>{{ item.round_no }}</td>
          <td>{{ item.user_id }}</td>
          <td>{{ item.disposition }}</td>
          <td>{{ item.owner }}</td>
        </tr>
        <tr v-if="!(report?.corrections.length)">
          <td colspan="7" class="empty-state">暂无财务更正</td>
        </tr>
      </tbody>
    </table>

    <h3>跳过批次</h3>
    <table class="data-table">
      <thead>
        <tr><th>批次编号</th><th>轮次</th><th>类型</th><th>原因</th><th>未处置订单</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in report?.skipped ?? []" :key="item.batch_id">
          <td>{{ item.batch_id }}</td>
          <td>{{ item.round_no }}</td>
          <td>{{ item.kind === 'incomplete_basis' ? '依据不完整' : '尚未结案' }}</td>
          <td>{{ item.reason }}</td>
          <td>{{ (item.unresolved_order_ids ?? []).join('、') || '—' }}</td>
        </tr>
        <tr v-if="!(report?.skipped.length)">
          <td colspan="5" class="empty-state">全部批次依据完整</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type ReportRow = {
  batch_id: string
  experiment_id: string
  round_no: number
  cutoff_at: string
  denominator: number
  numerator: number
  conversion_rate: number
  batch_status: string
}

type CorrectionRow = {
  original_order_id: string
  experiment_id: string
  batch_id: string
  round_no: number
  user_id: string
  disposition: string
  owner: string
}

type SkippedRow = {
  batch_id: string
  round_no: number
  kind?: string
  reason: string
  unresolved_order_ids?: string[]
}

type DailyReport = {
  report_date: string | null
  experiments: ReportRow[]
  corrections: CorrectionRow[]
  skipped: SkippedRow[]
  complete: boolean
}

const report = ref<DailyReport | null>(null)
const errorMessage = ref('')

async function reload() {
  errorMessage.value = ''
  try {
    report.value = await fetchJson<DailyReport>('/api/experiment-closure/daily-report')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结案日报读取失败'
  }
}

onMounted(reload)
</script>
