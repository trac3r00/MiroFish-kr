<template>
  <Transition name="ai-panel">
    <div v-if="open" class="ai-panel" @keydown.esc.stop="emit('close')">
      <div class="ai-panel__backdrop" @click="emit('close')"></div>

      <aside
        class="ai-panel__drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ai-panel-title"
      >
        <header class="ai-panel__head">
          <div class="ai-panel__heading">
            <h2 id="ai-panel-title" class="ai-panel__title">{{ $t('llm.panel.title') }}</h2>
            <p class="ai-panel__subtitle">{{ $t('llm.panel.subtitle') }}</p>
          </div>
          <div class="ai-panel__actions">
            <button
              type="button"
              class="ai-btn ai-btn--ghost"
              :disabled="loadingStatus || loadingUsage"
              @click="refreshAll"
            >
              {{ $t('llm.panel.refresh') }}
            </button>
            <button
              ref="closeBtn"
              type="button"
              class="ai-btn ai-btn--ghost ai-btn--icon"
              :aria-label="$t('common.close')"
              @click="emit('close')"
            >
              ✕
            </button>
          </div>
        </header>

        <div class="ai-panel__body">
          <p v-if="statusError" class="ai-alert ai-alert--error">
            {{ $t('llm.panel.loadError', { error: statusError }) }}
            <button type="button" class="ai-link" @click="loadStatus">{{ $t('common.retry') }}</button>
          </p>
          <p v-else-if="loadingStatus && !status" class="ai-muted">{{ $t('common.loading') }}</p>

          <!-- 01 profiles -->
          <section class="ai-section" style="--i: 0">
            <h3 class="ai-section__title">
              <span class="ai-section__index">01</span>{{ $t('llm.profiles.title') }}
            </h3>
            <p v-if="status && !profiles.length" class="ai-empty">{{ $t('llm.profiles.empty') }}</p>
            <ul v-else class="ai-list">
              <li v-for="p in profiles" :key="p.id" class="ai-profile">
                <div class="ai-profile__main">
                  <span class="ai-profile__label">{{ p.label }}</span>
                  <span v-if="p.id === status.default_profile_id" class="ai-tag ai-tag--solid">
                    {{ $t('llm.profiles.default') }}
                  </span>
                  <span class="ai-tag">{{ kindLabel(p.kind) }}</span>
                </div>
                <div class="ai-profile__meta">
                  <span class="ai-mono">{{ p.model }}</span>
                  <span v-if="p.base_url" class="ai-mono ai-muted ai-profile__url">{{ p.base_url }}</span>
                  <span v-if="p.api_key_env" class="ai-muted ai-small">
                    {{ $t('llm.profiles.keyEnv', { env: p.api_key_env }) }}
                  </span>
                  <span v-if="!p.supports_json_mode" class="ai-muted ai-small">
                    {{ $t('llm.profiles.noJsonMode') }}
                  </span>
                </div>
                <span class="ai-state" :class="`ai-state--${profileState(p)}`">
                  {{ $t(`llm.profiles.state.${profileState(p)}`) }}
                </span>
              </li>
            </ul>
          </section>

          <!-- 02 routes -->
          <section class="ai-section" style="--i: 1">
            <h3 class="ai-section__title">
              <span class="ai-section__index">02</span>{{ $t('llm.routes.title') }}
            </h3>
            <ul class="ai-list ai-list--tight">
              <li v-for="r in routes" :key="r.stage" class="ai-route">
                <span class="ai-route__stage">{{ stageLabel(r.stage) }}</span>
                <ModelBadge :model="r.model" :profile-label="r.label" />
              </li>
            </ul>
            <div v-if="agentPool.length" class="ai-pool">
              <div class="ai-pool__title">{{ $t('llm.routes.agentPool') }}</div>
              <div class="ai-pool__bar" aria-hidden="true">
                <span
                  v-for="(entry, index) in agentPool"
                  :key="entry.id"
                  class="ai-pool__segment"
                  :class="{ 'ai-pool__segment--alt': index % 2 === 1 }"
                  :style="{ flexGrow: entry.share }"
                ></span>
              </div>
              <ul class="ai-pool__legend">
                <li v-for="entry in agentPool" :key="entry.id" class="ai-pool__item">
                  <ModelBadge :model="entry.model" :profile-label="entry.label" />
                  <span class="ai-mono ai-muted">{{ fmtPct(entry.share) }}</span>
                </li>
              </ul>
            </div>
          </section>

          <!-- 03 ChatGPT -->
          <section class="ai-section" style="--i: 2">
            <h3 class="ai-section__title">
              <span class="ai-section__index">03</span>{{ $t('llm.chatgpt.title') }}
            </h3>
            <div class="ai-card">
              <p class="ai-card__desc">{{ $t('llm.chatgpt.description') }}</p>

              <template v-if="chatgpt && chatgpt.signed_in">
                <p class="ai-card__line">
                  <span class="ai-dot ai-dot--ok" aria-hidden="true"></span>
                  {{ chatgpt.email ? $t('llm.chatgpt.signedInAs', { email: chatgpt.email }) : $t('llm.chatgpt.signedIn') }}
                </p>
                <p v-if="!chatgpt.plan_usage_enabled" class="ai-alert ai-alert--warn">
                  {{ $t('llm.chatgpt.planUsageDisabled') }}
                </p>
                <p v-if="chatgpt.expires_at" class="ai-muted ai-small">
                  {{ $t('llm.chatgpt.expiresAt', { time: fmtTime(chatgpt.expires_at) }) }}
                </p>
                <div v-if="chatgpt.models && chatgpt.models.length" class="ai-models">
                  <span class="ai-muted ai-small">{{ $t('llm.chatgpt.models') }}</span>
                  <div class="ai-models__list">
                    <ModelBadge v-for="m in chatgpt.models" :key="m.slug" :model="m.slug" />
                  </div>
                </div>
                <p v-else-if="chatgpt.models_error" class="ai-muted ai-small">
                  {{ $t('llm.chatgpt.modelsError', { code: chatgpt.models_error }) }}
                </p>
                <p v-if="!hasOauthProfile" class="ai-alert ai-alert--info">{{ $t('llm.chatgpt.noProfile') }}</p>
              </template>
              <p v-else-if="chatgpt" class="ai-card__line">
                <span class="ai-dot" aria-hidden="true"></span>{{ $t('llm.chatgpt.signedOut') }}
              </p>

              <div v-if="oauth.phase === 'pending'" class="ai-pending" role="status" aria-live="polite">
                <span class="ai-spinner" aria-hidden="true"></span>
                <div class="ai-pending__text">
                  <p>{{ oauth.popupBlocked ? $t('llm.chatgpt.popupBlocked') : $t('llm.chatgpt.pending') }}</p>
                  <p class="ai-muted ai-small">{{ $t('llm.chatgpt.waiting', { seconds: oauth.remaining }) }}</p>
                  <a
                    v-if="oauth.authorizeUrl"
                    class="ai-link"
                    :href="oauth.authorizeUrl"
                    target="_blank"
                    rel="noopener noreferrer"
                  >{{ $t('llm.chatgpt.reopen') }}</a>
                </div>
              </div>
              <p v-else-if="oauth.phase === 'success'" class="ai-alert ai-alert--ok" role="status">
                {{ $t('llm.chatgpt.success') }}
              </p>
              <p v-else-if="oauth.phase === 'timeout'" class="ai-alert ai-alert--warn" role="status">
                {{ $t('llm.chatgpt.timeout') }}
              </p>
              <p v-else-if="oauth.phase === 'cancelled'" class="ai-alert ai-alert--info" role="status">
                {{ $t('llm.chatgpt.cancelled') }}
              </p>
              <p v-else-if="oauth.phase === 'error'" class="ai-alert ai-alert--error" role="status">
                {{ $t('llm.chatgpt.failed', { code: oauth.error }) }}
              </p>
              <p v-if="signOutError" class="ai-alert ai-alert--error" role="status">
                {{ $t('llm.chatgpt.signOutError', { error: signOutError }) }}
              </p>

              <div class="ai-card__actions">
                <template v-if="oauth.phase === 'pending'">
                  <button type="button" class="ai-btn ai-btn--ghost" @click="cancelSignIn">
                    {{ $t('common.cancel') }}
                  </button>
                </template>
                <template v-else-if="chatgpt && chatgpt.signed_in">
                  <button
                    v-if="!chatgpt.plan_usage_enabled"
                    type="button"
                    class="ai-btn ai-btn--primary"
                    :disabled="oauthBusy"
                    @click="continueWithChatgpt(false)"
                  >
                    {{ $t('llm.chatgpt.continue') }}
                  </button>
                  <button type="button" class="ai-btn ai-btn--ghost" :disabled="oauthBusy" @click="continueWithChatgpt(true)">
                    {{ $t('llm.chatgpt.anotherAccount') }}
                  </button>
                  <button type="button" class="ai-btn ai-btn--ghost" :disabled="oauthBusy" @click="signOut">
                    {{ $t('llm.chatgpt.signOut') }}
                  </button>
                </template>
                <button
                  v-else
                  type="button"
                  class="ai-btn ai-btn--primary"
                  :disabled="oauthBusy"
                  @click="continueWithChatgpt(false)"
                >
                  {{ $t('llm.chatgpt.continue') }}
                </button>
                <a
                  v-if="chatgpt && chatgpt.usage_url"
                  class="ai-link ai-card__ext"
                  :href="chatgpt.usage_url"
                  target="_blank"
                  rel="noopener noreferrer"
                >{{ $t('llm.chatgpt.usageLink') }} ↗</a>
              </div>
            </div>
          </section>

          <!-- 04 Claude Code CLI -->
          <section v-if="cliProfiles.length" class="ai-section" style="--i: 3">
            <h3 class="ai-section__title">
              <span class="ai-section__index">{{ sectionIndex.claude }}</span>{{ $t('llm.claude.title') }}
            </h3>
            <div class="ai-card">
              <p class="ai-card__desc">{{ $t('llm.claude.description') }}</p>
              <div v-for="p in cliProfiles" :key="p.id" class="ai-cli">
                <div class="ai-cli__head">
                  <span class="ai-profile__label">{{ p.label }}</span>
                  <span v-if="p.model" class="ai-mono ai-muted ai-small">{{ p.model }}</span>
                </div>
                <div class="ai-cli__chips">
                  <span class="ai-tag" :class="{ 'ai-tag--solid': cliOf(p).installed }">
                    {{ cliOf(p).installed ? $t('llm.claude.installed') : $t('llm.claude.notInstalled') }}
                  </span>
                  <span
                    v-if="cliOf(p).installed"
                    class="ai-tag"
                    :class="cliOf(p).signed_in ? 'ai-tag--solid' : 'ai-tag--warn'"
                  >
                    {{ cliOf(p).signed_in ? $t('llm.claude.signedIn') : $t('llm.claude.loginNeeded') }}
                  </span>
                  <span v-if="cliOf(p).version" class="ai-mono ai-muted ai-small">
                    {{ $t('llm.claude.version', { version: cliOf(p).version }) }}
                  </span>
                </div>
              </div>
              <p v-if="cliNeedsInstall" class="ai-alert ai-alert--warn">{{ $t('llm.claude.installHint') }}</p>
              <ol v-else-if="cliNeedsLogin" class="ai-alert ai-alert--warn ai-steps">
                <li>
                  <i18n-t keypath="llm.claude.loginStep1" tag="span">
                    <template #cmd><code class="ai-code">claude</code></template>
                  </i18n-t>
                </li>
                <li>
                  <i18n-t keypath="llm.claude.loginStep2" tag="span">
                    <template #cmd><code class="ai-code">/login</code></template>
                  </i18n-t>
                </li>
                <li>{{ $t('llm.claude.loginStep3') }}</li>
              </ol>
              <p class="ai-muted ai-small">{{ $t('llm.claude.policyNote') }}</p>
              <div class="ai-card__actions">
                <button type="button" class="ai-btn ai-btn--ghost" :disabled="loadingStatus" @click="recheckCli">
                  {{ loadingStatus ? $t('llm.claude.checking') : $t('llm.claude.recheck') }}
                </button>
              </div>
            </div>
          </section>

          <!-- Cursor CLI -->
          <section v-if="cursorProfiles.length" class="ai-section" style="--i: 4">
            <h3 class="ai-section__title">
              <span class="ai-section__index">{{ sectionIndex.cursor }}</span>{{ $t('llm.cursor.title') }}
            </h3>
            <div class="ai-card">
              <p class="ai-card__desc">{{ $t('llm.cursor.description') }}</p>
              <div v-for="p in cursorProfiles" :key="p.id" class="ai-cli">
                <div class="ai-cli__head">
                  <span class="ai-profile__label">{{ p.label }}</span>
                  <span v-if="p.model" class="ai-mono ai-muted ai-small">{{ p.model }}</span>
                </div>
                <div class="ai-cli__chips">
                  <span class="ai-tag" :class="{ 'ai-tag--solid': cliOf(p).installed }">
                    {{ cliOf(p).installed ? $t('llm.claude.installed') : $t('llm.claude.notInstalled') }}
                  </span>
                  <span
                    v-if="cliOf(p).installed"
                    class="ai-tag"
                    :class="cliOf(p).signed_in ? 'ai-tag--solid' : 'ai-tag--warn'"
                  >
                    {{ cliOf(p).signed_in ? $t('llm.claude.signedIn') : $t('llm.claude.loginNeeded') }}
                  </span>
                  <span v-if="cliOf(p).version" class="ai-mono ai-muted ai-small">
                    {{ $t('llm.claude.version', { version: cliOf(p).version }) }}
                  </span>
                </div>
              </div>
              <p v-if="cursorNeedsInstall" class="ai-alert ai-alert--warn">{{ $t('llm.cursor.installHint') }}</p>
              <p v-else-if="cursorNeedsLogin" class="ai-alert ai-alert--warn">
                <i18n-t keypath="llm.cursor.loginStep" tag="span">
                  <template #cmd><code class="ai-code">agent login</code></template>
                </i18n-t>
              </p>
              <p class="ai-muted ai-small">{{ $t('llm.cursor.policyNote') }}</p>
              <div class="ai-card__actions">
                <button type="button" class="ai-btn ai-btn--ghost" :disabled="loadingStatus" @click="recheckCli">
                  {{ loadingStatus ? $t('llm.claude.checking') : $t('llm.claude.recheck') }}
                </button>
              </div>
            </div>
          </section>

          <!-- usage -->
          <section class="ai-section" style="--i: 5">
            <h3 class="ai-section__title">
              <span class="ai-section__index">{{ sectionIndex.usage }}</span>{{ $t('llm.usage.title') }}
            </h3>
            <p class="ai-muted ai-small">{{ $t('llm.usage.note') }}</p>
            <p v-if="hasEstimatedUsage" class="ai-muted ai-small">{{ $t('llm.usage.estimatedNote') }}</p>
            <p v-if="usageError" class="ai-alert ai-alert--error">
              {{ $t('llm.usage.loadError', { error: usageError }) }}
              <button type="button" class="ai-link" @click="loadUsage">{{ $t('common.retry') }}</button>
            </p>
            <p v-else-if="usage && !usageRows.length" class="ai-empty">{{ $t('llm.usage.empty') }}</p>
            <div v-else-if="usageRows.length" class="ai-table-wrap">
            <table class="ai-table">
              <thead>
                <tr>
                  <th scope="col">{{ $t('llm.usage.stage') }} / {{ $t('llm.usage.model') }}</th>
                  <th scope="col" class="ai-table__num">{{ $t('llm.usage.calls') }}</th>
                  <th scope="col" class="ai-table__num">{{ $t('llm.usage.input') }}</th>
                  <th scope="col" class="ai-table__num">{{ $t('llm.usage.cached') }}</th>
                  <th scope="col" class="ai-table__num">{{ $t('llm.usage.output') }}</th>
                  <th scope="col" class="ai-table__num">{{ $t('llm.usage.cacheHit') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in usageRows" :key="`${row.stage}|${row.profile_id}|${row.model}`">
                  <td class="ai-table__stage-cell" :data-label="$t('llm.usage.stage')">
                    <div class="ai-table__stage">
                      <span class="ai-table__stage-name">{{ stageLabel(row.stage) }}</span>
                      <ModelBadge :model="row.model" :profile-label="profileLabelOf(row.profile_id)" />
                    </div>
                  </td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.calls')">{{ fmtNum(row.calls) }}</td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.input')">{{ fmtTok(row.input_tokens, row.estimated) }}</td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.cached')">{{ fmtNum(row.cached_tokens) }}</td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.output')">{{ fmtTok(row.output_tokens, row.estimated) }}</td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.cacheHit')">{{ fmtPct(row.hit) }}</td>
                </tr>
              </tbody>
              <tfoot v-if="usageTotals">
                <tr>
                  <td class="ai-table__stage-cell" :data-label="$t('llm.usage.stage')">
                    <span class="ai-table__stage-name">{{ $t('llm.usage.total') }}</span>
                  </td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.calls')">{{ fmtNum(usageTotals.calls) }}</td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.input')">{{ fmtTok(usageTotals.input_tokens, hasEstimatedUsage) }}</td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.cached')">{{ fmtNum(usageTotals.cached_tokens) }}</td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.output')">{{ fmtTok(usageTotals.output_tokens, hasEstimatedUsage) }}</td>
                  <td class="ai-table__num" :data-label="$t('llm.usage.cacheHit')">{{ fmtPct(usageTotals.hit) }}</td>
                </tr>
              </tfoot>
            </table>
            </div>
          </section>
        </div>
      </aside>
    </div>
  </Transition>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import ModelBadge from './ModelBadge.vue'
