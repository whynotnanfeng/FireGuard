<template>
  <div class="model-list-page fade-in">
    <div class="toolbar">
      <div class="toolbar-left">
        <span class="filter-label">模型名称</span>
        <a-input-search
          v-model:value="filters.search"
          placeholder="搜索..."
          class="filter-item search-input"
          allow-clear
          @search="applyFilters"
          @change="onSearchChange"
        />
        
        <span class="filter-label">格式</span>
        <a-select 
          v-model:value="filters.format" 
          placeholder="全部" 
          allow-clear 
          class="filter-item"
          @change="applyFilters" 
        >
          <a-select-option value="onnx">ONNX</a-select-option>
        </a-select>
        
        <span class="filter-label">输入类型</span>
        <a-select 
          v-model:value="filters.input_type" 
          placeholder="全部" 
          allow-clear 
          class="filter-item"
          @change="applyFilters" 
        >
          <a-select-option value="rgb">RGB</a-select-option>
          <a-select-option value="ir">IR</a-select-option>
          <a-select-option value="rgb,ir">RGB + IR</a-select-option>
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
        <template #icon><CloudUploadOutlined /></template>
        上传模型
      </a-button>
    </div>

    <div v-show="filtersExpanded" class="advanced-filters">
      <span class="filter-label">状态</span>
      <a-select 
        v-model:value="filters.status" 
        placeholder="全部" 
        allow-clear 
        class="filter-item"
        @change="applyFilters" 
      >
        <a-select-option value="completed">可用</a-select-option>
        <a-select-option value="processing">处理中</a-select-option>
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
         :loading="loading" 
         :dataSource="pagedModels" 
         :columns="columns"
         rowKey="id"
         :pagination="false"
         class="model-table"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <span :title="record.name">{{ record.name }}</span>
          </template>

          <template v-if="column.key === 'description'">
            <span :title="record.description || ''">{{ record.description || '—' }}</span>
          </template>

          <template v-if="column.key === 'format'">
            <a-tag>{{ record.format }}</a-tag>
          </template>
          
          <template v-if="column.key === 'input_types'">
            <a-tag v-for="t in record.input_types" :key="t" style="margin-right:4px">
              {{ t.toUpperCase() }}
            </a-tag>
          </template>
          
          <template v-if="column.key === 'status'">
            <a-tag :color="record.status === 'completed' ? 'success' : 'warning'">
               {{ record.status === 'completed' ? '可用' : '处理中' }}
            </a-tag>
          </template>

          <template v-if="column.key === 'created_at'">
             {{ formatDate(record.created_at) }}
          </template>

          <template v-if="column.key === 'action'">
            <a-space>
              <a-button 
                 type="link" 
                 :disabled="record.status !== 'completed'"
                 @click="openEditLabels(record)"
              >编辑标签</a-button>
              <a-popconfirm title="确定删除该模型吗？" @confirm="handleDelete(record.id)">
                   <a-button type="link" danger :disabled="record.status !== 'completed'">删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </a-table>

      <div class="pagination-wrap" v-if="totalFiltered > 0">
        <span class="pagination-info">
          第 {{ page }}/{{ totalPages }} 页，共 {{ totalFiltered }} 条
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
          :total="totalFiltered"
        />
      </div>
    </div>

    <!-- Upload Modal -->
    <a-modal 
      title="上传模型" 
      v-model:open="showCreate"
      width="500px"
      :bodyStyle="{ maxHeight: '550px', overflowY: 'auto' }"
      destroyOnClose
    >
      <a-form :model="form" ref="formRef" :rules="rules" :label-col="{ span: 5 }" :wrapper-col="{ span: 19 }">
        <a-form-item label="模型名称" name="name">
          <a-input v-model:value="form.name" placeholder="例如: fire-detection-v1.onnx" />
        </a-form-item>
        <a-form-item label="输入类型" name="input_types">
          <a-checkbox-group v-model:value="form.input_types">
            <a-checkbox value="rgb">RGB</a-checkbox>
            <a-checkbox value="ir">IR</a-checkbox>
          </a-checkbox-group>
        </a-form-item>
        <a-form-item label="模型描述">
          <a-textarea v-model:value="form.description" :rows="2" />
        </a-form-item>
        <a-form-item label="模型文件" required :extra="analyzing ? '正在解析元数据...' : ''">
          <div class="upload-zone" @click="fileInput?.click()">
             <CloudUploadOutlined v-if="!analyzing" class="icon" />
             <a-spin v-else size="large" style="margin-bottom: 8px" />
             <div class="text">{{ analyzing ? '解析中...' : '点击上传 .onnx 文件' }}</div>
             <div class="files" v-if="file && !analyzing">{{file.name}} ({{(file.size/1024/1024).toFixed(2)}}MB)</div>
          </div>
          <input type="file" ref="fileInput" accept=".onnx" style="display:none" @change="onFileChange" />
        </a-form-item>
        <a-form-item label="标签映射">
           <LabelMappingEditor v-model="form.label_config" />
        </a-form-item>
      </a-form>
      <template #footer>
        <a-button @click="showCreate = false">取消</a-button>
        <a-button type="primary" :loading="submitting" @click="submit">开始上传</a-button>
      </template>
    </a-modal>

    <!-- Label Edit Modal -->
    <a-modal 
      title="编辑标签" 
      v-model:open="showEditLabels"
      width="500px"
      :bodyStyle="{ maxHeight: '550px', overflowY: 'auto' }"
      destroyOnClose
    >
      <div v-if="editingModel" style="margin-bottom: 12px">
        正在修改模型: <strong>{{ editingModel.name }}</strong>
      </div>
      <LabelMappingEditor 
        v-if="editingModel" 
        :modelValue="editLabelConfig" 
        @update:modelValue="val => editLabelConfig = val"
      />
      <template #footer>
        <a-button @click="showEditLabels = false">取消</a-button>
        <a-button type="primary" :loading="updatingLabels" @click="saveLabelUpdate">保存修改</a-button>
      </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { modelsApi, type DetectionModel } from '@/api/models'
