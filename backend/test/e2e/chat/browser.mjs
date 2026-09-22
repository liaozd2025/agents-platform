// 真正 /agent 路由的浏览器回归；合成负载只写本页 Vue 状态，不冒充后端执行。
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
const env = process.env
const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
async function retry(fn, label) {
  for (let i = 0; i < 300; i++) {
    try { const result = await fn(); if (result) return result } catch { /* 等待独立服务启动 */ }
    await wait(200)
  }
  throw Error(`超时：${label}`)
}
const tabs = await retry(async () => (await fetch(`http://127.0.0.1:${env.CLIENT_CDP_PORT}/json`)).json(), 'Chrome')
const ws = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl)
await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }))
let sequence = 0
const pending = new Map()
ws.addEventListener('message', event => {
  const message = JSON.parse(event.data)
  if (!message.id) return
  const request = pending.get(message.id)
  pending.delete(message.id)
  message.error ? request.reject(message.error) : request.resolve(message.result)
})
const call = (method, params = {}) => new Promise((resolve, reject) => {
  pending.set(++sequence, { resolve, reject })
  ws.send(JSON.stringify({ id: sequence, method, params }))
})
async function evaluate(expression) {
  const result = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails))
  return result.result.value
}
async function screenshot(name) {
  const result = await call('Page.captureScreenshot', { format: 'png' })
  await fs.writeFile(`${env.CLIENT_EVIDENCE_DIR}/${name}.png`, Buffer.from(result.data, 'base64'))
}
const metrics = { history: [], streaming: {}, lifecycle: {} }
try {
  await call('Page.enable')
  await call('Runtime.enable')
  const config = JSON.parse(await fs.readFile(`${env.KB_E2E_STATE_DIR}/client.json`, 'utf8'))
  await call('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('user_token',${JSON.stringify(config.token)})` })
  await retry(async () => (await fetch(`http://127.0.0.1:${env.CLIENT_WEB_PORT}`)).ok, 'Vite')
  await call('Page.navigate', { url: `http://127.0.0.1:${env.CLIENT_WEB_PORT}/agent?agent_id=client-test` })
  await retry(() => evaluate(`Boolean(document.querySelector('.chat-box') && document.querySelector('[contenteditable="true"], textarea'))`), '主路由')
  await evaluate(`(() => {
    window.bindChat = () => {
    let instance = document.querySelector('.chat-box').__vueParentComponent;
    while (instance && instance.type.__name !== 'AgentChatComponent') instance = instance.parent;
    if (!instance) throw Error('未找到真实主聊天组件');
    window.chat = instance.setupState;
    window.chatApp = instance.appContext.app;
    }; bindChat();
    window.longTasks = []; window.inputEvents = []; window.peakHeap = 0;
    window.heapSampler=setInterval(()=>{peakHeap=Math.max(peakHeap,performance.memory.usedJSHeapSize)},50);
    new PerformanceObserver(list => longTasks.push(...list.getEntries().map(e => e.duration))).observe({type:'longtask'});
    new PerformanceObserver(list => inputEvents.push(...list.getEntries().map(e => e.processingStart-e.startTime))).observe({type:'event',durationThreshold:16});
    window.makeText = n => {const block='合成正文 **重点** 与列表。\\n\\n- 第一项\\n- 第二项\\n\\n';return block.repeat(Math.floor(n/block.length))+'文'.repeat(n%block.length)};
  })()`)
  metrics.markdown = await evaluate(`import('/test/browser/markdownStream.js').then(m=>m.checkMarkdownStream())`)
  if (env.CHAT_EXPECT_BASELINE !== '1') {
    assert.deepEqual(metrics.markdown, { waiting: '开始', pending: 1, flushed: '正文9', final: '尾部完整', remaining: 0 })
  }
  const created = await fetch(`http://127.0.0.1:${env.CLIENT_API_PORT}/api/chat/thread`, {
    method: 'POST', headers: { Authorization: `Bearer ${config.token}`, 'content-type': 'application/json' },
    body: JSON.stringify({ agent_id: 'client-test', title: '长聊天合成验证' })
  })
  assert.equal(created.status, 200)
  const thread = await created.json()
  await evaluate(`chat.selectThreadFromRoute(${JSON.stringify(thread.id)})`)
  await wait(1500)
  await evaluate("bindChat()")
  for (let repetition = 0; repetition < 3; repetition++) {
    await evaluate(`chat.threadMessages[chat.currentChatId]=[];chat.threadRuns[chat.currentChatId]=[]`)
    await wait(200)
    await call('HeapProfiler.collectGarbage')
    await evaluate(`longTasks=[];inputEvents=[];peakHeap=0;document.querySelector('[contenteditable="true"],textarea').focus()`)
    const mount = evaluate(`(() => {
      const id=chat.currentChatId;
      chat.threadRuns[id]=Array.from({length:500},(_,i)=>({run_id:'history-'+i,status:'completed',timing:{created_at:new Date(1700000000000+i*1000).toISOString()}}));
      chat.threadMessages[id]=Array.from({length:500},(_,i)=>({id:'message-'+i,type:'ai',role:'assistant',run_id:'history-'+i,content:makeText(1000)+'结束 '+i,created_at:new Date(1700000000000+i*1000).toISOString()}));
    })()`)
    await wait(5)
    const start = performance.now()
    await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'x', text: 'x', code: 'KeyX' })
    await call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'x', code: 'KeyX' })
    const inputRoundtrip = performance.now()-start
    await mount
    await wait(200)
    metrics.history.push({ inputRoundtrip, ...await evaluate(`({nodes:document.querySelectorAll('*').length,messages:document.querySelectorAll('.chat-box .message-md').length,longTasks,inputEvents,peakHeap,heap:performance.memory.usedJSHeapSize})`) })
  }
  await evaluate(`document.querySelector('.chat-main').scrollTop=0`)
  await screenshot('history-window')
  await evaluate(`import('/src/stores/theme.js').then(m=>m.useThemeStore().setTheme(true))`)
  await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true })
  await evaluate(`document.querySelector('[aria-label="折叠侧边栏"]').click()`)
  await wait(200)
  await screenshot('history-dark-mobile')
  await call('Emulation.clearDeviceMetricsOverride')
  await evaluate(`document.querySelector('[aria-label="展开侧边栏"]').click()`)
  await evaluate(`import('/src/stores/theme.js').then(m=>m.useThemeStore().setTheme(false))`)
  await wait(200)
  const baseline = env.CHAT_EXPECT_BASELINE === '1'
  if (!baseline) {
    assert.equal(metrics.history[2].messages, 20)
    for (let i = 0; i < 24; i++) {
      await evaluate(`[...document.querySelectorAll('.chat-box button')].find(b=>b.textContent.includes('查看更早消息')).click()`)
      await wait(50)
    }
    await retry(() => evaluate(`document.querySelectorAll('.chat-box .message-md').length===500`), '完整历史')
    assert.equal(await evaluate(`document.querySelector('.chat-box .message-md').textContent.includes('结束 0')`), true)
  }
  await screenshot('history')
  const secondResponse = await fetch(`http://127.0.0.1:${env.CLIENT_API_PORT}/api/chat/thread`, {
    method: 'POST', headers: { Authorization: `Bearer ${config.token}`, 'content-type': 'application/json' },
    body: JSON.stringify({ agent_id: 'client-test', title: '切换验证' })
  })
  assert.equal(secondResponse.status, 200)
  const second = await secondResponse.json()
  await evaluate(`chat.selectThreadFromRoute(${JSON.stringify(second.id)})`)
  await wait(300)
  await evaluate('bindChat()')
  assert.equal(await evaluate(`document.querySelectorAll('.chat-box .message-md').length`), 0)
  assert.equal(await evaluate(`[...document.querySelectorAll('.chat-box button')].some(b=>b.textContent.includes('查看更早消息'))`), false)
  await evaluate(`chat.threadMessages[chat.currentChatId]=[];chat.threadRuns[chat.currentChatId]=[]`)
  await wait(200)
  await call('HeapProfiler.collectGarbage')
  await evaluate(`(() => {
    const thread=chat.currentChatId,state=chat.getThreadState(thread);
    state.isStreaming=true;state.replyLoadingVisible=true;state.activeRunId='stream-test';
    window.expected=makeText(100000);window.streamDone=false;longTasks=[];inputEvents=[];peakHeap=0;
    const emit=content=>chat.handleStreamChunk({status:'loading',run_id:'stream-test',stream_event:{type:'message_delta',message_id:'stream-message',content}},thread);
    emit(expected);let count=0;
    window.streamTimer=setInterval(()=>{const chunk=makeText(100);expected+=chunk;emit(chunk);if(++count===30){clearInterval(streamTimer);chat.handleStreamChunk({status:'finished'},thread);streamDone=true}},30);
  })()`)
  const keys = [], started = performance.now()
  while (performance.now()-started < 20000) {
    await evaluate(`document.querySelector('[contenteditable="true"],textarea').focus()`)
    const start = performance.now()
    await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'y', text: 'y', code: 'KeyY' })
    await call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'y', code: 'KeyY' })
    keys.push(performance.now()-start)
    await wait(100)
    if (await evaluate('streamDone')) break
  }
  metrics.streaming = { elapsedMs: performance.now()-started, keys, ...await evaluate(`({done:streamDone,longTasks,inputEvents,peakHeap,heap:performance.memory.usedJSHeapSize})`) }
  assert.equal(metrics.streaming.done, true)
  await retry(() => evaluate(`chat.onGoingConvMessages.some(m=>m.content===expected)`), '流结束完整源文')
  assert.equal(await evaluate(`(() => {const plain=expected.replaceAll('**','').replace(/^- /gm,'').replace(/\\s+/g,' ').trim();return plain.length>50000 && document.querySelector('.chat-box .message-md').textContent.replace(/\\s+/g,' ').trim()===plain})()`), true)
  await screenshot('stream-complete')
  await evaluate(`(() => {
    window.aborted=[];
    for(const id of [chat.currentChatId,'background-thread']) {
      const state=chat.getThreadState(id);state.runStreamAbortController=new AbortController();
      state.runStreamAbortController.signal.addEventListener('abort',()=>aborted.push(id));
      state.requestStreams.synthetic={controller:new AbortController()};
      state.requestStreams.synthetic.controller.signal.addEventListener('abort',()=>aborted.push(id+':request'));
      chat.streamSmoother.pushChunk({id:'pending-'+id,type:'AIMessageChunk',content:'缓冲'.repeat(1000)},id);
    }
    clearInterval(heapSampler);chatApp.unmount();
  })()`)
  await wait(600)
  metrics.lifecycle = await evaluate(`({remaining:Object.keys(chat.chatState.threadStates),aborted,markdownNodes:document.querySelectorAll('.message-md').length})`)
  if (!baseline) {
    assert.deepEqual(metrics.lifecycle.remaining, [])
    assert.equal(metrics.lifecycle.aborted.length, 4)
    assert.equal(metrics.lifecycle.markdownNodes, 0)
  }
  console.log(JSON.stringify(metrics))
} catch (error) {
  await screenshot('failure')
  await fs.writeFile(`${env.CLIENT_EVIDENCE_DIR}/failure.txt`, await evaluate('document.body.innerText'))
  throw error
} finally {
  await fs.writeFile(`${env.CLIENT_EVIDENCE_DIR}/metrics.json`, JSON.stringify(metrics,null,2))
  ws.close()
}
