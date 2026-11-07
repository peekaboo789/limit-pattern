<script setup lang="ts">
import { dismiss, monitorState, startMonitorEvents } from '@/stores/monitor'
import { startWatchlist } from '@/stores/watchlist'
import { onMounted } from 'vue'

onMounted(() => {
  startMonitorEvents()
  startWatchlist()
})
</script>

<template>
  <div class="toast-stack" aria-live="assertive">
    <div v-for="toast in monitorState.toasts" :key="toast.id" class="toast" :class="toast.level" role="alert">
      <div class="toast-title">{{ toast.title }}</div>
      <div class="toast-text">{{ toast.text }}</div>
      <button class="toast-close" type="button" aria-label="关闭" @click="dismiss(toast.id)">×</button>
    </div>
  </div>
</template>
