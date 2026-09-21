<template>
  <div class="skill-cards-page extension-page-root">
    <PageShoulder search-placeholder="搜索技能..." v-model:search="searchQuery">
      <template #actions>
        <template v-if="!isBatchDeleteMode">
          <a-button
            v-if="canManageSkills && activeSkillArea === 'mine'"
            @click="isBatchDeleteMode = true"
            :disabled="loading || importing || filteredDeletableSkills.length === 0"
            class="lucide-icon-btn"
          >
            <span>批量管理</span>
          </a-button>
          <a-button
            v-if="canInstallSkills"
            @click="handleOpenRemoteInstall"
            :disabled="loading || importing"
            class="lucide-icon-btn"
          >
            <Computer :size="14" />
            <span>远程安装</span>
          </a-button>
          <a-upload
            v-if="canInstallSkills"
            accept=".zip,.md"
            :show-upload-list="false"
            :custom-request="handleImportUpload"
            :before-upload="beforeSkillUpload"
            :disabled="loading || importing"
          >
            <a-button type="primary" :loading="importing" class="lucide-icon-btn">
              <Upload :size="14" />
              <span>上传 Skill</span>
            </a-button>
          </a-upload>
          <a-button
            class="lucide-icon-btn"
            aria-label="刷新 Skills"
            :disabled="loading"
            @click="fetchSkills({ refreshPersonal: true })"
          >
            <RefreshCw :size="14" />
          </a-button>
        </template>
        <template v-else>
          <a-button size="small" type="link" @click="handleBatchSelectAll">全选</a-button>
          <a-button size="small" type="link" @click="handleBatchSelectInvert">反选</a-button>
          <a-button size="small" type="link" @click="handleBatchSelectNone">清空</a-button>
          <a-button
            type="primary"
            danger
            :disabled="selectedCardSlugs.length === 0"
            :loading="loading"
            @click="handleBatchDelete"
          >
            批量删除 ({{ selectedCardSlugs.length }})
          </a-button>
          <a-button :disabled="loading" @click="exitBatchDeleteMode">退出管理</a-button>
        </template>
      </template>
    </PageShoulder>

    <nav class="skill-area-tabs" aria-label="技能区域">
      <button
        v-for="tab in skillAreaTabs"
        :key="tab.key"
        type="button"
        class="skill-area-tab"
        :class="{ active: activeSkillArea === tab.key }"
        @click="activeSkillArea = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <nav v-if="activeSkillArea === 'mine'" class="skill-scope-tabs" aria-label="我的技能范围">
      <button
        v-for="tab in mineScopeTabsWithCount"
        :key="tab.key"
        type="button"
        class="skill-scope-tab"
        :class="{ active: activeMineScope === tab.key }"
        @click="activeMineScope = tab.key"
      >
        <span>{{ tab.label }}</span>
        <span class="skill-category-count">{{ tab.count }}</span>
      </button>
    </nav>

    <nav v-if="activeSkillArea === 'plaza'" class="skill-category-tabs" aria-label="Skill 分类">
      <button
        v-for="tab in currentCategoryTabs"
        :key="tab.key"
        type="button"
        class="skill-category-tab"
        :class="{ active: activePlazaCategory === tab.key }"
        :aria-current="activePlazaCategory === tab.key ? 'page' : undefined"
        @click="activePlazaCategory = tab.key"
      >
        <span>{{ tab.label }}</span>
        <span class="skill-category-count">{{ tab.count }}</span>
      </button>
    </nav>

    <div
      v-if="currentVisibleGroups.length === 0"
      class="extension-card-grid-empty-state skill-empty-state"
    >
      <div class="skill-empty-card">
        <div class="skill-empty-icon">
          <WandSparkles :size="22" />
        </div>
        <div class="skill-empty-title">
          {{ searchQuery ? '没有匹配的 Skill' : '还没有添加 Skill' }}
        </div>
        <div class="skill-empty-desc">
          {{
            searchQuery
              ? '换个关键词试试，或清空搜索条件。'
              : '可以从远程仓库安装，或上传本地 Skill 文件。'
          }}
        </div>
      </div>
    </div>

    <template v-else>
      <template v-for="group in currentVisibleGroups" :key="group.key">
        <div class="extension-section-header">{{ group.title }}</div>
        <ExtensionCardGrid :min-width="280">
          <div
            v-for="skill in group.skills"
            :key="`${group.key}:${skill.slug || skill.id}`"
            class="card-wrapper"
            :class="{
              selected: !skill.isSuite && selectedCardSlugs.includes(skill.slug),
              'batch-mode': isBatchDeleteMode && !skill.isSuite
            }"
          >
            <SkillSuiteCard
              v-if="skill.isSuite"
              :suite="skill"
              :installed-slugs="[...installedPersonalSkillKeys]"
              :installed-skills="installedSkillCards"
              @open="openRecommendedSuite"
            />
            <template v-else>
              <a-checkbox
                v-if="
                  isBatchDeleteMode &&
                  canManageSkill(skill) &&
                  skill.sourceType !== 'builtin' &&
                  skill.sourceScope !== 'personal'
                "
                :checked="selectedCardSlugs.includes(skill.slug)"
                @change="handleToggleCardSelect(skill.slug)"
                class="card-select-checkbox"
              />
              <InfoCard
                variant="default"
                :title="formatExtensionCardTitle(skill.name)"
                :subtitle="skill.slug"
                :description="skill.description || '暂无描述'"
                :tags="skillCardTags(skill)"
                :default-icon="getSkillIcon(skill.slug)"
                :disabled="skill.enabled === false"
                @click="handleCardClick(skill)"
                :class="{ 'card-clickable-select': isBatchDeleteMode }"
              >
                <template #actions>
                  <button
                    v-if="canManageSkill(skill)"
                    type="button"
                    class="skill-enabled-action"
                    :class="{ enabled: skill.enabled !== false }"
                    :disabled="isSkillToggling(skill.slug)"
                    :aria-label="skill.enabled === false ? '启用 Skill' : '禁用 Skill'"
                    @click.stop="handleToggleSkillEnabled(skill)"
                  >
                    <Plus v-if="skill.enabled === false" :size="15" class="action-icon" />
                    <template v-else>
                      <Check :size="15" class="action-icon action-icon-check" />
                      <Minus :size="15" class="action-icon action-icon-minus" />
                    </template>
                  </button>
                </template>
              </InfoCard>
            </template>
          </div>
        </ExtensionCardGrid>
      </template>
    </template>

    <a-modal
      v-model:open="skillPreviewVisible"
      class="skill-preview-modal"
      :footer="null"
      width="680px"
      :closable="false"
      :destroy-on-close="true"
      @cancel="closeSkillPreview"
    >
      <div v-if="previewSkill" class="skill-preview-panel">
        <div class="skill-preview-header">
          <div class="skill-preview-title-area">
            <div class="skill-preview-icon">
              <component :is="getSkillIcon(previewSkill.slug)" :size="18" />
            </div>
            <div class="skill-preview-title-text">
              <div class="skill-preview-title">
                {{ formatExtensionCardTitle(previewSkill.name) }}
              </div>
              <div class="skill-preview-meta">
                <span>{{ skillOriginLabel(previewSkill) }}</span>
                <span v-if="previewSkill.enabled === false" class="skill-preview-disabled-tag">
                  已禁用
                </span>
              </div>
            </div>
          </div>
          <div class="skill-preview-actions">
            <a-tooltip :title="useSkillDisabledHint">
              <a-button
                type="primary"
                size="small"
                class="skill-preview-use-btn"
                :disabled="!canUsePreviewSkill"
                @click="usePreviewSkillInChat"
              >
                立即使用
              </a-button>
            </a-tooltip>
            <a-switch
              v-if="canManageSkill(previewSkill)"
              :checked="previewSkill.enabled !== false"
              :disabled="isSkillToggling(previewSkill.slug)"
              :loading="isSkillToggling(previewSkill.slug)"
              size="small"
              @change="handlePreviewToggle"
            />
          </div>
        </div>

        <div class="skill-preview-body">
          <div v-if="skillPreviewLoading" class="skill-preview-loading">
            <a-spin />
          </div>
          <MarkdownPreview
            v-else-if="skillPreviewMarkdown"
            :content="skillPreviewMarkdown"
            :compact="true"
          />
          <a-empty v-else :description="skillPreviewError || '未读取到 SKILL.md'" />
        </div>

        <div class="skill-preview-footer">
          <div class="skill-preview-footer-left">
            <a-button
              v-if="canDeletePreviewSkill"
              danger
              :loading="deletingPreviewSkill"
              @click="confirmDeletePreviewSkill"
            >
              卸载
            </a-button>
          </div>
          <div class="skill-preview-footer-right">
            <a-button @click="closeSkillPreview">关闭</a-button>
            <a-button
              v-if="previewSkill.sourceScope !== 'personal'"
              type="primary"
              class="lucide-icon-btn"
              @click="goToPreviewSkillManagement"
            >
              <span>{{ canManageSkill(previewSkill) ? '去管理' : '查看详情' }}</span>
            </a-button>
          </div>
        </div>
      </div>
    </a-modal>

    <SkillInstallFlowModal
      :open="installFlowOpen"
      :flow="installFlow"
      @close="closeInstallFlow"
      @completed="handleInstallFlowCompleted"
      @skills-changed="handleSkillsChanged"
      @preview-skill="handleSuiteSkillPreview"
    >
      <template #selection>
        <div class="remote-install-panel">
          <a-tabs v-model:activeKey="activeTab" class="install-tabs">
            <!-- Tab 1: 按仓库拉取 -->
            <a-tab-pane key="repo" tab="按仓库拉取">
              <div class="tab-content-wrapper">
                <a-form layout="vertical" class="remote-install-form">
                  <div class="repo-input-row">
                    <div class="repo-input-field">
                      <a-input
                        v-model:value="remoteInstallForm.source"
                        placeholder="来源仓库或合集地址，如 https://modelscope.cn/collections/MiniMax/MiniMax-Office-skills"
                      >
                        <template #suffix>
                          <a-dropdown
                            :trigger="['click']"
                            placement="bottomRight"
                            overlay-class-name="history-dropdown-menu"
                          >
                            <div class="history-trigger-wrapper">
                              <a-tooltip title="历史仓库">
                                <History
                                  :size="14"
                                  class="history-icon-trigger"
                                  :class="{ 'has-history': repoHistory.length > 0 }"
                                />
                              </a-tooltip>
                            </div>
                            <template #overlay>
                              <a-menu @click="handleSelectHistory">
                                <a-menu-item v-if="repoHistory.length === 0" disabled>
                                  <span class="history-empty-text">暂无使用历史</span>
                                </a-menu-item>
                                <template v-else>
                                  <a-menu-item v-for="item in repoHistory" :key="item">
                                    <div class="history-item-menu-row">
                                      <span class="history-item-text" :title="item">{{
                                        item
                                      }}</span>
                                      <span
                                        class="history-item-del-btn"
                                        @click.stop="deleteHistoryItem(item)"
                                      >
                                        <Trash2 :size="12" />
                                      </span>
                                    </div>
                                  </a-menu-item>
                                  <a-menu-divider />
                                  <a-menu-item
                                    key="clear-all-history"
                                    class="clear-history-menu-item"
                                  >
                                    <div class="clear-history-btn-content">
                                      <Trash2 :size="12" class="clear-icon" />
                                      <span>清空历史记录</span>
                                    </div>
                                  </a-menu-item>
                                </template>
                              </a-menu>
                            </template>
                          </a-dropdown>
                        </template>
                      </a-input>
                    </div>
                    <a-button
                      type="primary"
                      :loading="listingRemoteSkills"
                      @click="handleListRemoteSkills"
                    >
                      拉取技能
                    </a-button>
                  </div>
                  <div class="repo-hint-text">
                    支持 `owner/repo` 或 GitHub URL。可前往
                    <a href="https://skills.sh/" target="_blank" rel="noopener noreferrer"
                      >skills.sh</a
                    >
                    查询开源 skills。 也支持 ModelScope 单个 Skill
                    地址，每次仅限安装一个：`https://modelscope.cn/skills/&lt;skill-id&gt;`。 Skill
                    ID 可在
                    <a href="https://modelscope.cn/skills" target="_blank" rel="noopener noreferrer"
                      >ModelScope Skill 市场</a
                    >
                    进入详情后从地址栏获取。
                  </div>

                  <!-- 仓库技能多选列表 -->
                  <div v-if="remoteSkillOptions.length" class="skills-list-section">
                    <template v-if="hasSingleRepoSkill">
                      <div class="single-remote-skill-card">
                        <div class="single-remote-skill-name">{{ singleRepoSkill.name }}</div>
                        <div class="single-remote-skill-meta">
                          {{ singleRepoSkill.description || '暂无描述' }}
                        </div>
                      </div>
                    </template>
                    <template v-else>
                      <div class="list-operations-bar">
                        <div class="op-buttons">
                          <a-button size="small" type="link" @click="handleRepoSelectAll"
                            >全选</a-button
                          >
                          <a-button size="small" type="link" @click="handleRepoSelectInvert"
                            >反选</a-button
                          >
                          <a-button size="small" type="link" @click="handleRepoSelectNone"
                            >清空</a-button
                          >
                        </div>
                        <a-input
                          v-model:value="repoFilterKeyword"
                          placeholder="本地过滤检索..."
                          size="small"
                          style="width: 180px"
                          allow-clear
                        />
                      </div>
                      <div class="skills-list-viewport">
                        <div class="remote-skills-list-container">
                          <div
                            v-for="item in filteredRepoSkills"
                            :key="item.name"
                            class="remote-skill-row"
                            :class="{ selected: selectedRepoSkills.includes(item.name) }"
                            role="checkbox"
                            tabindex="0"
                            :aria-checked="selectedRepoSkills.includes(item.name)"
                            @click="toggleRepoSkillFromRow(item.name)"
                            @keydown.enter.prevent="toggleRepoSkillFromRow(item.name)"
                            @keydown.space.prevent="toggleRepoSkillFromRow(item.name)"
                          >
                            <a-checkbox
                              class="remote-row-checkbox"
                              :checked="selectedRepoSkills.includes(item.name)"
                              :tabindex="-1"
                              aria-hidden="true"
                            />
                            <div class="remote-skill-row-content">
                              <span class="skill-item-name">{{ item.name }}</span>
                              <span class="skill-item-desc">{{
                                item.description || '暂无描述'
                              }}</span>
                            </div>
                          </div>
                        </div>
                      </div>
                    </template>
                  </div>
                </a-form>
              </div>
            </a-tab-pane>

            <!-- Tab 2: 全局搜索发现 -->
            <a-tab-pane key="search" tab="全局搜索发现">
              <div class="tab-content-wrapper">
                <a-form layout="vertical" class="remote-install-form">
                  <div class="repo-input-row">
                    <div class="repo-input-field">
                      <a-input
                        v-model:value="searchKeyword"
                        placeholder="输入 web、python 等关键字进行全局查找"
                        @pressEnter="handleSearchRemoteSkills"
                      />
                    </div>
                    <a-button
                      type="primary"
                      :loading="searchingRemoteSkills"
                      @click="handleSearchRemoteSkills"
                    >
                      查找技能
                    </a-button>
                  </div>
                  <div class="repo-hint-text">
                    直接输入关键字检索 skills.sh 上的开源 Skills 并批量拉取安装。
                  </div>

                  <!-- 搜索结果列表 -->
                  <div v-if="searchedSkills.length" class="skills-list-section">
                    <template v-if="hasSingleSearchedSkill">
                      <div class="single-remote-skill-card">
                        <div class="single-remote-skill-header">
                          <div class="single-remote-skill-name">
                            {{ singleSearchedSkill.name }}
                          </div>
                          <a-tag v-if="singleSearchedSkill.installs" class="skill-item-installs">
                            {{ singleSearchedSkill.installs }}
                          </a-tag>
                        </div>
                        <div class="single-remote-skill-meta">
                          {{ singleSearchedSkill.source }}
                        </div>
                      </div>
                    </template>
                    <template v-else>
                      <div class="list-operations-bar">
                        <div class="op-buttons">
                          <a-button size="small" type="link" @click="handleSearchSelectAll"
                            >全选</a-button
                          >
                          <a-button size="small" type="link" @click="handleSearchSelectInvert"
                            >反选</a-button
                          >
                          <a-button size="small" type="link" @click="handleSearchSelectNone"
                            >清空</a-button
                          >
                        </div>
                      </div>
                      <div class="skills-list-viewport">
                        <div class="remote-skills-list-container">
                          <div
                            v-for="item in searchedSkills"
                            :key="`${item.source}:${item.name}`"
                            class="remote-skill-row"
                            :class="{ selected: isSearchSkillSelected(item) }"
                            role="checkbox"
                            tabindex="0"
                            :aria-checked="isSearchSkillSelected(item)"
                            @click="toggleSearchSkillFromRow(item)"
                            @keydown.enter.prevent="toggleSearchSkillFromRow(item)"
                            @keydown.space.prevent="toggleSearchSkillFromRow(item)"
                          >
                            <a-checkbox
                              class="remote-row-checkbox"
                              :checked="isSearchSkillSelected(item)"
                              :tabindex="-1"
                              aria-hidden="true"
                            />
                            <div class="remote-skill-row-content">
                              <span class="skill-item-name">{{ item.name }}</span>
                              <span class="skill-item-desc">{{ item.source }}</span>
                            </div>
                            <span v-if="item.installs" class="skill-install-count">
                              {{ item.installs }}
                            </span>
                          </div>
                        </div>
                      </div>
                    </template>
                  </div>
                </a-form>
              </div>
            </a-tab-pane>
          </a-tabs>
        </div>
      </template>

      <template #selection-footer>
        <span class="modal-footer-summary">{{ remoteSelectionSummary }}</span>
        <div class="modal-footer-buttons">
          <a-button @click="closeInstallFlow">取消</a-button>
          <a-button
            type="primary"
            :disabled="
              activeTab === 'repo'
                ? selectedRepoSkills.length === 0
                : selectedSearchSkills.length === 0
            "
            @click="startInstallRemoteSkills"
          >
            解析并确认
          </a-button>
        </div>
      </template>
    </SkillInstallFlowModal>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import {
  RefreshCw,
  Upload,
  Computer,
  WandSparkles,
  History,
  Trash2,
  Check,
  Plus,
  Minus
} from '@lucide/vue'
import { skillApi } from '@/apis/skill_api'
import ExtensionCardGrid from './ExtensionCardGrid.vue'
import SkillInstallFlowModal from './SkillInstallFlowModal.vue'
import SkillSuiteCard from './SkillSuiteCard.vue'
import InfoCard from '@/components/shared/InfoCard.vue'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { formatExtensionCardTitle } from '@/utils/extensionDisplayName'
import { getShareConfigLabel } from '@/utils/shareConfig'
import { getSkillIcon } from '@/utils/skill_icon_utils'
import { prependSkillMentionToNewChatDraft } from '@/utils/skill_mention_draft'
import { refreshSelectedAgentSkillOptions } from '@/utils/agent_skill_options'
import { resolveAppNavigationPath, useEmbedContext } from '@/composables/useEmbedMode'
import { useAgentStore } from '@/stores/agent'
import { useUserStore } from '@/stores/user'

