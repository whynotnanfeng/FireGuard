<template>
  <div class="result-viewer">
    <div v-if="!result" class="loading">
      <a-spin tip="加载结果中..." />
    </div>
    
    <div v-else-if="result.type === 'image'" class="image-result">
      <a-carousel arrows :autoplay="false" @afterChange="onCarouselChange" class="carousel-box">
        <template #prevArrow>
          <div class="custom-slick-arrow" style="left: 10px; z-index: 1;">
             <LeftCircleOutlined />
          </div>
        </template>
        <template #nextArrow>
          <div class="custom-slick-arrow" style="right: 10px;">
            <RightCircleOutlined />
          </div>
        </template>
        <div v-for="(url, index) in result.urls" :key="index">
          <img :src="url" class="result-img" />
        </div>
      </a-carousel>

      <div class="summary">
        <h4>文件: {{ currentImageName }}</h4>
        <p>当前突破目标: {{ currentImageDetections.length }} 个 | 所有图片共计: {{ totalImageDetections }} 个</p>
        <div class="tags">
            <a-tag v-for="(d, i) in currentImageDetections" :key="i" color="error" style="margin: 4px;">
                {{d.class}} ({{(d.confidence*100).toFixed(0)}}%)
            </a-tag>
        </div>
      </div>
    </div>

    <div v-else-if="result.type === 'video'" class="video-result">
      <a-carousel arrows :autoplay="false" @afterChange="onCarouselChange" class="carousel-box">
        <template #prevArrow>
          <div class="custom-slick-arrow" style="left: 10px; z-index: 1;">
             <LeftCircleOutlined />
          </div>
        </template>
        <template #nextArrow>
          <div class="custom-slick-arrow" style="right: 10px;">
            <RightCircleOutlined />
          </div>
        </template>
        <div v-for="(url, index) in result.urls" :key="index">
          <video :src="url" controls class="result-video"></video>
        </div>
      </a-carousel>

      <div class="summary">
        <h4>文件: {{ currentImageName }}</h4>
        <p>该视频累计圈出目标: {{ currentImageDetections.length }} 帧框 | 总共计: {{ totalImageDetections }} 帧框</p>
      </div>
    </div>

    <div class="actions">
      <a-button type="primary" class="download-btn" @click="downloadFile">
        下载当前结果
      </a-button>
      <a-button 
         class="download-btn" 
         style="background: var(--success-green); color: white; border-color: var(--success-green);" 
         @click="downloadAll" 
         :loading="zipping"
      >
        下载全部结果 (ZIP)
      </a-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { tasksApi, type TaskResult } from '@/api/tasks'
import { LeftCircleOutlined, RightCircleOutlined } from '@ant-design/icons-vue'

const props = defineProps<{ taskId: string }>()
const result = ref<any>(null)
const zipping = ref(false)

const currentImageIndex = ref(0)

onMounted(async () => {
    try {
        result.value = await tasksApi.result(props.taskId)
    } catch(e) {
        console.error("Failed to load task result", e)
    }
})

const currentImageName = computed(() => {
    return result.value?.filenames?.[currentImageIndex.value] || '';
})

const currentImageDetections = computed(() => {
    if (!result.value?.detections) return [];
    const filename = 'annotated_' + currentImageName.value.replace('annotated_', '');
    // The dictionary keys have 'annotated_' prefix
    return result.value.detections[filename] || [];
})

const totalImageDetections = computed(() => {
    if (!result.value?.detections) return 0;
    return Object.values(result.value.detections).reduce((sum: number, dets: any) => sum + dets.length, 0);
})

function onCarouselChange(index: number) {
    currentImageIndex.value = index;
}

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
  const url = (result.value?.urls && result.value.urls.length > 0) 
        ? result.value.urls[currentImageIndex.value] 
        : result.value?.url;
        
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
  gap: 20px;
  align-items: center;
}
.loading {
  padding: 40px;
}
.carousel-box {
  width: 100%;
  max-width: 800px;
  background: black;
  border-radius: 8px;
  padding-bottom: 30px; /* space for dots */
}
/* Ant Design Carousel specific styles to make arrows visible */
:deep(.slick-slide) {
  text-align: center;
  height: 60vh;
  line-height: 60vh;
  background: #000;
  overflow: hidden;
}
.custom-slick-arrow {
  width: 35px;
  height: 35px;
  font-size: 35px;
  color: rgba(255,255,255,0.7);
  transition: ease all 0.3s;
  top: 50%;
  transform: translateY(-50%);
}
.custom-slick-arrow:before {
  display: none;
}
.custom-slick-arrow:hover {
  color: white;
  opacity: 1;
}
.image-result {
  width: 100%;
}
.result-img, .result-video {
  max-width: 100%;
  max-height: 60vh;
  object-fit: contain;
  width: 100%;
  height: 100%;
  border-radius: 8px;
  vertical-align: middle;
}
.result-video {
  border: 1px solid var(--border-color);
}
.summary {
  background: var(--bg-card);
  padding: 16px;
  border-radius: 8px;
  width: 100%;
  text-align: center;
  margin-top: 10px;
  border: 1px solid var(--border-color);
}
.actions {
  display: flex;
  gap: 12px;
}
.download-btn {
    width: 200px;
}
</style>
