<template>
  <div class="stage-timer" :class="{ finished }">
    <span v-if="!remainingOnly" class="timer-item">{{ finished ? $t('timer.took', { time: elapsedText }) : $t('timer.elapsed', { time: elapsedText }) }}</span>
    <template v-if="!finished && total > 0">
      <span v-if="!remainingOnly" class="timer-sep">·</span>
      <span class="timer-item remaining">{{ remainingText }}</span>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { estimateRemainingMs, splitDuration } from '../utils/eta'

const props = defineProps({
  // ISO string or epoch ms from the server; falls back to when this timer first saw progress.
  startedAt: { type: [String, Number], default: null },
  done: { type: Number, default: 0 },
  total: { type: Number, default: 0 },
  finished: { type: Boolean, default: false },
  finishedAt: { type: [String, Number], default: null },
  remainingOnly: { type: Boolean, default: false },
})

const { t } = useI18n()
const now = ref(Date.now())
const samples = ref([])
const mountedAt = Date.now()
let tick = null

const toMs = (value) => {
  if (value == null || value === '') return null
  const ms = typeof value === 'number' ? value : Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

const startMs = computed(() => toMs(props.startedAt) ?? mountedAt)

watch(() => props.done, (done) => {
  const last = samples.value[samples.value.length - 1]
  if (!last || done !== last.done) samples.value = [...samples.value.slice(-60), { t: Date.now(), done }]
}, { immediate: true })

const formatDuration = (ms) => {
  const { h, m, s } = splitDuration(ms)
  if (h > 0) return t('timer.hourMin', { h, m })
  if (m > 0) return t('timer.minSec', { m, s })
  return t('timer.sec', { s })
}

const elapsedText = computed(() => {
  const end = props.finished ? (toMs(props.finishedAt) ?? now.value) : now.value
  return formatDuration(end - startMs.value)
})

const remainingText = computed(() => {
  if (props.done >= props.total) return t('timer.finishing')
  const ms = estimateRemainingMs(samples.value, props.total, startMs.value, now.value)
  if (ms == null) return t('timer.calculating')
  if (ms < 60 * 1000) return t('timer.lessThanMinute')
  const { h, m } = splitDuration(ms)
  const rounded = h > 0 ? t('timer.hourMin', { h, m }) : t('timer.minutes', { m: Math.max(1, m) })
  return t('timer.remaining', { time: rounded })
})

const stopTick = () => { if (tick) { clearInterval(tick); tick = null } }
watch(() => props.finished, (finished) => { if (finished) stopTick() })

onMounted(() => {
  if (!props.finished) tick = setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(stopTick)
</script>

<style scoped>
.stage-timer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #4B5563;
  font-variant-numeric: tabular-nums;
}
.timer-sep { color: #9CA3AF; }
.remaining { color: #111827; font-weight: 600; }
.stage-timer.finished { color: #6B7280; }
</style>
