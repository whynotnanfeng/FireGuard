<template>
  <div class="task-list-page fade-in">
    <div class="toolbar">
      <div class="toolbar-left">
        <span class="filter-label">任务名称</span>
        <a-input-search
          v-model:value="filters.search"
          placeholder="搜索..."
          class="filter-item search-input"
          allow-clear
          @search="applyFilters"
          @change="onSearchChange"
        />
        
        <span class="filter-label">类型</span>
        <a-select 
          v-model:value="filters.task_type" 
          placeholder="全部" 
          allow-clear 
          class="filter-item"
          @change="applyFilters" 
        >
          <a-select-option value="image">图片</a-select-option>
          <a-select-option value="video">视频</a-select-option>
          <a-select-option value="stream">流媒体</a-select-option>
        </a-select>
        
        <span class="filter-label">状态</span>
        <a-select 
          v-model:value="filters.status" 
          placeholder="全部" 
          allow-clear 
          class="filter-item"
          @change="applyFilters" 
        >
          <a-select-option value="creating">创建中</a-select-option>
          <a-select-option value="pending">待执行</a-select-option>
          <a-select-option value="queued">排队中</a-select-option>
          <a-select-option value="initializing">初始化中</a-select-option>
          <a-select-option value="running">执行中</a-select-option>
          <a-select-option value="completed">已完成</a-select-option>
          <a-select-option value="failed">失败</a-select-option>
        </a-select>

        <a-button class="filter-item reset-btn" @click="resetFilters">
           <template #icon><ReloadOutlined /></template>
           重置
        </a-button>

        <a-button 
          type="link" 
          class="expand-toggle"
          @click="filtersExpanded = !filtersExpanded"
        >
          <template #icon>
            <DownOutlined v-if="!filtersExpanded" />
            <UpOutlined v-else />
          </template>
          {{ filtersExpanded ? '收起' : '更多筛选' }}
        </a-button>
      </div>
      
      <a-button type="primary" @click="showCreate = true">
         <template #icon><PlusOutlined /></template>
         新建任务
      </a-button>
    </div>

    <div v-show="filtersExpanded" class="advanced-filters">
      <span class="filter-label">模型</span>
      <a-select 
        v-model:value="filters.model_id" 
        placeholder="全部" 
        allow-clear 
        class="filter-item"
        @change="applyFilters" 
      >
        <a-select-option v-for="m in modelOptions" :key="m.id" :value="m.id">
          {{ m.name }}
        </a-select-option>
      </a-select>

      <span class="filter-label">描述</span>
      <a-input
        v-model:value="filters.description"
        placeholder="关键词..."
        class="filter-item desc-input"
        allow-clear
        @change="applyFilters"
      />

      <span class="filter-label">创建时间</span>
      <a-range-picker
        v-model:value="filters.dateRange"
        class="filter-item date-picker"
        @change="applyFilters"
      />
    </div>

    <div class="table-wrapper">
      <a-table 
         :loading="taskStore.loading" 
         :dataSource="taskStore.tasks" 
         :columns="columns"
         :pagination="false"
         rowKey="id"
         class="task-table"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
             <span :title="record.name">{{ record.name }}</span>
          </template>

          <template v-if="column.key === 'model_name'">
             <span :title="record.model_name">{{ record.model_name }}</span>
          </template>

          <template v-if="column.key === 'description'">
             <span :title="record.description || ''">{{ record.description || '—' }}</span>
          </template>

          <template v-if="column.key === 'task_type'">
             <span class="type-badge">{{ typeMap[record.task_type] || record.task_type }}</span>
          </template>
          
          <template v-if="column.key === 'status'">
             <TaskStatus :status="record.status" />
          </template>

          <template v-if="column.key === 'created_at'">
             {{ formatDate(record.created_at) }}
          </template>

          <template v-if="column.key === 'action'">
            <a-space>
              <a-button 
                 type="link"
                 :disabled="!canView(record)"
                 @click="handleViewResult(record)"
              >查看</a-button>

              <a-button 
                 type="link" 
                 :disabled="!canExecute(record)"
                 @click="handleExecute(record)"
              >执行</a-button>
              
              <a-button 
                 type="link"
                 :disabled="!['running', 'initializing'].includes(record.status) || record.task_type !== 'stream'"
                 @click="handleStop(record)"
              >停止</a-button>

              <a-button 
                 type="link"
                 :disabled="record.status !== 'pending'"
                 @click="handleConfig(record)"
              >配置</a-button>
              
              <a-popconfirm title="确定删除该任务吗？" @confirm="handleDelete(record.id)">
                 <a-button type="link" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </a-table>

      <div class="pagination-wrap" v-if="taskStore.total > 0">
        <span class="pagination-info">
          第 {{ page }}/{{ totalPages }} 页，共 {{ taskStore.total }} 条
        </span>
        <a-select 
          v-model:value="pageSize" 
          size="small" 
          @change="onPageSizeChange" 
          class="page-size-select"
        >
          <a-select-option :value="10">10 条/页</a-select-option>
          <a-select-option :value="20">20 条/页</a-select-option>
          <a-select-option :value="50">50 条/页</a-select-option>
          <a-select-option :value="100">100 条/页</a-select-option>
        </a-select>
        <CustomPagination
          v-model:current="page"
          :pageSize="pageSize"
          :total="taskStore.total"
          @change="onPageChange"
        />
      </div>
    </div>

    <!-- Modals -->
    <TaskCreate v-if="showCreate" @close="showCreate = false" @success="handleCreateSuccess" />
    <DetectionConfig 
       v-if="showConfig" 
       :task-id="configRow?.id || ''" 
       :model-id="configRow?.model_id || ''" 
       @close="showConfig = false" 
       @success="showConfig = false" 
    />
    
    <a-modal
       v-model:open="showResult"
       title=""
       width="1280px"
       :footer="null"
       :maskClosable="true"
       :closable="false"
       :bodyStyle="{ padding: 0, maxHeight: '85vh', overflow: 'hidden' }"
       @after-close="handleModalAfterClose"
       :destroyOnClose="true"
    >
       <template v-if="currentResultRow && renderReady">
         <ResultViewer
            :task-id="currentResultRow.id"
            :task-type="currentResultRow.task_type"
            :ws-url="wsStreamUrl"
            :key="'result-' + currentResultRow.id"
            @close="showResult = false"
         />
       </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useTaskStore } from '@/stores/task'
