<template>
  <span class="model-badge" :title="title">
    <span v-if="stageLabel" class="model-badge__stage">{{ stageLabel }}</span>
    <span v-if="profileLabel" class="model-badge__profile">{{ profileLabel }}</span>
    <span class="model-badge__model">{{ model || '—' }}</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  model: { type: String, default: '' },
  profileLabel: { type: String, default: '' },
  stage: { type: String, default: '' }
})

const { t, te } = useI18n()

const stageLabel = computed(() => {
  if (!props.stage) return ''
  const key = `llm.stage.${props.stage}`
  return te(key) ? t(key) : props.stage
})

const title = computed(() =>
  [stageLabel.value, props.profileLabel, props.model].filter(Boolean).join(' · ')
)
</script>

<style scoped>
.model-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--mf-space-1);
  max-width: 100%;
  padding: 0 var(--mf-space-2);
  border: 1px solid var(--mf-gray-400);
  border-radius: var(--mf-radius-pill);
  background: var(--mf-white);
  color: var(--mf-black);
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-xs);
  font-weight: 500;
  line-height: 2;
  letter-spacing: 0;
  white-space: nowrap;
  vertical-align: middle;
}

.model-badge > * + *::before {
  content: '·';
  margin-right: var(--mf-space-1);
  color: var(--mf-gray-500);
}

.model-badge__stage {
  color: var(--mf-gray-500);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.model-badge__profile {
  font-weight: 700;
}

.model-badge__model {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 22ch;
}
</style>
