<template>
  <div class="result-viewer">
    <div v-if="!result" class="loading-state">
      <a-empty v-if="error" description="结果加载失败或已过期" />
      <a-spin v-else tip="解析数据中..." />
    </div>
    
    <template v-else>
      <div v-if="result.type === 'image'" class="image-result">
      <div class="carousel-wrapper">
        <div class="carousel-nav prev" @click="prevImage">
          <LeftCircleOutlined />
        </div>
        
        <div class="carousel-viewport">
          <img :src="currentImageUrl" class="result-img" />
        </div>
        
        <div class="carousel-nav next" @click="nextImage">
          <RightCircleOutlined />
        </div>
        
        <div class="carousel-dots">
          <span 
            v-for="(_, i) in result.urls" 
            :key="i" 
            class="dot" 
            :class="{ active: i === currentImageIndex }"
            @click="goToImage(Number(i))"
          ></span>
        </div>
      </div>

      <div class="info-panel">
        <div class="info-header">
          <FileTextOutlined class="info-icon" />
          <span class="info-filename">{{ currentImageName }}</span>
          <span class="info-badge">{{ currentImageIndex + 1 }} / {{ result.filenames?.length || 0 }}</span>
        </div>

        <div class="info-content">
          <div class="info-section">
            <div class="section-label"><DashboardOutlined /> 检测概览</div>
            <div class="stats-grid">
              <div class="stats-item">
                <div class="stats-value">{{ currentImageDetections.length }}</div>
                <div class="stats-label">当前目标</div>
              </div>
              <div class="stats-item">
                <div class="stats-value">{{ totalImageDetections }}</div>
                <div class="stats-label">累计目标</div>
              </div>
            </div>
          </div>

          <div class="info-section expandable">
            <div class="section-label"><TagsOutlined /> 当前类别统计</div>
            <div class="tag-cloud" v-if="Object.keys(currentCategorySummary).length > 0">
              <div v-for="(count, cls) in currentCategorySummary" :key="cls" class="summary-tag">
                <span class="st-label">{{ cls }}</span>
                <span class="st-count">{{ count }}</span>
              </div>
            </div>
            <div v-else class="empty-hint">当前帧未发现目标</div>
          </div>

          <div class="info-section scrollable">
            <div class="section-label"><GlobalOutlined /> 累计类别分布</div>
            <div class="total-stats-list">
              <div v-for="(count, cls) in totalCategorySummary" :key="cls" class="stat-row">
                <span class="sr-label">{{ cls }}</span>
                <div class="sr-bar-wrapper">
                  <div class="sr-bar" :style="{ width: `${(count / (totalImageDetections || 1)) * 100}%` }"></div>
                </div>
                <span class="sr-count">{{ count }}</span>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>

    <div v-else-if="result.type === 'video'" class="video-result">
      <div class="carousel-wrapper">
        <div class="carousel-nav prev" @click="prevImage">
          <LeftCircleOutlined />
        </div>
        
        <div class="carousel-viewport">
          <video :src="currentImageUrl" controls class="result-video"></video>
        </div>
        
        <div class="carousel-nav next" @click="nextImage">
          <RightCircleOutlined />
        </div>
        
        <div class="carousel-dots">
          <span 
            v-for="(_, i) in result.urls" 
            :key="i" 
            class="dot" 
            :class="{ active: i === currentImageIndex }"
            @click="goToImage(Number(i))"
          ></span>
        </div>
      </div>

      <div class="info-panel">
        <div class="info-header">
          <FileTextOutlined class="info-icon" />
          <span class="info-filename">{{ currentImageName }}</span>
          <span class="info-badge">{{ currentImageIndex + 1 }} / {{ result.filenames.length }}</span>
        </div>
        <div class="info-content">
          <div class="info-section">
            <div class="section-label"><DashboardOutlined /> 检测概览</div>
            <div class="stats-grid">
              <div class="stats-item">
                <div class="stats-value">{{ currentImageDetections.length }}</div>
                <div class="stats-label">当前帧框</div>
              </div>
              <div class="stats-item">
                <div class="stats-value">{{ totalImageDetections }}</div>
                <div class="stats-label">累计帧框</div>
              </div>
            </div>
          </div>

          <div class="info-section expandable">
            <div class="section-label"><TagsOutlined /> 类别统计 (当前)</div>
            <div class="tag-cloud" v-if="Object.keys(currentCategorySummary).length > 0">
              <div v-for="(count, cls) in currentCategorySummary" :key="cls" class="summary-tag">
                <span class="st-label">{{ cls }}</span>
                <span class="st-count">{{ count }}</span>
              </div>
            </div>
            <div v-else class="empty-hint">当前帧未发现目标</div>
          </div>

          <div class="info-section scrollable">
            <div class="section-label"><GlobalOutlined /> 累计类别分布</div>
            <div class="total-stats-list">
              <div v-for="(count, cls) in totalCategorySummary" :key="cls" class="stat-row">
                <span class="sr-label">{{ cls }}</span>
                <div class="sr-bar-wrapper">
                  <div class="sr-bar" :style="{ width: `${(count / (totalImageDetections || 1)) * 100}%` }"></div>
                </div>
                <span class="sr-count">{{ count }}</span>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>

    <div class="actions">
      <a-button type="primary" class="download-btn" @click="downloadFile">
        <DownloadOutlined /> 下载当前结果
      </a-button>
      <a-button 
         class="download-btn download-all-btn"
         @click="downloadAll" 
         :loading="zipping"
      >
        <FolderOpenOutlined /> 下载全部结果 (ZIP)
      </a-button>
    </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { tasksApi, type TaskResult } from '@/api/tasks'
