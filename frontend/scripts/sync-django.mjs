import { cp, mkdir, rm, stat } from 'node:fs/promises'
import { resolve } from 'node:path'

const frontendRoot = resolve(import.meta.dirname, '..')
const projectRoot = resolve(frontendRoot, '..')
const outputRoot = resolve(frontendRoot, '.output/public')
const sourceIndex = resolve(outputRoot, 'index.html')
const sourceStatic = resolve(outputRoot, 'static/frontend')
const templateDir = resolve(projectRoot, 'backend/templates/frontend')
const staticDir = resolve(projectRoot, 'backend/frontend_static/frontend')

await stat(sourceIndex)
await stat(sourceStatic)
await mkdir(templateDir, { recursive: true })
await rm(staticDir, { recursive: true, force: true })
await mkdir(staticDir, { recursive: true })
await cp(sourceIndex, resolve(templateDir, 'index.html'))
await cp(sourceStatic, staticDir, { recursive: true })

console.log('Nuxt 정적 산출물을 Django templates/static으로 동기화했습니다.')

