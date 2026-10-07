<template>
  <a-modal 
    title="New Detection Task"
    :open="true"
    @cancel="$emit('close')"
    width="720px"
    destroyOnClose
    class="task-create-modal"
  >
    <div class="task-create-form">
      <a-form :model="form" :rules="rules" ref="formRef" layout="vertical">
        
        <div class="form-section">
          <div class="section-title">Basic Information</div>
          <div class="form-row">
            <a-form-item label="Task Name" name="name" class="form-col-2">
              <a-input v-model:value="form.name" placeholder="Enter a task name" :maxlength="50" showCount />
            </a-form-item>
            <a-form-item label="Task Type" name="task_type" class="form-col-1">
              <a-select v-model:value="form.task_type" placeholder="Select" @change="handleTaskTypeChange">
                <a-select-option value="image">Image Detection</a-select-option>
                <a-select-option value="video">Video Detection</a-select-option>
                <a-select-option value="stream">Live Stream</a-select-option>
              </a-select>
            </a-form-item>
          </div>
        </div>

        <div class="form-section">
          <div class="section-title">Model Configuration</div>

          <!-- Multi-model list -->
          <div v-for="(sm, idx) in selectedModels" :key="idx" class="multi-model-row">
            <div class="model-row-header">
              <span class="model-row-label">Model {{ idx + 1 }}</span>
              <a-button v-if="selectedModels.length > 1" type="link" size="small" danger @click="removeModel(idx)">
                Remove
              </a-button>
            </div>
            <div class="form-row">
              <a-form-item label="Select Model" class="form-col-2">
                <a-select
                  v-model:value="sm.model_id"
                  placeholder="Select a detection model"
                  :loading="loadingModels"
                  @change="(val: string) => handleMultiModelChange(idx, val)"
                  style="width: 100%"
                >
                  <a-select-option
                    v-for="m in filteredModels"
                    :key="m.id"
                    :value="m.id"
                    :disabled="isModelSelectedByOther(m.id, idx)"
                  >
                    {{ m.name }} ({{ m.input_types.map((t: string) => t.toUpperCase()).join(', ') }})
                  </a-select-option>
                </a-select>
              </a-form-item>
              <a-form-item label="Weight" class="form-col-1">
                <a-slider v-model:value="sm.weight" :min="0.1" :max="3.0" :step="0.1" />
              </a-form-item>
            </div>
            <div v-if="sm.model_id" class="model-requirement-hint fade-in">
              <span class="hint-label">Input types:</span>
              <a-tag v-for="t in sm.input_types" :key="t" color="blue" class="requirement-tag">
                {{ t.toUpperCase() }}
              </a-tag>
              <template v-if="selectedModels.length > 1">
                <a-button size="small" style="margin-left: 12px" @click="configuringModelIndex = idx; showPerModelConfig = true">
                  Configure Classes &amp; Thresholds
                </a-button>
                <span class="config-summary">
                  {{ sm.categories.filter(c => c.selected).length }}/{{ sm.categories.length }} classes enabled
                </span>
              </template>
            </div>
          </div>

          <a-button type="dashed" block @click="addModel" class="add-model-btn">
            + Add Model
          </a-button>

          <!-- Fusion configuration (shown when multiple models are used) -->
          <div v-if="selectedModels.length > 1" class="fusion-config-section">
            <div class="config-header">
              <span class="config-title">Fusion Configuration</span>
            </div>
            <a-form-item label="WBF IoU Threshold">
              <a-slider v-model:value="fusionConfig.wbf_iou_threshold" :min="0.3" :max="0.8" :step="0.05" />
              <div class="config-hint">Boxes of the same class are fused when their IoU exceeds this value (default 0.55)</div>
            </a-form-item>
          </div>

          <div v-if="hasAnyModelSelected" class="inference-device-section">
            <a-form-item label="Inference Device" class="device-item">
              <a-radio-group v-model:value="form.use_gpu">
                <a-radio :value="false">CPU Inference</a-radio>
                <a-radio :value="true" :disabled="!gpuAvailable && !gpuChecking">GPU Inference</a-radio>
              </a-radio-group>
              <a-button
                size="small"
                @click="checkGpuStatus"
                :loading="gpuChecking"
                style="margin-left: 8px"
              >
                <template #icon><ThunderboltOutlined /></template>
                {{ gpuAvailable ? 'Re-check GPU' : 'Check GPU Status' }}
              </a-button>
              <div v-if="gpuStatusMsg" class="gpu-status-msg" :class="{ 'gpu-ok': gpuAvailable, 'gpu-fail': !gpuAvailable }">
                {{ gpuStatusMsg }}
              </div>
            </a-form-item>
          </div>
          
          <div v-if="selectedModels.length === 1 && selectedModels[0].model_id" class="detection-config-section">
            <div class="config-header">
              <span class="config-title">Detection Configuration</span>
              <a-button size="small" @click="showDetectionConfig = true">
                <template #icon><SettingOutlined /></template>
                Configure Classes &amp; Thresholds
              </a-button>
            </div>
            <div class="config-summary" v-if="categories.length > 0">
              <span class="summary-item">
                {{ selectedCategoriesCount }}/{{ categories.length }} classes selected
              </span>
              <span class="summary-divider">|</span>
              <span class="summary-item">
                Global threshold: {{ globalThreshold }}
              </span>
            </div>
          </div>
        </div>

        <div class="form-section data-source-section">
          <div class="section-title">Data Source</div>
          
          <!-- Placeholder shown while no model is selected -->
          <div v-if="!hasAnyModelSelected" class="model-not-selected-placeholder">
            <div class="placeholder-content">
              <InfoCircleOutlined class="icon" />
              <span>Select a model first — the upload area will unlock based on its requirements</span>
            </div>
          </div>

          <!-- Actual content shown once a model is selected -->
          <div v-else class="data-source-content fade-in">
            <a-form-item name="source_type" class="source-type-item">
              <a-radio-group v-model:value="form.source_type" button-style="solid" @change="handleSourceChange">
                <a-radio-button value="upload" :disabled="!allowedSourceTypes.upload">
                  <UploadOutlined /> File Upload
                </a-radio-button>
                <a-radio-button value="url" :disabled="!allowedSourceTypes.url">
                  <LinkOutlined /> URL
                </a-radio-button>
                <a-radio-button value="rtsp" :disabled="!allowedSourceTypes.rtsp">
                  <VideoCameraOutlined /> RTSP Stream
                </a-radio-button>
              </a-radio-group>
            </a-form-item>

            <template v-if="form.source_type === 'upload'">
              <div class="upload-row" v-if="form.input_types.includes('rgb')">
                <div class="upload-label">
                  <span class="required-mark">*</span> RGB Files
                </div>
                <div class="upload-zone" @click="triggerUpload('rgb')">
                  <CloudUploadOutlined class="icon" />
                  <div class="text">Click to upload RGB {{form.task_type==='image'?'images':'videos'}}</div>
                  <div class="files" v-if="rgbFiles.length">
                    <CheckCircleOutlined /> {{rgbFiles.length}} file(s) selected
                  </div>
                </div>
                <input type="file" multiple ref="rgbInput" style="display:none" @change="onFileChange($event, 'rgb')" />
              </div>

              <div class="upload-row" v-if="form.input_types.includes('ir')">
                <div class="upload-label">
                  <span class="required-mark">*</span> IR Files
                </div>
                <div class="upload-zone" @click="triggerUpload('ir')">
                  <CloudUploadOutlined class="icon" />
                  <div class="text">Click to upload IR {{form.task_type==='image'?'images':'videos'}}</div>
                  <div class="files" v-if="irFiles.length">
                    <CheckCircleOutlined /> {{irFiles.length}} file(s) selected
                  </div>
                </div>
                <input type="file" multiple ref="irInput" style="display:none" @change="onFileChange($event, 'ir')" />
              </div>
            </template>

            <a-form-item v-else label="RGB Source URL" name="source_url">
              <a-input
                v-model:value="form.source_url"
                :placeholder="hasIrModel ? 'RGB stream URL' : sourceUrlPlaceholder"
              >
                <template #prefix>
                  <LinkOutlined style="color: var(--text-muted)" />
                </template>
              </a-input>
            </a-form-item>
            <a-form-item v-if="source_type !== 'upload' && hasIrModel" label="IR Source URL" name="source_url_ir">
              <a-input
                v-model:value="form.source_url_ir"
                placeholder="IR stream URL"
              >
                <template #prefix>
                  <LinkOutlined style="color: var(--text-muted)" />
                </template>
              </a-input>
            </a-form-item>
          </div>
        </div>

        <a-form-item v-if="form.model_id" label="Description" class="description-item">
          <a-textarea v-model:value="form.description" :rows="2" placeholder="Optional: add a task description" :maxlength="200" showCount />
        </a-form-item>

      </a-form>
    </div>

    <template #footer>
      <a-button @click="$emit('close')">Cancel</a-button>
      <a-button type="primary" :loading="submitting" @click="submit">Create Task</a-button>
    </template>
    
    <DetectionConfig
      :task-id="''"
       v-if="showDetectionConfig"
       :model-id="form.model_id"
       :initial-categories="categories"
       :initial-global-threshold="globalThreshold"
       @close="showDetectionConfig = false"
       @success="handleDetectionConfigSuccess"
    />

    <!-- Multi-model: per-model class configuration -->
    <DetectionConfig
      :task-id="''"
       v-if="showPerModelConfig && configuringModelIndex >= 0"
       :model-id="selectedModels[configuringModelIndex]?.model_id || ''"
       :initial-categories="selectedModels[configuringModelIndex]?.categories || []"
       :initial-global-threshold="selectedModels[configuringModelIndex]?.global_threshold || 0.6"
       @close="showPerModelConfig = false"
       @success="handlePerModelConfigSuccess"
    />
  </a-modal>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { modelsApi, type DetectionModel } from '@/api/models'
