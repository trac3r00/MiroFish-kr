import { createI18n } from 'vue-i18n'
import languages from '../../../locales/languages.json'

// Catalogs are ~60 kB each; bundle them as separate chunks and load only the active locale
// (plus the 'en' fallback) instead of shipping every language in the entry chunk.
const localeLoaders = {}
const availableLocales = []

for (const [path, loader] of Object.entries(import.meta.glob('../../../locales/!(languages).json'))) {
  const key = path.match(/\/([^/]+)\.json$/)[1]
  if (languages[key]) {
    localeLoaders[key] = loader
    availableLocales.push({ key, label: languages[key].label })
  }
}

const availableKeys = new Set(availableLocales.map(({ key }) => key))
const matchLocale = (tag) => {
  const normalized = tag.toLowerCase()
  if (availableKeys.has(normalized)) return normalized
  const primary = normalized.split('-')[0]
  return availableKeys.has(primary) ? primary : null
}
const savedLocale = localStorage.getItem('locale')
const browserLocale = (navigator.languages || [])
  .map(matchLocale)
  .find(Boolean)
const initialLocale = (savedLocale && availableKeys.has(savedLocale))
  ? savedLocale
  : browserLocale || 'en'

const i18n = createI18n({
  legacy: false,
  locale: initialLocale,
  fallbackLocale: 'en',
  messages: {}
})

const loadLocale = async (key) => {
  if (!localeLoaders[key] || i18n.global.availableLocales.includes(key)) return
  const mod = await localeLoaders[key]()
  i18n.global.setLocaleMessage(key, mod.default)
}

const setupI18n = () => Promise.all([loadLocale(initialLocale), loadLocale('en')])

export { availableLocales, loadLocale, setupI18n }
export default i18n
