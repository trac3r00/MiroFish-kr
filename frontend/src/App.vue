<template>
  <router-view />

  <Teleport v-if="triggerTarget" :key="triggerKey" :to="triggerTarget">
    <button
      ref="triggerEl"
      type="button"
      class="ai-trigger"
      :class="`ai-trigger--${llmSummary.state}`"
      :title="triggerTitle"
      :aria-label="$t('llm.panel.title')"
      aria-haspopup="dialog"
      :aria-expanded="panelOpen"
      @click="panelOpen = true"
    >
      <span class="ai-trigger__dot" aria-hidden="true"></span>
      <span class="ai-trigger__label">{{ $t('llm.panel.trigger') }}</span>
      <span v-if="llmSummary.model" class="ai-trigger__model">{{ llmSummary.model }}</span>
    </button>
  </Teleport>
  <button
    v-else-if="triggerResolved"
    ref="triggerEl"
    type="button"
    class="ai-trigger ai-trigger--floating"
    :class="`ai-trigger--${llmSummary.state}`"
    :title="triggerTitle"
    :aria-label="$t('llm.panel.title')"
    aria-haspopup="dialog"
    :aria-expanded="panelOpen"
    @click="panelOpen = true"
  >
    <span class="ai-trigger__dot" aria-hidden="true"></span>
    <span class="ai-trigger__label">{{ $t('llm.panel.trigger') }}</span>
  </button>

  <AiConnectionPanel :open="panelOpen" @close="closePanel" @status-change="llmSummary = $event" />
</template>

<script setup>
// 使用 Vue Router 来管理页面
import { ref, computed, nextTick, onMounted, onUnmounted, defineAsyncComponent } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
const AiConnectionPanel = defineAsyncComponent(() => import('./components/AiConnectionPanel.vue'))

const HEADER_SLOT_SELECTORS = ['.app-header .header-right', '.navbar .nav-links']
const PLACEMENT_RETRIES = 5
const NARROW_QUERY = '(max-width: 480px)'

const router = useRouter()
const { t } = useI18n()

const panelOpen = ref(false)
const triggerEl = ref(null)
const triggerTarget = ref(null)
const triggerKey = ref(0)
const triggerResolved = ref(false)
const llmSummary = ref({ state: 'loading', model: '', label: '' })
const narrowQuery = window.matchMedia(NARROW_QUERY)

const triggerTitle = computed(() =>
  llmSummary.value.model
    ? t('llm.panel.triggerTitle', { label: llmSummary.value.label, model: llmSummary.value.model })
    : t('llm.panel.title')
)

let placementFrame = 0

function findHeaderSlot() {
  for (const selector of HEADER_SLOT_SELECTORS) {
    const el = document.querySelector(selector)
    if (el) return el
  }
  return null
}

async function placeTrigger(attempt = 0) {
  await nextTick()
  const slot = narrowQuery.matches ? null : findHeaderSlot()
  if (slot) {
    if (slot !== triggerTarget.value) {
      triggerTarget.value = slot
      triggerKey.value += 1
    }
    triggerResolved.value = true
    return
  }
  if (!narrowQuery.matches && attempt < PLACEMENT_RETRIES) {
    placementFrame = requestAnimationFrame(() => placeTrigger(attempt + 1))
    return
  }
  triggerTarget.value = null
  triggerResolved.value = true
}

function closePanel() {
  panelOpen.value = false
  nextTick(() => triggerEl.value?.focus())
}

function replaceTrigger() {
  cancelAnimationFrame(placementFrame)
  placeTrigger()
}

const removeAfterEach = router.afterEach(replaceTrigger)

onMounted(() => {
  narrowQuery.addEventListener('change', replaceTrigger)
  placeTrigger()
})

onUnmounted(() => {
  narrowQuery.removeEventListener('change', replaceTrigger)
  cancelAnimationFrame(placementFrame)
  removeAfterEach()
})
</script>

<style>
/* 设计令牌：从 Home.vue / MainView.vue / LanguageSwitcher.vue 中已有的值提取，供共享组件使用 */
:root {
  --mf-black: #000000;
  --mf-white: #FFFFFF;
  --mf-orange: #FF4500;
  --mf-gray-50: #F5F5F5;
  --mf-gray-100: #F0F0F0;
  --mf-gray-200: #EAEAEA;
  --mf-gray-400: #CCCCCC;
  --mf-gray-500: #999999;
  --mf-gray-600: #666666;
  --mf-gray-800: #333333;
  --mf-backdrop: rgba(0, 0, 0, 0.4);

  --mf-font-mono: 'JetBrains Mono', monospace;
  --mf-font-sans: 'Space Grotesk', 'Pretendard', 'Noto Sans KR', 'Apple SD Gothic Neo', 'Noto Sans SC', system-ui, sans-serif;

  --mf-text-xs: 0.625rem;
  --mf-text-sm: 0.75rem;
  --mf-text-label: 0.8rem;
  --mf-text-md: 0.875rem;
  --mf-text-lg: 1.125rem;

  --mf-space-1: 4px;
  --mf-space-2: 8px;
  --mf-space-3: 12px;
  --mf-space-4: 16px;
  --mf-space-6: 24px;
  --mf-space-8: 32px;
  --mf-control: 28px;

  --mf-radius-sm: 2px;
  --mf-radius-pill: 999px;

  --mf-shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.1);
  --mf-shadow-lg: 0 12px 40px rgba(0, 0, 0, 0.18);

  --mf-z-dropdown: 1000;
  --mf-z-overlay: 2000;

  --mf-ease: cubic-bezier(0.2, 0.7, 0.2, 1);
  --mf-duration: 0.2s;
  --mf-duration-slow: 0.32s;
}

