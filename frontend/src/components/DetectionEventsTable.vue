<template>
  <div class="info-section stream-records-section">
    <div class="record-filter-bar">
      <button
        v-for="opt in filterOptions"
        :key="opt.value"
        class="record-filter-btn"
        :class="{ active: filter === opt.value }"
        @click="emit('update:filter', opt.value)"
      >
        {{ opt.label }}
      </button>
      <span class="record-summary">活跃 {{ activeCount }} / 共 {{ totalCount }}</span>
    </div>
    <div class="table-scroll">
      <table class="detection-table" v-if="events.length > 0">
        <thead>
          <tr><th>目标</th><th>出现</th><th>消失</th><th>时长</th><th>最大置信度</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="rec in events" :key="rec.id || rec.track_id">
            <td>
              <span class="track-id">
                <span class="track-id-main">#{{ rec.track_id }}</span>
                <span class="track-label">{{ rec.class_name }}</span>
              </span>
            </td>
            <td>
              <span class="time-cell">
                <span class="date-part">{{ fmtDate(rec.entered_at) }}</span>
                <span class="time-part">{{ fmtTime(rec.entered_at) }}</span>
              </span>
            </td>
            <td>
              <span class="time-cell" v-if="rec.left_at">
                <span class="date-part">{{ fmtDate(rec.left_at) }}</span>
                <span class="time-part">{{ fmtTime(rec.left_at) }}</span>
              </span>
              <span v-else>-</span>
            </td>
            <td class="duration-cell">{{ fmtDuration(durationAt(rec), statusAt(rec) === 'enter') }}</td>
            <td class="confidence-cell">{{ fmtConfidence(rec.max_confidence || rec.confidence) }}</td>
            <td><span class="status-badge" :class="statusAt(rec) === 'leave' ? 'completed' : 'active'">{{ statusAt(rec) === 'leave' ? '结束' : '活跃' }}</span></td>
          </tr>
        </tbody>
      </table>
      <div v-else class="record-empty">当前筛选暂无目标</div>
    </div>
    <div class="table-pagination" v-if="total > 0">
      <span class="page-info">第 {{ page }}/{{ totalPages }} 页，共 {{ total }} 条</span>
      <a-select
        :value="pageSize"
        size="small"
        class="page-size-select"
        @change="onPageSizeChange"
      >
        <a-select-option :value="10">10 条/页</a-select-option>
        <a-select-option :value="20">20 条/页</a-select-option>
        <a-select-option :value="50">50 条/页</a-select-option>
      </a-select>
      <CustomPagination
        :current="page"
        :page-size="pageSize"
        :total="total"
        @update:current="onPageChange"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { DetectionEvent } from '@/api/tasks'
import type { EventFilter } from '@/composables/useDetectionFeed'
import CustomPagination from '@/components/CustomPagination.vue'

const props = defineProps<{
  events: DetectionEvent[]
  total: number
  filter: EventFilter
  page: number
  pageSize: number
  activeCount: number
  totalCount: number
  /** 历史回放游标（epoch ms）。给出时按该时刻还原每个目标的状态与时长。 */
  atTime?: number
}>()

const emit = defineEmits<{
  'update:filter': [value: EventFilter]
  'update:page': [value: number]
  'update:pageSize': [value: number]
}>()

const filterOptions: Array<{ value: EventFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '活跃' },
  { value: 'closed', label: '结束' },
]

const totalPages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))

// 库里"一目标一行"：离开时把同一行改成 leave。回放时必须用 left_at 与
// 游标比较，否则会把"后来才离开"的目标显示成当时就已结束。
function statusAt(rec: DetectionEvent): 'enter' | 'leave' {
  if (!props.atTime) return rec.event_type === 'leave' ? 'leave' : 'enter'
  const left = rec.left_at ? parseTs(rec.left_at) : 0
  return left > 0 && left <= props.atTime ? 'leave' : 'enter'
}

function durationAt(rec: DetectionEvent): number {
  const entered = parseTs(rec.entered_at)
  if (!entered) return rec.duration_ms || 0
  if (!props.atTime) return rec.duration_ms || 0
  const left = rec.left_at ? parseTs(rec.left_at) : 0
  const end = left > 0 ? Math.min(left, props.atTime) : props.atTime
  return Math.max(0, end - entered)
}

