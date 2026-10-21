<template>
  <a-tag :color="statusType" class="status-tag">
    {{ statusText }}
  </a-tag>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

const statusMap: Record<string, { type: string; text: string }> = {
  creating:     { type: 'default', text: 'Creating' },
  pending:      { type: 'default', text: 'Pending' },
  queued:       { type: 'warning', text: 'Queued' },
  initializing: { type: 'processing', text: 'Initializing' },
  running:      { type: 'processing', text: 'Running' },
  completed:    { type: 'success', text: 'Completed' },
  failed:       { type: 'error', text: 'Failed' },
  exception:    { type: 'error', text: 'Exception' },
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