import { useTaskStore } from '@/stores/task'
import { message } from 'ant-design-vue'
import { 
  CloudUploadOutlined, 
  UploadOutlined, 
  LinkOutlined, 
  VideoCameraOutlined,
  CheckCircleOutlined,
  InfoCircleOutlined,
  SettingOutlined,
  ThunderboltOutlined 
} from '@ant-design/icons-vue'
import DetectionConfig from '@/components/DetectionConfig.vue'

const emit = defineEmits(['close', 'success'])
const taskStore = useTaskStore()
const formRef = ref()

const models = ref<DetectionModel[]>([])
const loadingModels = ref(false)
const submitting = ref(false)

const rgbInput = ref<HTMLInputElement>()
const irInput = ref<HTMLInputElement>()

const rgbFiles = ref<File[]>([])
const irFiles = ref<File[]>([])

const showDetectionConfig = ref(false)
const showPerModelConfig = ref(false)
const configuringModelIndex = ref<number>(-1)
const categories = ref<Array<{ id: string; name: string; selected: boolean; threshold: number | null }>>([])
const globalThreshold = ref<number>(0.6)

// Multi-model support
interface SelectedModel {
  model_id: string
  weight: number
  input_types: string[]
  categories: Array<{ id: string; name: string; selected: boolean; threshold: number | null }>
  global_threshold: number
}
const selectedModels = ref<SelectedModel[]>([
  { model_id: '', weight: 1.0, input_types: [], categories: [], global_threshold: 0.6 }
])
const fusionConfig = reactive({ wbf_iou_threshold: 0.55 })