const RECOMMENDED_SUITES = [
  {
    id: 'minimax-office-skills',
    name: 'MiniMax 办公文档套件',
    provider: 'MiniMax-AI',
    category: 'office',
    description:
      'MiniMax 开源的办公文档 Skills 合集，覆盖 DOCX、PDF、XLSX 与 PPTX 演示文稿的创建与格式化。',
    source: 'https://modelscope.cn/collections/MiniMax/MiniMax-Office-skills',
    skills: [
      {
        slug: 'pptx-generator',
        name: 'pptx-generator',
        description:
          '生成、编辑和阅读 PowerPoint 演示文稿。使用 PptxGenJS 从头开始创建，通过 XML 工作流编辑现有的 PPTX，或使用 markitdown 提取文本。'
      },
      {
        slug: 'minimax-docx',
        name: 'minimax-docx',
        description:
          '使用 OpenXML SDK（.NET）进行专业的 DOCX 文档创建、编辑和格式化，支持模板应用与 XSD 验证门控检查。'
      },
      {
        slug: 'minimax-xlsx',
        name: 'minimax-xlsx',
        description:
          '创建、读取、分析、编辑或验证 Excel/电子表格文件，支持公式重算校验与专业财务格式标准。'
      },
      {
        slug: 'minimax-pdf',
        name: 'minimax-pdf',
        description: '高视觉质量与设计感的 PDF 生成、表单字段填写、样式转换与专业打印级文档排版。'
      }
    ]
  },
  {
    id: 'skill-builder-suite',
    name: 'Skill 能力与进化套件',
    provider: 'Community',
    category: 'devtools',
    description: '用于 Agent 技能发现、创建、评测调优与自主进化的核心工具合集。',
    skills: [
      {
        slug: 'skill-creator',
        name: 'skill-creator',
        source: 'https://modelscope.cn/skills/@anthropics/skill-creator',
        description: '创建新技能、修改与优化现有技能，并通过方差基准分析评测技能表现与调优描述。'
      },
      {
        slug: 'find-skills',
        name: 'find-skills',
        source: 'https://modelscope.cn/skills/@vercel-labs/find-skills',
        description: '协助智能体根据用户需求检索并发现可安装的开源 Agent Skills，动态扩展自身能力。'
      },
      {
        slug: 'self-improving-agent',
        name: 'self-improving-agent',
        source: 'https://github.com/zhaono1/agent-playbook',
        description: '通用自我进化技能，基于多重记忆架构从经验与错误中持续学习并自我迭代。'
      }
    ]
  },
  {
    id: 'anthropic-office-skills',
    name: 'Anthropic 官方文档套件',
    provider: 'Anthropic',
    category: 'office',
    description:
      'Anthropic 官方开源的文档 Skills，覆盖 PPTX 演示文稿、DOCX 文档、XLSX 表格与 PDF 的创建、编辑与分析。',
    source: 'https://github.com/anthropics/skills',
    skills: [
      {
        slug: 'pptx',
        name: 'pptx',
        description: '创建、编辑 PowerPoint 演示文稿，支持按内容规划分页、套用版式与批量生成幻灯片。'
      },
      {
        slug: 'docx',
        name: 'docx',
        description: '创建、编辑与分析 Word 文档，支持样式、目录、批注与修订。'
      },
      {
        slug: 'xlsx',
        name: 'xlsx',
        description: '创建、读取与分析 Excel 工作簿，支持公式、图表与财务格式。'
      },
      {
        slug: 'pdf',
        name: 'pdf',
        description: 'PDF 读取、拆分合并、表单填写、水印与版面分析。'
      }
    ]
  },
  {
    id: 'slide-visual-suite',
    name: '演示文稿与视觉套件',
    provider: 'Community',
    category: 'creation',
    description: '从文案到视觉再到路演：幻灯片内容撰写、版式与配色规范、投资人 Pitch Deck 与数据驱动幻灯片。',
    skills: [
      {
        slug: 'presentation-content',
        name: 'presentation-content',
        source: 'https://modelscope.cn/skills/@aaronvanston/presentation-content',
        description: '把想法写成适合演讲的幻灯片文案：有力标题、简练正文与有冲击力的要点。'
      },
      {
        slug: 'presentation-design',
        name: 'presentation-design',
        source: 'https://modelscope.cn/skills/@aaronvanston/presentation-design',
        description: '演示文稿视觉设计指导：布局模式、字体层级、配色规范与幻灯片构图规则。'
      },
      {
        slug: 'presentation-pitch-deck',
        name: 'presentation-pitch-deck',
        source: 'https://modelscope.cn/skills/@aaronvanston/presentation-pitch-deck',
        description: '按 Sequoia / YC 框架生成可独立阅读的路演稿，结论先行、无需讲者陪同。'
      },
      {
        slug: 'presentation-creator',
        name: 'presentation-creator',
        source: 'https://modelscope.cn/skills/@getsentry/presentation-creator',
        description: '用 React + Vite + Recharts 生成数据驱动的幻灯片应用，带图表与动画，产出单个 HTML 文件。'
      }
    ]
  },
  {
    id: 'translation-writing-suite',
    name: '翻译与写作套件',
    provider: 'Community',
    category: 'creation',
    description: '多模式翻译与本地化：长文分块翻译、术语表一致性、PDF 文档翻译与多语言文档流水线。',
    skills: [
      {
        slug: 'baoyu-translate',
        name: 'baoyu-translate',
        source: 'https://modelscope.cn/skills/@jimliu/baoyu-translate',
        description:
          '多模式翻译（快速 / 分析后翻译 / 精修润色），支持长文分块、术语表一致性与受众风格定制。'
      },
      {
        slug: 'translate-pdf',
        name: 'translate-pdf',
        source: 'https://github.com/wshuyi/translate-pdf-skill',
        description: '整篇 PDF 文档翻译，保留版面结构后再导出目标语言版本。'
      },
      {
        slug: 'translation',
        name: 'translation',
        source: 'https://modelscope.cn/skills/@kostja94/translation',
        description: '建立翻译工作流：术语表、风格指南与多语言内容的质量校对。'
      },
      {
        slug: 'mkdocs-translations',
        name: 'mkdocs-translations',
        source: 'https://modelscope.cn/skills/@github/mkdocs-translations',
        description: '为 mkdocs 文档站点批量生成多语言翻译，适合技术文档国际化。'
      }
    ]
  },
  {
    id: 'de-ai-writing-suite',
    name: '中文去 AI 味套件',
    provider: 'Community',
    category: 'devtools',
    description: '把中文文本从「像模型拼出来的稿子」改成「像母语者真的写出来的文章」：消除翻译腔、空泛结论与机械排版腔。',
    skills: [
      {
        slug: 'humanizer-zh',
        name: 'humanizer-zh',
        source: 'https://github.com/ai-zixun/humanizer-zh',
        description:
          '中文「去 AI 味」改写：识别并修复翻译腔、空泛大词、公式化对照句、口号式结尾、列表堆砌与段落节奏，输出更自然的母语表达。'
      }
    ]
  },
  {
    id: 'research-analysis-suite',
    name: '研究与竞品分析套件',
    provider: 'Community',
    category: 'learning',
    description: '调研与汇报场景：客户与公司背景研究、用户研究结论提炼、人物画像整理与状态报告撰写。',
    skills: [
      {
        slug: 'account-research',
        name: 'account-research',
        source: 'https://modelscope.cn/skills/@anthropics/account-research',
        description: '对目标公司做背景与信号研究，输出账户级情报（需接入 Common Room 数据源）。'
      },
      {
        slug: 'research-synthesis',
        name: 'research-synthesis',
        source: 'https://modelscope.cn/skills/@anthropics/research-synthesis',
        description: '把访谈记录、问卷、可用性测试等原始素材提炼成主题、洞察与优先建议。'
      },
      {
        slug: 'persona-researcher',
        name: 'persona-researcher',
        source: 'https://modelscope.cn/skills/@googleworkspace/persona-researcher',
        description: '组织研究资料：管理参考文献、笔记与协作整理。'
      },
      {
        slug: 'status-report',
        name: 'status-report',
        source: 'https://modelscope.cn/skills/@anthropics/status-report',
        description: '生成含 KPI、风险与行动项的状态报告，用红黄绿总结项目健康度。'
      }
    ]
  },
  {
    id: 'office-automation-suite',
    name: '办公自动化套件',
    provider: 'Community',
    category: 'office',
    description: '把重复的文档与表格工作自动化：Excel 表格智能处理、Word/PDF 程序化生成与多格式互转。',
    skills: [
      {
        slug: 'wps-excel',
        name: 'wps-excel',
        source: 'https://modelscope.cn/skills/@lc2panda/wps-excel',
        description: 'WPS 表格智能助手，用自然语言操控 Excel：公式编写、数据清洗与图表创建。'
      },
      {
        slug: 'excel-automation',
        name: 'excel-automation',
        source: 'https://modelscope.cn/skills/@claude-office-skills/excel-automation',
        description: 'Excel 自动化脚本与批量处理，适合循环改表、跑透视与生成报表。'
      },
      {
        slug: 'docx-manipulation',
        name: 'docx-manipulation',
        source: 'https://modelscope.cn/skills/@claude-office-skills/docx-manipulation',
        description: '用 python-docx 程序化创建、编辑与批量操作 Word 文档。'
      },
      {
        slug: 'pdf-to-docx',
        name: 'pdf-to-docx',
        source: 'https://modelscope.cn/skills/@claude-office-skills/pdf-to-docx',
        description: '用 pdf2docx 把 PDF 转成可编辑的 Word 文档。'
      }
    ]
  }
]

