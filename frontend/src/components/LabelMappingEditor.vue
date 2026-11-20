<template>
  <div class="label-mapping-editor">
    <div class="manual-container">
      <a-table 
          :dataSource="mappingList" 
          :columns="columns" 
          size="small" 
          bordered 
          :pagination="false"
          rowKey="id"
          :scroll="{ y: 300 }"
      >
        <template #bodyCell="{ column, record, index }">
          <template v-if="column.key === 'id'">
            <a-input-number v-model:value="record.id" :min="0" size="small" />
          </template>
          <template v-if="column.key === 'name'">
            <a-input v-model:value="record.name" size="small" placeholder="smoke / fire / ..." />
          </template>
          <template v-if="column.key === 'action'">
            <a-button type="text" danger size="small" @click="removeRow(index)">
                <template #icon><DeleteOutlined /></template>
            </a-button>
          </template>
        </template>
      </a-table>
      <div class="actions">
        <a-button type="dashed" size="small" @click="addRow">
          <template #icon><PlusOutlined /></template>
          添加标签
        </a-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons-vue'

interface MappingItem {
  id: number
  name: string
}

const props = defineProps<{
  modelValue: Record<string, string> | null
}>()

const emit = defineEmits(['update:modelValue'])

const mappingList = ref<MappingItem[]>([])

const columns = [
  { title: 'ID', key: 'id', width: 100 },
  { title: '标签名称', key: 'name' },
  { title: '操作', key: 'action', width: 80 }
]

// Helper to convert internal list back to object for comparison (skipping empty rows)
const getMappingObject = () => {
  const obj: Record<string, string> = {}
  mappingList.value.forEach(item => {
    const trimmed = item.name.trim()
    if (trimmed) {
      obj[item.id.toString()] = trimmed
    }
  })
  return obj
}

// Sync internal state with prop ONLY if externally changed
watch(() => props.modelValue, (newVal) => {
  const currentObj = getMappingObject()
  const incomingObj = newVal || {}
  
  // Only overwrite if the prop represents a REAL change from another source
  if (JSON.stringify(currentObj) !== JSON.stringify(incomingObj)) {
    mappingList.value = Object.entries(incomingObj).map(([k, v]) => ({
      id: parseInt(k),
      name: v as string
    })).sort((a, b) => a.id - b.id)
  }
}, { immediate: true, deep: true })

// Sync back to parent
watch(mappingList, () => {
  const obj = getMappingObject()
  if (Object.keys(obj).length > 0 || mappingList.value.length === 0) {
    emit('update:modelValue', Object.keys(obj).length > 0 ? obj : null)
  }
}, { deep: true })

function addRow() {
  const maxId = mappingList.value.length > 0 
    ? Math.max(...mappingList.value.map(i => i.id)) 
    : -1
  mappingList.value.push({ id: maxId + 1, name: '' })
}

function removeRow(index: number) {
  mappingList.value.splice(index, 1)
}
</script>

<style scoped>
.label-mapping-editor {
  border: 1px solid var(--border-color);
  border-radius: 4px;
  padding: 10px;
  background: var(--bg-input);
}
.manual-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.actions {
  display: flex;
  justify-content: flex-start;
  gap: 8px;
  margin-top: 8px;
}
</style>