import { tasksApi } from '@/api/tasks'
import { modelsApi } from '@/api/models'
import TaskStatus from '@/components/TaskStatus.vue'
import TaskCreate from './TaskCreate.vue'
import DetectionConfig from '@/components/DetectionConfig.vue'
import ResultViewer from '@/components/ResultViewer.vue'
import CustomPagination from '@/components/CustomPagination.vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, ReloadOutlined, DownOutlined, UpOutlined } from '@ant-design/icons-vue'
import dayjs, { type Dayjs } from 'dayjs'

const taskStore = useTaskStore()

const page = ref(1)
const pageSize = ref(10)

const totalPages = computed(() => Math.ceil(taskStore.total / pageSize.value) || 1)
const filters = reactive({ 
  status: undefined as string | undefined,
  task_type: undefined as string | undefined,
  model_id: undefined as string | undefined,
  description: '',
  search: '',
  dateRange: undefined as [Dayjs, Dayjs] | undefined
})

const filtersExpanded = ref(false)

const showCreate = ref(false)
const showConfig = ref(false)
const configRow = ref<any>(null)
const showResult = ref(false)
const currentResultRow = ref<any>(null)
const renderReady = ref(false)

watch(showResult, (val) => {
  if (!val) renderReady.value = false
})

const typeMap: any = { image: '图片', video: '视频', stream: '流媒体' }


const columns = [
  { title: '任务名称', dataIndex: 'name', key: 'name', width: 180, ellipsis: true },
  { title: '类型', key: 'task_type', width: 90 },
  { title: '模型', dataIndex: 'model_name', key: 'model_name', width: 160, ellipsis: true },
  { title: '描述', dataIndex: 'description', key: 'description', width: 150, ellipsis: true },
  { title: '状态', key: 'status', width: 100 },
  { title: '创建时间', key: 'created_at', width: 170 },
  { title: '操作', key: 'action', width: 240, fixed: 'right' }
]

function formatDate(ds: string) {
  if (!ds) return ''
  return new Date(ds).toLocaleString()
}

function canView(record: any): boolean {
  if (record.task_type === 'stream') {
    if (record.status === 'running') return true
    if (record.has_history === true && !['initializing'].includes(record.status)) return true
    return false
  }
  return record.status === 'completed'
}

function canExecute(record: any): boolean {
  return ['pending', 'exception', 'failed'].includes(record.status)
}

function onPageChange() {
  loadData()
}

function onPageSizeChange() {
  page.value = 1
  loadData()
}

function applyFilters() {
  page.value = 1
  loadData()
}

function onSearchChange() {
  page.value = 1
}

function resetFilters() {
  filters.search = ''
  filters.status = undefined
  filters.task_type = undefined
  filters.model_id = undefined
  filters.description = ''
  filters.dateRange = undefined
  page.value = 1
  loadData()
}

const modelOptions = ref<any[]>([])

async function loadModelOptions() {
  try {
    const res = await modelsApi.list({ limit: 100 })
    modelOptions.value = res.items
  } catch (e) {
    console.error('Failed to load model options', e)
  }
}

