/**
 * 技能状态变化后同步「对话页可 @ 技能」候选。
 *
 * 对话页的 @技能 候选来自 agent 详情的 `configurable_items.skills.options`
 * （经 useAgentMentionConfig 归一化），而 agent 详情在 agent store 里是按 agentId
 * 缓存的（fetchAgentDetail 默认命中缓存）。技能被禁用/卸载后如果不强制刷新这份缓存，
 * 已经不可用的技能会一直留在 @ 候选里，看起来像「禁用没生效」。
 *
 * 这里的 refresh 只是让前端列表与服务端 `list_accessible_skills(require_enabled=True)`
 * 对齐——服务端在解析运行时技能时本来就会把已禁用的技能剔除掉。
 */

/**
 * 强制刷新当前选中 Agent 的详情，从而刷新可 @ 的技能候选。
 *
 * @param {object} agentStore agent store 实例（显式传入，便于测试与复用）
 * @returns {Promise<boolean>} 是否真正发起了刷新（未选 Agent 时为 false）
 */
export const refreshSelectedAgentSkillOptions = async (agentStore) => {
  const agentId = agentStore?.selectedAgentId
  if (!agentId) return false

  try {
    await agentStore.fetchAgentDetail(agentId, true)
    return true
  } catch (error) {
    // 技能状态已经写入成功，刷新失败只影响候选列表的即时性，不打断主流程
    console.warn('[Skill] 刷新当前 Agent 的 Skill 候选失败', { agentId, error })
    return false
  }
}