import { 
  LeftCircleOutlined, 
  RightCircleOutlined, 
  FileTextOutlined, 
  DownloadOutlined,
  FolderOpenOutlined,
  DashboardOutlined,
  TagsOutlined,
  GlobalOutlined
} from '@ant-design/icons-vue'

const props = defineProps<{ taskId: string }>()
const result = ref<any>(null)
const zipping = ref(false)
const currentImageIndex = ref(0)
const error = ref(false)

onMounted(async () => {
    error.value = false
    try {
        const res = await tasksApi.result(props.taskId)
        if (!res) throw new Error('Empty response')
        result.value = res
        console.log('[ResultViewer] Task result loaded:', {
            type: result.value?.type,
            filenames: result.value?.filenames?.length
        })
    } catch(e) {
        console.error("[ResultViewer] Failed to load task result", e)
        error.value = true
    }
})

const currentImageUrl = computed(() => {
    return result.value?.urls?.[currentImageIndex.value] || '';
})

const currentImageName = computed(() => {
    return result.value?.filenames?.[currentImageIndex.value] || '';
})

const currentImageDetections = computed(() => {
    if (!result.value?.detections) return [];
    
    const target = currentImageName.value;
    const base = target.replace(/^annotated_/, '');
    
    const detections = (result.value?.detections?.[target]) || 
           (result.value?.detections?.[base]) || 
           (result.value?.detections?.['annotated_' + base]) || [];
    
    return detections;
})

const currentCategorySummary = computed(() => {
    const summary: Record<string, number> = {};
    const detections = currentImageDetections.value;
    if (!Array.isArray(detections)) return summary;
    
    detections.forEach((d: any) => {
        if (!d) return;
        const cls = d.class_name || d.class || 'Unknown';
        summary[cls] = (summary[cls] || 0) + 1;
    });
    return summary;
})

const totalImageDetections = computed(() => {
    const allDets = result.value?.detections;
    if (!allDets || typeof allDets !== 'object') return 0;
    
    let sum = 0;
    Object.values(allDets).forEach((arr: any) => {
        if (Array.isArray(arr)) sum += arr.length;
    });
    return sum;
})

const totalCategorySummary = computed(() => {
    const summary: Record<string, number> = {};
    const allDets = result.value?.detections;
    if (!allDets || typeof allDets !== 'object') return summary;
    
    Object.values(allDets).forEach((dets: any) => {
        if (!Array.isArray(dets)) return;
        dets.forEach((d: any) => {
            if (!d) return;
            const cls = d.class_name || d.class || 'Unknown';
            summary[cls] = (summary[cls] || 0) + 1;
        });
    });
    return summary;
})

function prevImage() {
    if (currentImageIndex.value > 0) {
        currentImageIndex.value--
        console.log('[ResultViewer] Navigated to previous image:', currentImageIndex.value)
    }
}

function nextImage() {
    const maxIndex = (result.value?.urls?.length || 1) - 1
    if (currentImageIndex.value < maxIndex) {
        currentImageIndex.value++
        console.log('[ResultViewer] Navigated to next image:', currentImageIndex.value)
    }
}

function goToImage(index: number) {
    currentImageIndex.value = index
    console.log('[ResultViewer] Jumped to image:', index)
}

watch(currentImageIndex, (newIndex) => {
    console.log('[ResultViewer] Index changed to:', newIndex, 'Filename:', currentImageName.value)
})

async function downloadAll() {
  if (!result.value) return
  zipping.value = true
  try {
      const url = `/api/tasks/${props.taskId}/download`
      const token = localStorage.getItem('token')
      const response = await fetch(url, {
          headers: {
              'Authorization': `Bearer ${token}`
          }
      })
      if (!response.ok) throw new Error('ZIP generation failed')
      const blob = await response.blob()
      const downloadUrl = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.style.display = 'none'
      a.href = downloadUrl
      a.download = `${result.value?.name || 'task'}_results.zip`
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(downloadUrl)
      document.body.removeChild(a)
  } catch(err) {
      console.error(err)
  } finally {
      zipping.value = false
  }
}

async function downloadFile() {
  const url = currentImageUrl.value
        
  if (url) {
      if (url.startsWith('ws:')) return;
      try {
          const response = await fetch(url);
          const blob = await response.blob();
          const downloadUrl = window.URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.style.display = 'none';
          a.href = downloadUrl;
          a.download = url.split('/').pop() || 'result_file';
          document.body.appendChild(a);
          a.click();
          window.URL.revokeObjectURL(downloadUrl);
          document.body.removeChild(a);
      } catch (err) {
          window.open(url, '_blank');
      }
  }
}
</script>

