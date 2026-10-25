<script setup lang="ts">
import { SIMILARITY_DISCLAIMER as REQUIRED } from '@/utils/disclaimer'
import { computed } from 'vue'

const props = defineProps<{
  text?: string
}>()

const shown = computed(() => {
  const text = props.text?.trim()
  if (!text) return REQUIRED
  if (text.includes(REQUIRED)) return text
  return `${REQUIRED}。${text}`
})
</script>

<template>
  <div class="disclaimer-strip" role="note">{{ shown }}</div>
</template>
