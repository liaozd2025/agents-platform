const conversationTimestamp = (conversation) => {
  const timestamp = Date.parse(conversation.created_at || '')
  return Number.isNaN(timestamp) ? 0 : timestamp
}

const sortSidebarConversations = (conversations) =>
  [...conversations].sort((left, right) => {
    if (left.is_pinned !== right.is_pinned) return left.is_pinned ? -1 : 1
    return conversationTimestamp(right) - conversationTimestamp(left)
  })

const conversationAgeGroup = (conversation, now = Date.now()) => {
  const timestamp = conversationTimestamp(conversation)
  const age = Math.max(0, now - timestamp)
  const day = 24 * 60 * 60 * 1000
  if (age < 7 * day) return 'recent'
  if (age < 30 * day) return 'week'
  return 'month'
}

export const buildProjectConversationGroups = (projects, conversations, now = Date.now()) => {
  const sortedConversations = sortSidebarConversations(conversations)
  const activeProjects = projects.filter(
    (project) => project.status !== 'deleted' && project.selection_status === 'selectable'
  )
  const conversationsByProject = new Map(activeProjects.map((project) => [project.id, []]))
  const otherConversations = []

  sortedConversations.forEach((conversation) => {
    const projectConversations = conversationsByProject.get(conversation.project_id)
    if (projectConversations) {
      projectConversations.push(conversation)
    } else {
      otherConversations.push(conversation)
    }
  })

  const recentGroups = {
    recent: [],
    week: [],
    month: []
  }
  otherConversations.forEach((conversation) => {
    recentGroups[conversationAgeGroup(conversation, now)].push(conversation)
  })

  return {
    groups: activeProjects.map((project) => ({
      project,
      conversations: conversationsByProject.get(project.id)
    })),
    otherConversations,
    recentGroups
  }
}
