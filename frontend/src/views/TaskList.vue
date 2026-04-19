<template>
  <div class="task-list-page fade-in">
    <div class="toolbar">
      <div class="filter-group">
        <a-select 
          v-model:value="filters.status" 
          placeholder="状态" 
          allow-clear 
          @change="loadData" 
          style="width: 120px"
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
            <a-button 
               type="link"
               :disabled="record.status !== 'completed' && !(record.task_type === 'stream' && ['running', 'paused'].includes(record.status))"
               @click="handleViewResult(record)"
            >查看</a-button>

            <a-button 
               type="link" 
               :disabled="record.status !== 'pending' && record.status !== 'paused'"
               @click="handleExecute(record)"
            >执行</a-button>
            
            <a-button 
               type="link"
               :disabled="record.status !== 'running' || record.task_type !== 'stream'"
               @click="handlePause(record)"
            >暂停</a-button>
            
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
    
    <a-modal 
       v-model:open="showResult" 
       title="任务结果" 
       width="800px" 
       :footer="null"
       destroyOnClose
    >
       <ResultViewer v-if="currentResultRow?.task_type !== 'stream'" :task-id="currentResultRow?.id || ''" />
       <VideoPlayer 
          v-else 
          :task-id="currentResultRow?.id || ''" 
          ws-url="ws://localhost:8000/ws/stream" 
          @close="showResult = false"
       />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, watch } from 'vue'
import { useTaskStore } from '@/stores/task'
import { tasksApi } from '@/api/tasks'
import TaskStatus from '@/components/TaskStatus.vue'
import TaskCreate from './TaskCreate.vue'
import ResultViewer from '@/components/ResultViewer.vue'
import VideoPlayer from '@/components/VideoPlayer.vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons-vue'

const taskStore = useTaskStore()

const page = ref(1)
const limit = ref(10)
const filters = reactive({ status: null })

const showCreate = ref(false)
const showResult = ref(false)
const currentResultRow = ref<any>(null)

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

async function loadData() {
  await taskStore.fetchTasks({
    skip: (page.value - 1) * limit.value,
    limit: limit.value,
    status: filters.status || undefined
  })
}

let timer: any = null

onMounted(() => {
  loadData()
  startPolling()
})

onUnmounted(() => {
  stopPolling()
})

function startPolling() {
    if (timer) return
    timer = setInterval(() => {
        const hasActiveProcessing = taskStore.tasks.some(t => 
            t.status === 'running' && t.task_type !== 'stream'
        )
        if (hasActiveProcessing) {
            loadData()
        } else {
            stopPolling()
        }
    }, 3000)
}

function stopPolling() {
    if (timer) {
        clearInterval(timer)
        timer = null
    }
}

watch(() => taskStore.tasks, (newTasks) => {
    const hasActiveProcessing = newTasks.some(t => 
        t.status === 'running' && t.task_type !== 'stream'
    )
    if (hasActiveProcessing) {
        startPolling()
    }
}, { deep: true })

function handleCreateSuccess() {
  showCreate.value = false
  page.value = 1
  loadData()
}

async function handleExecute(row: any) {
  const oldStatus = row.status
  try {
    row.status = 'queued'
    await tasksApi.execute(row.id)
    message.success('任务已加入执行队列')
    loadData()
  } catch(e) {
    row.status = oldStatus
  }
}

async function handlePause(row: any) {
  try {
    await tasksApi.pause(row.id)
    message.success('已暂停执行')
    loadData()
  } catch(e) {}
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

function handleViewResult(row: any) {
  currentResultRow.value = row
  showResult.value = true
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