import {
  getLlmStatus,
  getLlmUsage,
  startChatgptSignIn,
  getChatgptStatus,
  signOutChatgpt
} from '../api/llm'

const STAGES = [
  'ontology',
  'profile',
  'config',
  'report',
  'report_chat',
  'interview',
  'search_planning',
  'simulation_agent'
]
const USAGE_POLL_MS = 10000
const OAUTH_POLL_MS = 2000
const OAUTH_TIMEOUT_MS = 120000

const props = defineProps({
  open: { type: Boolean, default: false }
})
const emit = defineEmits(['close', 'status-change'])

const { t, te, locale } = useI18n()

const closeBtn = ref(null)
const status = ref(null)
const usage = ref(null)
const statusError = ref('')
const usageError = ref('')
const loadingStatus = ref(false)
const loadingUsage = ref(false)
const signOutError = ref('')

const idleOauth = () => ({
  phase: 'idle',
  attemptId: null,
  authorizeUrl: '',
  popupBlocked: false,
  remaining: 0,
  error: ''
})
const oauth = ref(idleOauth())
const oauthBusy = computed(() => oauth.value.phase === 'starting' || oauth.value.phase === 'pending')

let usageTimer = null
let oauthTimer = null
let oauthDeadline = 0

const profiles = computed(() => status.value?.profiles ?? [])
const profileById = computed(() => Object.fromEntries(profiles.value.map(p => [p.id, p])))
const chatgpt = computed(() => status.value?.oauth?.chatgpt ?? null)
const hasOauthProfile = computed(() => profiles.value.some(p => p.kind === 'chatgpt_oauth'))
const cliProfiles = computed(() => profiles.value.filter(p => p.kind === 'claude_code_cli'))
const cliOf = profile => status.value?.cli?.[profile.id] ?? { installed: false, version: null, signed_in: false }
const cliNeedsInstall = computed(() => cliProfiles.value.some(p => !cliOf(p).installed))
const cliNeedsLogin = computed(() => cliProfiles.value.some(p => cliOf(p).installed && !cliOf(p).signed_in))
const cursorProfiles = computed(() => profiles.value.filter(p => p.kind === 'cursor_agent_cli'))
const cursorNeedsInstall = computed(() => cursorProfiles.value.some(p => !cliOf(p).installed))
const cursorNeedsLogin = computed(() => cursorProfiles.value.some(p => cliOf(p).installed && !cliOf(p).signed_in))
const sectionIndex = computed(() => {
  let next = 4
  const label = () => String(next++).padStart(2, '0')
  const claude = cliProfiles.value.length ? label() : ''
  const cursor = cursorProfiles.value.length ? label() : ''
  return { claude, cursor, usage: label() }
})
// Profiles whose provider reports no token usage; their counts are estimates (chars / 4).
const ESTIMATED_KINDS = ['cursor_agent_cli']
const isEstimated = profileId => ESTIMATED_KINDS.includes(profileById.value[profileId]?.kind)

