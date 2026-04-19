<template>
  <a-modal 
    title="新建检测任务"
    :open="true"
    @cancel="$emit('close')"
    width="650px"
    destroyOnClose
  >
    <a-form :model="form" :rules="rules" ref="formRef" :label-col="{ span: 5 }" :wrapper-col="{ span: 19 }">
      
      <a-form-item label="任务名称" name="name">
        <a-input v-model:value="form.name" placeholder="请输入任务名称" :maxlength="50" showCount />
      </a-form-item>

      <a-form-item label="任务类型" name="task_type">
        <a-select v-model:value="form.task_type" placeholder="请选择" style="width: 100%">
          <a-select-option value="image">图片检测</a-select-option>
          <a-select-option value="video">视频检测</a-select-option>
          <a-select-option value="stream">实时视频流</a-select-option>
        </a-select>
      </a-form-item>

      <a-form-item label="输入类型" name="input_types">
        <a-checkbox-group v-model:value="form.input_types" @change="handleTypeChange">
          <a-checkbox value="rgb">RGB 图像</a-checkbox>
          <a-checkbox value="ir">红外(IR) 图像</a-checkbox>
        </a-checkbox-group>
      </a-form-item>

      <a-form-item label="选择模型" name="model_id">
        <a-select 
          v-model:value="form.model_id" 
          placeholder="请选择支持所选输入类型的模型" 
          style="width: 100%"
          :disabled="!form.input_types.length || loadingModels"
        >
          <a-select-option 
            v-for="m in filteredModels" 
            :key="m.id" 
            :value="m.id" 
          >
            {{ m.name }} (支持: {{ m.input_types.join(', ') }})
          </a-select-option>
        </a-select>
      </a-form-item>

      <a-form-item label="来源类型" name="source_type">
        <a-radio-group v-model:value="form.source_type">
          <a-radio-button value="upload" :disabled="form.task_type==='stream'">文件上传</a-radio-button>
          <a-radio-button value="url" :disabled="form.task_type==='stream'">URL链接</a-radio-button>
          <a-radio-button value="rtsp">RTSP流</a-radio-button>
        </a-radio-group>
      </a-form-item>

      <template v-if="form.source_type === 'upload'">
        <a-form-item label="RGB 文件" v-if="form.input_types.includes('rgb')" required>
          <div class="upload-zone" @click="triggerUpload('rgb')">
            <CloudUploadOutlined class="icon" />
            <div class="text">点击或拖拽上传 RGB {{form.task_type==='image'?'图片':'视频'}}</div>
            <div class="files" v-if="rgbFiles.length">{{rgbFiles.length}} 个文件已选择</div>
          </div>
          <input type="file" multiple ref="rgbInput" style="display:none" @change="onFileChange($event, 'rgb')" />
        </a-form-item>

        <a-form-item label="IR 文件" v-if="form.input_types.includes('ir')" required>
          <div class="upload-zone" @click="triggerUpload('ir')">
            <CloudUploadOutlined class="icon" />
            <div class="text">点击上传 IR {{form.task_type==='image'?'图片':'视频'}}</div>
            <div class="files" v-if="irFiles.length">{{irFiles.length}} 个文件已选择</div>
          </div>
          <input type="file" multiple ref="irInput" style="display:none" @change="onFileChange($event, 'ir')" />
        </a-form-item>
      </template>

      <template v-else>
        <a-form-item label="资源地址" name="source_url">
          <a-input v-model:value="form.source_url" placeholder="http://... 或 rtsp://..." />
        </a-form-item>
      </template>

      <a-form-item label="任务描述">
        <a-textarea v-model:value="form.description" :rows="2" />
      </a-form-item>

    </a-form>

    <template #footer>
      <a-button @click="$emit('close')">取消</a-button>
      <a-button type="primary" :loading="submitting" @click="submit">确定新建</a-button>
    </template>
  </a-modal>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { modelsApi, type DetectionModel } from '@/api/models'
import { useTaskStore } from '@/stores/task'
import { message } from 'ant-design-vue'
import { CloudUploadOutlined } from '@ant-design/icons-vue'

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

const form = reactive({
  name: '',
  task_type: 'image',
  input_types: ['rgb'],
  model_id: '',
  source_type: 'upload',
  source_url: '',
  description: ''
})

const rules = {
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  task_type: [{ required: true }],
  input_types: [{ required: true, type: 'array', min: 1, message: '请至少选择一种输入类型' }],
  model_id: [{ required: true, message: '请选择模型', trigger: 'change' }],
}

onMounted(async () => {
    loadingModels.value = true
    try {
        const res = await modelsApi.list({ limit: 100 })
        models.value = res.items
    } finally {
        loadingModels.value = false
    }
})

// Filter models based on selected input_types
const filteredModels = computed(() => {
    if (!form.input_types.length) return []
    return models.value.filter(m => {
        // Model must support ALL selected input types
        return form.input_types.every(reqType => m.input_types.includes(reqType))
    })
})

function handleTypeChange() {
    form.model_id = '' // Reset model when types change
}

function triggerUpload(type: 'rgb'|'ir') {
    if (type === 'rgb') rgbInput.value?.click()
    else irInput.value?.click()
}

function onFileChange(e: Event, type: 'rgb'|'ir') {
    const target = e.target as HTMLInputElement
    if (target.files) {
        const files = Array.from(target.files)
        if (type === 'rgb') rgbFiles.value = files
        else irFiles.value = files
    }
}

async function submit() {
    if (!formRef.value) return
    try {
        await formRef.value.validate()
    } catch(err) {
        return
    }

    // Validate files if upload
    if (form.source_type === 'upload') {
        if (form.input_types.includes('rgb') && rgbFiles.value.length === 0) {
            message.warning('请上传 RGB 文件')
            return
        }
        if (form.input_types.includes('ir') && irFiles.value.length === 0) {
            message.warning('请上传 IR 文件')
            return
        }
    } else {
         if (!form.source_url) {
             message.warning('请输入资源地址')
             return
         }
    }

    const fd = new FormData()
    fd.append('name', form.name)
    fd.append('task_type', form.task_type)
    fd.append('input_types', JSON.stringify(form.input_types))
    fd.append('model_id', form.model_id)
    fd.append('source_type', form.source_type)
    fd.append('description', form.description)

    if (form.source_type === 'upload') {
        rgbFiles.value.forEach(f => fd.append('rgb_files', f))
        irFiles.value.forEach(f => fd.append('ir_files', f))
    } else {
        fd.append('source_url', form.source_url)
    }

    submitting.value = true
    try {
        await taskStore.createTask(fd)
        message.success('任务创建成功')
        emit('success')
    } catch(e) {
        // interceptor handles error message
    } finally {
        submitting.value = false
    }
}
</script>

<style scoped>
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
