<template>
  <div class="info-section">
    <div class="section-header"><span class="section-title"><span class="icon">◈</span> 检测概览</span></div>
    <div class="stats-grid">
      <div class="stat-card">
        <div class="value">{{ currentBoxCount }}</div>
        <div class="label">{{ targetWord }}</div>
      </div>
      <div class="stat-card success">
        <div class="value">{{ totalRecords }}</div>
        <div class="label">{{ totalWord }}</div>
      </div>
    </div>
  </div>

  <div class="info-section" v-if="Object.keys(currentCategoryCounts).length > 0">
    <div class="section-header">
      <span class="section-title"><span class="icon">◈</span> 当前{{ frameWord }}类别</span>
      <span class="section-badge">{{ Object.keys(currentCategoryCounts).length }} 类</span>
    </div>
    <div class="tag-cloud">
      <span v-for="(count, cls) in currentCategoryCounts" :key="cls" class="tag-item">
        <span class="tag-label">{{ cls }}</span><span class="tag-count">{{ count }}</span>
      </span>
    </div>
  </div>

  <div class="info-section" v-if="Object.keys(categoryDistribution).length > 0">
    <div class="section-header"><span class="section-title"><span class="icon">◈</span> 累计类别分布</span></div>
    <div class="category-list">
      <div v-for="(count, cls) in categoryDistribution" :key="cls" class="category-item">
        <span class="category-name">{{ cls }}</span>
        <div class="category-bar-bg"><div class="category-bar" :style="{ width: (count / maxCategoryCount * 100) + '%' }"></div></div>
        <span class="category-count">{{ count }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  kind: string
  currentBoxCount: number
  totalRecords: number
  currentCategoryCounts: Record<string, number>
  categoryDistribution: Record<string, number>
  maxCategoryCount: number
}>()

const isStill = computed(() => props.kind === 'image' || props.kind === 'video')
const targetWord = computed(() => (isStill.value ? '当前目标' : '当前帧框'))
const totalWord = computed(() => (isStill.value ? '累计目标' : '累计帧框'))
const frameWord = computed(() => (isStill.value ? '目标' : '帧'))
</script>

<style scoped>
.info-section {
  padding: 16px 18px;
  border-bottom: 1px solid var(--border-light);
}

.info-section:last-child { border-bottom: none; }

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 6px;
}

.section-title .icon {
  width: 16px;
  height: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--primary);
}

.section-badge {
  font-size: 10px;
  padding: 2px 6px;
  background: var(--primary-light);
  color: var(--primary);
  border-radius: 10px;
  font-weight: 600;
}

.stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.stat-card {
  background: var(--bg-secondary);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm);
  padding: 12px;
  text-align: center;
}

.stat-card .value {
  font-size: 22px;
  font-weight: 700;
  color: var(--primary);
  line-height: 1;
  margin-bottom: 4px;
}

.stat-card .label { font-size: 11px; color: var(--text-muted); }
.stat-card.success .value { color: var(--success); }

.category-list { display: flex; flex-direction: column; gap: 8px; }

.category-item { display: flex; align-items: center; gap: 10px; }

.category-name {
  width: 60px;
  font-size: 12px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.category-bar-bg {
  flex: 1;
  height: 6px;
  background: var(--bg-hover);
  border-radius: 3px;
  overflow: hidden;
}

.category-bar {
  height: 100%;
  background: var(--primary);
  border-radius: 3px;
  transition: width 0.4s ease;
}

.category-count {
  width: 28px;
  font-size: 11px;
  color: var(--text-muted);
  text-align: right;
  font-family: var(--font-mono);
}

.tag-cloud { display: flex; flex-wrap: wrap; gap: 6px; }

.tag-item {
  display: inline-flex;
  align-items: center;
  background: var(--bg-secondary);
  border: 1px solid var(--border-light);
  border-radius: 5px;
  overflow: hidden;
  font-size: 11px;
}

.tag-label {
  padding: 3px 8px;
  background: rgba(22, 119, 255, 0.08);
  color: var(--primary);
  font-weight: 600;
}

.tag-count {
  padding: 3px 8px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}
</style>