const router = useRouter()
const { isEmbedded } = useEmbedContext()
const userStore = useUserStore()
const agentStore = useAgentStore()
const canUseSkills = computed(() => userStore.hasPermission('skill:use'))
const canManageSkills = computed(() => userStore.hasPermission('skill:manage'))
const canInstallSkills = computed(() => canUseSkills.value || canManageSkills.value)
// iframe 刷新时 OA 授权是异步完成的，权限未就绪前不能判断应访问哪套 Skill 列表接口。
const userPermissionsReady = computed(
  () => Boolean(userStore.userId) && userStore.effectivePermissions.length > 0
)

const loading = ref(false)
const importing = ref(false)
const listingRemoteSkills = ref(false)
const searchQuery = ref('')

const isBatchDeleteMode = ref(false)
const selectedCardSlugs = ref([])
const togglingSkillSlugs = ref([])

const skills = ref([])
const skillPreviewVisible = ref(false)
const previewSkill = ref(null)
const skillPreviewMarkdown = ref('')
const skillPreviewLoading = ref(false)
const skillPreviewError = ref('')
const deletingPreviewSkill = ref(false)
let previewRequestSeq = 0
const installFlowOpen = ref(false)
const installFlow = ref(null)

const activeTab = ref('repo') // 'repo' 或 'search'

