import assert from 'node:assert/strict'
import test from 'node:test'

import { MessageProcessor } from '../../src/utils/messageProcessor.js'

test('交付物只归属于调用 present_artifacts 的对话', () => {
  const artifactConversation = {
    messages: [
      {
        type: 'ai',
        tool_calls: [
          {
            name: 'present_artifacts',
            tool_call_result: { content: '已将交付物展示给用户' },
            args: JSON.stringify({
              filepaths: [
                '/home/gem/user-data/outputs/bubble_sort.py',
                '/home/gem/user-data/outputs/bubble_sort.js'
              ]
            })
          },
          {
            function: { name: 'present_artifacts' },
            status: 'success',
            args: { filepaths: ['/home/gem/user-data/outputs/bubble_sort.py'] }
          }
        ]
      }
    ]
  }
  const laterConversation = {
    messages: [{ type: 'human', content: '运行 Python 的' }]
  }

  assert.deepEqual(MessageProcessor.extractArtifactsFromConversation(artifactConversation), [
    '/home/gem/user-data/outputs/bubble_sort.py',
    '/home/gem/user-data/outputs/bubble_sort.js'
  ])
  assert.deepEqual(MessageProcessor.extractArtifactsFromConversation(laterConversation), [])
})

test('query_kb 的实际命中结果保留知识库来源和 OA 链接', () => {
  const sources = MessageProcessor.extractSourcesFromMessage(
    {
      type: 'ai',
      tool_calls: [
        {
          name: 'query_kb',
          args: { kb_id: 'kb-1' },
          tool_call_result: {
            content: JSON.stringify({
              kb_id: 'kb-1',
              results: [
                {
                  file_id: 'file-1',
                  content: '命中片段',
                  metadata: {
                    file_id: 'file-1',
                    source: '导入文件.md',
                    source_ref: {
                      source_type: 'knowledge_base',
                      title: '新闻详细-2191146',
                      author: '刘从新',
                      url: 'https://hnjiudian.cn/web/index.html#/corporate-culture/view-page/3?title=x&taskID=2191146'
                    }
                  }
                }
              ]
            })
          }
        }
      ]
    },
    [{ kb_id: 'kb-1', name: '九典视界知识库' }]
  )

  assert.equal(sources.knowledgeChunks.length, 1)
  assert.equal(sources.knowledgeChunks[0].kb_name, '九典视界知识库')
  assert.equal(sources.knowledgeChunks[0].metadata.source_ref.source_type, 'knowledge_base')
  assert.equal(sources.knowledgeChunks[0].metadata.source_ref.url.endsWith('taskID=2191146'), true)
})

test('自动知识库检索从助手消息元数据恢复来源', () => {
  const sources = MessageProcessor.extractSourcesFromMessage(
    {
      type: 'ai',
      extra_metadata: {
        knowledge_sources: [
          {
            kb_id: 'kb-1',
            file_id: 'file-1',
            content: '自动检索命中片段',
            metadata: {
              file_id: 'file-1',
              source: '导入文章.md',
              source_ref: {
                source_type: 'knowledge_base',
                title: '新闻详细-2191146',
                url: 'https://hnjiudian.cn/web/index.html#/corporate-culture/view-page/3?title=x&taskID=2191146'
              }
            }
          }
        ]
      }
    },
    [{ kb_id: 'kb-1', name: '九典视界知识库' }]
  )

  assert.equal(sources.knowledgeChunks.length, 1)
  assert.equal(sources.knowledgeChunks[0].content, '自动检索命中片段')
  assert.equal(sources.knowledgeChunks[0].metadata.source_ref.url.endsWith('taskID=2191146'), true)
})