const hasIrModel = computed(() =>
  selectedModels.value.some(sm => sm.input_types.includes('ir'))
)
const hasAnyModelSelected = computed(() =>
  selectedModels.value.some(sm => sm.model_id !== '')
)

const selectedCategoriesCount = computed(() => {
  return categories.value.filter(c => c.selected).length
})

interface TaskForm {
  name: string
  task_type: 'image' | 'video' | 'stream'
  input_types: string[]
  model_id: string
  source_type: 'upload' | 'url' | 'rtsp'
  source_url: string
  source_url_ir: string
  description: string
  use_gpu: boolean
}

const form = reactive<TaskForm>({
  name: '',
  task_type: 'image',
  input_types: [],
  model_id: '',
  source_type: 'upload',
  source_url: '',
  source_url_ir: '',
  description: '',
  use_gpu: false
})

const gpuAvailable = ref(false)
const gpuChecking = ref(false)
const gpuStatusMsg = ref('')

const allowedSourceTypes = computed(() => {
  const types = { upload: true, url: true, rtsp: true }
  if (form.task_type === 'stream') {
    types.upload = false
    types.url = false
    types.rtsp = true
  } else if (form.task_type === 'image') {
    types.rtsp = false
  } else if (form.task_type === 'video') {
    types.rtsp = false
  }
  return types
})

