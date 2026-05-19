<template>
  <a-modal 
    title="检测配置"
    :open="true"
    @cancel="$emit('close')"
    width="720px"
    destroyOnClose
    class="detection-config-modal"
  >
    <div class="config-content">
      <div class="config-section">
        <div class="section-header">
          <span class="section-title">类别选择与阈值配置</span>
          <a-checkbox 
            v-model:checked="selectAll" 
            @change="handleSelectAll"
            class="select-all-checkbox"
          >
            全选
          </a-checkbox>
        </div>
        
        <a-input-search
          v-model:value="searchText"
          placeholder="搜索类别..."
          size="small"
          class="category-search"
          allow-clear
        />
        
        <div class="category-list">
          <div 
            v-for="cat in filteredCategories" 
            :key="cat.id" 
            class="category-row"
          >
            <a-checkbox 
              v-model:checked="cat.selected" 
              @change="onCategoryChange(cat)"
            >
              {{ cat.name }}
            </a-checkbox>
            
            <div class="threshold-input">
              <span class="threshold-label">阈值:</span>
              <a-input-number 
                v-model:value="cat.threshold" 
                :min="CONF_FLOOR" 
                :max="1" 
                :step="0.01"
                :disabled="!cat.selected"
                size="small"
                style="width: 100px"
                :placeholder="`≥ ${CONF_FLOOR}`"
              />
            </div>
          </div>
        </div>
        
        <div class="config-explanation">
          <InfoCircleOutlined class="info-icon" />
          <div class="explanation-text">
            <p><strong>公共阈值:</strong> 对所有选中类别生效的默认阈值。范围 0.25–1.00（推荐 0.40–0.80）。</p>
            <p><strong>类别阈值:</strong> 针对特定类别的独立阈值，优先级高于公共阈值。留空则使用公共阈值。</p>
            <p><strong>类别选择:</strong> 未选中的类别在检测时将被忽略。</p>
            <p class="floor-hint"><WarningOutlined /> 系统推理最低门限为 {{ CONF_FLOOR }}，设置低于该值时没有额外效果。</p>
          </div>
        </div>
        
        <div class="global-threshold">
          <span class="threshold-label">公共阈值:</span>
          <a-input-number 
            v-model:value="globalThreshold" 
            :min="CONF_FLOOR" 
            :max="1" 
            :step="0.01"
            size="small"
            style="width: 120px"
            :placeholder="`≥ ${CONF_FLOOR}`"
          />

          <span class="floor-badge">最低门限 {{ CONF_FLOOR }}</span>
        </div>
      </div>
    </div>

    <template #footer>
      <a-button @click="$emit('close')">取消</a-button>
      <a-button type="primary" :loading="submitting" @click="submit">保存配置</a-button>
    </template>
  </a-modal>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { tasksApi } from '@/api/tasks'
import { modelsApi } from '@/api/models'
import { message } from 'ant-design-vue'
import { InfoCircleOutlined, WarningOutlined } from '@ant-design/icons-vue'

// 系统推理最低门限（与后端 ONNX NMS conf 保持一致）
// 用户设置低于该値时，后端在 NMS 阶段已丢弃这些候选框，设置无效果
const CONF_FLOOR = 0.25

const props = defineProps<{
  taskId: string
  modelId: string
  initialCategories?: Array<{ id: string; name: string; selected: boolean; threshold: number | null }>
  initialGlobalThreshold?: number
}>()

const emit = defineEmits(['close', 'success'])

interface CategoryItem {
  id: string
  name: string
  selected: boolean
  threshold: number | null
}

const categories = ref<CategoryItem[]>([])
const globalThreshold = ref<number>(0.4)
const selectAll = ref(true)
const submitting = ref(false)
const searchText = ref('')

const filteredCategories = computed(() => {
  if (!searchText.value.trim()) return categories.value
  const keyword = searchText.value.trim().toLowerCase()
  return categories.value.filter(c => c.name.toLowerCase().includes(keyword))
})

