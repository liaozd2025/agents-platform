import assert from 'node:assert/strict'
import test from 'node:test'
import { createMarkdownRenderer } from '../../src/utils/markdown_preview.js'
import {
  citationSourcesCacheKey,
  getMessageCitationSources,
  getToolCitationSources,
  isBoundCitationSource,
  mergeCitationSources,
  resolveAnswerCitation,
  safeCitationUrl
} from '../../src/utils/answerCitations.js'

const renderer = createMarkdownRenderer({ themeName: 'github-light' })
const sourceA = {
  source: 'kb://kb-a/file-1',
  source_type: 'file',
  title: '同名.pdf',
  kb_id: 'kb-a',
  file_id: 'file-1',
  excerpts: [{ text: '原文甲，不能替换为智能体摘要。' }]
}
const sourceB = {
  source: 'kb://kb-b/file-2',
  source_type: 'file',
  title: '同名.pdf',
  kb_id: 'kb-b',
  file_id: 'file-2',
  excerpts: [{ text: '原文乙' }]
}

test('两个子任务局部编号均为 1，主正文统一编号，重复来源复用编号', () => {
  const input =
    '甲<cite source="kb://kb-a/file-1" type="file">1</cite>。' +
    '乙<cite type="file" source="kb://kb-b/file-2">1</cite>。' +
    '再引用甲<cite source="kb://kb-a/file-1" type="file">9</cite>。'
  const html = renderer.render(input)
  assert.deepEqual(
    [...html.matchAll(/aria-label="查看引用 (\d+) 原文"/g)].map((m) => m[1]),
    ['1', '2', '1']
  )
  assert.match(html, /<button type="button" class="citation-ref"/)
  assert.match(html, /aria-haspopup="dialog"/)
  assert.match(
    renderer.render('新回复<cite source="https://example.com" type="url">8</cite>'),
    />1<\/button>/
  )
})

test('代码块与行内代码中的引用不产生按钮，也不占用编号', () => {
  const cite = '<cite source="https://example.com" type="url">8</cite>'
  const html = renderer.render('`' + cite + '`\n\n```html\n' + cite + '\n```\n\n正文' + cite)
  assert.equal((html.match(/class="citation-ref"/g) || []).length, 1)
  assert.match(html, /&lt;cite/)
  assert.match(html, />1<\/button>/)
})

test('紧邻的同一来源引用只渲染一个徽标，正文他处复引正常保留', () => {
  const cite = (number) => `<cite source="kb://kb-a/file-1" type="file">${number}</cite>`
  const numbers = (content) =>
    [...renderer.render(content).matchAll(/aria-label="查看引用 (\d+) 原文"/g)].map((m) => m[1])

  // 模型在同一处连贴两个相同来源的标签（自带局部编号），归一化后不得并排显示
  assert.deepEqual(numbers(`共处${cite(1)}${cite(6)}。`), ['1'])
  assert.deepEqual(numbers(`共处${cite(1)} ${cite(6)}。`), ['1'])
  assert.deepEqual(numbers(`${cite(1)}${cite(6)}${cite(3)}`), ['1'])
  // 不同来源相邻，以及同一来源在正文其他位置被再次引用，都属于正常引用
  const other = '<cite source="kb://kb-b/file-2" type="file">2</cite>'
  assert.deepEqual(numbers(`甲${cite(1)}${other}乙${cite(3)}`), ['1', '2', '1'])
})

test('引用属性按文本转义，危险地址和相对地址不能成为打开入口', () => {
  const html = renderer.render(
    '<cite source="https://example.com/?q=&quot; onmouseover=&quot;alert(1)" type="url">1</cite>'
  )
  assert.match(html, /q=&quot; onmouseover=&quot;alert/)
  assert.equal(html.includes(' onmouseover="'), false)
  for (const url of [
    'javascript:alert(1)',
    'data:text/html,x',
    '//example.com',
    '/local',
    'https://u:p@example.com'
  ]) {
    assert.equal(safeCitationUrl(url), '')
    assert.equal(resolveAnswerCitation(url, []).url, '')
  }
  assert.equal(safeCitationUrl('https://example.com/a'), 'https://example.com/a')
})

test('同名文件保持不同身份，同一来源的多个真实片段合并', () => {
  const sources = mergeCitationSources([
    sourceA,
    sourceB,
    { ...sourceA, excerpts: [{ text: '第二个片段' }, ...sourceA.excerpts] }
  ])
  assert.equal(sources.length, 2)
  assert.deepEqual(
    resolveAnswerCitation(sourceA.source, sources).excerpts.map((e) => e.text),
    ['原文甲，不能替换为智能体摘要。', '第二个片段']
  )
  assert.deepEqual(resolveAnswerCitation(sourceB.source, sources).excerpts, [{ text: '原文乙' }])
  const unknown = resolveAnswerCitation('同名.pdf', sources)
  assert.deepEqual(unknown.excerpts, [])
  assert.equal(unknown.kb_id, undefined)
})