const remoteInstallForm = reactive({
  source: 'https://modelscope.cn/collections/MiniMax/MiniMax-Office-skills',
  skills: []
})
const remoteSkillOptions = ref([])
const repoFilterKeyword = ref('')
const selectedRepoSkills = ref([])
const hasSingleRepoSkill = computed(() => remoteSkillOptions.value.length === 1)
const singleRepoSkill = computed(() => remoteSkillOptions.value[0] || null)

const searchKeyword = ref('')
const searchingRemoteSkills = ref(false)
const searchedSkills = ref([])
const selectedSearchSkills = ref([])
const hasSingleSearchedSkill = computed(() => searchedSkills.value.length === 1)
const singleSearchedSkill = computed(() => searchedSkills.value[0] || null)
const remoteSelectionSummary = computed(() => {
  if (activeTab.value === 'repo') {
    if (!remoteSkillOptions.value.length) return '请先拉取仓库中的 Skill'
    return `已选 ${selectedRepoSkills.value.length} / 共发现 ${remoteSkillOptions.value.length} 个 Skill`
  }

  if (!searchedSkills.value.length) return '请输入关键词查找 Skill'
  return `已选 ${selectedSearchSkills.value.length} / 共找到 ${searchedSkills.value.length} 个 Skill`
})

const repoHistory = ref([])