const sourceUrlPlaceholder = computed(() => {
  if (form.task_type === 'stream') return 'rtsp://...'
  return 'http://... or https://...'
})

const rules = {
  name: [
    { required: true, message: 'Please enter a task name', trigger: 'blur' },
    { min: 2, max: 50, message: 'Name must be 2-50 characters', trigger: 'blur' }
  ],
  task_type: [{ required: true, message: 'Please select a task type', trigger: 'change' }],
  input_types: [{ required: true, type: 'array', min: 1, message: 'The selected model requires an input type', trigger: 'change' }],
  model_id: [{ required: true, message: 'Please select a model', trigger: 'change' }],
  source_type: [{ required: true, message: 'Please select a data source', trigger: 'change' }],
  source_url: [
    { required: true, message: 'Please enter a source URL', trigger: 'blur' },
    {
      validator: (_rule: any, value: string) => {
        if (!value) return Promise.resolve()
        const url = value.trim().toLowerCase()
        if (form.task_type === 'stream') {
          if (!url.startsWith('rtsp://')) {
            return Promise.reject(new Error('The stream URL must start with rtsp://'))
          }
        } else {
          if (url.startsWith('rtsp://')) {
            return Promise.reject(new Error('Image and video tasks do not support RTSP URLs'))
          }
          if (!url.startsWith('http://') && !url.startsWith('https://')) {
            return Promise.reject(new Error('Please enter a valid HTTP/HTTPS URL'))
          }
        }
        return Promise.resolve()
      },
      trigger: 'blur'
    }
  ]
}

onMounted(async () => {
    loadingModels.value = true
    try {
        const res = await modelsApi.list({ limit: 100 })
        models.value = res.items
    } finally {
        loadingModels.value = false
    }
    await checkGpuStatus()
})

const filteredModels = computed(() => {
    return models.value
})

function handleTaskTypeChange() {
    form.model_id = ''
    form.source_url = ''
    form.source_url_ir = ''
    form.input_types = []
    rgbFiles.value = []
    irFiles.value = []
    selectedModels.value = [{ model_id: '', weight: 1.0, input_types: [], categories: [], global_threshold: 0.6 }]
    categories.value = []

    if (form.task_type === 'stream') {
        form.source_type = 'rtsp'
    } else if (form.source_type === 'rtsp') {
        form.source_type = 'upload'
    }
}

function handleSourceChange() {
    form.source_url = ''
    form.source_url_ir = ''
    rgbFiles.value = []
    irFiles.value = []
}

function handleModelChange(modelId: string) {
    const model = models.value.find(m => m.id === modelId)
    console.log('Model changed:', modelId, model?.input_types)
    if (model) {
        form.input_types = [...model.input_types]
        
        let classList: Array<{ id: string; name: string }> = []
        if (model.label_config && Object.values(model.label_config).length > 0) {
            classList = Object.entries(model.label_config).map(([id, name]) => ({ id, name }))
        } else if (model.class_names && model.class_names.length > 0) {
            classList = model.class_names.map((name: string, idx: number) => ({ id: String(idx), name }))
        }
        
        if (classList.length === 0) {
            classList = [{ id: 'default', name: 'Default Category' }]
        }
        
        categories.value = classList.map(cat => ({
            id: cat.id,
            name: cat.name,
            selected: true,
            threshold: null
        }))
        
        // V1.4.6: Force standard default threshold to 0.60 for initial creation
        globalThreshold.value = 0.6
    }
}