<style scoped>
.result-viewer {
  display: flex;
  flex-direction: column;
  gap: 12px; /* Reduced from 20px */
  width: 100%;
}
.loading {
  padding: 40px;
}

.carousel-wrapper {
  position: relative;
  flex: 1;
  min-width: 0;
  background: black;
  border-radius: 8px;
  overflow: hidden;
  height: 60vh;
  min-height: 400px; /* Reduced from 450px */
}

.carousel-viewport {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #000;
}

.carousel-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 32px;
  color: rgba(255, 255, 255, 0.7);
  cursor: pointer;
  z-index: 10;
  transition: all 0.3s;
  background: rgba(0, 0, 0, 0.3);
  border-radius: 50%;
}

.carousel-nav:hover {
  color: white;
  background: rgba(0, 0, 0, 0.6);
}

.carousel-nav.prev {
  left: 12px;
}

.carousel-nav.next {
  right: 12px;
}

.carousel-dots {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 6px;
  z-index: 10;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.4);
  cursor: pointer;
  transition: all 0.3s;
}

.dot.active {
  background: white;
  width: 20px;
  border-radius: 4px;
}

.image-result, .video-result {
  display: flex;
  flex-direction: row;
  gap: 16px;
  width: 100%;
  align-items: flex-start;
}
.result-img, .result-video {
  max-width: 100%;
  max-height: 60vh;
  object-fit: contain;
  width: 100%;
  height: 100%;
  border-radius: 0;
  vertical-align: middle;
}
.result-video {
  border: none;
}

.info-panel {
  width: 320px;
  flex-shrink: 0;
  background: var(--bg-card);
  border-radius: 10px;
  border: 1px solid var(--border-color);
  overflow: hidden;
  height: 65vh;
  min-height: 500px;
  display: flex;
  flex-direction: column;
}

.info-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 20px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
}

.info-icon {
  font-size: 16px;
  color: var(--text-muted);
}

.info-filename {
  flex: 1;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: 'SF Mono', 'Fira Code', monospace;
  overflow: hidden;
  word-break: break-all;
}

.info-badge {
  font-size: 12px;
  color: var(--text-muted);
  background: var(--bg-card);
  padding: 2px 10px;
  border-radius: 10px;
  border: 1px solid var(--border-color);
  flex-shrink: 0;
}

.info-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  background: var(--bg-card);
}

.info-section {
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}

.info-section.expandable {
  flex: 1;
  overflow: hidden;
  min-height: 150px; /* Reduced from 200px */
}

.info-section.scrollable {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-height: 120px; /* Reduced from 180px */
  border-bottom: none;
}

.section-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.stats-item {
  background: var(--bg-hover);
  padding: 12px;
  border-radius: 8px;
  text-align: center;
  border: 1px solid var(--border-color);
}

.stats-value {
  font-size: 20px;
  font-weight: 700;
  color: var(--primary-color);
  line-height: 1.2;
}

.stats-label {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
}

.tag-cloud {
  display: flex;
  flex-wrap: wrap;
  align-content: flex-start; /* Prevent tags from stretching vertically */
  gap: 8px;
  flex: 1; /* Allow to grow in expandable section */
  overflow-y: auto;
  padding-right: 4px;
}

.tag-cloud::-webkit-scrollbar,
.total-stats-list::-webkit-scrollbar {
  width: 4px;
}

.tag-cloud::-webkit-scrollbar-thumb,
.total-stats-list::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 2px;
}

.summary-tag {
  display: inline-flex;
  align-items: center;
  background: var(--bg-hover);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  overflow: hidden;
  font-size: 11px;
}

.st-label {
  padding: 3px 8px;
  background: rgba(232, 93, 38, 0.1);
  color: #e85d26;
  font-weight: 600;
}

.st-count {
  padding: 3px 8px;
  background: var(--bg-card);
  color: var(--text-primary);
}

.total-stats-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  padding-right: 8px;
  flex: 1;
}

.stat-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sr-label {
  width: 60px;
  font-size: 12px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sr-bar-wrapper {
  flex: 1;
  height: 6px;
  background: var(--border-color);
  border-radius: 3px;
  overflow: hidden;
}

.sr-bar {
  height: 100%;
  background: var(--primary-color);
  border-radius: 3px;
  transition: width 0.6s ease;
}

.sr-count {
  width: 30px;
  font-size: 11px;
  color: var(--text-muted);
  text-align: right;
}

.empty-hint {
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
  padding: 10px;
  font-style: italic;
}


.actions {
  display: flex;
  gap: 12px;
  justify-content: center;
  padding-top: 4px; /* Reduced from 10px */
}
.download-btn {
    width: 200px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
}
.download-all-btn {
    background: var(--success-green);
    color: white;
    border-color: var(--success-green);
}
</style>
