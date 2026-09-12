import assert from 'node:assert/strict'
import test from 'node:test'
import { getPreviewTypeByPath, normalizePreviewResponse } from '../../src/utils/file_preview.js'

test('六种 Office 格式区分 PDF 转换与只读工作簿', () => {
  for (const suffix of ['doc', 'docx', 'ppt', 'pptx']) {
    assert.equal(getPreviewTypeByPath(`文件.${suffix}`), 'office')
  }
  for (const suffix of ['xls', 'XLSX']) {
    assert.equal(getPreviewTypeByPath(`文件.${suffix}`), 'spreadsheet')
  }
})

test('预览响应保留多工作表内容及失败提示', async () => {
  const content = {
    sheets: [
      { name: '销售', rows: [[{ text: '25.00%' }]] },
      { name: '说明', rows: [] }
    ]
  }
  const result = await normalizePreviewResponse(
    Response.json({
      preview_type: 'spreadsheet',
      content,
      supported: true
    })
  )
  assert.deepEqual(result.content, content)
  assert.equal(result.status, 'ready')
  const failure = await normalizePreviewResponse(
    Response.json({
      supported: false,
      message: 'Excel 展开区域超过 100000 个单元格'
    })
  )
  assert.equal(failure.status, 'unsupported')
  assert.match(failure.message, /100000/)
})