test('未绑定本消息来源的引用不渲染徽标，安全链接与已绑定来源保留', () => {
  const fabricated = '<cite source="file:///home/gem/skills/image-gen/SKILL.md" type="file">1</cite>'
  const bound = '<cite source="kb://kb-a/file-1" type="file">1</cite>'
  const html = renderer.render(`甲${bound}假${fabricated}乙`, { answerCitationSources: [sourceA] })
  assert.equal((html.match(/class="citation-ref"/g) || []).length, 1)
  assert.equal(html.includes('file://'), false)
  assert.match(html, />1<\/button>/)

  const linked = '<cite source="https://example.com/a" type="url">1</cite>'
  assert.match(renderer.render(linked, { answerCitationSources: [] }), /class="citation-ref"/)
  // 未接入来源集合的渲染场景保持原行为
  assert.match(renderer.render(fabricated), /class="citation-ref"/)
  assert.match(
    renderer.render('<cite source="https://example.com/summary" type="url">1</cite>', {
      answerCitationSources: []
    }),
    /class="citation-ref"/
  )
})

test('绑定判定与缓存键覆盖来源身份，避免不同来源串用渲染结果', () => {
  assert.equal(isBoundCitationSource(sourceA.source, [sourceA]), true)
  assert.equal(isBoundCitationSource('kb://kb-a/file-9', [sourceA]), false)
  assert.equal(isBoundCitationSource('https://example.com/a', []), true)
  assert.equal(isBoundCitationSource('file:///tmp/a.md', [sourceA]), false)
  assert.equal(citationSourcesCacheKey(undefined), '')
  assert.equal(citationSourcesCacheKey([sourceA]), `kb://kb-a/file-1\u0001`)
  assert.notEqual(citationSourcesCacheKey([sourceA]), citationSourcesCacheKey([sourceB]))
})

test('历史回复只消费当前消息中精确来源，网页摘要不能充当原文', () => {
  const message = {
    content: '旧正文不变',
    extra_metadata: {
      knowledge_sources: [
        { kb_id: 'kb-a', file_id: 'file-1', content: '旧原文', metadata: { source: '同名.pdf' } }
      ]
    },
    tool_calls: [
      {
        name: 'web_search',
        tool_call_result: {
          content: JSON.stringify({
            results: [
              { url: 'https://example.com/summary', title: '只有摘要', content: '这是模型摘要' },
              {
                url: 'https://example.com/raw',
                title: '原始页面',
                content: '摘要',
                raw_content: '<script>原文按文本显示</script>'
              }
            ]
          })
        }
      }
    ]
  }
  const before = JSON.stringify(message)
  const sources = getMessageCitationSources(message)
  assert.deepEqual(resolveAnswerCitation('https://example.com/summary', sources).excerpts, [])
  assert.deepEqual(resolveAnswerCitation('https://example.com/raw', sources).excerpts, [
    { text: '<script>原文按文本显示</script>' }
  ])
  assert.deepEqual(
    resolveAnswerCitation('kb://kb-a/file-1', sources).excerpts.map((e) => e.text),
    ['旧原文']
  )
  assert.deepEqual(resolveAnswerCitation('同名.pdf', sources).excerpts, [])
  assert.equal(JSON.stringify(message), before)
  assert.equal(renderer.render(message.content), '<p>旧正文不变</p>\n')
})

test('同步与异步子任务可读取绑定证据，消息字段优先且不借相邻 Run', () => {
  const task = {
    tool_call_result: { content: '核查结果', artifact: { citation_sources: [sourceA] } }
  }
  const asyncTask = {
    tool_call_result: {
      content: JSON.stringify({ result: { output: '核查结果', citation_sources: [sourceB] } })
    }
  }
  assert.deepEqual(getToolCitationSources(task)[0].excerpts, sourceA.excerpts)
  assert.deepEqual(getToolCitationSources(asyncTask)[0].excerpts, sourceB.excerpts)
  const message = { extra_metadata: { citation_sources: [sourceB] }, tool_calls: [task] }
  assert.deepEqual(
    getMessageCitationSources(message).map((s) => s.source),
    [sourceB.source]
  )
  assert.deepEqual(
    getMessageCitationSources({ extra_metadata: { citation_sources: [] }, tool_calls: [task] }),
    []
  )
  assert.deepEqual(getMessageCitationSources({ content: '无引用旧消息' }), [])
})

test('历史打开文档与文档内查找保留精确文件和全部原文窗口', () => {
  const sources = getMessageCitationSources({
    tool_calls: [
      {
        name: 'open_kb_document',
        args: { kb_id: 'kb-a', file_id: 'file-1' },
        tool_call_result: {
          content: { title: '同名.pdf', content: '窗口一', start_line: 2, end_line: 3 }
        }
      },
      {
        name: 'find_kb_document',
        args: { kb_id: 'kb-a', file_id: 'file-1' },
        tool_call_result: {
          content: { windows: [{ content: '窗口二', start_line: 8, end_line: 9 }] }
        }
      }
    ]
  })
  assert.deepEqual(
    sources[0].excerpts.map((e) => e.text),
    ['窗口一', '窗口二']
  )
  assert.equal(sources[0].excerpts[1].start_line, 8)
  assert.equal(sources[0].source, 'kb://kb-a/file-1')
})
