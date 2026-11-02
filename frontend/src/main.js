import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import i18n, { setupI18n } from './i18n'

const app = createApp(App)

app.use(router)
app.use(i18n)

setupI18n().then(() => app.mount('#app'))
