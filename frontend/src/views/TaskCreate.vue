<template>
  <a-modal 
    title="新建检测任务"
    :open="true"
    @cancel="$emit('close')"
    width="880px"
    destroyOnClose
    class="task-create-modal"
  >
    <div class="task-create-form">
      <a-form :model="form" :rules="rules" ref="formRef" layout="vertical">
        
        <div class="form-section">
          <div class="section-title">基本信息</div>
          <div class="form-row">
            <a-form-item label="任务名称" name="name" class="form-col-2">
              <a-input v-model:value="form.name" placeholder="请输入任务名称" :maxlength="50" showCount />
            </a-form-item>
            <a-form-item label="任务类型" name="task_type" class="form-col-1">
              <a-select v-model:value="form.task_type" placeholder="请选择" @change="handleTaskTypeChange">
                <a-select-option value="image">图片检测</a-select-option>
                <a-select-option value="video">视频检测</a-select-option>
                <a-select-option value="stream">实时视频流</a-select-option>
              </a-select>
            </a-form-item>
          </div>
        </div>

        <TaskModelConfigSection
          :selected-models="selectedModels"
          :filtered-models="filteredModels"
          :loading-models="loadingModels"
          :fusion-config="fusionConfig"
          :is-model-selected-by-other="isModelSelectedByOther"
          @add-model="addModel"
          @remove-model="removeModel"
          @model-change="handleMultiModelChange"
          @configure-model="openPerModelConfig"
        />

        <div class="form-section">
          <div class="section-title">运行配置</div>

          <div v-if="hasAnyModelSelected" class="inference-device-section">
            <a-form-item label="推理设备" class="device-item">
              <a-radio-group v-model:value="form.use_gpu">
                <a-radio :value="false">CPU 推理</a-radio>
                <a-radio :value="true" :disabled="!gpuAvailable && !gpuChecking">GPU 推理</a-radio>
              </a-radio-group>
              <a-button
                size="small"
                @click="checkGpuStatus"
                :loading="gpuChecking"
                style="margin-left: 8px"
              >
                <template #icon><ThunderboltOutlined /></template>
                {{ gpuAvailable ? '重新检测 GPU' : '检测 GPU 状态' }}
              </a-button>
              <div v-if="gpuStatusMsg" class="gpu-status-msg" :class="{ 'gpu-ok': gpuAvailable, 'gpu-fail': !gpuAvailable }">
                {{ gpuStatusMsg }}
              </div>
            </a-form-item>
          </div>

          <div class="inference-device-section">
            <a-form-item label="检测记录保留">
              <a-select v-model:value="form.record_retention_days" style="width: 180px">
                <a-select-option :value="7">7 天</a-select-option>
                <a-select-option :value="30">30 天（推荐）</a-select-option>
                <a-select-option :value="90">90 天</a-select-option>
                <a-select-option :value="180">180 天</a-select-option>
                <a-select-option :value="365">365 天</a-select-option>
              </a-select>
              <div class="config-hint">超过保留期的检测记录/事件会自动清理，默认保守保留 30 天。</div>
            </a-form-item>
            <a-form-item v-if="form.task_type === 'stream'" label="录像保留">
              <a-select v-model:value="form.video_retention_hours" style="width: 180px">
                <a-select-option :value="6">6 小时</a-select-option>
                <a-select-option :value="12">12 小时（推荐）</a-select-option>
                <a-select-option :value="24">24 小时</a-select-option>
                <a-select-option :value="72">72 小时</a-select-option>
              </a-select>
              <div class="config-hint">仅影响历史录像分片，超出后由保洁自动删除。</div>
            </a-form-item>
          </div>
          
          <div v-if="selectedModels.length === 1 && selectedModels[0].model_id" class="detection-config-section">
            <div class="config-header">
              <span class="config-title">检测配置</span>
              <a-button size="small" @click="showDetectionConfig = true">
                <template #icon><SettingOutlined /></template>
                配置类别和阈值
              </a-button>
            </div>
            <div class="config-summary" v-if="categories.length > 0">
              <span class="summary-item">
                已选择 {{ selectedCategoriesCount }}/{{ categories.length }} 个类别
              </span>
              <span class="summary-divider">|</span>
              <span class="summary-item">
                全局阈值: {{ globalThreshold }}
              </span>
            </div>
          </div>
        </div>

        <TaskSourceSection
          :form="form"
          :has-any-model-selected="hasAnyModelSelected"
          :has-ir-model="hasIrModel"
          :allowed-source-types="allowedSourceTypes"
          :source-url-placeholder="sourceUrlPlaceholder"
          :onvif-scanning="onvifScanning"
          v-model:rgb-files="rgbFiles"
          v-model:ir-files="irFiles"
          @source-change="handleSourceChange"
          @scan-onvif="scanOnvif"
        />

        <a-form-item v-if="form.model_id" label="任务描述" class="description-item">
          <a-textarea v-model:value="form.description" :rows="2" placeholder="可选：添加任务描述信息" :maxlength="200" showCount />
        </a-form-item>

      </a-form>
    </div>

    <template #footer>
      <a-button @click="$emit('close')">取消</a-button>
      <a-button type="primary" :loading="submitting" @click="submit">创建任务</a-button>
    </template>
    
    <DetectionConfig
      :task-id="''"
       v-if="showDetectionConfig"
       :model-id="form.model_id"
       :initial-categories="categories"
       :initial-global-threshold="globalThreshold"
       :initial-record-retention-days="form.record_retention_days"
       :initial-video-retention-hours="form.video_retention_hours"
       @close="showDetectionConfig = false"
       @success="handleDetectionConfigSuccess"
    />

    <!-- 多模型：每模型类别配置 -->
    <DetectionConfig
      :task-id="''"
       v-if="showPerModelConfig && configuringModelIndex >= 0"
       :model-id="selectedModels[configuringModelIndex]?.model_id || ''"
       :initial-categories="selectedModels[configuringModelIndex]?.categories || []"
       :initial-global-threshold="selectedModels[configuringModelIndex]?.global_threshold || 0.6"
       :initial-record-retention-days="form.record_retention_days"
       :initial-video-retention-hours="form.video_retention_hours"
       @close="showPerModelConfig = false"
       @success="handlePerModelConfigSuccess"
    />
    <!-- ONVIF：局域网设备扫描与取流地址 -->
    <a-modal v-model:open="onvifOpen" title="ONVIF 设备" :footer="null" width="560px">
      <a-spin :spinning="onvifFetching">
        <div v-if="!onvifDevices.length" class="onvif-empty">
          未发现设备。请确认摄像头与电脑在同一网段、已开启 ONVIF，并允许防火墙的局域网组播（UDP 3702）。
        </div>
        <div v-else class="onvif-list">
          <div
            v-for="d in onvifDevices"
            :key="d.xaddr"
            class="onvif-item"
            :class="{ active: onvifSelected?.xaddr === d.xaddr }"
            @click="onvifSelected = d"
          >
            <div class="onvif-name">{{ d.name || d.hardware || d.host }}</div>
            <div class="onvif-sub">{{ d.host }}:{{ d.port }} · {{ d.xaddr }}</div>
          </div>
        </div>
        <a-form layout="vertical" style="margin-top: 12px">
          <a-form-item label="设备用户名" style="margin-bottom: 8px">
            <a-input v-model:value="onvifUser" placeholder="通常为 admin" />
          </a-form-item>
          <a-form-item label="设备密码" style="margin-bottom: 0">
            <a-input-password v-model:value="onvifPass" placeholder="摄像头管理密码" />
          </a-form-item>
        </a-form>
        <div class="onvif-actions">
          <a-button @click="onvifOpen = false">取消</a-button>
          <a-button type="primary" :disabled="!onvifSelected" :loading="onvifFetching" @click="fetchOnvifUri">
            获取 RTSP 地址
          </a-button>
        </div>
      </a-spin>
    </a-modal>
  </a-modal>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { modelsApi, type DetectionModel } from '@/api/models'