let debounceTimer: any = null
async function loadData() {
  if (debounceTimer) clearTimeout(debounceTimer)
  
  debounceTimer = setTimeout(async () => {
    try {
      await taskStore.fetchTasks({
        skip: (page.value - 1) * pageSize.value,
        limit: pageSize.value,
        status: filters.status || undefined,
        search: filters.search || undefined,
        task_type: filters.task_type || undefined,
        model_id: filters.model_id || undefined,
        description: filters.description || undefined,
        date_from: filters.dateRange ? filters.dateRange[0].toISOString() : undefined,
        date_to: filters.dateRange ? filters.dateRange[1].toISOString() : undefined
      })
    } catch (e) {
      console.error('Failed to reload task list', e)
    } finally {
      debounceTimer = null
    }
  }, 300)
}

let notificationWs: WebSocket | null = null
let pollTimer: any = null

onMounted(() => {
  loadData()
  loadModelOptions()
  startNotifications()
  
  pollTimer = setInterval(() => {
    console.log('[Poll Heartbeat] Syncing task status...')
    loadData()
  }, 30000)
})

onUnmounted(() => {
  stopNotifications()
  if (pollTimer) clearInterval(pollTimer)
})

function startNotifications() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host // 自动包含端口
    const url = `${protocol}//${host}/ws/notifications`
    notificationWs = new WebSocket(url)
    
    let lastNotificationTime = 0
    const NOTIFICATION_THROTTLE_MS = 5000

    notificationWs.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data)
            if (data.type === 'task_status_update') {
                const TASK_STATUSES = ['creating', 'pending', 'queued', 'initializing', 'running', 'completed', 'failed', 'exception']
                const normalizedStatus = data.status === 'error' ? 'exception' : data.status
                const task = taskStore.tasks.find(t => t.id === data.task_id)
                if (task) {
                    if (TASK_STATUSES.includes(normalizedStatus)) {
                        task.status = normalizedStatus
                    }
                    if (data.message) task.error_msg = data.message
                    task.updated_at = new Date().toISOString()
                }
                const now = Date.now()
                if (now - lastNotificationTime >= NOTIFICATION_THROTTLE_MS) {
                    lastNotificationTime = now
                    loadData()
                }
            }
        } catch(e) {}
    }
    
    notificationWs.onclose = () => {
        setTimeout(() => {
            if (!notificationWs || notificationWs.readyState === WebSocket.CLOSED) {
                startNotifications()
            }
        }, 5000)
    }
}

function stopNotifications() {
    if (notificationWs) {
        notificationWs.close()
        notificationWs = null
    }
}

function handleCreateSuccess() {
  showCreate.value = false
  page.value = 1
  loadData()
}

async function handleExecute(row: any) {
  const oldStatus = row.status
  try {
    await tasksApi.execute(row.id)
    message.success('任务已启动，正在连接视频源...')
    loadData()
  } catch(e) {
    row.status = oldStatus
  }
}

async function handleStop(record: any) {
    try {
        await tasksApi.pause(record.id)
        message.success('任务已停止')
        loadData()
    } catch (e) {
        message.error('停止任务失败')
    }
}

async function handleDelete(id: string) {
  try {
    await taskStore.deleteTask(id)
    message.success('删除成功')
    if (taskStore.tasks.length === 0 && page.value > 1) {
       page.value--
       loadData()
    }
  } catch(e) {}
}

async function handleViewResult(row: any) {
  currentResultRow.value = row
  showResult.value = true
  await nextTick()
  renderReady.value = true
}

const wsStreamUrl = computed(() => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  return `${protocol}//${host}/ws/stream`
})

function handleModalAfterClose() {
  currentResultRow.value = null
  renderReady.value = false
}

function handleConfig(row: any) {
  configRow.value = row
  showConfig.value = true
}
</script>

<style scoped>
.task-list-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: calc(100vh - 120px);
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  background: var(--bg-card);
  padding: 12px 16px;
  border-radius: 8px;
  border: 1px solid var(--border-color);
  flex-shrink: 0;
}

.toolbar-left {
  display: flex;
  gap: 16px;
  align-items: center;
  flex: 1;
}

.filter-label {
  font-size: 14px;
  color: var(--text-primary);
  white-space: nowrap;
  font-weight: 500;
}

.filter-item {
  min-width: 120px;
}

.search-input {
  width: 200px;
}

.reset-btn {
}

.expand-toggle {
  padding: 0 4px;
  font-size: 13px;
  color: var(--text-secondary);
}

.expand-toggle:hover {
  color: var(--primary-blue);
}

.advanced-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: center;
  padding: 12px 16px;
  background: var(--bg-card);
  border-radius: 8px;
  border: 1px solid var(--border-color);
  flex-shrink: 0;
}

.date-picker {
  width: 260px;
}

.desc-input {
  width: 200px;
}

.table-wrapper {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.task-table {
  background: var(--bg-card);
  border-radius: 8px;
  box-shadow: var(--shadow-card);
  flex: 1;
}

.pagination-wrap {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 12px;
  padding: 12px 0;
  flex-shrink: 0;
}

.page-size-select {
  width: 95px;
  margin-right: 4px;
}

.pagination-info {
  font-size: 13px;
  color: var(--text-secondary);
}

.type-badge {
  background: rgba(9, 30, 66, 0.04);
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