onMounted(async () => {
  try {
    const model = await modelsApi.get(props.modelId)
    
    let classList: Array<{ id: string; name: string }> = []
    
    if (model.label_config && Object.values(model.label_config).length > 0) {
      classList = Object.entries(model.label_config).map(([id, name]) => ({ id, name }))
    } else if (model.class_names && model.class_names.length > 0) {
      classList = model.class_names.map((name: string, idx: number) => ({ id: String(idx), name }))
    }
    
    if (classList.length === 0) {
      message.warning('该模型未配置类别信息，将使用默认设置')
      classList = [{ id: 'default', name: '默认类别' }]
    }
    
    let existingConfig: any = null
    
    if (props.taskId) {
      try {
        const task = await tasksApi.get(props.taskId)
        existingConfig = task.detection_config
      } catch (e) {
        console.warn('Failed to load task config, using defaults')
      }
    } else if (props.initialCategories && props.initialCategories.length > 0) {
      existingConfig = {
        global_threshold: props.initialGlobalThreshold,
        categories: props.initialCategories
      }
    }
    
    categories.value = classList.map((cat: any) => {
      let selected = true
      let threshold: number | null = null
      
      if (existingConfig?.categories) {
        const existing = existingConfig.categories.find((c: any) => c.id === cat.id)
        if (existing) {
          selected = existing.selected !== false
          threshold = existing.threshold ?? null
        }
      }
      
      return {
        id: cat.id,
        name: cat.name,
        selected,
        threshold
      }
    })
    
    // V1.4.7: Smart Defaulting - Use 0.4 as default for better recall
    if (existingConfig?.global_threshold !== undefined && existingConfig.global_threshold !== null) {
      globalThreshold.value = existingConfig.global_threshold
    } else if (props.initialGlobalThreshold !== undefined) {
      globalThreshold.value = props.initialGlobalThreshold
    } else if (model.default_threshold !== undefined) {
      globalThreshold.value = (model.default_threshold < 0.4) ? 0.4 : model.default_threshold
    } else {
      globalThreshold.value = 0.4
    }
    

  } catch (e) {
    console.error('Failed to load config:', e)
    message.error('加载配置失败')
  }
})

function handleSelectAll() {
  const newState = selectAll.value
  categories.value.forEach(cat => {
    cat.selected = newState
    if (!newState) {
      cat.threshold = null
    }
  })
}

function onCategoryChange(cat: CategoryItem) {
  if (!cat.selected) {
    cat.threshold = null
  }
  
  selectAll.value = categories.value.every(c => c.selected)
}

async function submit() {
  submitting.value = true
  try {
    const config = {
      global_threshold: globalThreshold.value,
      categories: categories.value.map(cat => ({
        id: cat.id,
        name: cat.name,
        selected: cat.selected,
        threshold: cat.selected ? cat.threshold : null
      }))
    }
    
    if (props.taskId) {
      await tasksApi.updateDetectionConfig(props.taskId, {
        detection_config: config
      })
    }
    
    message.success('配置保存成功')
    emit('success', {
      categories: categories.value,
      globalThreshold: globalThreshold.value
    })
  } catch (e) {
    console.error('Failed to save detection config:', e)
    message.error('配置保存失败')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.config-content {
  max-height: 65vh;
  overflow-y: auto;
  padding-right: 8px;
}

.config-section {
  margin-bottom: 24px;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-color);
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.select-all-checkbox {
  font-weight: 500;
}

.category-search {
  margin-bottom: 12px;
}

.category-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 280px;
  overflow-y: auto;
  margin-bottom: 20px;
  padding-right: 4px;
}

.category-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: var(--bg-input);
  border-radius: 6px;
  border: 1px solid var(--border-color);
  flex-shrink: 0;
}

.threshold-input {
  display: flex;
  align-items: center;
  gap: 8px;
}

.threshold-label {
  font-size: 13px;
  color: var(--text-secondary);
}

.threshold-unit {
  font-size: 13px;
  color: var(--text-secondary);
  margin-left: -4px;
}

.default-hint {
  font-size: 12px;
  color: var(--text-muted);
}

.config-explanation {
  display: flex;
  gap: 8px;
  padding: 12px;
  background: rgba(0, 102, 204, 0.04);
  border-radius: 6px;
  margin-bottom: 16px;
}

.info-icon {
  color: var(--primary-blue);
  font-size: 16px;
  margin-top: 2px;
}

.explanation-text {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.explanation-text p {
  margin: 4px 0;
}

.floor-hint {
  color: #d48806 !important;
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 6px !important;
  font-weight: 500;
}

.floor-badge {
  font-size: 11px;
  padding: 1px 8px;
  background: rgba(212, 136, 6, 0.05);
  color: #d48806;
  border: 1px solid rgba(212, 136, 6, 0.2);
  border-radius: 10px;
  white-space: nowrap;
}

.global-threshold {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
  background: var(--bg-input);
  border-radius: 6px;
  border: 1px solid var(--border-color);
}
</style>
