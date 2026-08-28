export const isSteerableMainChatRun = (run) =>
  run?.status === 'running' && run?.run_type === 'chat' && run?.source === 'chat'

const RUN_STATUS_VIEWS = {
  pending: { label: '等待中', color: 'default' },
  running: { label: '进行中', color: 'green' },
  cancel_requested: { label: '取消中', color: 'orange' },
  completed: { label: '已完成', color: 'blue' },
  failed: { label: '失败', color: 'red' },
  cancelled: { label: '已取消', color: 'default' },
  interrupted: { label: '已中断', color: 'orange' }
}

export const getAgentRunStatusView = (status) =>
  RUN_STATUS_VIEWS[status] || { label: status || '未运行', color: 'default' }