const routes = computed(() =>
  STAGES.map(stage => {
    const profileId = status.value?.routes?.[stage] ?? status.value?.default_profile_id ?? ''
    const profile = profileById.value[profileId]
    return {
      stage,
      profileId,
      label: profile?.label ?? profileId,
      model: profile?.model ?? ''
    }
  })
)

const agentPool = computed(() => {
  const pool = status.value?.agent_pool ?? {}
  const total = Object.values(pool).reduce((sum, weight) => sum + weight, 0)
  return Object.entries(pool).map(([id, weight]) => ({
    id,
    label: profileById.value[id]?.label ?? id,
    model: profileById.value[id]?.model ?? '',
    share: total ? weight / total : 0
  }))
})

const hitRate = entry => (entry.input_tokens ? entry.cached_tokens / entry.input_tokens : null)
const usageRows = computed(() =>
  (usage.value?.entries ?? []).map(entry => ({ ...entry, hit: hitRate(entry), estimated: isEstimated(entry.profile_id) }))
)
const hasEstimatedUsage = computed(() => usageRows.value.some(row => row.estimated))
const usageTotals = computed(() => {
  const totals = usage.value?.totals
  return totals ? { ...totals, hit: hitRate(totals) } : null
})

const numberFormat = computed(() => new Intl.NumberFormat(locale.value))
const fmtNum = value => numberFormat.value.format(value ?? 0)
const fmtTok = (value, estimated) => (estimated ? `≈${fmtNum(value)}` : fmtNum(value))
const fmtPct = ratio => {
  if (ratio == null) return '—'
  const percent = ratio * 100
  return `${percent > 0 && percent < 10 ? percent.toFixed(1) : Math.round(percent)}%`
}
const fmtTime = iso => (iso ? new Date(iso).toLocaleString(locale.value) : '')
const localized = (key, fallback) => (te(key) ? t(key) : fallback)
const stageLabel = stage => localized(`llm.stage.${stage}`, stage)
const kindLabel = kind => localized(`llm.profiles.kind.${kind}`, kind)
const profileLabelOf = profileId => profileById.value[profileId]?.label ?? profileId ?? ''