import { onvifApi, type OnvifDevice } from '@/api/onvif'
import { useTaskStore } from '@/stores/task'
import { message } from 'ant-design-vue'
import { 
  SettingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import DetectionConfig from '@/components/DetectionConfig.vue'
import TaskSourceSection from '@/components/TaskSourceSection.vue'
import TaskModelConfigSection from '@/components/TaskModelConfigSection.vue'

const emit = defineEmits(['close', 'success'])
const taskStore = useTaskStore()
const formRef = ref()

const models = ref<DetectionModel[]>([])
const loadingModels = ref(false)
const submitting = ref(false)

const rgbFiles = ref<File[]>([])
const irFiles = ref<File[]>([])

const showDetectionConfig = ref(false)
const showPerModelConfig = ref(false)
const configuringModelIndex = ref<number>(-1)
const categories = ref<Array<{ id: string; name: string; selected: boolean; threshold: number | null }>>([])
const globalThreshold = ref<number>(0.6)

// 多模型支持
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
  record_retention_days: number
  video_retention_hours: number
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
  use_gpu: false,
  record_retention_days: 30,
  video_retention_hours: 12
})

const gpuAvailable = ref(false)
const gpuChecking = ref(false)
const gpuStatusMsg = ref('')

// ONVIF 设备扫描
const onvifOpen = ref(false)
const onvifScanning = ref(false)
const onvifFetching = ref(false)
const onvifDevices = ref<OnvifDevice[]>([])
const onvifSelected = ref<OnvifDevice | null>(null)
const onvifUser = ref('admin')
const onvifPass = ref('')