/* 全局样式重置 */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  font-family: 'JetBrains Mono', 'Space Grotesk', 'Pretendard', 'Noto Sans KR', 'Apple SD Gothic Neo', 'Noto Sans SC', monospace;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  color: #000000;
  background-color: #ffffff;
}

:lang(ko) {
  word-break: keep-all;
  overflow-wrap: break-word;
}

/* Workflow views (Process/Simulation/Run/Report/Interaction) share a copy-pasted desktop-only
   header whose centered view switcher overlaps the right-hand controls on phones. */
@media (max-width: 760px) {
  .app-header {
    height: auto !important;
    flex-wrap: wrap;
    row-gap: 8px;
    padding: 10px 14px !important;
  }
  .app-header .header-center {
    position: static !important;
    transform: none !important;
    order: 3;
    width: 100%;
    display: flex;
    justify-content: center;
  }
  .app-header .header-right {
    gap: 8px !important;
    flex-wrap: wrap;
  }
  .app-header .step-divider,
  .app-header .step-num {
    display: none !important;
  }
  .app-header .switch-btn {
    white-space: nowrap;
  }
  /* Split view: stack graph above the step panel instead of two 195px columns.
     Panel widths are inline styles set per view mode, so match on them. */
  .content-area {
    flex-direction: column;
  }
  .content-area > .panel-wrapper[style*="width: 50%"] {
    width: 100% !important;
    height: 50% !important;
  }
  .content-area > .panel-wrapper[style*="width: 100%"] {
    height: 100% !important;
  }
  .content-area > .panel-wrapper[style*="width: 0%"] {
    height: 0 !important;
  }
  .content-area > .panel-wrapper.left {
    border-right: none !important;
    border-bottom: 1px solid #EAEAEA;
  }
}

/* 滚动条样式 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #f1f1f1;
}

::-webkit-scrollbar-thumb {
  background: #000000;
}

::-webkit-scrollbar-thumb:hover {
  background: #333333;
}

/* 全局按钮样式 */
button {
  font-family: inherit;
}

/* AI 连接入口：通过 Teleport 挂到各页面头部，找不到头部时退化为右上角悬浮按钮 */
.ai-trigger {
  display: inline-flex;
  align-items: center;
  gap: var(--mf-space-2);
  height: var(--mf-control);
  padding: 0 var(--mf-space-3);
  border: 1px solid currentColor;
  border-radius: var(--mf-radius-pill);
  background: transparent;
  color: inherit;
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-sm);
  font-weight: 700;
  letter-spacing: 1px;
  line-height: 1;
  cursor: pointer;
  white-space: nowrap;
  transition: background var(--mf-duration), color var(--mf-duration);
}

.ai-trigger:hover,
.ai-trigger[aria-expanded="true"] {
  background: var(--mf-gray-100);
  color: var(--mf-black);
}

.ai-trigger:focus-visible {
  outline: 2px solid var(--mf-orange);
  outline-offset: 2px;
}

.ai-trigger__dot {
  width: var(--mf-space-2);
  height: var(--mf-space-2);
  border-radius: 50%;
  border: 1px solid var(--mf-gray-400);
  background: transparent;
  flex-shrink: 0;
}

.ai-trigger--loading .ai-trigger__dot {
  animation: ai-trigger-pulse 1.2s ease-in-out infinite;
}

.ai-trigger--ok .ai-trigger__dot {
  background: currentColor;
  border-color: currentColor;
}

.ai-trigger--warn .ai-trigger__dot {
  background: var(--mf-orange);
  border-color: var(--mf-orange);
}

.ai-trigger--error .ai-trigger__dot {
  border-color: var(--mf-orange);
}

.ai-trigger__model {
  display: none;
  max-width: 16ch;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 500;
  letter-spacing: 0;
  opacity: 0.7;
}

@media (min-width: 1024px) {
  .ai-trigger__model {
    display: inline;
  }
}

.ai-trigger--floating {
  position: fixed;
  top: var(--mf-space-4);
  right: var(--mf-space-4);
  z-index: var(--mf-z-dropdown);
  background: var(--mf-white);
  color: var(--mf-black);
  box-shadow: var(--mf-shadow-sm);
}

@media (max-width: 480px) {
  .ai-trigger--floating {
    top: auto;
    bottom: var(--mf-space-4);
  }
}

@keyframes ai-trigger-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

@media (prefers-reduced-motion: reduce) {
  .ai-trigger--loading .ai-trigger__dot {
    animation: none;
  }
}
</style>