function profileState(profile) {
  if (profile.kind === 'chatgpt_oauth') {
    if (!chatgpt.value?.signed_in) return 'missing'
    return chatgpt.value.plan_usage_enabled ? 'ok' : 'warn'
  }
  if (profile.kind === 'claude_code_cli' || profile.kind === 'cursor_agent_cli') {
    const cli = cliOf(profile)
    if (!cli.installed) return 'missing'
    return cli.signed_in ? 'ok' : 'warn'
  }
  return profile.has_api_key ? 'ok' : 'missing'
}

function summarize() {
  const list = profiles.value
  const fallback = list.find(p => p.id === status.value?.default_profile_id) ?? list[0] ?? null
  const states = list.map(profileState)
  const state = !list.length || states.includes('missing') || states.includes('warn') ? 'warn' : 'ok'
  return { state, model: fallback?.model ?? '', label: fallback?.label ?? '' }
}

async function loadStatus(refresh = false) {
  loadingStatus.value = true
  try {
    const res = await getLlmStatus(refresh === true)
    status.value = res.data
    statusError.value = ''
    emit('status-change', summarize())
  } catch (error) {
    statusError.value = error.message || String(error)
    emit('status-change', { state: 'error', model: '', label: '' })
  } finally {
    loadingStatus.value = false
  }
}

