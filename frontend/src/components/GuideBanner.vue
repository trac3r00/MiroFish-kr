<template>
  <div class="guide-banner" :class="chipState" role="status" aria-live="polite">
    <span class="guide-progress">{{ $t('guide.progress', { current: step, name: stepName }) }}</span>
    <span class="guide-chip">{{ $t(`guide.chip.${chipState}`) }}</span>
    <span class="guide-text">{{ message }}</span>
    <span class="guide-bar" role="progressbar" aria-valuemin="0" aria-valuemax="5" :aria-valuenow="step">
      <span class="guide-bar-fill" :style="{ width: `${(step / 5) * 100}%` }"></span>
    </span>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  step: { type: Number, required: true },
  // working: nothing to do but wait; action/choose: the user has the next move
  // (choose = pick options before work starts); ingesting: step 3 rounds are done but
  // graph memory is still being written; error: something failed
  state: { type: String, default: 'working' },
})

const { t, tm } = useI18n()

const NEXT_BUTTON_KEYS = {
  1: 'step1.enterEnvSetup',
  2: 'step2.startDualWorldSim',
  3: 'step3.startGenerateReportBtn',
  4: 'step4.downloadReport',
}

const CHIP_FOR_STATE = { choose: 'action', ingesting: 'working' }
const chipState = computed(() => CHIP_FOR_STATE[props.state] || props.state)

const stepName = computed(() => (tm('main.stepNames') || [])[props.step - 1] || '')

const message = computed(() => {
  if (props.state === 'error') return t('guide.error')
  const buttonKey = NEXT_BUTTON_KEYS[props.step]
  return t(`guide.s${props.step}.${props.state}`, { button: buttonKey ? t(buttonKey) : '' })
})
</script>

<style scoped>
.guide-banner {
  position: relative;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 24px;
  font-size: 13px;
  line-height: 1.4;
  border-bottom: 1px solid #EAEAEA;
  background: #F8FAFC;
  color: #1F2937;
}
.guide-progress {
  font-size: 12px;
  color: #6B7280;
  white-space: nowrap;
}
.guide-chip {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  background: #E0E7FF;
  color: #3730A3;
}
.guide-bar {
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 3px;
  background: transparent;
}
.guide-bar-fill {
  display: block;
  height: 100%;
  background: #FF5722;
  transition: width 0.4s ease;
}
.guide-banner.action { background: #FFF7ED; }
.guide-banner.action .guide-chip { background: #FF5722; color: #FFF; }
.guide-banner.error { background: #FEF2F2; }
.guide-banner.error .guide-chip { background: #DC2626; color: #FFF; }

@media (max-width: 760px) {
  .guide-banner {
    flex-wrap: wrap;
    padding: 8px 12px;
    gap: 6px;
  }
  .guide-text { flex-basis: 100%; }
}
</style>
