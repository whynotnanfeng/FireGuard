<template>
  <div class="form-section">
    <div class="section-title">模型配置</div>

    <div v-for="(sm, idx) in selectedModels" :key="idx" class="multi-model-row">
      <div class="model-row-header">
        <span class="model-row-label">模型 {{ idx + 1 }}</span>
        <a-button v-if="selectedModels.length > 1" type="link" size="small" danger @click="emit('remove-model', idx)">
          移除
        </a-button>
      </div>
      <div class="form-row">
        <a-form-item label="选择模型" class="form-col-2">
          <a-select
            v-model:value="sm.model_id"
            placeholder="请选择检测模型"
            :loading="loadingModels"
            @change="(val: string) => emit('model-change', idx, val)"
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
        <a-form-item label="权重" class="form-col-1">
          <div class="slider-with-value">
            <a-slider v-model:value="sm.weight" :min="0.1" :max="3.0" :step="0.1" />
            <a-input-number
              v-model:value="sm.weight"
              :min="0.1"
              :max="3.0"
              :step="0.1"
              :precision="1"
              size="small"
              class="slider-value-input"
            />
          </div>
        </a-form-item>
      </div>
      <div v-if="sm.model_id" class="model-requirement-hint fade-in">
        <span class="hint-label">输入要求:</span>
        <a-tag
          v-if="sm.input_types.includes('rgb') && sm.input_types.includes('ir')"
          color="gold"
          class="requirement-tag"
        >
          RGBIR 双光
        </a-tag>
        <a-tag v-for="t in sm.input_types" :key="t" color="blue" class="requirement-tag">
          {{ t.toUpperCase() }}
        </a-tag>
        <template v-if="selectedModels.length > 1">
          <a-button size="small" style="margin-left: 12px" @click="emit('configure-model', idx)">
            配置类别和阈值
          </a-button>
          <span class="config-summary">
            {{ sm.categories.filter(c => c.selected).length }}/{{ sm.categories.length }} 类别已启用
          </span>
        </template>
      </div>
    </div>

    <a-button type="dashed" block @click="emit('add-model')" class="add-model-btn">
      + 添加模型
    </a-button>

    <div v-if="selectedModels.length > 1" class="fusion-config-section">
      <div class="config-header">
        <span class="config-title">融合配置</span>
      </div>
      <a-form-item label="WBF IoU 阈值">
        <div class="slider-with-value">
          <a-slider v-model:value="fusionConfig.wbf_iou_threshold" :min="0.3" :max="0.8" :step="0.05" />
          <a-input-number
            v-model:value="fusionConfig.wbf_iou_threshold"
            :min="0.3"
            :max="0.8"
            :step="0.05"
            :precision="2"
            size="small"
            class="slider-value-input"
          />
        </div>
        <div class="config-hint">同类别检测框 IoU 超过此值时融合（默认 0.55）</div>
      </a-form-item>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { DetectionModel } from '@/api/models'

interface SelectedModelConfig {
  model_id: string
  weight: number
  input_types: string[]
  categories: Array<{ id: string; name: string; selected: boolean; threshold: number | null }>
  global_threshold: number
}

defineProps<{
  selectedModels: SelectedModelConfig[]
  filteredModels: DetectionModel[]
  loadingModels: boolean
  fusionConfig: { wbf_iou_threshold: number }
  isModelSelectedByOther: (modelId: string, currentIdx: number) => boolean
}>()

const emit = defineEmits<{
  'add-model': []
  'remove-model': [index: number]
  'model-change': [index: number, modelId: string]
  'configure-model': [index: number]
}>()
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

.multi-model-row {
  padding: 14px;
  margin-bottom: 12px;
  border: 1px solid var(--border-color);
  border-radius: 10px;
  background: var(--bg-input);
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

/* 滑块 + 数值输入：只看滑块分不清 0.8 和 0.85，权重必须给出具体数值 */
.slider-with-value {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}

.slider-with-value :deep(.ant-slider) {
  flex: 1;
  margin: 4px 0;
}

.slider-value-input {
  width: 78px;
  flex-shrink: 0;
}
</style>