async function loadUsage() {
  loadingUsage.value = true
  try {
    const res = await getLlmUsage()
    usage.value = res.data
    usageError.value = ''
  } catch (error) {
    usageError.value = error.message || String(error)
  } finally {
    loadingUsage.value = false
  }
}

function refreshAll() {
  loadStatus()
  loadUsage()
}

function recheckCli() {
  loadStatus(true)
}

function stopUsagePolling() {
  if (usageTimer) {
    clearInterval(usageTimer)
    usageTimer = null
  }
}

function stopOauthPolling() {
  if (oauthTimer) {
    clearTimeout(oauthTimer)
    oauthTimer = null
  }
}

function finishOauth(phase, error = '') {
  stopOauthPolling()
  oauth.value = { ...oauth.value, phase, error, remaining: 0 }
}

function scheduleOauthPoll() {
  stopOauthPolling()
  oauthTimer = setTimeout(pollOauth, OAUTH_POLL_MS)
}

async function pollOauth() {
  oauthTimer = null
  if (oauth.value.phase !== 'pending') return
  try {
    const res = await getChatgptStatus(oauth.value.attemptId)
    const data = res.data
    const attempt = data.attempt
    const succeeded = attempt ? attempt.status === 'success' : Boolean(data.signed_in)
    if (succeeded) {
      finishOauth('success')
      await loadStatus()
      return
    }
    if (attempt && attempt.status === 'error') {
      finishOauth('error', attempt.error || 'oauth_error')
      return
    }
    if (attempt && attempt.status === 'cancelled') {
      finishOauth('cancelled')
      return
    }
  } catch (error) {
    oauth.value.error = error.message || String(error)
  }
  const remainingMs = oauthDeadline - Date.now()
  if (remainingMs <= 0) {
    finishOauth('timeout')
    return
  }
  oauth.value.remaining = Math.ceil(remainingMs / 1000)
  scheduleOauthPoll()
}

