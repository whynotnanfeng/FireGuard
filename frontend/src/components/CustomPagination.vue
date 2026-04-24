<template>
  <div class="custom-pagination-container">
    <ul class="custom-pagination">
      <!-- 上一页 -->
      <li 
        class="pagination-item pagination-prev" 
        :class="{ 'pagination-disabled': current <= 1 }"
        @click="current > 1 && jump(current - 1)"
      >
        <LeftOutlined />
      </li>

      <!-- 左侧省略号 -->
      <li v-if="paginationWindow.start > 1" class="pagination-ellipsis">
        <span>...</span>
      </li>

      <!-- 页码列表 (严格限制最多4个) -->
      <li 
        v-for="p in visiblePages" 
        :key="p"
        class="pagination-item"
        :class="{ 'pagination-item-active': p === current }"
        @click="jump(p)"
      >
        <span>{{ p }}</span>
      </li>

      <!-- 右侧省略号 -->
      <li v-if="paginationWindow.end < totalPages" class="pagination-ellipsis">
        <span>...</span>
      </li>

      <!-- 下一页 -->
      <li 
        class="pagination-item pagination-next" 
        :class="{ 'pagination-disabled': current >= totalPages }"
        @click="current < totalPages && jump(current + 1)"
      >
        <RightOutlined />
      </li>

      <!-- 快速跳转 -->
      <li class="pagination-options">
        <div class="pagination-options-quick-jumper">
          跳至
          <input type="text" v-model="jumpInput" @keydown.enter="handleJump" />
          页
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

// 确保输入框显示当前页码（可选，或者保持为空）
watch(() => props.current, () => {
  jumpInput.value = ''
})

// 滑动窗口逻辑：计算 4 页长度的“可见窗口”
const paginationWindow = computed(() => {
  const total = totalPages.value
  const current = props.current
  
  if (total <= 4) {
    return { start: 1, end: total }
  }
  
  // 按照您的规则：以当前页为中心偏移（左1右2）
  let start = current - 1
  let end = current + 2
  
  // 边界修正
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

// 最终渲染的数字页码数组
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

/* 适配暗色模式变量，如果项目中未定义则使用备选值 */
:root {
  --primary-blue: #1677ff;
  --border-color: #d9d9d9;
  --text-primary: rgba(0, 0, 0, 0.88);
  --text-secondary: rgba(0, 0, 0, 0.45);
  --bg-card: #ffffff;
}

/* 假设项目中已有变量 */
[data-theme='dark'] .pagination-item {
  background-color: transparent;
}
</style>
