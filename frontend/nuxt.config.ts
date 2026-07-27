export default defineNuxtConfig({
  compatibilityDate: '2026-07-15',
  ssr: false,
  devtools: { enabled: false },
  modules: ['@nuxt/eslint'],
  css: ['~/assets/css/main.css'],
  app: {
    buildAssetsDir: '/static/frontend/_nuxt/',
    head: {
      htmlAttrs: { lang: 'ko' },
      title: 'Slack Dashboard',
      meta: [
        { name: 'description', content: 'Slack에서 수집한 지식과 일정을 한곳에서 확인합니다.' },
        { name: 'theme-color', content: '#f4f1e8' },
      ],
    },
  },
  devServer: {
    host: '0.0.0.0',
    port: 3000,
    https: process.env.NUXT_HTTPS === '1'
      ? {
          key: process.env.NUXT_HTTPS_KEY || '../pem/server.key',
          cert: process.env.NUXT_HTTPS_CERT || '../pem/server.crt',
        }
      : false,
  },
  nitro: {
    preset: 'static',
    devProxy: {
      '/api': {
        target: process.env.NUXT_DEV_API_TARGET || 'http://127.0.0.1:8000/api',
        changeOrigin: true,
        secure: false,
      },
    },
  },
  typescript: {
    strict: true,
    typeCheck: true,
  },
})