async function continueWithChatgpt(newAccount) {
  stopOauthPolling()
  signOutError.value = ''
  oauth.value = { ...idleOauth(), phase: 'starting' }
  let tab = null
  try {
    tab = window.open('about:blank', '_blank')
    if (tab) tab.opener = null
  } catch (error) {
    tab = null
  }
  try {
    const res = await startChatgptSignIn(newAccount)
    const { authorize_url: authorizeUrl, attempt_id: attemptId } = res.data
    if (tab && !tab.closed) {
      tab.location.href = authorizeUrl
    }
    oauthDeadline = Date.now() + OAUTH_TIMEOUT_MS
    oauth.value = {
      phase: 'pending',
      attemptId,
      authorizeUrl,
      popupBlocked: !tab || tab.closed,
      remaining: Math.ceil(OAUTH_TIMEOUT_MS / 1000),
      error: ''
    }
    scheduleOauthPoll()
  } catch (error) {
    if (tab && !tab.closed) tab.close()
    finishOauth('error', error.message || String(error))
  }
}

function cancelSignIn() {
  finishOauth('cancelled')
}

async function signOut() {
  signOutError.value = ''
  oauth.value = idleOauth()
  try {
    await signOutChatgpt()
    await loadStatus()
  } catch (error) {
    signOutError.value = error.message || String(error)
  }
}

function onOpen() {
  refreshAll()
  stopUsagePolling()
  usageTimer = setInterval(loadUsage, USAGE_POLL_MS)
  nextTick(() => closeBtn.value?.focus())
}

watch(
  () => props.open,
  isOpen => {
    if (isOpen) onOpen()
    else stopUsagePolling()
  }
)

onMounted(() => {
  loadStatus()
  if (props.open) onOpen()
})

onUnmounted(() => {
  stopUsagePolling()
  stopOauthPolling()
})
</script>

<style scoped>
.ai-panel {
  position: fixed;
  inset: 0;
  z-index: var(--mf-z-overlay);
  font-family: var(--mf-font-sans);
  color: var(--mf-black);
}

.ai-panel__backdrop {
  position: absolute;
  inset: 0;
  background: var(--mf-backdrop);
}

.ai-panel__drawer {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(560px, 100%);
  display: flex;
  flex-direction: column;
  background: var(--mf-white);
  border-left: 1px solid var(--mf-black);
  box-shadow: var(--mf-shadow-lg);
}

.ai-panel__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--mf-space-4);
  padding: var(--mf-space-4) var(--mf-space-6);
  border-bottom: 1px solid var(--mf-gray-200);
}

.ai-panel__title {
  margin: 0;
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-lg);
  font-weight: 800;
  letter-spacing: 1px;
}

.ai-panel__subtitle {
  margin: var(--mf-space-1) 0 0;
  font-size: var(--mf-text-sm);
  color: var(--mf-gray-600);
}

.ai-panel__actions {
  display: flex;
  gap: var(--mf-space-2);
  flex-shrink: 0;
}

.ai-panel__body {
  flex: 1;
  overflow-y: auto;
  padding: var(--mf-space-6);
  display: flex;
  flex-direction: column;
  gap: var(--mf-space-8);
}

.ai-section {
  animation: ai-rise var(--mf-duration-slow) var(--mf-ease) both;
  animation-delay: calc(var(--i, 0) * 50ms);
}

.ai-section__title {
  display: flex;
  align-items: baseline;
  gap: var(--mf-space-2);
  margin: 0 0 var(--mf-space-3);
  font-size: var(--mf-text-md);
  font-weight: 700;
}

.ai-section__index {
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-xs);
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--mf-gray-500);
}

.ai-list {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 1px solid var(--mf-gray-200);
}

.ai-list > li {
  border-bottom: 1px solid var(--mf-gray-200);
}

.ai-profile {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: var(--mf-space-1) var(--mf-space-3);
  padding: var(--mf-space-3) 0;
}

