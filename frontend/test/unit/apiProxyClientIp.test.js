import assert from 'node:assert/strict'
import process from 'node:process'
import { createServer } from 'node:http'
import { once } from 'node:events'
import test from 'node:test'
import { createTestServer } from '../helpers/createTestServer.js'

test('所有后端代理入口用实际连接IP覆盖客户端伪造的转发身份', async (t) => {
  const backend = createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify({ address: request.headers['x-real-ip'] }))
  }).listen(0, '127.0.0.1')
  await once(backend, 'listening')
  t.after(() => new Promise((resolve) => backend.close(resolve)))
  const previous = process.env.VITE_API_URL
  process.env.VITE_API_URL = `http://127.0.0.1:${backend.address().port}`
  let frontend
  try {
    frontend = await createTestServer({ server: { host: '127.0.0.1', port: 0 } })
    await frontend.listen()
    const base = `http://127.0.0.1:${frontend.httpServer.address().port}`
    for (const path of ['/api/auth/proxy-test', '/api/mcp', '/.well-known/oauth-proxy-test']) {
      const response = await fetch(base + path, {
        headers: { 'X-Real-IP': '203.0.113.99', 'X-Forwarded-For': '198.51.100.1, 198.51.100.2' }
      })
      assert.equal(response.status, 200)
      assert.equal((await response.json()).address, '127.0.0.1', path)
    }
  } finally {
    await frontend?.close()
    if (previous === undefined) delete process.env.VITE_API_URL
    else process.env.VITE_API_URL = previous
  }
})