const matchesSearch = (skill) => {
  if (!searchQuery.value) return true
  const q = searchQuery.value.toLowerCase()
  const text = [
    skill.name,
    skill.slug,
    skill.description,
    ...(skill.skills || []).flatMap((item) => [item.name, item.slug, item.description])
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return text.includes(q)
}

const installedSkillCards = computed(() =>
  (skills.value || []).map((skill) => ({
    ...skill,
    sourceType: skill.source_type || 'upload',
    sourceScope: skill.source_scope
  }))
)

const installedPersonalSkillKeys = computed(() => {
  const keys = new Set()
  installedSkillCards.value.forEach((skill) => {
    if (skill.sourceScope !== 'personal') return
    const identifiers = [skill.slug, skill.name]
    identifiers.forEach((value) => {
      if (value) keys.add(String(value).toLowerCase())
    })
  })
  return keys
})

const recommendedSuiteCards = computed(() =>
  RECOMMENDED_SUITES.map((suite) => ({ ...suite, isSuite: true }))
)

// 一级分类来自产品侧《技能广场分类及分类规则》。规则里「无/预留」且当前无任何
// 技能的分类（效率工具、商业运营）不生成页签，等有内容时再补回。
const SKILL_CATEGORIES = [
  { key: 'all', label: '全部' },
  { key: 'office', label: '办公协同' },
  { key: 'learning', label: '知识与学习' },
  { key: 'creation', label: '内容创作' },
  { key: 'analytics', label: '数据分析' },
  { key: 'devtools', label: '开发工具' },
  { key: 'news', label: '信息资讯' }
]

const skillAreaTabs = [
  { key: 'plaza', label: '技能广场' },
  { key: 'mine', label: '我的技能' }
]

const activeSkillArea = ref('plaza')
const activePlazaCategory = ref('all')
const activeMineScope = ref('all')

/**
 * 分类规则文档点名的单技能 → 分类 key。
 *
 * 键为 slug 或展示名的小写形式，两者任一命中即可。规则里点名但项目不存在的
 * Skill（如 Huashu Excel）不建占位条目，直接忽略。
 */
const SKILL_CATEGORY_BY_NAME = {
  'image-gen': 'creation',
  'mysql-reporter': 'analytics',
  'html-preview': 'devtools',
  'knowledge-base': 'devtools',
  'deep-research': 'news'
}

/** 未点名 Skill 的兜底：按规则文档给出的各类判定标准做关键词匹配。 */
const categorizeSkill = (skill) => {
  for (const key of [skill?.slug, skill?.name]) {
    const hit = SKILL_CATEGORY_BY_NAME[String(key || '').trim().toLowerCase()]
    if (hit) return hit
  }
  const text = [skill?.name, skill?.slug, skill?.description].filter(Boolean).join(' ').toLowerCase()
  if (/docx|xlsx|pptx|pdf|公文|报表|办公/.test(text)) return 'office'
  if (/excel|表格|数据库|指标|图表|看板|数据分析|reporter/.test(text)) return 'analytics'
  if (/skill|prompt|代码|调试|评测|预览|preview|知识库|上下文/.test(text)) return 'devtools'
  if (/deep research|联网|检索|抓取|资讯|简报|快讯/.test(text)) return 'news'
  if (/研究|调研|学习|竞品|访谈/.test(text)) return 'learning'
  if (/写作|翻译|文案|译稿|设计|绘图|演示|图像|图片|image|gen/.test(text)) return 'creation'
  // 「效率工具 / 商业运营」两个分类当前无技能、不生成页签，未命中者归入最通用的办公协同，
  // 保证每个技能都落在已存在的分类里，不会出现无归属的悬空项。
  return 'office'
}

const skillCategory = (skill) => skill?.category || categorizeSkill(skill)
// 「技能广场技能」包含内置、共享，以及从技能广场下载到本地的技能。
const isPlazaSkill = (skill) => skill?.sourceScope !== 'personal' || skill?.origin === 'remote'
// 「个人上传技能」只统计用户自己上传的 Skill；没有来源标记的历史技能按自行上传处理。
const isPersonalUploadSkill = (skill) =>
  skill?.sourceScope === 'personal' && skill?.origin !== 'remote'

const filteredInstalledSkills = computed(() => installedSkillCards.value.filter(matchesSearch))
const plazaGroups = computed(() =>
  SKILL_CATEGORIES.map((category) => ({
    key: category.key,
    title: category.label,
    skills: [
      ...recommendedSuiteCards.value,
      // 广场浏览区只列套件与内置/共享 Skill；个人 Skill 一律只出现在「我的技能」。
      ...installedSkillCards.value.filter((skill) => skill?.sourceScope !== 'personal')
    ].filter(
      (skill) => matchesSearch(skill) && (category.key === 'all' || skillCategory(skill) === category.key)
    )
  })).filter((group) => group.skills.length)
)

const mineSkills = computed(() => {
  const installed = filteredInstalledSkills.value.filter((skill) => {
    if (activeMineScope.value === 'personal') return isPersonalUploadSkill(skill)
    if (activeMineScope.value === 'system') return isPlazaSkill(skill)
    return true
  })
  return installed
})

const currentCategoryTabs = computed(() => {
  return SKILL_CATEGORIES.map((category) => {
    const group = plazaGroups.value.find((item) => item.key === category.key)
    return { key: category.key, label: category.label, count: group?.skills.length || 0 }
  })
})
const currentVisibleGroups = computed(() => {
  if (activeSkillArea.value === 'plaza') {
    return plazaGroups.value.filter((group) => group.key === activePlazaCategory.value)
  }
  if (mineSkills.value.length === 0) return []
  const title = mineScopeTabs.find((tab) => tab.key === activeMineScope.value)?.label || '我的技能'
  return [{ key: `mine-${activeMineScope.value}`, title, skills: mineSkills.value }]
})

const mineScopeTabs = [
  { key: 'all', label: '全部技能' },
  { key: 'system', label: '技能广场技能' },
  { key: 'personal', label: '个人上传技能' }
]

const mineScopeTabsWithCount = computed(() =>
  mineScopeTabs.map((tab) => ({
    ...tab,
    count:
      tab.key === 'all'
        ? filteredInstalledSkills.value.length
        : filteredInstalledSkills.value.filter((skill) =>
            tab.key === 'personal' ? isPersonalUploadSkill(skill) : isPlazaSkill(skill)
          ).length
  }))
)

const filteredDeletableSkills = computed(() =>
  filteredInstalledSkills.value.filter(
    (skill) =>
      canManageSkill(skill) && skill.sourceType !== 'builtin' && skill.sourceScope !== 'personal'
  )
)
const canDeletePreviewSkill = computed(
  () =>
    !!previewSkill.value &&
    canManageSkill(previewSkill.value) &&
    previewSkill.value.sourceType !== 'builtin'
)

// 技能广场的单技能、我的技能、套件弹窗都复用同一个预览面板：
// 只有启用状态的技能才能跳到对话里 @ 使用（停用的技能 @ 了也不会生效）。
// 按钮始终展示，未启用时置灰不可点，避免「按了没反应」的歧义。
const canUsePreviewSkill = computed(
  () => !!previewSkill.value && previewSkill.value.enabled !== false
)
const useSkillDisabledHint = computed(() =>
  previewSkill.value && !canUsePreviewSkill.value ? 'Skill 已禁用，启用后才能使用' : ''
)

// 仓库拉取的技能列表过滤
const filteredRepoSkills = computed(() => {
  if (!repoFilterKeyword.value.trim()) return remoteSkillOptions.value
  const kw = repoFilterKeyword.value.trim().toLowerCase()
  return remoteSkillOptions.value.filter(
    (item) =>
      item.name.toLowerCase().includes(kw) ||
      (item.description && item.description.toLowerCase().includes(kw))
  )
})

// 批量选择/反选/清空管理
const handleRepoSelectAll = () => {
  selectedRepoSkills.value = filteredRepoSkills.value.map((item) => item.name)
}
const handleRepoSelectNone = () => {
  selectedRepoSkills.value = []
}
const handleRepoSelectInvert = () => {
  const currentSelected = new Set(selectedRepoSkills.value)
  const newSelected = []
  filteredRepoSkills.value.forEach((item) => {
    if (!currentSelected.has(item.name)) {
      newSelected.push(item.name)
    }
  })
  selectedRepoSkills.value = newSelected
}

const handleSearchSelectAll = () => {
  selectedSearchSkills.value = [...searchedSkills.value]
}
const handleSearchSelectNone = () => {
  selectedSearchSkills.value = []
}
const handleSearchSelectInvert = () => {
  const newSelected = []
  searchedSkills.value.forEach((item) => {
    const isSelected = selectedSearchSkills.value.some(
      (s) => s.name === item.name && s.source === item.source
    )
    if (!isSelected) {
      newSelected.push(item)
    }
  })
  selectedSearchSkills.value = newSelected
}

const handleToggleRepoSkill = (name, checked) => {
  if (checked) {
    if (!selectedRepoSkills.value.includes(name)) {
      selectedRepoSkills.value.push(name)
    }
  } else {
    selectedRepoSkills.value = selectedRepoSkills.value.filter((n) => n !== name)
  }
}

const toggleRepoSkillFromRow = (name) => {
  handleToggleRepoSkill(name, !selectedRepoSkills.value.includes(name))
}

const isSearchSkillSelected = (item) =>
  selectedSearchSkills.value.some(
    (skill) => skill.name === item.name && skill.source === item.source
  )

const handleToggleSearchSkill = (item, checked) => {
  if (checked) {
    const isExist = selectedSearchSkills.value.some(
      (s) => s.name === item.name && s.source === item.source
    )
    if (!isExist) {
      selectedSearchSkills.value.push(item)
    }
  } else {
    selectedSearchSkills.value = selectedSearchSkills.value.filter(
      (s) => !(s.name === item.name && s.source === item.source)
    )
  }
}

const toggleSearchSkillFromRow = (item) => {
  handleToggleSearchSkill(item, !isSearchSkillSelected(item))
}

const sourceTypeLabel = (sourceType) => {
  if (sourceType === 'personal') return '个人上传技能'
  if (sourceType === 'builtin') return '内置'
  if (sourceType === 'remote') return '远程'
  return '上传'
}

/** 预览弹窗的来源文案：个人 Skill 需按自行上传 / 广场下载区分。 */
const skillOriginLabel = (skill) => {
  if (skill?.sourceScope === 'personal') {
    return isPersonalUploadSkill(skill) ? '个人上传技能' : '从技能广场安装'
  }
  return `${sourceTypeLabel(skill?.sourceType || skill?.source_type)} Skill`
}

/** 返回 Skill 共享范围的简短展示文案。 */
const getSkillShareLabel = (skill) => getShareConfigLabel(skill?.share_config)

const skillCardTags = (skill) => {
  if (skill.sourceScope === 'personal') {
    return [
      { name: isPersonalUploadSkill(skill) ? '个人上传技能' : '技能广场', color: 'gray' },
      ...(skill.overrides_shared ? [{ name: '覆盖共享版本', color: 'orange' }] : [])
    ]
  }
  return [
    { name: getSkillShareLabel(skill), color: 'gray' },
    ...(skill.shadowed_by_personal ? [{ name: '已被个人版本覆盖', color: 'orange' }] : [])
  ]
}

const canManageSkill = (skill) => {
  if (skill?.sourceScope === 'personal') return canUseSkills.value && skill?.can_manage !== false
  return canManageSkills.value && skill?.can_manage !== false
}
const isSkillToggling = (slug) => togglingSkillSlugs.value.includes(slug)
const navigateToDetail = (skill) => {
  if (skill?.sourceScope === 'personal') return
  router.push({ path: `/extensions/skill/${encodeURIComponent(skill.slug)}` })
}

const closeSkillPreview = () => {
  skillPreviewVisible.value = false
}

const openSkillPreview = async (skill) => {
  if (!skill?.slug) return
  const requestSeq = ++previewRequestSeq
  previewSkill.value = skill
  skillPreviewMarkdown.value = ''
  skillPreviewError.value = ''
  skillPreviewLoading.value = true
  skillPreviewVisible.value = true
  try {
    const result =
      skill.sourceScope === 'personal'
        ? await skillApi.getPersonalSkillFile(skill.slug, 'SKILL.md')
        : await skillApi.getSkillFile(skill.slug, 'SKILL.md')
    if (requestSeq !== previewRequestSeq || previewSkill.value?.slug !== skill.slug) return
    skillPreviewMarkdown.value = result?.data?.content || ''
  } catch (error) {
    if (requestSeq !== previewRequestSeq || previewSkill.value?.slug !== skill.slug) return
    skillPreviewError.value = error?.response?.data?.detail || error.message || '读取 SKILL.md 失败'
  } finally {
    if (requestSeq === previewRequestSeq) skillPreviewLoading.value = false
  }
}

const goToPreviewSkillManagement = () => {
  if (!previewSkill.value) return
  navigateToDetail(previewSkill.value)
  closeSkillPreview()
}

/** 立即使用：把技能提及写入新建对话草稿后跳到对话页。 */
const usePreviewSkillInChat = () => {
  // 兜底拦截：按钮已置灰，这里再挡一层，避免其它入口绕过 disabled 直接跳转
  if (!canUsePreviewSkill.value) return
  const skill = previewSkill.value
  if (!skill?.slug) return
  prependSkillMentionToNewChatDraft(skill.slug)
  closeSkillPreview()
  router.push(resolveAppNavigationPath(isEmbedded.value, '/agent'))
}

const handleCardClick = (skill) => {
  if (isBatchDeleteMode.value) {
    handleToggleCardSelect(skill.slug)
  } else {
    openSkillPreview(skill)
  }
}

const handleToggleCardSelect = (slug) => {
  const target = installedSkillCards.value.find(
    (skill) => skill.slug === slug && skill.sourceScope !== 'personal'
  )
  if (!canManageSkill(target) || target?.sourceType === 'builtin') return
  const idx = selectedCardSlugs.value.indexOf(slug)
  if (idx > -1) {
    selectedCardSlugs.value.splice(idx, 1)
  } else {
    selectedCardSlugs.value.push(slug)
  }
}

const handleToggleSkillEnabled = async (skill) => {
  if (!skill || !canManageSkill(skill) || isSkillToggling(skill.slug)) return
  const enabled = skill.enabled === false
  const isPersonal = skill.sourceScope === 'personal'
  togglingSkillSlugs.value.push(skill.slug)
  try {
    const result = isPersonal
      ? await skillApi.updatePersonalSkillEnabled(skill.slug, enabled)
      : await skillApi.updateSkillEnabled(skill.slug, enabled)
    const updatedSkill = result?.data
    // 个人与共享 Skill 允许同名，必须按作用域定位卡片，避免改到另一个版本。
    const index = skills.value.findIndex(
      (item) => item.slug === skill.slug && (item.source_scope === 'personal') === isPersonal
    )
    if (updatedSkill && index > -1) {
      skills.value[index] = updatedSkill
    } else {
      await fetchSkills()
    }
    const isSamePreview =
      previewSkill.value?.slug === skill.slug &&
      previewSkill.value?.sourceScope === skill.sourceScope
    if (isSamePreview) {
      previewSkill.value = updatedSkill
        ? { ...updatedSkill, sourceType: updatedSkill.source_type || 'upload' }
        : { ...previewSkill.value, enabled }
    }
    message.success(`Skill 已${enabled ? '启用' : '禁用'}`)
    // 启停后必须刷新当前 Agent 的技能候选，否则对话页仍能 @ 到刚禁用的技能
    await refreshSelectedAgentSkillOptions(agentStore)
  } catch (error) {
    message.error(error?.response?.data?.detail || error.message || '更新 Skill 启用状态失败')
  } finally {
    togglingSkillSlugs.value = togglingSkillSlugs.value.filter((slug) => slug !== skill.slug)
  }
}

const handlePreviewToggle = () => {
  if (!previewSkill.value) return
  handleToggleSkillEnabled(previewSkill.value)
}

const confirmDeletePreviewSkill = () => {
  const target = previewSkill.value
  if (!target || !canDeletePreviewSkill.value || deletingPreviewSkill.value) return

  Modal.confirm({
    title: `卸载 ${target.name || target.slug}`,
    content:
      target.sourceScope === 'personal'
        ? '卸载后会删除个人 Skill；如有同名共享版本，Agent 将恢复使用共享版本。'
        : '卸载后会删除该 Skill 的数据库记录和本地文件，操作不可恢复。',
    okText: '卸载',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      deletingPreviewSkill.value = true
      try {
        if (target.sourceScope === 'personal') {
          await skillApi.deletePersonalSkill(target.slug)
        } else {
          await skillApi.deleteSkill(target.slug)
        }
        message.success('Skill 已卸载')
        closeSkillPreview()
        previewSkill.value = null
        await fetchSkills()
        // 卸载后同样要让对话页的 @技能 候选失效
        await refreshSelectedAgentSkillOptions(agentStore)
      } catch (error) {
        message.error(error?.response?.data?.detail || error.message || '卸载 Skill 失败')
      } finally {
        deletingPreviewSkill.value = false
      }
    }
  })
}