.ai-profile__main {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--mf-space-2);
}

.ai-profile__label {
  font-weight: 700;
  font-size: var(--mf-text-md);
}

.ai-profile__meta {
  grid-column: 1;
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-space-1) var(--mf-space-3);
  font-size: var(--mf-text-sm);
}

.ai-profile__url {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}

.ai-state {
  grid-column: 2;
  grid-row: 1 / span 2;
  align-self: start;
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-xs);
  font-weight: 700;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  white-space: nowrap;
}

.ai-state--ok { color: var(--mf-black); }
.ai-state--warn { color: var(--mf-orange); }
.ai-state--missing { color: var(--mf-gray-500); }

.ai-list--tight > li {
  padding: var(--mf-space-2) 0;
}

.ai-route {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-space-3);
  font-size: var(--mf-text-sm);
}

.ai-route__stage {
  color: var(--mf-gray-800);
}

.ai-pool {
  margin-top: var(--mf-space-4);
}

.ai-pool__title {
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-xs);
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--mf-gray-500);
  margin-bottom: var(--mf-space-2);
}

.ai-pool__bar {
  display: flex;
  height: var(--mf-space-2);
  gap: 1px;
  border: 1px solid var(--mf-black);
  background: var(--mf-white);
}

.ai-pool__segment {
  flex-basis: 0;
  background: var(--mf-black);
}

.ai-pool__segment--alt {
  background: var(--mf-gray-400);
}

.ai-pool__legend {
  list-style: none;
  margin: var(--mf-space-2) 0 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-space-2) var(--mf-space-4);
}

.ai-pool__item {
  display: inline-flex;
  align-items: center;
  gap: var(--mf-space-2);
  font-size: var(--mf-text-sm);
}

.ai-card {
  border: 1px solid var(--mf-black);
  padding: var(--mf-space-4);
  display: flex;
  flex-direction: column;
  gap: var(--mf-space-3);
}

.ai-card__desc {
  margin: 0;
  font-size: var(--mf-text-sm);
  color: var(--mf-gray-600);
}

.ai-card__line {
  display: flex;
  align-items: center;
  gap: var(--mf-space-2);
  margin: 0;
  font-size: var(--mf-text-md);
  font-weight: 500;
}

.ai-card__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--mf-space-2);
}

.ai-card__ext {
  margin-left: auto;
}

.ai-models {
  display: flex;
  flex-direction: column;
  gap: var(--mf-space-1);
}

.ai-models__list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-space-1);
}

.ai-pending {
  display: flex;
  align-items: flex-start;
  gap: var(--mf-space-3);
  padding: var(--mf-space-3);
  background: var(--mf-gray-50);
  font-size: var(--mf-text-sm);
}

.ai-pending__text {
  display: flex;
  flex-direction: column;
  gap: var(--mf-space-1);
}

.ai-pending__text p {
  margin: 0;
}

.ai-spinner {
  flex-shrink: 0;
  width: var(--mf-space-3);
  height: var(--mf-space-3);
  margin-top: 2px;
  border: 2px solid var(--mf-gray-400);
  border-top-color: var(--mf-black);
  border-radius: 50%;
  animation: ai-spin 0.8s linear infinite;
}

.ai-dot {
  width: var(--mf-space-2);
  height: var(--mf-space-2);
  border-radius: 50%;
  border: 1px solid var(--mf-gray-400);
  background: transparent;
  flex-shrink: 0;
}

.ai-dot--ok {
  background: var(--mf-black);
  border-color: var(--mf-black);
}

.ai-table-wrap {
  margin-top: var(--mf-space-3);
  overflow-x: auto;
}

.ai-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--mf-text-sm);
}

.ai-table__stage {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--mf-space-1);
}

.ai-table__stage-name {
  white-space: nowrap;
}

.ai-table th,
.ai-table td {
  padding: var(--mf-space-2) var(--mf-space-1);
  border-bottom: 1px solid var(--mf-gray-200);
  text-align: left;
  vertical-align: middle;
}

.ai-table th {
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-xs);
  font-weight: 700;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--mf-gray-500);
  border-bottom-color: var(--mf-black);
}

.ai-table__num {
  text-align: right;
  font-family: var(--mf-font-mono);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.ai-table tfoot td {
  font-weight: 700;
  border-top: 1px solid var(--mf-black);
  border-bottom: none;
}

.ai-btn {
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-label);
  font-weight: 500;
  line-height: 1;
  padding: var(--mf-space-2) var(--mf-space-3);
  border: 1px solid var(--mf-black);
  background: var(--mf-white);
  color: var(--mf-black);
  cursor: pointer;
  white-space: nowrap;
  transition: background var(--mf-duration), color var(--mf-duration), border-color var(--mf-duration);
}

.ai-btn:hover:not(:disabled) {
  background: var(--mf-gray-100);
}

.ai-btn:disabled {
  cursor: default;
  opacity: 0.5;
}

.ai-btn--primary {
  background: var(--mf-black);
  color: var(--mf-white);
  font-weight: 700;
}

