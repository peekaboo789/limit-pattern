<script setup lang="ts">
import AppShell from '@/components/AppShell.vue'
</script>

<template>
  <AppShell>
    <!-- 切换左侧页面不卸载：结果、筛选、进度都留着。设置页每次重新读取，避免显示旧值。 -->
    <router-view v-slot="{ Component, route }">
      <keep-alive :exclude="['SettingsView']" :max="12">
        <component :is="Component" :key="route.name" />
      </keep-alive>
    </router-view>
  </AppShell>
</template>