const handleBatchSelectAll = () => {
  selectedCardSlugs.value = filteredDeletableSkills.value.map((skill) => skill.slug)
}

const handleBatchSelectNone = () => {
  selectedCardSlugs.value = []
}

const handleBatchSelectInvert = () => {
  const currentSet = new Set(selectedCardSlugs.value)
  selectedCardSlugs.value = filteredDeletableSkills.value
    .filter((skill) => !currentSet.has(skill.slug))
    .map((skill) => skill.slug)
}

const exitBatchDeleteMode = () => {
  isBatchDeleteMode.value = false
  selectedCardSlugs.value = []
}

const handleBatchDelete = () => {
  const deletableSlugs = selectedCardSlugs.value.filter((slug) => {
    const target = installedSkillCards.value.find(
      (skill) => skill.slug === slug && skill.sourceScope !== 'personal'
    )
    return (
      canManageSkill(target) &&
      target?.sourceType !== 'builtin' &&
      target?.sourceScope !== 'personal'
    )
  })
  if (deletableSlugs.length === 0) return

  Modal.confirm({
    title: '确定要批量删除选中的技能吗？',
    content: `您已选中了 ${deletableSlugs.length} 个技能。该操作将从数据库和物理磁盘中彻底删除这些技能包，且不可恢复！`,
    okText: '确定删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      loading.value = true
      try {
        const res = await skillApi.deleteSkillsBatch(deletableSlugs)
        const results = res?.data || []
        const successList = results.filter((r) => r.success)
        const failList = results.filter((r) => !r.success)

        if (failList.length === 0) {
          message.success(`批量删除成功，已删除 ${successList.length} 个技能`)
        } else {
          message.warning(`批量删除完成：成功 ${successList.length} 个，失败 ${failList.length} 个`)
        }

        exitBatchDeleteMode()
        await fetchSkills()
      } catch (error) {
        message.error(error?.response?.data?.detail || error.message || '批量删除失败')
      } finally {
        loading.value = false
      }
    }
  })
}