import { message, Modal } from 'ant-design-vue'
import { CloudUploadOutlined, ReloadOutlined, DownOutlined, UpOutlined } from '@ant-design/icons-vue'
import LabelMappingEditor from '@/components/LabelMappingEditor.vue'
import CustomPagination from '@/components/CustomPagination.vue'
import dayjs, { type Dayjs } from 'dayjs'

const models = ref<DetectionModel[]>([])
const loading = ref(false)
const showCreate = ref(false)

const formRef = ref()
const submitting = ref(false)
const analyzing = ref(false)
const fileInput = ref<HTMLInputElement>()
const file = ref<File | null>(null)

const form = reactive({
  name: '',
  input_types: ['rgb'],
  description: '',
  label_config: null as Record<string, string> | null
})

// Edit Labels State
const showEditLabels = ref(false)
const editingModel = ref<DetectionModel | null>(null)
const editLabelConfig = ref<Record<string, string> | null>(null)
const updatingLabels = ref(false)

// Filters
const filters = reactive({
  search: '',
  format: undefined as string | undefined,
  input_type: undefined as string | undefined,
  status: undefined as string | undefined,
  description: '',
  dateRange: undefined as [Dayjs, Dayjs] | undefined
})

const filtersExpanded = ref(false)

const page = ref(1)
const pageSize = ref(10)

const totalFiltered = computed(() => filteredModels.value.length)
const totalPages = computed(() => Math.ceil(totalFiltered.value / pageSize.value) || 1)


const filteredModels = computed(() => {
  return models.value.filter(m => {
    if (filters.search && !m.name.toLowerCase().includes(filters.search.toLowerCase())) {
      return false
    }
    if (filters.format && m.format !== filters.format) {
      return false
    }
    if (filters.status && m.status !== filters.status) {
      return false
    }
    if (filters.input_type) {
      const types = m.input_types || []
      if (filters.input_type === 'rgb,ir') {
        if (!types.includes('rgb') || !types.includes('ir')) return false
      } else if (!types.includes(filters.input_type)) {
        return false
      }
    }
    if (filters.description && !m.description.toLowerCase().includes(filters.description.toLowerCase())) {
      return false
    }
    if (filters.dateRange && filters.dateRange.length === 2) {
      const created = new Date(m.created_at).getTime()
      const start = filters.dateRange[0].startOf('day').valueOf()
      const end = filters.dateRange[1].endOf('day').valueOf()
      if (created < start || created > end) {
        return false
      }
    }
    return true
  })
})


const pagedModels = computed(() => {
  const start = (page.value - 1) * pageSize.value
  const end = start + pageSize.value
  return filteredModels.value.slice(start, end)
})

function onPageChange() {
}

function onPageSizeChange() {
  page.value = 1
}

function applyFilters() {
  page.value = 1
}

function onSearchChange() {
  page.value = 1
}

function resetFilters() {
  filters.search = ''
  filters.format = undefined
  filters.input_type = undefined
  filters.status = undefined
  filters.description = ''
  filters.dateRange = undefined
  page.value = 1
}

const rules = {
  name: [{ required: true, message: '请输入模型名称' }],
  input_types: [{ required: true, type: 'array', min: 1, message: '请选择至少一种输入类型' }]
}

