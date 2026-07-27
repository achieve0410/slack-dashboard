import withNuxt from './.nuxt/eslint.config.mjs'

export default withNuxt({
  rules: {
    'vue/multi-word-component-names': 'off',
    // MarkdownContent escapes HTML before applying the small, allow-listed Slack formatter.
    'vue/no-v-html': 'off',
  },
})
