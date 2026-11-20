<template>
  <div class="form-section data-source-section">
    <div class="section-title">数据来源</div>

    <div v-if="!hasAnyModelSelected" class="model-not-selected-placeholder">
      <div class="placeholder-content">
        <InfoCircleOutlined class="icon" />
        <span>请先选择模型，系统将根据模型要求自动开放上传区域</span>
      </div>
    </div>

    <div v-else class="data-source-content fade-in">
      <a-form-item name="source_type" class="source-type-item">
        <a-radio-group v-model:value="form.source_type" button-style="solid" @change="emit('source-change')">
          <a-radio-button value="upload" :disabled="!allowedSourceTypes.upload">
            <UploadOutlined /> 文件上传
          </a-radio-button>
          <a-radio-button value="url" :disabled="!allowedSourceTypes.url">
            <LinkOutlined /> URL链接
          </a-radio-button>
          <a-radio-button value="rtsp" :disabled="!allowedSourceTypes.rtsp">
            <VideoCameraOutlined /> RTSP流
          </a-radio-button>
        </a-radio-group>
      </a-form-item>

      <template v-if="form.source_type === 'upload'">
        <div class="upload-grid">
          <div class="upload-row" v-if="form.input_types.includes('rgb')">
            <div class="upload-label">
              <span class="required-mark">*</span> RGB 文件
            </div>
            <div class="upload-zone" @click="triggerUpload('rgb')">
              <CloudUploadOutlined class="icon" />
              <div class="text">点击上传 RGB {{ form.task_type === 'image' ? '图片' : '视频' }}</div>
              <div class="files" v-if="rgb.length">
                <CheckCircleOutlined /> {{ rgb.length }} 个文件已选择
              </div>
            </div>
            <input type="file" multiple ref="rgbInput" style="display:none" @change="onFileChange($event, 'rgb')" />
          </div>

          <div class="upload-row" v-if="form.input_types.includes('ir')">
            <div class="upload-label">
              <span class="required-mark">*</span> IR 文件
            </div>
            <div class="upload-zone" @click="triggerUpload('ir')">
              <CloudUploadOutlined class="icon" />
              <div class="text">点击上传 IR {{ form.task_type === 'image' ? '图片' : '视频' }}</div>
              <div class="files" v-if="ir.length">
                <CheckCircleOutlined /> {{ ir.length }} 个文件已选择
              </div>
            </div>
            <input type="file" multiple ref="irInput" style="display:none" @change="onFileChange($event, 'ir')" />
          </div>
        </div>
      </template>

      <a-form-item v-else label="RGB 源地址" name="source_url">
        <a-input
          v-model:value="form.source_url"
          :placeholder="hasIrModel ? 'RGB 视频流地址' : sourceUrlPlaceholder"
        >
          <template #prefix>
            <LinkOutlined style="color: var(--text-muted)" />
          </template>
        </a-input>
      </a-form-item>
      <div v-if="form.source_type === 'rtsp'" class="onvif-scan-row">
        <a-button size="small" :loading="onvifScanning" @click="emit('scan-onvif')">
          <WifiOutlined /> 扫描局域网 ONVIF 设备
        </a-button>
        <span class="onvif-scan-hint">支持海康/大华/宇视等标准 ONVIF 摄像头</span>
      </div>
      <a-form-item
        v-if="form.source_type !== 'upload' && hasIrModel"
        label="IR 源地址"
        name="source_url_ir"
        required
      >
        <a-input v-model:value="form.source_url_ir" placeholder="IR 视频流地址（双光模型必填）">
          <template #prefix>
            <LinkOutlined style="color: var(--text-muted)" />
          </template>
        </a-input>
      </a-form-item>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  CheckCircleOutlined,
  CloudUploadOutlined,
  InfoCircleOutlined,
  LinkOutlined,
  UploadOutlined,
  VideoCameraOutlined,
  WifiOutlined,
} from '@ant-design/icons-vue'

interface SourceForm {
  task_type: string
  input_types: string[]
  source_type: string
  source_url: string
  source_url_ir: string
}

interface SourceTypes {
  upload: boolean
  url: boolean
  rtsp: boolean
}

const props = defineProps<{
  form: SourceForm
  hasAnyModelSelected: boolean
  hasIrModel: boolean
  allowedSourceTypes: SourceTypes
  sourceUrlPlaceholder: string
  onvifScanning: boolean
  rgbFiles: File[]
  irFiles: File[]
}>()

const emit = defineEmits<{
  'update:rgbFiles': [files: File[]]
  'update:irFiles': [files: File[]]
  'source-change': []
  'scan-onvif': []
}>()

const rgbInput = ref<HTMLInputElement>()
const irInput = ref<HTMLInputElement>()

const rgb = computed({
  get: () => props.rgbFiles,
  set: (files: File[]) => emit('update:rgbFiles', files),
})
const ir = computed({
  get: () => props.irFiles,
  set: (files: File[]) => emit('update:irFiles', files),
})

function triggerUpload(type: 'rgb' | 'ir') {
  if (type === 'rgb') rgbInput.value?.click()
  else irInput.value?.click()
}

function onFileChange(e: Event, type: 'rgb' | 'ir') {
  const target = e.target as HTMLInputElement
  if (!target.files) return
  const files = Array.from(target.files)
  const maxFileSize = 20 * 1024 * 1024 * 1024 // 20GB
  for (const file of files) {
    if (file.size > maxFileSize) {
      message.error(
        `文件 "${file.name}" 大小 ${(file.size / 1024 / 1024).toFixed(1)}MB 已超出 20GB 系统最大文件大小限制！`
      )
      target.value = ''
      return
    }
  }
  if (type === 'rgb') rgb.value = files
  else ir.value = files
}
</script>

<style scoped>
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

.source-type-item {
  margin-bottom: 16px;
}

.upload-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

@media (max-width: 760px) {
  .upload-grid { grid-template-columns: 1fr; }
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

.onvif-scan-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: -8px 0 16px;
}

.onvif-scan-hint {
  font-size: 12px;
  color: var(--text-muted);
}
</style>