function resetForm() {
  form.name = ''
  form.description = ''
  form.input_types = ['rgb']
  form.label_config = null
  file.value = null
  if (fileInput.value) {
    fileInput.value.value = ''
  }
}

watch(showCreate, (val) => {
  if (!val) {
    resetForm()
  }
})

const columns = [
  { title: '模型名称', dataIndex: 'name', key: 'name', width: 200, ellipsis: true },
  { title: '格式', key: 'format', width: 100 },
  { title: '支持输入类型', key: 'input_types', width: 150 },
  { title: '描述', dataIndex: 'description', key: 'description', width: 150, ellipsis: true },
  { title: '状态', key: 'status', width: 100 },
  { title: '创建时间', key: 'created_at', width: 180 },
  { title: '操作', key: 'action', width: 200, fixed: 'right' }
]

function formatDate(ds: string) {
  if (!ds) return ''
  return new Date(ds).toLocaleString()
}

async function loadData() {
  loading.value = true
  try {
    const res = await modelsApi.list({ limit: 100 })
    models.value = res.items
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadData()
})

async function handleDelete(id: string) {
  try {
    await modelsApi.delete(id)
    message.success('删除成功')
    loadData()
  } catch(err: any) {
    if (err.response?.status === 409) {
      const detail = err.response.data?.detail || ''
      const match = detail.match(/tasks \[(.*)\]/)
      let countText = '一些'
      if (match && match[1]) {
        countText = match[1].split(',').length.toString()
      }
      
      Modal.error({
        title: '无法删除模型',
        content: `该模型正被 ${countText} 个任务使用，请先删除对应任务。`,
        okText: '知道了',
        centered: true
      })
    }
  }
}

async function onFileChange(e: Event) {
   const target = e.target as HTMLInputElement
   if (target.files && target.files.length > 0) {
      const selectedFile = target.files[0]
      file.value = selectedFile

      const fd = new FormData()
      fd.append('file', selectedFile)
      
      analyzing.value = true
      try {
        const res = await modelsApi.analyze(fd)
        if (res.label_config && Object.keys(res.label_config).length > 0) {
          form.label_config = res.label_config
          message.success('已自动提取模型标签')
        }
      } catch (err) {
        console.error('Failed to analyze model:', err)
      } finally {
        analyzing.value = false
      }
   }
}

async function submit() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch (err) {
    return
  }
  
  if (!file.value) {
     message.warning('请选择模型文件')
     return
  }

  const fd = new FormData()
  fd.append('name', form.name)
  fd.append('input_types', JSON.stringify(form.input_types))
  fd.append('description', form.description)
  if (form.label_config) {
    fd.append('label_config', JSON.stringify(form.label_config))
  }
  fd.append('file', file.value)

  submitting.value = true
  try {
     await modelsApi.create(fd)
     message.success('上传成功')
     showCreate.value = false
     loadData()
  } catch(e) {} finally {
     submitting.value = false
  }
}

function openEditLabels(model: DetectionModel) {
  editingModel.value = model
  editLabelConfig.value = model.label_config ? JSON.parse(JSON.stringify(model.label_config)) : null
  showEditLabels.value = true
}

async function saveLabelUpdate() {
  if (!editingModel.value) return
  updatingLabels.value = true
  
  const payload = {
    label_config: editLabelConfig.value || {}
  }
  
  try {
    const updatedModel = await modelsApi.update(editingModel.value.id, payload)

    // Validation: Did the server actually save it?
    const sentCount = Object.keys(payload.label_config).length
    const receivedCount = Object.keys(updatedModel.label_config || {}).length

    if (sentCount > 0 && receivedCount === 0) {
      message.error('服务器响应成功但数据未持久化，请检查后端服务是否需要重启')
      return 
    }

    message.success('标签映射更新成功')
    showEditLabels.value = false
    loadData()
  } catch(e) {
    console.error("SAVE FAILED:", e)
  } finally {
    updatingLabels.value = false
  }
}
</script>

<style scoped>
.model-list-page {
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
.desc-input {
  width: 180px;
}
.date-picker {
  width: 260px;
}
.table-wrapper {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.model-table {
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
.upload-zone {
    width: 100%;
    border: 1px dashed var(--border-color);
    border-radius: 8px;
    padding: 20px;
    text-align: center;
    cursor: pointer;
    transition: all 0.3s;
    background: var(--bg-input);
}
.upload-zone:hover {
    border-color: var(--primary-blue);
    background: rgba(9, 30, 66, 0.04);
}
.upload-zone .icon {
    font-size: 32px;
    color: var(--text-secondary);
    margin-bottom: 8px;
}
.upload-zone .text {
    color: var(--text-primary);
    font-size: 14px;
}
.files {
    margin-top: 8px;
    color: var(--success-green);
    font-weight: bold;
}
</style>
