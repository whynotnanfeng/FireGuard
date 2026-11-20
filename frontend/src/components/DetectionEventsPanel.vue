<template>
  <div class="detection-events-panel">
    <div class="panel-header">
      <h3>检测事件</h3>
      <div class="stats">
        <span class="stat-item">
          <span class="stat-dot"></span>
          活跃目标: {{ activeTracks }}
        </span>
        <span class="stat-item">
          <span class="stat-dot green"></span>
          当前筛选: {{ total }}
        </span>
      </div>
    </div>

    <div class="filter-bar">
      <button
        v-for="opt in filterOptions"
        :key="opt.value"
        class="filter-btn"
        :class="{ active: filter === opt.value }"
        @click="emit('update:filter', opt.value)"
      >
        {{ opt.label }}
      </button>
    </div>

    <div class="event-list">
      <div
        v-for="event in events"
        :key="event.id"
        class="event-item"
        :class="event.event_type"
      >
        <div class="event-icon">
          <span v-if="event.event_type === 'enter'" class="icon-enter">+</span>
          <span v-else-if="event.event_type === 'leave'" class="icon-leave">-</span>
          <span v-else class="icon-update">~</span>
        </div>
        <div class="event-content">
          <div class="event-header">
            <span class="event-class">
              <span class="event-track">#{{ event.track_id }}</span>
              {{ event.class_name }}
            </span>
            <span class="event-confidence">{{ (event.confidence * 100).toFixed(1) }}%</span>
          </div>
          <div class="event-meta">
            <span class="event-time">{{ formatTime(event.entered_at) }}</span>
            <span class="event-state" :class="{ active: event.event_type !== 'leave' }">
              {{ event.event_type === 'leave' ? '结束' : '活跃' }}
            </span>
            <span v-if="event.duration_ms" class="event-duration">
              持续 {{ formatDuration(event.duration_ms) }}
            </span>
          </div>
          <div v-if="event.event_type === 'leave'" class="event-summary">
            最大置信度: {{ (event.max_confidence * 100).toFixed(1) }}% |
            平均置信度: {{ (event.avg_confidence * 100).toFixed(1) }}% |
            更新 {{ event.update_count }} 次
          </div>
        </div>
      </div>

      <div v-if="events.length === 0" class="empty-state">
        <span class="empty-icon">📋</span>
        <span class="empty-text">{{ emptyText }}</span>
      </div>
    </div>

    <div v-if="total > 0" class="panel-footer">
      <span class="pagination-info">第 {{ page }}/{{ totalPages }} 页，共 {{ total }} 条</span>
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
        @update:current="(p: number) => emit('update:page', p)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import CustomPagination from '@/components/CustomPagination.vue'
import type { EventFilter } from '@/composables/useDetectionFeed'

interface DetectionEvent {
  id: string
  track_id: number
  event_type: 'enter' | 'leave' | 'update'
  class_name: string
  confidence: number
  box: number[]
  entered_at: string
  left_at: string | null
  duration_ms: number
  max_confidence: number
  avg_confidence: number
  update_count: number
}

const props = defineProps<{
  taskId: string
  events: DetectionEvent[]
  activeTracks?: number
  total: number
  filter: EventFilter
  page: number
  pageSize: number
}>()

const emit = defineEmits<{
  (e: 'update:filter', value: EventFilter): void
  (e: 'update:page', value: number): void
  (e: 'update:pageSize', value: number): void
}>()

const filterOptions: Array<{ value: EventFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '活跃' },
  { value: 'closed', label: '结束' },
]

const totalPages = computed(() => Math.ceil(props.total / props.pageSize) || 1)

const emptyText = computed(() => {
  if (props.filter === 'active') return '暂无活跃目标'
  if (props.filter === 'closed') return '暂无已结束目标'
  return '暂无检测事件'
})

function onPageSizeChange(size: number) {
  emit('update:pageSize', size)
  emit('update:page', 1)
}

const formatTime = (timeStr: string) => {
  const date = new Date(timeStr)
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

const formatDuration = (ms: number) => {
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = seconds % 60
  return `${minutes}m ${remainingSeconds}s`
}
</script>

<style scoped>
.detection-events-panel {
  /* 面板是视频上的深色浮层，这里把分页组件的主题变量覆写为深色。 */
  --bg-card: rgba(255, 255, 255, 0.06);
  --border-color: rgba(255, 255, 255, 0.16);
  --text-primary: #e0e0e0;
  --text-secondary: #a0a0a0;
  --text-quaternary: rgba(255, 255, 255, 0.3);
  --primary-blue: #4a9eff;

  background: #1a1a2e;
  border-radius: 8px;
  padding: 16px;
  height: 100%;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.panel-header h3 {
  margin: 0;
  color: #e0e0e0;
  font-size: 16px;
  font-weight: 500;
}

.stats {
  display: flex;
  gap: 12px;
}

.stat-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #a0a0a0;
}

.stat-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #ff4757;
  animation: pulse 2s infinite;
}

.stat-dot.green {
  background: #2ed573;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.filter-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.filter-btn {
  flex: 1;
  padding: 6px 0;
  font-size: 13px;
  color: #a0a0a0;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.filter-btn:hover {
  color: #4a9eff;
  border-color: #4a9eff;
}

.filter-btn.active {
  color: #4a9eff;
  border-color: #4a9eff;
  background: rgba(74, 158, 255, 0.12);
}

.event-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}

.event-item {
  display: flex;
  gap: 12px;
  padding: 12px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 6px;
  border-left: 3px solid transparent;
  transition: all 0.2s ease;
}

.event-item:hover {
  background: rgba(255, 255, 255, 0.08);
}

.event-item.enter {
  border-left-color: #2ed573;
}

.event-item.leave {
  border-left-color: #ff4757;
}

.event-item.update {
  border-left-color: #ffa502;
}

.event-icon {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  font-size: 14px;
  font-weight: bold;
}

.icon-enter {
  color: #2ed573;
}

.icon-leave {
  color: #ff4757;
}

.icon-update {
  color: #ffa502;
}

.event-content {
  flex: 1;
  min-width: 0;
}

.event-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.event-class {
  font-weight: 500;
  color: #e0e0e0;
  text-transform: capitalize;
}

.event-track {
  color: #4a9eff;
  font-variant-numeric: tabular-nums;
  margin-right: 4px;
}

.event-confidence {
  font-size: 12px;
  color: #a0a0a0;
}

.event-meta {
  display: flex;
  gap: 12px;
  font-size: 12px;
  color: #808080;
  margin-bottom: 4px;
}

.event-state {
  color: #808080;
}

.event-state.active {
  color: #2ed573;
}

.event-summary {
  font-size: 11px;
  color: #606060;
  line-height: 1.4;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  color: #606060;
}

.empty-icon {
  font-size: 32px;
  margin-bottom: 8px;
}

.empty-text {
  font-size: 14px;
}

.panel-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
}

.pagination-info {
  font-size: 12px;
  color: #a0a0a0;
  white-space: nowrap;
}

.page-size-select {
  width: 92px;
  flex-shrink: 0;
}

.panel-footer :deep(.custom-pagination) {
  flex-wrap: nowrap;
}

.panel-footer :deep(.pagination-options) {
  display: none;
}
</style>
