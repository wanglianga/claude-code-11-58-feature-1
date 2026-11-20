<template>
  <div>
    <el-card>
      <template #header>
        <div class="bar">
          <span>计量不确定度归因与校准优先级
            <el-tooltip content="只读主服务能量事实,不改变能量值或测量边界">
              <el-tag size="small" type="info" style="margin-left:6px">只读归因</el-tag>
            </el-tooltip>
          </span>
          <span class="hint">先选范围与指标,再对比两种传播方法</span>
        </div>
      </template>
      <el-form :inline="true" size="small">
        <el-form-item label="范围">
          <el-select v-model="scope" style="width: 90px" @change="onScopeChange">
            <el-option label="运行" value="RUN" />
            <el-option label="班次" value="SHIFT" />
          </el-select>
        </el-form-item>
        <el-form-item label="对象">
          <el-select v-model="scopeKey" filterable style="width: 190px" @change="load">
            <el-option v-for="k in scopeKeys" :key="k" :label="k" :value="k" />
          </el-select>
        </el-form-item>
        <el-form-item label="指标">
          <el-select v-model="metric" style="width: 190px" @change="load">
            <el-option v-for="m in meta.target_metrics" :key="m.key"
                       :label="m.label" :value="m.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="方法">
          <el-radio-group v-model="method" @change="load">
            <el-radio-button value="INTERVAL">区间传播</el-radio-button>
            <el-radio-button value="JOINT_SAMPLING">联合抽样</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="校准预算">
          <el-input-number v-model="budget" :min="0" :step="1" style="width: 110px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="busy" @click="recompute">按当前输入重算</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-alert v-if="current && current.attributable === 'FALSE'" type="error"
              :closable="false" style="margin-top: 12px"
              title="该指标暂不可归因:关键计量输入缺失或能量值未知,不虚构精确贡献">
      <div v-for="(u, i) in current.unattributable" :key="i" class="unattr">
        <b>{{ u.code }}</b>:{{ u.detail }}
      </div>
    </el-alert>

    <el-card v-if="!current" style="margin-top: 16px">
      <el-empty description="该范围/指标/方法暂无评估,点击下方按当前输入生成">
        <el-button type="primary" :loading="busy" @click="recompute">按当前输入生成评估</el-button>
      </el-empty>
    </el-card>

    <template v-if="current">
      <el-row :gutter="16" style="margin-top: 16px">
        <el-col :span="8">
          <el-card>
            <template #header>范围({{ metricLabel }})</template>
            <div class="big" :class="{ unknown: !rangeOk }">{{ rangeText }}</div>
            <div class="sub">主服务范围:{{ baseText }}</div>
            <div class="sub">时间桶边界宽度:{{ num(current.decomposition.boundary_width) }} kWh
              · 计量扩展宽度:{{ num(current.decomposition.extra_width) }} kWh</div>
            <el-tag :type="current.attributable === 'TRUE' ? 'success' : 'danger'" size="small">
              {{ current.attributable === 'TRUE' ? '可归因' : '不可归因' }}
            </el-tag>
            <el-tag size="small" type="info" style="margin-left:6px">
              {{ UNCERTAINTY_METHOD_LABEL[current.method] }} v{{ current.version }}
            </el-tag>
          </el-card>
        </el-col>
        <el-col :span="8">
          <el-card>
            <template #header>范围分解(宽度 kWh)</template>
            <el-table :data="decompRows" size="small" max-height="240">
              <el-table-column prop="k" label="组成" />
              <el-table-column prop="v" label="宽度" width="90" align="right" />
              <el-table-column prop="note" label="说明" />
            </el-table>
            <div class="sub" style="margin-top:6px">
              可归因 {{ num(catWidth.attributable) }} + 相关来源 {{ num(catWidth.correlated) }}
              + 残余 {{ num(current.decomposition.residual) }} + 边界
              {{ num(current.decomposition.boundary_width) }}
              = {{ num(current.decomposition.identity_width) }} kWh
            </div>
          </el-card>
        </el-col>
        <el-col :span="8">
          <el-card>
            <template #header>方法选择建议</template>
            <div class="sub">共享计量关系占比 {{ (guidance.shared_fraction * 100).toFixed(0) }}%</div>
            <ul class="tips">
              <li v-for="(r, i) in guidance.recommendations" :key="i">{{ r }}</li>
            </ul>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="16" style="margin-top: 16px">
        <el-col :span="12">
          <el-card>
            <template #header>逐来源贡献(可归因 &amp; 相关)</template>
            <el-table :data="contributionRows" size="small" max-height="320">
              <el-table-column label="输入" min-width="140">
                <template #default="{ row }">{{ row.input_key }}</template>
              </el-table-column>
              <el-table-column label="来源" width="120">
                <template #default="{ row }">{{ row.source_label }}</template>
              </el-table-column>
              <el-table-column label="类型" width="80">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.shared ? 'warning' : 'success'">
                    {{ row.shared ? '相关' : '独立' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="组" prop="group" width="130" />
              <el-table-column label="贡献宽度" width="100" align="right">
                <template #default="{ row }">{{ num(row.width ?? row.group_width) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
        <el-col :span="12">
          <el-card>
            <template #header>相关来源分组(共享变电所表/同表多量,不得当独立误差抵消)</template>
            <el-table :data="current.correlated_groups" size="small" max-height="320">
              <el-table-column prop="group" label="共享组" width="140" />
              <el-table-column prop="source_label" label="来源" width="120" />
              <el-table-column label="成员(共同影响)" min-width="180">
                <template #default="{ row }">{{ row.members.join('、') }}</template>
              </el-table-column>
              <el-table-column label="组宽度" width="90" align="right">
                <template #default="{ row }">{{ num(row.width) }}</template>
              </el-table-column>
            </el-table>
            <el-empty v-if="!current.correlated_groups.length"
                      description="无共享来源(各输入独立)" :image-size="50" />
          </el-card>
        </el-col>
      </el-row>

      <el-card style="margin-top: 16px">
        <template #header>
          <div class="bar">
            <span>校准优先级(预算 {{ current.candidates.budget }} ·
              当前宽度 {{ num(current.candidates.current_width) }} →
              优化后 {{ num(current.candidates.final_width) }})</span>
            <span class="hint">已选 {{ current.candidates.selected.length }} 项 ·
              剩余预算 {{ num(current.candidates.remaining_budget) }}</span>
          </div>
        </template>
        <el-table :data="current.candidates.evaluated" size="small" max-height="360">
          <el-table-column label="采用" width="60" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.selected" type="success" size="small">✓</el-tag>
              <span v-else class="dash">—</span>
            </template>
          </el-table-column>
          <el-table-column label="行动" width="130">
            <template #default="{ row }">{{ ACTION_LABEL[row.action] || row.action }}</template>
          </el-table-column>
          <el-table-column prop="device" label="设备" width="110" />
          <el-table-column prop="label" label="说明" min-width="200" />
          <el-table-column label="成本" width="70" align="right">
            <template #default="{ row }">{{ num(row.cost) }}</template>
          </el-table-column>
          <el-table-column label="独立收益" width="100" align="right">
            <template #default="{ row }">
              {{ row.restores_attribution ? '恢复可归因' : num(row.benefit) }}
            </template>
          </el-table-column>
          <el-table-column label="收益/成本" width="100" align="right">
            <template #default="{ row }">
              {{ row.restores_attribution ? '—' : num(row.benefit_per_cost) }}
            </template>
          </el-table-column>
          <el-table-column label="贪心边际收益" width="110" align="right">
            <template #default="{ row }">
              {{ row.selected ? num(row.marginal_benefit) : '—' }}
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-row :gutter="16" style="margin-top: 16px">
        <el-col :span="14">
          <el-card>
            <template #header>本次评估使用的计量输入(倍率证书/校准/分辨率/时钟/覆盖)</template>
            <el-table :data="current.inputs" size="small" max-height="340">
              <el-table-column prop="input_key" label="输入" width="130" />
              <el-table-column label="版本" width="60">
                <template #default="{ row }">v{{ row.version ?? '—' }}</template>
              </el-table-column>
              <el-table-column label="证书误差" width="80" align="right">
                <template #default="{ row }">
                  {{ row.cert_accuracy === null ? '—' : (row.cert_accuracy * 100).toFixed(2) + '%' }}
                </template>
              </el-table-column>
              <el-table-column label="校准有效期" width="150">
                <template #default="{ row }">{{ row.calib_valid_to || '—' }}</template>
              </el-table-column>
              <el-table-column label="分辨率" width="70" align="right">
                <template #default="{ row }">{{ num(row.resolution) }}</template>
              </el-table-column>
              <el-table-column label="时钟容差" width="90" align="right">
                <template #default="{ row }">{{ num(row.clock_tolerance_s) }}s</template>
              </el-table-column>
              <el-table-column label="覆盖" width="70" align="right">
                <template #default="{ row }">
                  {{ row.coverage_ratio === null ? '—' : (row.coverage_ratio * 100).toFixed(0) + '%' }}
                </template>
              </el-table-column>
              <el-table-column label="状态" width="130">
                <template #default="{ row }">
                  <el-tag size="small" :type="INPUT_STATUS_TAG[row.status]">
                    {{ INPUT_STATUS_LABEL[row.status] }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
        <el-col :span="10">
          <el-card>
            <template #header>历史版本(证书晚到只形成新范围版本,旧版保留)</template>
            <el-timeline v-if="history.length">
              <el-timeline-item v-for="h in history" :key="h.id"
                                :timestamp="h.created_at" :type="h.version === 1 ? 'info' : 'warning'">
                <b>v{{ h.version }}</b> · {{ h.note }}
                <div class="sub">
                  范围宽度 {{ widthOf(h) }} · 可归因 {{ h.attributable }}
                  <span v-if="h.supersedes_id">(覆盖 #{{ h.supersedes_id }})</span>
                </div>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-else description="无历史版本" :image-size="50" />
          </el-card>
        </el-col>
      </el-row>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../api'
import {
  ACTION_LABEL, INPUT_STATUS_LABEL, INPUT_STATUS_TAG, UNCERTAINTY_METHOD_LABEL, fmtVal,
} from '../utils'

const meta = ref({ target_metrics: [] })
const runs = ref([])
const scope = ref('RUN')
const scopeKey = ref('RUN-1004')
const metric = ref('RECON_DIFF_KWH')
const method = ref('INTERVAL')
const budget = ref(5)
const current = ref(null)
const history = ref([])
const latest = ref([])
const guidance = ref({ shared_fraction: 0, recommendations: [] })
const busy = ref(false)

const metricLabel = computed(() => (meta.value.target_metrics.find((m) => m.key === metric.value) || {}).label || metric.value)
const rangeOk = computed(() => current.value && current.value.range_band && current.value.range_band.lo !== null)
const rangeText = computed(() => (rangeOk.value
  ? `${current.value.range_band.v} [${current.value.range_band.lo} ~ ${current.value.range_band.hi}]`
  : (current.value ? '不可归因' : '-')))
const baseText = computed(() => {
  if (!current.value || !current.value.base_band || current.value.base_band.lo === null) return '未知'
  const b = current.value.base_band
  return `${b.v} [${b.lo} ~ ${b.hi}]`
})

const decompRows = computed(() => {
  const d = current.value?.decomposition || {}
  const attrW = (d.attributable || []).reduce((s, c) => s + c.width, 0)
  const corrW = (d.correlated || []).reduce((s, c) => s + c.width, 0)
  return [
    { k: '可归因(独立输入)', v: fmtVal(attrW, 3), note: '分辨率等可单列来源' },
    { k: '相关来源共同影响', v: fmtVal(corrW, 3), note: '共享表/同表多量,同向传播' },
    { k: '不可独立拆分残余', v: fmtVal(d.residual ?? 0, 3), note: '方法相关(非线性/交叠)' },
    { k: '时间桶边界', v: fmtVal(d.boundary_width ?? 0, 3), note: '主服务已有覆盖边界' },
  ]
})
const catWidth = computed(() => ({
  attributable: (current.value?.decomposition?.attributable || []).reduce((s, c) => s + c.width, 0),
  correlated: (current.value?.decomposition?.correlated || []).reduce((s, c) => s + c.width, 0),
}))
const contributionRows = computed(() => {
  const seen = new Set()
  const rows = []
  for (const c of current.value?.contributions || []) {
    const k = `${c.input_key}|${c.source_type}`
    if (seen.has(k)) continue
    seen.add(k)
    rows.push(c)
  }
  return rows
})

const scopeKeys = computed(() => {
  if (scope.value === 'RUN') return [...new Set(runs.value.map((r) => r.run_key))].sort()
  return [...new Set(runs.value.map((r) => r.shift))].filter(Boolean).sort()
})

function num(v) {
  if (v === null || v === undefined) return '—'
  return Number(v).toFixed(3).replace(/\.?0+$/, '')
}
function widthOf(h) {
  if (!h.range_band || h.range_band.lo === null) return '不可归因'
  return num(h.range_band.hi - h.range_band.lo)
}
function onScopeChange() {
  scopeKey.value = scopeKeys.value[0]
  load()
}

async function load() {
  if (!scopeKey.value) return
  const q = { scope: scope.value, scope_key: scopeKey.value,
              target_metric: metric.value, method: method.value }
  latest.value = (await api.get('/uncertainty/assessments', { params: q })).data
  // 优先显示最高版本;若无则提示重算
  current.value = latest.value[0] || null
  history.value = [...latest.value].sort((a, b) => a.version - b.version)
  guidance.value = (await api.get('/uncertainty/guidance',
    { params: { scope: scope.value, scope_key: scopeKey.value,
                target_metric: metric.value } })).data
  if (current.value && current.value.candidates) {
    budget.value = current.value.candidates.budget  // 回显该版本使用的预算
  }
}

async function recompute() {
  if (!scopeKey.value) return
  busy.value = true
  try {
    const r = await api.post('/uncertainty/recompute', null, {
      params: { scope: scope.value, scope_key: scopeKey.value,
                target_metric: metric.value, method: method.value,
                budget: budget.value, persist: true },
    })
    current.value = r.data
    await load()
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  meta.value = (await api.get('/uncertainty/meta')).data
  runs.value = (await api.get('/runs')).data
  await load()
})
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.hint { color: #999; font-size: 12px; }
.big { font-size: 22px; font-weight: 600; color: #1c5d8c; }
.big.unknown { color: #c45656; }
.sub { color: #888; font-size: 12px; margin-top: 6px; }
.tips { margin: 6px 0 0; padding-left: 18px; color: #555; font-size: 13px; }
.tips li { margin-bottom: 4px; }
.unattr { font-size: 12px; }
.dash { color: #ccc; }
</style>