function handleTypeChange() {
}

// ── Multi-model functions ──

function addModel() {
    selectedModels.value.push({
        model_id: '', weight: 1.0, input_types: [], categories: [], global_threshold: 0.6
    })
}

function removeModel(idx: number) {
    selectedModels.value.splice(idx, 1)
    syncInputTypes()
    // When falling back to a single model, sync form.model_id and the class configuration
    if (selectedModels.value.length === 1) {
        const sm = selectedModels.value[0]
        if (sm.model_id) {
            form.model_id = sm.model_id
            form.input_types = [...sm.input_types]
            categories.value = [...sm.categories]
            globalThreshold.value = sm.global_threshold
        }
    }
}

function isModelSelectedByOther(modelId: string, currentIdx: number): boolean {
    return selectedModels.value.some((sm, i) => i !== currentIdx && sm.model_id === modelId)
}

function handleMultiModelChange(idx: number, modelId: string) {
    const model = models.value.find(m => m.id === modelId)
    if (!model) return
    const sm = selectedModels.value[idx]
    sm.input_types = [...model.input_types]
    // Build the class configuration for this model
    let classList: Array<{ id: string; name: string }> = []
    if (model.label_config && Object.values(model.label_config).length > 0) {
        classList = Object.entries(model.label_config).map(([id, name]) => ({ id, name: name as string }))
    } else if (model.class_names && model.class_names.length > 0) {
        classList = model.class_names.map((name: string, i: number) => ({ id: String(i), name }))
    }
    if (classList.length === 0) classList = [{ id: 'default', name: 'Default Category' }]
    sm.categories = classList.map(cat => ({ ...cat, selected: true, threshold: null }))
    sm.global_threshold = 0.6
    // Sync to the main form (compatible with single-model mode)
    syncInputTypes()
    if (selectedModels.value.length === 1) {
        form.model_id = modelId
        form.input_types = [...sm.input_types]
        categories.value = [...sm.categories]
        globalThreshold.value = sm.global_threshold
    }
}

function syncInputTypes() {
    const allTypes = new Set<string>()
    selectedModels.value.forEach(sm => sm.input_types.forEach(t => allTypes.add(t)))
    form.input_types = [...allTypes]
}

function triggerUpload(type: 'rgb'|'ir') {
    if (type === 'rgb') rgbInput.value?.click()
    else irInput.value?.click()
}

function onFileChange(e: Event, type: 'rgb'|'ir') {
    const target = e.target as HTMLInputElement
    if (target.files) {
        const files = Array.from(target.files)
        const maxFileSize = 20 * 1024 * 1024 * 1024 // 20GB limit
        for (const file of files) {
            if (file.size > maxFileSize) {
                message.error(`File "${file.name}" (${(file.size / 1024 / 1024).toFixed(1)}MB) exceeds the 20GB maximum file size limit`)
                target.value = ''
                return
            }
        }
        if (type === 'rgb') rgbFiles.value = files
        else irFiles.value = files
    }
}

async function checkGpuStatus() {
  gpuChecking.value = true
  gpuStatusMsg.value = ''
  try {
    const res = await taskStore.checkGpuStatus()
    gpuAvailable.value = res.available
    if (res.available) {
      gpuStatusMsg.value = `GPU ready: ${res.checks.device_name || 'Unknown'}, VRAM: ${res.checks.vram_mb || 0}MB`
    } else {
      gpuStatusMsg.value = `GPU unavailable: ${res.reason || 'The environment does not meet the requirements'}`
    }
  } catch (e: any) {
    gpuAvailable.value = false
    gpuStatusMsg.value = `GPU check failed: ${e?.message || 'Unknown error'}`
  } finally {
    gpuChecking.value = false
  }
}

