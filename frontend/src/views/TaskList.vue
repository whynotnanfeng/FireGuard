<template>
  <div class="task-list-page fade-in">
    <div class="toolbar">
      <div class="filter-group">
        <a-input-search
          v-model:value="filters.search"
          placeholder="搜索任务名称..."
          style="width: 280px"
          allow-clear
          @search="loadData"
          @change="onSearchChange"
        />
        
        <a-select 
          v-model:value="filters.status" 
          placeholder="全部状态" 
          allow-clear 
          @change="loadData" 
          style="width: 140px"
        >
          <a-select-option value="creating">创建中</a-select-option>
          <a-select-option value="pending">待执行</a-select-option>
          <a-select-option value="queued">排队中</a-select-option>
          <a-select-option value="running">执行中</a-select-option>
          <a-select-option value="paused">已暂停</a-select-option>
          <a-select-option value="completed">已完成</a-select-option>
          <a-select-option value="failed">失败</a-select-option>
        </a-select>
        
        <a-button @click="loadData">
           <template #icon><ReloadOutlined /></template>
           刷新
        </a-button>
      </div>
      
      <a-button type="primary" @click="showCreate = true">
         <template #icon><PlusOutlined /></template>
         新建任务
      </a-button>
    </div>

    <a-table 
       :loading="taskStore.loading" 
       :dataSource="taskStore.tasks" 
       :columns="columns"
       :pagination="false"
       rowKey="id"
       class="task-table"
    >
      <template #bodyCell="{ column, record }">
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
            <!-- 查看按钮：running时可查；或曾执行成功(has_history)时可查历史 -->
            <a-button 
               type="link"
               :disabled="!canView(record)"
               @click="handleViewResult(record)"
            >查看</a-button>

            <!-- 执行按钮：仅 pending/exception/failed 可执行（not running） -->
            <a-button 
               type="link" 
               :disabled="!canExecute(record)"
               @click="handleExecute(record)"
            >执行</a-button>
            
            <!-- 停止按钮：仅 running 的 stream 任务 -->
            <a-button 
               type="link"
               :disabled="record.status !== 'running' || record.task_type !== 'stream'"
               @click="handleStop(record)"
            >停止</a-button>

            <!-- 配置按钮：仅 pending 状态（未运行） -->
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
      <a-pagination
        v-model:current="page"
        v-model:pageSize="limit"
        :total="taskStore.total"
        show-less-items
        @change="loadData"
      />
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
       title="任务结果" 
       width="1200px" 
       :footer="null"
       :maskClosable="true"
       :bodyStyle="{ padding: '20px', maxHeight: '85vh', overflowY: 'auto' }"
       @after-close="handleModalAfterClose"
       :destroyOnClose="true"
    >
       <template v-if="currentResultRow && renderReady">
         <ResultViewer v-if="currentResultRow.task_type !== 'stream'" :task-id="currentResultRow.id" :key="'result-' + currentResultRow.id" />
         <VideoPlayer 
            v-if="currentResultRow.task_type === 'stream'"
            :task-id="currentResultRow.id" 
            ws-url="ws://localhost:8000/ws/stream" 
            @close="showResult = false"
            :key="'stream-' + currentResultRow.id"
         />
       </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useTaskStore } from '@/stores/task'
import { tasksApi } from '@/api/tasks'
import TaskStatus from '@/components/TaskStatus.vue'
import TaskCreate from './TaskCreate.vue'
import DetectionConfig from '@/components/DetectionConfig.vue'
import ResultViewer from '@/components/ResultViewer.vue'
import VideoPlayer from '@/components/VideoPlayer.vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons-vue'

const taskStore = useTaskStore()

const page = ref(1)
const limit = ref(10)
const filters = reactive({ 
  status: null,
  search: ''
})

const showCreate = ref(false)
const showConfig = ref(false)
const configRow = ref<any>(null)
const showResult = ref(false)
const currentResultRow = ref<any>(null)
const renderReady = ref(false)

// V1.4.20: Immediate Unmount Strategy
// When showResult becomes false, we kill renderReady IMMEDIATELY.
// This triggers v-if unmount of VideoPlayer BEFORE the modal animation finishes, 
// ensuring WebSocket/Canvas processing stops and frees the main thread for mask removal.
watch(showResult, (val) => {
  if (!val) renderReady.value = false
})

const typeMap: any = { image: '图片', video: '视频', stream: '流媒体' }

const columns = [
  { title: '任务名称', dataIndex: 'name', key: 'name' },
  { title: '类型', key: 'task_type', width: 100 },
  { title: '模型', dataIndex: 'model_name', key: 'model_name' },
  { title: '状态', key: 'status', width: 120 },
  { title: '创建时间', key: 'created_at', width: 180 },
  { title: '操作', key: 'action', width: 260, fixed: 'right' }
]

function formatDate(ds: string) {
  if (!ds) return ''
  return new Date(ds).toLocaleString()
}

/**
 * V12: 查看按钮控制
 * - stream任务: running时可看实时流；has_history=true时可看历史记录（即使已暂停）
 * - 非stream任务: 仅 completed 可查看
 */
function canView(record: any): boolean {
  if (record.task_type === 'stream') {
    return record.status === 'running' || record.has_history === true
  }
  return record.status === 'completed'
}

/**
 * V12: 执行按钮控制
 * - 仅 pending/exception/failed 可执行（running 时不允许重复执行）
 */
function canExecute(record: any): boolean {
  return ['pending', 'exception', 'failed'].includes(record.status)
}

let debounceTimer: any = null
async function loadData() {
  // Clear any pending requests
  if (debounceTimer) clearTimeout(debounceTimer)
  
  // Schedule a new request after 300ms of quiet
  debounceTimer = setTimeout(async () => {
    try {
      await taskStore.fetchTasks({
        skip: (page.value - 1) * limit.value,
        limit: limit.value,
        status: filters.status || undefined,
        search: filters.search || undefined
      })
    } catch (e) {
      console.error('Failed to reload task list', e)
    } finally {
      debounceTimer = null
    }
  }, 300)
}

function onSearchChange() {
  // Reset to first page on search
  page.value = 1
  loadData()
}

let notificationWs: WebSocket | null = null
let pollTimer: any = null

onMounted(() => {
  loadData()
  startNotifications()
  
  // V18: Defensive polling heartbeat for final consistency
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
    const url = `${protocol}//${window.location.hostname}:8000/ws/notifications`
    notificationWs = new WebSocket(url)
    
    notificationWs.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data)
            console.log('Task Status Update Received:', data)
            if (data.type === 'task_status_update') {
                loadData()
            }
        } catch(e) {}
    }
    
    notificationWs.onclose = () => {
        // Retry connection after 5 seconds if lost
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
    message.success('任务已加入执行队列')
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
  // V1.4.15: Two-stage activation to ensure Modal frame is ready before components mount
  currentResultRow.value = row
  showResult.value = true
  await nextTick()
  renderReady.value = true
}

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
  gap: 20px;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  background: var(--bg-card);
  padding: 16px;
  border-radius: 8px;
  border: 1px solid var(--border-color);
}

.filter-group {
  display: flex;
  gap: 12px;
}

.pagination-wrap {
  display: flex;
  justify-content: flex-end;
  margin-top: 20px;
}

.type-badge {
  background: rgba(9, 30, 66, 0.04);
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