.ai-btn--primary:hover:not(:disabled) {
  background: var(--mf-gray-800);
}

.ai-btn--ghost {
  border-color: var(--mf-gray-400);
}

.ai-btn--ghost:hover:not(:disabled) {
  border-color: var(--mf-black);
}

.ai-btn--icon {
  padding: var(--mf-space-2);
  min-width: var(--mf-control);
  text-align: center;
}

.ai-link {
  padding: 0;
  border: none;
  background: none;
  font: inherit;
  font-size: var(--mf-text-sm);
  color: var(--mf-black);
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}

.ai-tag {
  display: inline-block;
  padding: 0 var(--mf-space-2);
  border: 1px solid var(--mf-gray-400);
  border-radius: var(--mf-radius-sm);
  font-family: var(--mf-font-mono);
  font-size: var(--mf-text-xs);
  line-height: 2;
  color: var(--mf-gray-600);
}

.ai-tag--solid {
  background: var(--mf-black);
  border-color: var(--mf-black);
  color: var(--mf-white);
}

.ai-tag--warn {
  border-color: var(--mf-orange);
  color: var(--mf-orange);
}

.ai-cli {
  display: flex;
  flex-direction: column;
  gap: var(--mf-space-1);
}

.ai-cli__head,
.ai-cli__chips {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--mf-space-2);
}

.ai-steps {
  padding-left: var(--mf-space-6);
  display: flex;
  flex-direction: column;
  gap: var(--mf-space-1);
}

.ai-code {
  font-family: var(--mf-font-mono);
  padding: 0 var(--mf-space-1);
  background: var(--mf-white);
  border: 1px solid var(--mf-gray-200);
}

.ai-alert {
  margin: 0;
  padding: var(--mf-space-2) var(--mf-space-3);
  border-left: 2px solid var(--mf-black);
  background: var(--mf-gray-50);
  font-size: var(--mf-text-sm);
}

.ai-alert--error,
.ai-alert--warn {
  border-left-color: var(--mf-orange);
}

.ai-alert--info {
  border-left-color: var(--mf-gray-400);
}

.ai-empty {
  margin: 0;
  padding: var(--mf-space-3);
  border: 1px dashed var(--mf-gray-400);
  font-size: var(--mf-text-sm);
  color: var(--mf-gray-600);
}

.ai-mono {
  font-family: var(--mf-font-mono);
}

.ai-muted {
  color: var(--mf-gray-500);
}

.ai-small {
  font-size: var(--mf-text-sm);
}

.ai-panel-enter-active,
.ai-panel-leave-active {
  transition: opacity var(--mf-duration) var(--mf-ease);
}

.ai-panel-enter-active .ai-panel__drawer,
.ai-panel-leave-active .ai-panel__drawer {
  transition: transform var(--mf-duration-slow) var(--mf-ease);
}

.ai-panel-enter-from,
.ai-panel-leave-to {
  opacity: 0;
}

.ai-panel-enter-from .ai-panel__drawer,
.ai-panel-leave-to .ai-panel__drawer {
  transform: translateX(100%);
}

@keyframes ai-rise {
  from {
    opacity: 0;
    transform: translateY(var(--mf-space-2));
  }
  to {
    opacity: 1;
    transform: none;
  }
}

@keyframes ai-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 480px) {
  .ai-panel__drawer {
    width: 100%;
    border-left: none;
  }

  .ai-panel__head,
  .ai-panel__body {
    padding-left: var(--mf-space-4);
    padding-right: var(--mf-space-4);
  }

  .ai-table thead {
    display: none;
  }

  .ai-table,
  .ai-table tbody,
  .ai-table tfoot,
  .ai-table tr,
  .ai-table td {
    display: block;
  }

  .ai-table tr {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--mf-space-1) var(--mf-space-3);
    padding: var(--mf-space-2) 0;
    border-bottom: 1px solid var(--mf-gray-200);
  }

  .ai-table td {
    display: flex;
    justify-content: space-between;
    gap: var(--mf-space-2);
    padding: 0;
    border: none;
  }

  .ai-table td::before {
    content: attr(data-label);
    font-family: var(--mf-font-mono);
    font-size: var(--mf-text-xs);
    text-transform: uppercase;
    color: var(--mf-gray-500);
  }

  .ai-table__stage-cell {
    grid-column: 1 / -1;
    justify-content: flex-start;
  }

  .ai-table__stage {
    flex-direction: row;
    align-items: center;
    flex-wrap: wrap;
  }

  .ai-table__stage-name {
    font-weight: 700;
  }

  .ai-table tfoot td {
    border-top: none;
  }

  .ai-table tfoot tr {
    border-top: 1px solid var(--mf-black);
  }

  .ai-route {
    flex-wrap: wrap;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ai-section,
  .ai-spinner {
    animation: none;
  }

  .ai-panel-enter-active,
  .ai-panel-leave-active,
  .ai-panel-enter-active .ai-panel__drawer,
  .ai-panel-leave-active .ai-panel__drawer {
    transition: none;
  }
}
</style>