const fetchSkills = async ({ refreshPersonal = false } = {}) => {
  if (!userPermissionsReady.value) {
    console.debug('[Skill] 用户权限尚未就绪，暂不加载 Skill 列表')
    return
  }

  loading.value = true
  try {
    const usePersonalSkillList = canUseSkills.value
    console.debug('[Skill] 加载 Skill 列表', {
      usePersonalSkillList,
      refreshPersonal
    })
    const skillResult = canUseSkills.value
      ? await skillApi.listSkillCards({ refreshPersonal })
      : await skillApi.listSkills()
    skills.value = skillResult?.data || []
  } catch {
    message.error('加载失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => [userStore.userId, userStore.effectivePermissions.join('|')],
  ([userId, permissionSnapshot]) => {
    if (!userId || !permissionSnapshot) return

    // 权限从 OA 授权流程写入后，强制重新扫描个人 Skill，避免首次请求使用旧列表。
    console.debug('[Skill] 用户权限已就绪，刷新个人 Skill 列表')
    void fetchSkills({ refreshPersonal: true })
  }
)

const beforeSkillUpload = (file) => {
  const lower = file.name.toLowerCase()
  if (!lower.endsWith('.zip') && lower !== 'skill.md') {
    message.error('仅支持上传 .zip 文件或 SKILL.md 文件')
    return false
  }
  return true
}

const openInstallFlow = (flow) => {
  installFlow.value = flow
  installFlowOpen.value = true
}

const resetRemoteSelection = () => {
  selectedRepoSkills.value = []
  selectedSearchSkills.value = []
  remoteSkillOptions.value = []
  searchedSkills.value = []
  repoFilterKeyword.value = ''
  searchKeyword.value = ''
}

const closeInstallFlow = () => {
  const wasRemoteFlow = installFlow.value?.kind === 'remote'
  installFlowOpen.value = false
  installFlow.value = null
  if (wasRemoteFlow) resetRemoteSelection()
}

const openRecommendedSuite = (suite) => {
  openInstallFlow({
    kind: 'suite',
    suite,
    installedSlugs: [...installedPersonalSkillKeys.value],
    installedSkills: installedSkillCards.value
  })
}

/** 套件内切换启用状态后，同步技能广场与我的技能列表。 */
const handleSkillsChanged = () => {
  void fetchSkills()
  // 套件内启停同样影响对话页的 @技能 候选
  void refreshSelectedAgentSkillOptions(agentStore)
}

/** 套件安装弹窗里点击已安装技能：打开技能详情预览。 */
const handleSuiteSkillPreview = (skill) => {
  if (!skill?.slug) return
  void openSkillPreview(skill)
}

const handleInstallFlowCompleted = async ({ success, failed }) => {
  if (failed === 0) message.success(`已添加 ${success} 个 Skill`)
  else message.warning(`安装完成：成功 ${success} 个，失败 ${failed} 个`)
  await fetchSkills({ refreshPersonal: true })

  // 安装完成后，强制刷新当前 Agent 详情，更新对话页 Skill 提及选项。
  if (success > 0) {
    await refreshSelectedAgentSkillOptions(agentStore)
  }
}

const handleImportUpload = async ({ file, onSuccess, onError }) => {
  importing.value = true
  try {
    const result = await skillApi.prepareSkillUpload(file)
    openInstallFlow({
      kind: 'draft',
      title: `安装 ${file.name}`,
      description: '检查 Skill 依赖与生效范围，然后完成安装。',
      drafts: [result?.data]
    })
    onSuccess?.(result)
  } catch (e) {
    message.error(e?.response?.data?.detail || e.message || '解析 Skill 失败')
    onError?.(e)
  } finally {
    importing.value = false
  }
}

const handleOpenRemoteInstall = () => {
  resetRemoteSelection()
  openInstallFlow({
    kind: 'remote',
    title: '远程安装 Skill',
    description: '按仓库拉取或全局搜索，选择需要安装的 Skill。'
  })
}

const rememberRemoteSource = (source) => {
  let history = [...repoHistory.value]
  history = history.filter((item) => item !== source)
  history.unshift(source)
  if (history.length > 10) {
    history = history.slice(0, 10)
  }
  repoHistory.value = history
  localStorage.setItem('yuxi_remote_repo_history', JSON.stringify(history))
}

const handleListRemoteSkills = async () => {
  const source = remoteInstallForm.source.trim()
  if (!source) {
    message.warning('请输入来源仓库')
    return
  }
  listingRemoteSkills.value = true
  try {
    const result = await skillApi.listRemoteSkills(source)
    remoteSkillOptions.value = result?.data || []
    selectedRepoSkills.value =
      remoteSkillOptions.value.length === 1 ? [remoteSkillOptions.value[0].name] : []
    if (!remoteSkillOptions.value.length) {
      message.warning('未发现可安装的 Skills')
      return
    }
    if (remoteSkillOptions.value.length === 1) {
      message.success('已发现 1 个 Skill，已自动选中')
    } else {
      message.success(`已发现 ${remoteSkillOptions.value.length} 个 Skills`)
    }

    rememberRemoteSource(source)
  } catch (error) {
    message.error(error?.response?.data?.detail || error.message || '获取远程 Skills 失败')
  } finally {
    listingRemoteSkills.value = false
  }
}

const loadHistory = () => {
  try {
    const raw = localStorage.getItem('yuxi_remote_repo_history')
    if (raw) {
      repoHistory.value = JSON.parse(raw)
    }
  } catch (e) {
    console.error('Failed to load repo history', e)
  }
}

const deleteHistoryItem = (item) => {
  repoHistory.value = repoHistory.value.filter((h) => h !== item)
  localStorage.setItem('yuxi_remote_repo_history', JSON.stringify(repoHistory.value))
}

const clearAllHistory = () => {
  repoHistory.value = []
  localStorage.removeItem('yuxi_remote_repo_history')
  message.success('历史记录已清空')
}

const handleSelectHistory = ({ key }) => {
  if (key === 'clear-all-history') {
    clearAllHistory()
    return
  }
  remoteInstallForm.source = key
}

const handleSearchRemoteSkills = async () => {
  const query = searchKeyword.value.trim()
  if (!query) {
    message.warning('请输入搜索关键字')
    return
  }
  searchingRemoteSkills.value = true
  try {
    const result = await skillApi.searchRemoteSkills(query)
    searchedSkills.value = result?.data || []
    selectedSearchSkills.value = searchedSkills.value.length === 1 ? [...searchedSkills.value] : []
    if (!searchedSkills.value.length) {
      message.warning('未搜索到相关的 Skills')
    } else if (searchedSkills.value.length === 1) {
      message.success('搜索到 1 个 Skill，已自动选中')
    } else {
      message.success(`搜索到 ${searchedSkills.value.length} 个 Skills`)
    }
  } catch (error) {
    message.error(error?.response?.data?.detail || error.message || '搜索远程 Skills 失败')
  } finally {
    searchingRemoteSkills.value = false
  }
}

const startInstallRemoteSkills = () => {
  const requests = []
  if (activeTab.value === 'repo') {
    requests.push({
      source: remoteInstallForm.source.trim(),
      skills: [...selectedRepoSkills.value],
      skillDetails: remoteSkillOptions.value
        .filter((item) => selectedRepoSkills.value.includes(item.name))
        .map((item) => ({ ...item, slug: item.name }))
    })
  } else {
    const groups = new Map()
    selectedSearchSkills.value.forEach((item) => {
      if (!groups.has(item.source)) groups.set(item.source, [])
      groups.get(item.source).push(item)
    })
    groups.forEach((items, source) => {
      requests.push({
        source,
        skills: items.map((item) => item.name),
        skillDetails: items.map((item) => ({ ...item, slug: item.name }))
      })
    })
  }

  openInstallFlow({
    kind: 'remote',
    title: '远程安装 Skill',
    description: `${requests.length} 个来源 · ${requests.reduce((total, item) => total + item.skills.length, 0)} 个 Skill`,
    requests
  })
}

watch(activeTab, () => {
  selectedRepoSkills.value = []
  selectedSearchSkills.value = []
})

onMounted(() => {
  fetchSkills()
  loadHistory()
})

defineExpose({
  fetchSkills,
  handleImportUpload,
  openRemoteInstallModal: handleOpenRemoteInstall,
  loading
})
</script>

<style lang="less" scoped>
@import '@/assets/css/extensions.less';
</style>

<style lang="less" scoped>
.skill-area-tabs,
.skill-scope-tabs {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  padding: 14px var(--page-padding) 4px;
  overflow-x: auto;
  scrollbar-width: thin;
}

.skill-area-tab,
.skill-scope-tab {
  min-height: 34px;
  padding: 0 16px;
  border: 1px solid transparent;
  border-radius: 17px;
  background: transparent;
  color: var(--gray-500);
  font-size: 14px;
  line-height: 20px;
  cursor: pointer;
  transition:
    color 0.18s ease,
    background-color 0.18s ease,
    border-color 0.18s ease;

  &:hover {
    border-color: var(--gray-150);
    background: var(--gray-25);
    color: var(--gray-800);
  }

  &:focus-visible {
    outline: 2px solid var(--main-color);
    outline-offset: 2px;
  }

  &.active {
    border-color: color-mix(in srgb, var(--main-color) 12%, var(--gray-0));
    background: color-mix(in srgb, var(--main-color) 10%, var(--gray-0));
    color: var(--main-color);
    font-weight: 600;
  }
}

.skill-scope-tabs {
  padding-top: 6px;
  padding-bottom: 0;
}

.skill-category-tabs {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  padding: 14px var(--page-padding) 4px;
  overflow-x: auto;
  scrollbar-width: thin;
}

.skill-category-tab {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
  min-height: 34px;
  padding: 0 15px;
  border: 1px solid transparent;
  border-radius: 17px;
  background: transparent;
  color: var(--gray-500);
  font-size: 14px;
  line-height: 20px;
  cursor: pointer;
  transition:
    color 0.18s ease,
    background-color 0.18s ease,
    border-color 0.18s ease;

  &:hover {
    border-color: var(--gray-150);
    background: var(--gray-25);
    color: var(--gray-800);
  }

  &:focus-visible {
    outline: 2px solid var(--main-color);
    outline-offset: 2px;
  }

  &.active {
    border-color: color-mix(in srgb, var(--main-color) 12%, var(--gray-0));
    background: color-mix(in srgb, var(--main-color) 10%, var(--gray-0));
    color: var(--main-color);
    font-weight: 600;
  }
}

.skill-category-count {
  color: var(--gray-400);
  font-size: 12px;
  font-variant-numeric: tabular-nums;

  .active & {
    color: color-mix(in srgb, var(--main-color) 70%, var(--gray-500));
  }
}

.skill-empty-state {
  width: 100%;
  min-height: 280px;
  padding: 40px var(--page-padding);
}

.skill-empty-card {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 220px;
  flex-direction: column;
  border: 1px dashed var(--gray-150);
  border-radius: 16px;
  background: linear-gradient(180deg, var(--gray-0) 0%, var(--gray-25) 100%);
  color: var(--gray-500);
  text-align: center;
}

.skill-empty-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  margin-bottom: 12px;
  border-radius: 14px;
  background: var(--gray-50);
  color: var(--gray-600);
}

.skill-empty-title {
  color: var(--gray-800);
  font-size: 15px;
  font-weight: 700;
  line-height: 22px;
}

.skill-empty-desc {
  margin-top: 4px;
  color: var(--gray-500);
  font-size: 13px;
  line-height: 20px;
}

.card-wrapper {
  position: relative;

  :deep(.info-card-mini-desc) {
    display: -webkit-box;
    min-height: 36px;
    color: var(--gray-700);
    white-space: normal;
    line-clamp: 2;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }

  :deep(.info-card-mini .info-card-icon) {
    align-self: flex-start;
  }

  &.batch-mode {
    :deep(.info-card) {
      cursor: pointer;
      border-color: var(--gray-200);

      &:hover {
        border-color: var(--gray-300);
      }
    }

    :deep(.info-card-status),
    :deep(.info-card-mini-action) {
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
    }

    :deep(.info-card-mini .info-card-info) {
      padding-right: 28px;
    }
  }

  &.selected {
    :deep(.info-card) {
      border-color: var(--gray-500) !important;
      background: var(--gray-25) !important;
    }
  }

  .card-select-checkbox {
    position: absolute;
    top: 16px;
    right: 16px;
    z-index: 10;
  }
}

.skill-enabled-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  color: var(--gray-600);
  font-size: 18px;
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
  transition:
    border-color 0.18s ease,
    background-color 0.18s ease,
    color 0.18s ease;

  &:hover,
  &:focus {
    outline: none;
    border-color: var(--gray-300);
    background: var(--gray-50);
  }

  &:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  &.loading:disabled {
    cursor: wait;
    opacity: 1;
  }

  &.enabled {
    color: var(--color-success-700);

    .action-icon-minus {
      display: none;
    }

    &:hover,
    &:focus {
      border-color: var(--color-error-200, #ffccc7);
      background: var(--color-error-50, #fff2f0);
      color: var(--color-error-700, #cf1322);

      .action-icon-check {
        display: none;
      }

      .action-icon-minus {
        display: block;
      }
    }
  }
}

.action-icon {
  flex-shrink: 0;
}

.skill-preview-panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.skill-preview-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.skill-preview-title-area {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 10px;
}

.skill-preview-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: 9px;
  background: var(--gray-50);
  color: var(--gray-600);
}

.skill-preview-title-text {
  min-width: 0;
}

.skill-preview-title {
  overflow: hidden;
  color: var(--gray-900);
  font-size: 16px;
  font-weight: 700;
  line-height: 22px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-preview-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
  color: var(--gray-500);
  font-size: 12px;
  line-height: 18px;
}

.skill-preview-disabled-tag {
  display: inline-flex;
  align-items: center;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--gray-100);
  color: var(--gray-600);
  font-size: 11px;
  font-weight: 600;
}

.skill-preview-actions {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  gap: 8px;
  padding-top: 2px;
}

.skill-preview-use-btn {
  height: 28px;
  padding: 0 12px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
}

.skill-preview-body {
  min-height: 260px;
  max-height: min(56vh, 520px);
  padding: 14px 16px;
  overflow-y: auto;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-25);

  :deep(.yk-markdown-preview) {
    background: transparent;
  }
}