async function submit() {
    if (!formRef.value) return
    try {
        await formRef.value.validate()
    } catch(err) {
        return
    }

    if (form.source_type === 'upload') {
        if (form.input_types.includes('rgb') && rgbFiles.value.length === 0) {
            message.warning('Please upload an RGB file')
            return
        }
        if (form.input_types.includes('ir') && irFiles.value.length === 0) {
            message.warning('Please upload an IR file')
            return
        }
    } else if (form.source_type !== 'upload' && hasIrModel.value && !form.source_url_ir) {
        message.warning('The selected model requires IR input. Please provide an IR source URL')
        return
    }

    const validModels = selectedModels.value.filter(sm => sm.model_id)
    if (validModels.length === 0) {
        message.warning('Please select at least one detection model')
        return
    }

    const fd = new FormData()
    fd.append('name', form.name)
    fd.append('task_type', form.task_type)
    fd.append('input_types', JSON.stringify(form.input_types))
    fd.append('source_type', form.source_type)
    fd.append('description', form.description)
    fd.append('use_gpu', String(form.use_gpu))

    if (validModels.length > 1) {
        const modelIdsPayload = validModels.map(sm => {
            const enabledClasses = sm.categories.filter(c => c.selected).map(c => c.id)
            const perClassThresholds: Record<string, number> = {}
            sm.categories.forEach(cat => {
                if (cat.selected && cat.threshold !== null) {
                    perClassThresholds[cat.id] = cat.threshold
                }
            })
            return {
                model_id: sm.model_id,
                weight: sm.weight,
                enabled_classes: enabledClasses,
                per_class_thresholds: perClassThresholds,
            }
        })
        fd.append('model_ids', JSON.stringify(modelIdsPayload))
        fd.append('fusion_config', JSON.stringify(fusionConfig))
        // Backward compatibility: use the first model as the primary model_id
        fd.append('model_id', validModels[0].model_id)
        fd.append('threshold', String(validModels[0].global_threshold))
    } else {
        // Single-model mode: derive from selectedModels to avoid form.model_id going out of sync
        const sm0 = validModels[0]
        fd.append('model_id', sm0.model_id)
        fd.append('threshold', String(sm0.global_threshold || globalThreshold.value))

        const smCategories = sm0.categories.length > 0 ? sm0.categories : categories.value
        const selectedCategoryIds = smCategories.filter(c => c.selected).map(c => c.id)
        if (selectedCategoryIds.length > 0) {
            fd.append('enabled_classes', JSON.stringify(selectedCategoryIds))
        }

        const categoryThresholds: Record<string, number> = {}
        smCategories.forEach(cat => {
            if (cat.selected && cat.threshold !== null) {
                categoryThresholds[cat.id] = cat.threshold
            }
        })
        if (Object.keys(categoryThresholds).length > 0) {
            fd.append('category_thresholds', JSON.stringify(categoryThresholds))
        }
    }

    if (form.source_type === 'upload') {
        rgbFiles.value.forEach(f => fd.append('rgb_files', f))
        irFiles.value.forEach(f => fd.append('ir_files', f))
    } else {
        // With an IR model, join the RGB and IR source URLs with a semicolon
        const url = hasIrModel.value && form.source_url_ir
          ? `${form.source_url};${form.source_url_ir}`
          : form.source_url
        fd.append('source_url', url)
    }

    submitting.value = true
    try {
        await taskStore.createTask(fd)
        message.success('Task created')
        emit('success')
    } catch(e: any) {
        message.error('Failed to create task: ' + (e?.message || e?.detail || 'Unknown error'))
    } finally {
        submitting.value = false
    }
}

function handleDetectionConfigSuccess(config: any) {
    if (config.categories) {
        categories.value = config.categories
    }
    if (config.globalThreshold !== undefined) {
        globalThreshold.value = config.globalThreshold
    }
    showDetectionConfig.value = false
}

function handlePerModelConfigSuccess(config: any) {
    const idx = configuringModelIndex.value
    if (idx < 0 || idx >= selectedModels.value.length) return
    const sm = selectedModels.value[idx]
    if (config.categories) {
        sm.categories = config.categories
    }
    if (config.globalThreshold !== undefined) {
        sm.global_threshold = config.globalThreshold
    }
    showPerModelConfig.value = false
}
</script>

