// 真实页面、HTTP、worker 和 PG；只在响应已由真实服务生成后注入丢包。
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
const env = process.env
const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
async function retry(fn, label) {
  for (let i = 0; i < 300; i++) {
    try { const result = await fn(); if (result) return result } catch { /* 服务启动中 */ }
    await wait(200)
  }
  throw Error(`超时：${label}`)
}
const tabs = await retry(async () => (await fetch(`http://127.0.0.1:${env.CLIENT_CDP_PORT}/json`)).json(), 'Chrome')
const ws = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl)
await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }))
let id = 0
const pending = new Map()
ws.addEventListener('message', event => {
  const message = JSON.parse(event.data)
  if (message.id) {
    const promise = pending.get(message.id)
    pending.delete(message.id)
    message.error ? promise.reject(message.error) : promise.resolve(message.result)
  }
})
const call = (method, params = {}) => new Promise((resolve, reject) => {
  pending.set(++id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params }))
})
async function evaluate(expression) {
  const value = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (value.exceptionDetails) throw Error(value.exceptionDetails.text)
  return value.result.value
}
async function screenshot(name) {
  const result = await call('Page.captureScreenshot', { format: 'png' })
  await fs.writeFile(`${env.CLIENT_EVIDENCE_DIR}/${name}.png`, Buffer.from(result.data, 'base64'))
}
async function arm(path, marker, drops, stream = false) {
  await evaluate(`(() => {
    window.originalFetch ||= window.fetch.bind(window);
    window.submissions = [];
    let remaining = ${drops};
    window.fetch = async (url, options) => {
      if (!String(url).endsWith(${JSON.stringify(path)}) || options?.method !== 'POST') return originalFetch(url, options);
      const body = JSON.parse(options.body);
      if ((body.query || body.message) !== ${JSON.stringify(marker)}) return originalFetch(url, options);
      submissions.push(body);
      const response = await originalFetch(url, options);
      if (remaining-- > 0 && response.ok) {
        const content = await response.text();
        ${stream ? `return new Response(content.split('\\n').filter(line => line && JSON.parse(line).type === 'meta').join('\\n') + '\\n', { headers: { 'content-type': 'application/x-ndjson' } });` : `throw new TypeError('synthetic response lost after commit');`}
      }
      return response;
    };
  })()`)
}
try {
  await call('Page.enable'); await call('Runtime.enable')
  const config = JSON.parse(await fs.readFile(`${env.KB_E2E_STATE_DIR}/client.json`, 'utf8'))
  await call('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('user_token',${JSON.stringify(config.token)})` })
  await retry(async () => (await fetch(`http://127.0.0.1:${env.CLIENT_WEB_PORT}`)).ok, 'Vite')
  await call('Page.navigate', { url: `http://127.0.0.1:${env.CLIENT_WEB_PORT}/agent?agent_id=client-test` })
  await retry(() => evaluate(`Boolean(document.querySelector('[contenteditable="true"], textarea'))`), 'Web 输入框')
  for (const [marker, drops] of [['web-auto', 1], ['web-manual', 2]]) {
    await wait(2200)
    await arm('/api/agent/runs', marker, drops)
    await evaluate(`(() => {
      const input = document.querySelector('[contenteditable="true"], textarea');
      input.focus();
      if (input.tagName === 'TEXTAREA') input.value = ${JSON.stringify(marker)};
      else input.textContent = ${JSON.stringify(marker)};
      input.dispatchEvent(new Event('input', { bubbles: true }));
    })()`)
    await wait(200)
    await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 })
    await call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 })
    if (drops === 2) {
      await retry(() => evaluate(`Boolean([...document.querySelectorAll('button')].find(b => b.textContent.includes('恢复发送') && !b.classList.contains('ant-btn-loading')))`), 'Web 恢复按钮')
      await screenshot('web-uncertain')
      await evaluate(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('恢复发送')).click()`)
    }
    await retry(() => evaluate(`document.body.innerText.includes('RECOVERED:${marker}')`), marker)
    const bodies = await evaluate('submissions')
    assert.equal(bodies.length, drops + 1, marker)
    for (const body of bodies) assert.deepEqual(body, bodies[0])
  }
  await screenshot('web-recovered')
  await retry(async () => (await fetch(`http://127.0.0.1:${env.CLIENT_CLI_PORT}`)).ok, 'CLI')
  await call('Page.navigate', { url: `http://127.0.0.1:${env.CLIENT_CLI_PORT}` })
  await retry(() => evaluate(`Boolean(document.querySelector('#input'))`), 'CLI 输入框')
  for (const [marker, stream] of [['cli-first', false], ['cli-stream', true], ['cli-repeat', false]]) {
    await arm('/api/chat', marker, marker === 'cli-repeat' ? 0 : 1, stream)
    const send = () => evaluate(`document.querySelector('#input').value = ${JSON.stringify(marker)}; document.querySelector('#send').click()`)
    await send()
    if (marker !== 'cli-repeat') {
      await retry(() => evaluate(`document.querySelector('#status').textContent.includes('结果尚未确认')`), marker + '断线')
      assert.equal(await evaluate(`document.querySelector('#input').disabled && document.querySelector('#new-chat').disabled`), true)
      await screenshot(marker + '-uncertain')
      await evaluate(`document.querySelector('#send').click()`)
    }
    await retry(() => evaluate(`document.querySelector('#send').textContent === '发送' && document.querySelector('#messages').textContent.includes('RECOVERED:${marker}')`), marker + '恢复')
    if (marker === 'cli-repeat') { await send(); await retry(() => evaluate(`document.querySelector('#send').textContent === '发送' && [...document.querySelectorAll('.assistant .content')].filter(e => e.textContent === 'RECOVERED:cli-repeat').length === 2`), '有意重复') }
    const bodies = await evaluate('submissions')
    assert.equal(bodies.length, 2)
    if (marker === 'cli-repeat') assert.notEqual(bodies[0].request_id, bodies[1].request_id)
    else assert.deepEqual(bodies[0], bodies[1])
    if (marker === 'cli-first') assert.equal(bodies[1].thread_id, null)
  }
  await screenshot('cli-recovered')
  // 协议负向检查：权威队列终态应解锁首次发送和恢复中的页面。
  for (const recovering of [false, true]) {
    await evaluate(`(() => {
      let calls = 0;
      window.fetch = async () => {
        const uncertain = ${recovering} && calls++ === 0;
        return new Response(JSON.stringify({ error: '排队请求结束：cancelled', uncertain, terminal: !uncertain }), { status: uncertain ? 502 : 400, headers: { 'content-type': 'application/json' } });
      };
      document.querySelector('#input').value = 'terminal-control';
      document.querySelector('#send').click();
    })()`)
    if (recovering) {
      await retry(() => evaluate(`document.querySelector('#status').textContent.includes('结果尚未确认')`), '终态前未知')
      await evaluate(`document.querySelector('#send').click()`)
    }
    await retry(() => evaluate(`!document.querySelector('#input').disabled && !document.querySelector('#new-chat').disabled && document.querySelector('#send').textContent === '发送'`), '终态解锁')
  }
  console.log('真实 Web/CLI 页面：自动恢复、手动恢复、首次空线程和流中断恢复均保持原身份；有意重复使用新编号。')
} catch (error) {
  await screenshot('failure')
  await fs.writeFile(`${env.CLIENT_EVIDENCE_DIR}/failure.txt`, await evaluate('document.body.innerText'))
  throw error
} finally { ws.close() }
