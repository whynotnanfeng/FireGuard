<template>
  <a-tag :color="statusType" class="status-tag">
    {{ statusText }}
  </a-tag>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

const statusMap: Record<string, { type: string; text: string }> = {
  creating:  { type: 'default', text: '创建中' },
  pending:   { type: 'default', text: '待执行' },
  queued:    { type: 'warning', text: '排队中' },
  running:   { type: 'processing', text: '执行中' },
  paused:    { type: 'default', text: '已暂停' },
  completed: { type: 'success', text: '已完成' },
  failed:    { type: 'error', text: '失败' },
}

const statusType = computed(() => statusMap[props.status]?.type || 'default')
const statusText = computed(() => statusMap[props.status]?.text || props.status)
</script>

<style scoped>
.status-tag {
  letter-spacing: 1px;
  font-weight: 600;
  border-radius: 4px;
}
</style>