<style scoped>
.task-create-form {
  max-height: 65vh;
  overflow-y: auto;
  padding-right: 8px;
}

.form-section {
  margin-bottom: 24px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-color);
}

.form-row {
  display: flex;
  gap: 16px;
}

.form-col-1 {
  flex: 1;
}

.form-col-2 {
  flex: 2;
}

.form-col-3 {
  flex: 3;
}

.source-type-item {
  margin-bottom: 16px;
}

.upload-row {
  margin-bottom: 16px;
}

.upload-label {
  font-size: 14px;
  color: var(--text-primary);
  margin-bottom: 8px;
  font-weight: 500;
}

.required-mark {
  color: #ff4d4f;
  margin-right: 4px;
}

.upload-zone {
    width: 100%;
    border: 1px dashed var(--border-color);
    border-radius: 8px;
    padding: 24px;
    text-align: center;
    cursor: pointer;
    transition: all 0.3s;
    background: var(--bg-input);
}
.upload-zone:hover {
    border-color: var(--primary-blue);
    background: rgba(0, 102, 204, 0.02);
}
.upload-zone .icon {
    font-size: 28px;
    color: var(--text-muted);
    margin-bottom: 8px;
}
.upload-zone .text {
    color: var(--text-secondary);
    font-size: 13px;
}
.files {
    margin-top: 8px;
    color: var(--success-green);
    font-weight: 600;
    font-size: 13px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
}

.description-item {
  margin-top: 8px;
}

:deep(.ant-form-item-label > label) {
  font-weight: 500;
  font-size: 14px;
}

:deep(.ant-radio-group-solid .ant-radio-button-wrapper-checked) {
  background: var(--primary-blue);
  border-color: var(--primary-blue);
  color: #fff;
}

:deep(.ant-radio-group-solid .ant-radio-button-wrapper) {
  border-radius: 6px;
  height: 36px;
  line-height: 34px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

:deep(.ant-radio-group-solid .ant-radio-button-wrapper-disabled) {
  color: #d0d5dd;
  background: #f9fafb;
  border-color: #e8ecf0;
}

.model-requirement-hint {
  margin-top: 10px;
  font-size: 13px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-secondary);
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
}

.hint-label {
  font-weight: 500;
  color: var(--text-muted);
}

.requirement-tag {
  margin-right: 0 !important;
  font-weight: 600;
  border-radius: 4px;
}

.model-not-selected-placeholder {
  padding: 32px;
  text-align: center;
  background: var(--bg-secondary);
  border: 1px dashed var(--border-color);
  border-radius: 8px;
  color: var(--text-muted);
}

.placeholder-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.placeholder-content .icon {
  font-size: 24px;
}

.inference-device-section {
  margin-top: 12px;
  padding: 12px 16px;
  background: var(--bg-secondary);
  border-radius: 8px;
  border: 1px solid var(--border-color);
}

.device-item {
  margin-bottom: 0;
}

.device-item :deep(.ant-form-item-label) {
  margin-bottom: 8px;
}

.gpu-status-msg {
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.4;
}

.gpu-status-msg.gpu-ok {
  background: rgba(82, 196, 26, 0.1);
  color: #52c41a;
  border: 1px solid rgba(82, 196, 26, 0.3);
}

.gpu-status-msg.gpu-fail {
  background: rgba(255, 77, 79, 0.1);
  color: #ff4d4f;
  border: 1px solid rgba(255, 77, 79, 0.3);
}

.detection-config-section {
  margin-top: 16px;
  padding: 12px 16px;
  background: var(--bg-secondary);
  border-radius: 8px;
  border: 1px solid var(--border-color);
}

.config-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.config-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.config-summary {
  display: flex;
  gap: 12px;
  align-items: center;
  font-size: 12px;
  color: var(--text-secondary);
}

.model-row .config-summary {
  display: inline;
  margin-left: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.summary-divider {
  color: var(--border-color);
}
</style>