.skill-preview-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 220px;
}

.skill-preview-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
}

.skill-preview-footer-left,
.skill-preview-footer-right {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.remote-install-panel {
  :deep(.install-tabs > .ant-tabs-nav .ant-tabs-nav-wrap) {
    justify-content: center;
  }

  .repo-input-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 4px;

    .repo-input-field {
      flex: 1;
      min-width: 0;
    }
  }

  .history-trigger-wrapper {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    cursor: pointer;
    outline: none;
    margin-right: -4px;
    border-radius: 4px;
    transition: background-color 0.2s ease;

    &:hover {
      background-color: var(--gray-100);
    }

    &:focus,
    &:focus-visible {
      outline: none;
    }
  }

  .history-icon-trigger {
    color: var(--gray-400);
    transition: color 0.2s ease;
    outline: none;

    &:hover {
      color: var(--gray-700);
    }

    &.has-history {
      color: var(--gray-500);

      &:hover {
        color: var(--gray-700);
      }
    }
  }

  .repo-hint-text {
    font-size: 12px;
    color: var(--gray-400);
    margin-bottom: 12px;
    line-height: 1.4;

    a {
      color: var(--gray-700);
      text-decoration: underline;
    }
  }

  .tab-content-wrapper {
    padding: 4px 0 8px 0;
  }

  .skills-list-section {
    margin-top: 12px;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    overflow: hidden;
    background: var(--gray-0);
  }

  .list-operations-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--gray-150);
    padding: 8px 10px;

    .op-buttons {
      display: flex;
      gap: 2px;

      .ant-btn {
        padding: 0 4px;
        height: auto;
        font-size: 12px;
        color: var(--gray-600);

        &:hover {
          color: var(--gray-900);
        }
      }
    }
  }

  .skills-list-viewport {
    max-height: min(42vh, 420px);
    overflow-y: auto;
    overscroll-behavior: contain;
    background: var(--gray-0);
  }

  .remote-skills-list-container {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1px;
    background: var(--gray-100);
  }

  .remote-skill-row {
    display: flex;
    align-items: center;
    min-height: 46px;
    padding: 6px 10px;
    gap: 8px;
    background: var(--gray-0);
    cursor: pointer;
    transition: background-color 0.18s ease;

    &:hover,
    &.selected {
      background: var(--gray-25);
    }

    &:focus-visible {
      outline: 2px solid var(--gray-400);
      outline-offset: -2px;
    }

    &[aria-disabled='true'] {
      cursor: not-allowed;
    }
  }

  .remote-row-checkbox {
    pointer-events: none;
  }

  .single-remote-skill-card {
    border: 1px solid var(--gray-150);
    border-radius: 6px;
    background: var(--gray-0);
    padding: 12px;
  }

  .single-remote-skill-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;

    .ant-tag {
      flex-shrink: 0;
      margin-inline-end: 0;
    }
  }

  .single-remote-skill-name {
    line-height: 20px;
    font-weight: 600;
    color: var(--gray-900);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .single-remote-skill-meta {
    margin-top: 2px;
    font-size: 12px;
    line-height: 18px;
    color: var(--gray-500);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .remote-skill-row-content {
    display: flex;
    min-width: 0;
    flex: 1;
    flex-direction: column;

    .skill-item-name {
      font-weight: 600;
      color: var(--gray-900);
    }

    .skill-item-desc {
      display: block;
      font-size: 12px;
      color: var(--gray-500);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
  }

  .skill-install-count {
    flex-shrink: 0;
    color: var(--gray-500);
    font-size: 11px;
  }
}

@media (max-width: 600px) {
  .remote-install-panel {
    .skills-list-viewport {
      max-height: 40vh;
      overflow-y: auto;
    }

    .remote-skills-list-container {
      grid-template-columns: 1fr;
    }
  }
}

.modal-footer-summary {
  color: var(--gray-500);
  font-size: 12px;
  line-height: 18px;
}

.modal-footer-buttons {
  display: flex;
  margin-left: auto;
  gap: 8px;
}
</style>

<!-- NOTE: unscoped style block 用于 dropdown overlay 样式穿透 teleport -->
<style lang="less">
/* Ant Design Dropdown overlay 通过 teleport 挂载到 body，
   scoped CSS 无法穿透，因此必须使用 unscoped 样式。
   使用 .history-dropdown-menu 作为 overlayClassName 命名空间。 */
.history-dropdown-menu {
  min-width: 280px;

  .ant-dropdown-menu {
    padding: 4px;
  }

  .ant-dropdown-menu-item {
    padding: 8px 12px;
    border-radius: 6px;

    .ant-dropdown-menu-title-content {
      display: flex;
      align-items: center;
      width: 100%;
    }
  }
}

/* 历史记录行：仓库地址 + 删除按钮 */
.history-item-menu-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  gap: 12px;

  .history-item-text {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 13px;
    color: var(--gray-800);
    line-height: 1;
  }

  .history-item-del-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    color: var(--gray-400);
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.2s ease;
    flex-shrink: 0;

    svg {
      display: block;
    }

    &:hover {
      color: var(--color-error-500, #ff4d4f);
      background: var(--color-error-10, rgba(255, 77, 79, 0.1));
    }
  }
}

.history-empty-text {
  color: var(--gray-400);
  font-size: 12px;
}

/* 清空历史记录按钮内容 — 图标在左文字在右，水平居中 */
.clear-history-btn-content {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--color-error-500, #ff4d4f);
  font-weight: 500;
  font-size: 13px;
  width: 100%;

  .clear-icon {
    display: flex;
    align-items: center;
  }
}
</style>