// 后端返回的是不带时区的北京时间，new Date() 会按本地时区解析，通常即为 +08:00。
function parseTs(value: string | null | undefined): number {
  if (!value) return 0
  const t = new Date(value).getTime()
  return Number.isFinite(t) ? t : 0
}

function onPageSizeChange(size: number) {
  emit('update:pageSize', size)
}

function onPageChange(page: number) {
  emit('update:page', page)
}

function formatDuration(seconds: number, isActive = false): string {
  const suffix = isActive ? '+' : ''
  const secs = Math.floor(seconds)
  if (secs < 60) return `${secs}s${suffix}`
  if (secs < 3600) {
    const m = Math.floor(secs / 60)
    const s = secs % 60
    return s > 0 ? `${m}m ${s}s${suffix}` : `${m}m${suffix}`
  }
  if (secs < 86400) {
    const h = Math.floor(secs / 3600)
    const m = Math.floor((secs % 3600) / 60)
    return m > 0 ? `${h}h ${m}m${suffix}` : `${h}h${suffix}`
  }
  const d = Math.floor(secs / 86400)
  const h = Math.floor((secs % 86400) / 3600)
  return h > 0 ? `${d}d ${h}h${suffix}` : `${d}d${suffix}`
}

function fmtDate(dateStr: string | null | undefined): string {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  const year = d.getFullYear()
  const month = (d.getMonth() + 1).toString().padStart(2, '0')
  const day = d.getDate().toString().padStart(2, '0')
  return `${year}-${month}-${day}`
}

function fmtTime(dateStr: string | null | undefined): string {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  const h = d.getHours().toString().padStart(2, '0')
  const m = d.getMinutes().toString().padStart(2, '0')
  const s = d.getSeconds().toString().padStart(2, '0')
  return `${h}:${m}:${s}`
}

function fmtDuration(msOrSec: number, isActive = false): string {
  let seconds = msOrSec
  if (msOrSec > 1000) {
    seconds = msOrSec / 1000
  }
  return formatDuration(seconds, isActive)
}

function fmtConfidence(conf: number | null | undefined): string {
  if (!conf) return '—'
  return `${(conf * 100).toFixed(1)}%`
}
</script>

<style scoped>
.stream-records-section {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.table-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.detection-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}

.detection-table th {
  text-align: left;
  padding: 6px 8px;
  background: var(--bg-secondary);
  color: var(--text-muted);
  font-weight: 600;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
}

.detection-table td {
  padding: 8px;
  border-bottom: 1px solid var(--border-light);
  color: var(--text-secondary);
  font-family: var(--font-mono);
  vertical-align: middle;
  white-space: nowrap;
}

.detection-table td:first-child {
  white-space: normal;
  font-family: var(--font-body);
}

.detection-table tbody tr { cursor: default; transition: background 0.15s; }
.detection-table tbody tr:hover { background: var(--bg-hover); }

.track-id {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  line-height: 1.3;
}

.track-id-main {
  font-size: 13px;
  color: var(--text-muted);
}

.track-label {
  font-size: 11px;
  color: var(--text-primary);
}

.status-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  font-family: var(--font-body);
}

.status-badge.completed { background: var(--success-light); color: var(--success); }
.status-badge.active { background: var(--primary-light); color: var(--primary); }

.duration-cell { font-weight: 600; color: var(--text-primary); }
.confidence-cell { font-weight: 600; color: var(--primary); }
.time-cell {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  line-height: 1.3;
}
.time-cell .date-part {
  color: var(--text-muted);
  font-size: 10px;
}
.time-cell .time-part {
  font-size: 12px;
  color: var(--text-primary);
}

.table-pagination {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 10px;
  padding: 10px 0 0;
}

.page-info {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.page-size-select {
  width: 92px;
  flex-shrink: 0;
}

.record-filter-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.record-filter-btn {
  padding: 4px 12px;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
}

.record-filter-btn:hover {
  border-color: var(--primary);
  color: var(--primary);
}

.record-filter-btn.active {
  border-color: var(--primary);
  color: var(--primary);
  background: rgba(24, 144, 255, 0.08);
}

.record-summary {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-tertiary);
}

.record-empty {
  padding: 24px 0;
  text-align: center;
  font-size: 12px;
  color: var(--text-tertiary);
}
</style>
