<template>
  <div class="custom-pagination-container">
    <ul class="custom-pagination">
      <!-- Previous page -->
      <li 
        class="pagination-item pagination-prev" 
        :class="{ 'pagination-disabled': current <= 1 }"
        @click="current > 1 && jump(current - 1)"
      >
        <LeftOutlined />
      </li>

      <!-- Left ellipsis -->
      <li v-if="paginationWindow.start > 1" class="pagination-ellipsis">
        <span>...</span>
      </li>

      <!-- Page numbers (strictly capped at 4) -->
      <li 
        v-for="p in visiblePages" 
        :key="p"
        class="pagination-item"
        :class="{ 'pagination-item-active': p === current }"
        @click="jump(p)"
      >
        <span>{{ p }}</span>
      </li>

      <!-- Right ellipsis -->
      <li v-if="paginationWindow.end < totalPages" class="pagination-ellipsis">
        <span>...</span>
      </li>

      <!-- Next page -->
      <li 
        class="pagination-item pagination-next" 
        :class="{ 'pagination-disabled': current >= totalPages }"
        @click="current < totalPages && jump(current + 1)"
      >
        <RightOutlined />
      </li>

      <!-- Quick jump -->
      <li class="pagination-options">
        <div class="pagination-options-quick-jumper">
          Go to
          <input type="text" v-model="jumpInput" @keydown.enter="handleJump" />
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { LeftOutlined, RightOutlined } from '@ant-design/icons-vue'

const props = defineProps<{
  current: number
  pageSize: number
  total: number
}>()

const emit = defineEmits(['update:current', 'change'])

const totalPages = computed(() => Math.ceil(props.total / props.pageSize) || 1)
const jumpInput = ref('')

watch(() => props.current, () => {
  jumpInput.value = ''
})

// Sliding window logic: compute a 4-page-long "visible window"
const paginationWindow = computed(() => {
  const total = totalPages.value
  const current = props.current
  
  if (total <= 4) {
    return { start: 1, end: total }
  }
  
  // Following your rule: centre on the current page with an offset (1 left, 2 right)
  let start = current - 1
  let end = current + 2
  
  if (start < 1) {
    start = 1
    end = 4
  }
  if (end > total) {
    end = total
    start = total - 3
  }
  
  return { start, end }
})

const visiblePages = computed(() => {
  const { start, end } = paginationWindow.value
  const pages = []
  for (let i = start; i <= end; i++) {
    pages.push(i)
  }
  return pages
})

const jump = (page: number) => {
  if (page < 1 || page > totalPages.value || page === props.current) return
  emit('update:current', page)
  emit('change', page, props.pageSize)
}

const handleJump = (e: any) => {
  const val = parseInt(jumpInput.value)
  if (!isNaN(val) && val >= 1 && val <= totalPages.value) {
    jump(val)
  }
  jumpInput.value = ''
}
</script>

<style scoped>
.custom-pagination-container {
  display: flex;
  align-items: center;
  user-select: none;
}

.custom-pagination {
  display: flex;
  flex-wrap: wrap;
  row-gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.pagination-item {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 32px;
  height: 32px;
  margin-right: 8px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  line-height: 30px;
  text-align: center;
  vertical-align: middle;
  background-color: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.3s;
  color: var(--text-primary);
  font-size: 14px;
}

.pagination-item:hover {
  border-color: var(--primary-blue);
  color: var(--primary-blue);
}

.pagination-item-active {
  font-weight: 600;
  background-color: transparent;
  border-color: var(--primary-blue);
  color: var(--primary-blue);
}

.pagination-disabled {
  color: var(--text-quaternary, rgba(0, 0, 0, 0.25));
  background-color: var(--bg-card);
  border-color: var(--border-color);
  cursor: not-allowed;
}

.pagination-disabled:hover {
  border-color: var(--border-color);
  color: var(--text-quaternary, rgba(0, 0, 0, 0.25));
}

.pagination-ellipsis {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 32px;
  height: 32px;
  margin-right: 8px;
  color: var(--text-secondary);
  font-family: Arial, Helvetica, sans-serif;
  letter-spacing: 2px;
}

.pagination-options {
  display: inline-flex;
  align-items: center;
  margin-left: 16px;
}

.pagination-options-quick-jumper {
  display: flex;
  align-items: center;
  height: 32px;
  line-height: 32px;
  color: var(--text-secondary);
  font-size: 14px;
}

.pagination-options-quick-jumper input {
  width: 50px;
  height: 32px;
  margin: 0 8px;
  padding: 4px 11px;
  background-color: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  transition: all 0.3s;
  color: var(--text-primary);
  outline: none;
}

.pagination-options-quick-jumper input:hover,
.pagination-options-quick-jumper input:focus {
  border-color: var(--primary-blue);
}

/* Dark theme variable fallbacks, in case the project does not define them */
:root {
  --primary-blue: #1677ff;
  --border-color: #d9d9d9;
  --text-primary: rgba(0, 0, 0, 0.88);
  --text-secondary: rgba(0, 0, 0, 0.45);
  --bg-card: #ffffff;
}

/* Assumes the project already defines these variables */
[data-theme='dark'] .pagination-item {
  background-color: transparent;
}
</style>