async function scanOnvif() {
  onvifScanning.value = true
  try {
    const res = await onvifApi.discover(3000)
    onvifDevices.value = res.devices || []
    onvifSelected.value = onvifDevices.value[0] || null
    onvifOpen.value = true
    if (!onvifDevices.value.length) {
      message.warning('未发现 ONVIF 设备，请确认摄像头与电脑在同一网段')
    } else {
      message.success(`发现 ${onvifDevices.value.length} 台 ONVIF 设备`)
    }
  } catch (e) {
    // 错误提示由 axios 拦截器统一展示
  } finally {
    onvifScanning.value = false
  }
}

async function fetchOnvifUri() {
  if (!onvifSelected.value) return
  onvifFetching.value = true
  try {
    const res = await onvifApi.streamUri({
      xaddr: onvifSelected.value.xaddr,
      username: onvifUser.value,
      password: onvifPass.value
    })
    form.source_url = res.rtsp_url
    const label = [res.device?.manufacturer, res.device?.model].filter(Boolean).join(' ')
    message.success(label ? `已填入 RTSP 地址（${label}）` : '已填入 RTSP 地址')
    onvifOpen.value = false
  } catch (e) {
    // 错误提示由 axios 拦截器统一展示
  } finally {
    onvifFetching.value = false
  }
}

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
  return 'http://... 或 https://...'
})

