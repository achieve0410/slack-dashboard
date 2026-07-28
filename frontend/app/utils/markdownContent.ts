function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

function decodeSlackEntities(value: string): string {
  return value
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
}

function renderText(value: string): string {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*\n]+)\*/g, '<strong>$1</strong>')
    .replace(/_([^_\n]+)_/g, '<em>$1</em>')
}

function renderInline(value: string): string {
  const decoded = decodeSlackEntities(value)
  const linkPattern = /<(https?:\/\/[^>|]+)(?:\|([^>]+))?>|\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g
  let rendered = ''
  let cursor = 0
  for (const match of decoded.matchAll(linkPattern)) {
    const index = match.index
    rendered += renderText(decoded.slice(cursor, index))
    const rawUrl = match[1] || match[4] || ''
    const rawLabel = match[2] || match[3]
    const url = escapeHtml(rawUrl)
    const label = escapeHtml(rawLabel || rawUrl)
    rendered += `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
    cursor = index + match[0].length
  }
  rendered += renderText(decoded.slice(cursor))
  return rendered
}

function renderCodeBlock(lines: string[], language: string): string {
  const lang = language.trim().replace(/[^a-z0-9_+-]/gi, '').toLowerCase()
  const className = lang ? ` class="language-${escapeHtml(lang)}"` : ''
  return `<pre class="content-code-block"><code${className}>${escapeHtml(lines.join('\n'))}</code></pre>`
}

function tableCells(line: string): string[] {
  let body = line.trim()
  if (body.startsWith('|')) body = body.slice(1)
  if (body.endsWith('|')) body = body.slice(0, -1)
  return body.split('|').map(cell => cell.trim())
}

function isTableSeparator(line: string): boolean {
  const cells = tableCells(line)
  return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell))
}

function isTableRow(line: string): boolean {
  return line.trim().includes('|')
}

function renderTable(lines: string[]): string {
  const header = tableCells(lines[0] || '')
  const bodyRows = lines.slice(2).map(tableCells)
  const columns = header.length
  const headerHtml = header
    .map(cell => `<th scope="col">${renderInline(cell)}</th>`)
    .join('')
  const bodyHtml = bodyRows
    .map((row) => {
      const cells = Array.from({ length: columns }, (_, index) => row[index] || '')
      return `<tr>${cells.map(cell => `<td>${renderInline(cell)}</td>`).join('')}</tr>`
    })
    .join('')
  return `<div class="content-table-wrap"><table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`
}

function restoreMarkdownBreaks(content: string): string {
  return content
    .split('\n')
    .map((line) => {
      if (line.trim().startsWith('|')) return line
      return line
        .replace(/\s+(#{1,3}\s+)/g, '\n$1')
        .split('\n')
        .map((segment) => {
          if (segment.trim().startsWith('|')) return segment
          return segment.replace(/\s+(?=\|[^|\n]+\|[^|\n]+\|)/g, '\n')
        })
        .join('\n')
    })
    .join('\n')
}

function renderLine(line: string): string {
  const trimmed = line.trim()
  if (!trimmed) return '<div class="content-gap"></div>'
  if (/^-{5,}$/.test(trimmed)) return '<hr>'
  const heading = trimmed.match(/^(#{1,3})\s+(.+)$/)
  if (heading) {
    const level = Math.min(heading[1]?.length || 2, 3)
    return `<h${level}>${renderInline(heading[2] || '')}</h${level}>`
  }
  const bullet = trimmed.match(/^[-•]\s+(.+)$/)
  if (bullet) return `<div class="content-list"><span>•</span><p>${renderInline(bullet[1] || '')}</p></div>`
  const numbered = trimmed.match(/^(\d+[.)])\s+(.+)$/)
  if (numbered) return `<div class="content-list numbered"><span>${numbered[1]}</span><p>${renderInline(numbered[2] || '')}</p></div>`
  return `<p>${renderInline(trimmed)}</p>`
}

export function renderMarkdownContent(content: string): string {
  const lines = restoreMarkdownBreaks(content).split('\n')
  const rendered: string[] = []
  for (let index = 0; index < lines.length; index += 1) {
    const fence = (lines[index] || '').trim().match(/^```([^`]*)$/)
    if (fence) {
      const codeLines: string[] = []
      index += 1
      while (index < lines.length && !(lines[index] || '').trim().startsWith('```')) {
        codeLines.push(lines[index] || '')
        index += 1
      }
      rendered.push(renderCodeBlock(codeLines, fence[1] || ''))
    }
    else if (isTableRow(lines[index] || '') && isTableSeparator(lines[index + 1] || '')) {
      const tableLines = [lines[index] || '', lines[index + 1] || '']
      index += 2
      while (index < lines.length && isTableRow(lines[index] || '')) {
        tableLines.push(lines[index] || '')
        index += 1
      }
      index -= 1
      rendered.push(renderTable(tableLines))
    }
    else {
      rendered.push(renderLine(lines[index] || ''))
    }
  }
  return rendered.join('')
}