const rules = {
  name: [
    { required: true, message: '请输入任务名称', trigger: 'blur' },
    { min: 2, max: 50, message: '名称长度 2-50 个字符', trigger: 'blur' }
  ],
  task_type: [{ required: true, message: '请选择任务类型', trigger: 'change' }],
  input_types: [{ required: true, type: 'array', min: 1, message: '模型要求输入类型缺失', trigger: 'change' }],
  model_id: [{ required: true, message: '请选择模型', trigger: 'change' }],
  source_type: [{ required: true, message: '请选择数据来源', trigger: 'change' }],
  source_url: [
    { required: true, message: '请输入资源地址', trigger: 'blur' },
    {
      validator: (_rule: any, value: string) => {
        if (!value) return Promise.resolve()
        const url = value.trim().toLowerCase()
        if (form.task_type === 'stream') {
          if (!url.startsWith('rtsp://')) {
            return Promise.reject(new Error('实时视频流地址必须以 rtsp:// 开头'))
          }
        } else {
          if (url.startsWith('rtsp://')) {
            return Promise.reject(new Error('图片或视频任务不支持 RTSP 地址'))
          }
          if (!url.startsWith('http://') && !url.startsWith('https://')) {
            return Promise.reject(new Error('请输入有效的 HTTP/HTTPS 地址'))
          }
        }
        return Promise.resolve()
      },
      trigger: 'blur'
    }
  ],
  // 双光模型的 IR 地址必须真的填了可用的流地址：只填空格、写错协议都要拦住。
  source_url_ir: [
    {
      validator: (_rule: any, value: string) => {
        if (!hasIrModel.value || form.source_type === 'upload') return Promise.resolve()
        const raw = (value || '').trim()
        if (!raw) return Promise.reject(new Error('双光模型必须填写 IR 视频流地址'))
        const url = raw.toLowerCase()
        if (form.task_type === 'stream') {
          if (!url.startsWith('rtsp://')) {
            return Promise.reject(new Error('IR 地址必须以 rtsp:// 开头'))
          }
        } else if (!url.startsWith('http://') && !url.startsWith('https://')) {
          return Promise.reject(new Error('请输入有效的 HTTP/HTTPS 地址'))
        }
        if (/[\s；]/.test(raw)) {
          return Promise.reject(new Error('IR 地址不能包含空格或分号'))
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
            classList = [{ id: 'default', name: '默认类别' }]
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
    // 移除原有的手动触发逻辑，改为由模型驱动
}

// ── 多模型函数 ──

function addModel() {
    selectedModels.value.push({
        model_id: '', weight: 1.0, input_types: [], categories: [], global_threshold: 0.6
    })
}

function removeModel(idx: number) {
    selectedModels.value.splice(idx, 1)
    syncInputTypes()
    // 回到单模型时，同步 form.model_id 和类别配置
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

function openPerModelConfig(idx: number) {
    configuringModelIndex.value = idx
    showPerModelConfig.value = true
}

function handleMultiModelChange(idx: number, modelId: string) {
    const model = models.value.find(m => m.id === modelId)
    if (!model) return
    const sm = selectedModels.value[idx]
    sm.input_types = [...model.input_types]
    // 构建该模型的类别配置
    let classList: Array<{ id: string; name: string }> = []
    if (model.label_config && Object.values(model.label_config).length > 0) {
        classList = Object.entries(model.label_config).map(([id, name]) => ({ id, name: name as string }))
    } else if (model.class_names && model.class_names.length > 0) {
        classList = model.class_names.map((name: string, i: number) => ({ id: String(i), name }))
    }
    if (classList.length === 0) classList = [{ id: 'default', name: '默认类别' }]
    sm.categories = classList.map(cat => ({ ...cat, selected: true, threshold: null }))
    sm.global_threshold = 0.6
    // 同步到主 form（兼容单模型模式）
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

async function checkGpuStatus() {
  gpuChecking.value = true
  gpuStatusMsg.value = ''
  try {
    const res = await taskStore.checkGpuStatus()
    gpuAvailable.value = res.available
    if (res.available) {
      gpuStatusMsg.value = `GPU就绪: ${res.checks.device_name || '未知'}, 显存: ${res.checks.vram_mb || 0}MB`
    } else {
      gpuStatusMsg.value = `GPU不可用: ${res.reason || '环境不满足要求'}`
    }
  } catch (e: any) {
    gpuAvailable.value = false
    gpuStatusMsg.value = `GPU检测失败: ${e?.message || '未知错误'}`
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
            message.warning('请上传 RGB 文件')
            return
        }
        if (form.input_types.includes('ir') && irFiles.value.length === 0) {
            message.warning('请上传 IR 文件')
            return
        }
    } else if (hasIrModel.value && !form.source_url_ir.trim()) {
        message.warning('选中的模型包含 IR 输入，请提供 IR 源地址')
        return
    }

    const validModels = selectedModels.value.filter(sm => sm.model_id)
    if (validModels.length === 0) {
        message.warning('请至少选择一个检测模型')
        return
    }

    // 模型输入要求必须与任务输入类型严格一致：RGBIR 双光模型必须同时有 RGB + IR。
    const mismatched = validModels.find(sm => {
        const model = models.value.find(m => m.id === sm.model_id)
        if (!model) return false
        const hasRgb = model.input_types.includes('rgb')
        const hasIr = model.input_types.includes('ir')
        if (hasRgb && hasIr) {
            return !(form.input_types.includes('rgb') && form.input_types.includes('ir'))
        }
        if (hasIr && !hasRgb) return true
        return hasRgb && !form.input_types.includes('rgb')
    })
    if (mismatched) {
        message.warning('模型输入要求与任务输入类型不一致：RGBIR 双光模型必须同时提供 RGB 与 IR')
        return
    }

    const fd = new FormData()
    fd.append('name', form.name)
    fd.append('task_type', form.task_type)
    fd.append('input_types', JSON.stringify(form.input_types))
    fd.append('source_type', form.source_type)
    fd.append('description', form.description)
    fd.append('use_gpu', String(form.use_gpu))
    fd.append('record_retention_days', String(form.record_retention_days))
    if (form.task_type === 'stream') {
        fd.append('video_retention_hours', String(form.video_retention_hours))
    }

    if (validModels.length > 1) {
        // 多模型模式：发送 model_ids + fusion_config
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
        // 向后兼容：第一个模型作为主 model_id
        fd.append('model_id', validModels[0].model_id)
        fd.append('threshold', String(validModels[0].global_threshold))
    } else {
        // 单模型模式：从 selectedModels 派生，避免 form.model_id 不同步
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
        // 有 IR 模型时，用分号拼接 RGB 和 IR 源地址（两侧都 trim，避免空格混进 URL）
        const rgbUrl = form.source_url.trim()
        const irUrl = form.source_url_ir.trim()
        const url = hasIrModel.value && irUrl ? `${rgbUrl};${irUrl}` : rgbUrl
        fd.append('source_url', url)
    }

    submitting.value = true
    try {
        await taskStore.createTask(fd)
        message.success('任务创建成功')
        emit('success')
    } catch(e: any) {
        message.error('任务创建失败: ' + (e?.message || e?.detail || '未知错误'))
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
    if (config.recordRetentionDays !== undefined) {
        form.record_retention_days = config.recordRetentionDays
    }
    if (config.videoRetentionHours !== undefined) {
        form.video_retention_hours = config.videoRetentionHours
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
    if (config.recordRetentionDays !== undefined) {
        form.record_retention_days = config.recordRetentionDays
    }
    if (config.videoRetentionHours !== undefined) {
        form.video_retention_hours = config.videoRetentionHours
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
  margin-bottom: 16px;
  padding: 16px;
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 10px;
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

.onvif-list {
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--border-color, rgba(0, 0, 0, 0.08));
  border-radius: 6px;
}

.onvif-item {
  padding: 10px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--border-color, rgba(0, 0, 0, 0.06));
}

.onvif-item:last-child {
  border-bottom: none;
}

.onvif-item.active {
  background: rgba(22, 119, 255, 0.08);
}

.onvif-name {
  font-weight: 500;
  font-size: 13px;
}

.onvif-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-muted);
  word-break: break-all;
}

.onvif-empty {
  padding: 12px;
  font-size: 13px;
  color: var(--text-muted);
  background: rgba(0, 0, 0, 0.03);
  border-radius: 6px;
}

.onvif-actions {
  margin-top: 16px;
  text-align: right;
  display: flex;
  gap: 8px;
  justify-content: flex-end;
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

.summary-divider {
  color: var(--border-color);
}
</style>
