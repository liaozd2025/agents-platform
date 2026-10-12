<template>
  <a-dropdown
    trigger="click"
    :open="dropdownOpen"
    :disabled="props.disabled"
    @open-change="handleOpenChange"
  >
    <div class="model-select" :class="modelSelectClasses" @click.prevent.stop @mousedown.stop>
      <div class="model-select-content">
        <div class="model-info">
          <span class="model-text text" :title="displayModelTitle">{{ displayModelText }}</span>
          <button
            v-if="props.clearable && props.model_spec && !props.disabled"
            class="model-clear-btn"
            @mousedown.prevent.stop
            @click.stop="handleClear"
            title="清空选择"
          >
            <X :size="14" />
          </button>
        </div>
        <div v-if="resolvedSize !== 'nano'" class="model-status-controls">
          <span
            v-if="state.currentModelStatus"
            class="model-status-indicator"
            :class="state.currentModelStatus.status"
            :title="getCurrentModelStatusTitle()"
          >
            {{ modelStatusIcon }}
          </span>
          <a-button
            :size="buttonSize"
            type="text"
            :loading="state.checkingStatus"
            @click.stop="checkCurrentModelStatus"
            :disabled="props.disabled || state.checkingStatus"
            class="status-check-button"
          >
            {{ state.checkingStatus ? '检查中...' : '检查' }}
          </a-button>
        </div>
      </div>
    </div>
    <template #overlay>
      <div class="model-dropdown" @click.stop>
        <div class="model-search">
          <a-input
            v-model:value="modelSearchKeyword"
            placeholder="搜索模型"
            allow-clear
            autocomplete="off"
            autocapitalize="none"
            autocorrect="off"
            spellcheck="false"
            @keydown.stop
          >
            <template #suffix>
              <button
                :disabled="props.disabled || state.refreshingCache"
                :title="state.refreshingCache ? '刷新中...' : '刷新缓存'"
                class="cache-refresh-button"
                @mousedown.prevent.stop
                @click.stop="refreshCache"
              >
                <RefreshCw :size="13" :class="{ spin: state.refreshingCache }" />
              </button>
            </template>
          </a-input>
        </div>
        <a-menu class="scrollable-menu">
          <a-menu-item v-if="loadingV2Models" key="loading" disabled>加载中...</a-menu-item>
          <a-menu-item v-else-if="!hasFilteredModels" key="empty" disabled
            >暂无匹配模型</a-menu-item
          >
          <template v-else>
            <a-menu-item-group
              v-for="(providerData, providerId) in filteredV2Models"
              :key="`v2-${providerId}`"
            >
              <template #title>
                <span :title="providerId">{{
                  getProviderDisplayName(providerId, providerData)
                }}</span>
              </template>
              <a-menu-item
                v-for="model in providerData.models"
                :key="model.spec"
                @click="handleSelectV2Model(model.spec)"
              >
                <div class="model-option">
                  <span class="model-option-name" :title="model.display_name">{{
                    model.display_name
                  }}</span>
                  <div class="model-option-signals">
                    <a-tooltip v-if="getModelInfo(model).vision" title="支持图像输入">
                      <span class="model-signal-icon" role="img" aria-label="支持图像输入">
                        <Eye :size="13" />
                      </span>
                    </a-tooltip>
                    <a-tooltip
                      v-if="getModelInfo(model).isOneMillionContext"
                      title="约 1M tokens 上下文窗口"
                    >
                      <span class="model-context-badge">1M</span>
                    </a-tooltip>
                  </div>
                </div>
              </a-menu-item>
            </a-menu-item-group>
          </template>
        </a-menu>
        <div
          v-if="
            !modelMetadataNoticeDismissed &&
            (userStore.hasPermission('model_provider:manage') || hasModelMetadata)
          "
          class="model-metadata-source"
        >
          <div class="model-metadata-source-content">
            <template v-if="userStore.hasPermission('model_provider:manage')">
              没有合适的模型？
              <RouterLink :to="{ path: '/agent-manage', query: { tab: 'providers' } }" @click.stop>
                配置模型
              </RouterLink>
            </template>
            <template v-if="hasModelMetadata">
              <span v-if="userStore.hasPermission('model_provider:manage')">。 </span>
              部分信息（价格、能力等）来自
              <a href="https://models.dev" target="_blank" rel="noreferrer" @click.stop
                >models.dev</a
              >
              填补。仅供参考，可能和官网有偏差。
            </template>
          </div>
          <button
            type="button"
            class="model-metadata-source-close"
            title="不再显示"
            aria-label="关闭模型信息提示"
            @click.stop="dismissModelMetadataNotice"
          >
            <X :size="13" />
          </button>
        </div>
      </div>
    </template>
  </a-dropdown>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { modelProviderApi } from '@/apis/system_api'
import { Eye, RefreshCw, X } from '@lucide/vue'
import { useUserStore } from '@/stores/user'
import { loadModelMetadataCatalog, resolveModelDisplayMetadata } from '@/utils/modelMetadata'
import {
  extractModelName,
  isModelSpecAvailable,
  resolveFallbackModelSpec
} from '@/utils/conversationModel'

const props = defineProps({
  model_spec: {
    type: String,
    default: ''
  },
  placeholder: {
    type: String,
    default: '请选择模型'
  },
  size: {
    type: String,
    default: 'small',
    validator: (value) => ['nano', 'small', 'middle', 'large'].includes(value)
  },
  disabled: {
    type: Boolean,
    default: false
  },
  clearable: {
    type: Boolean,
    default: false
  },
  displayName: {
    type: String,
    default: 'full',
    validator: (value) => ['full', 'short', 'mini'].includes(value)
  },
  autoSelectFirst: {
    type: Boolean,
    default: false
  },
  // 当前模型已从供应商下架时，是否自动改选为列表首项。
  // 与 autoSelectFirst 分开控制：autoSelectFirst 表达「没有选择时兜底首项」，
  // 而本项表达「已有选择但该选择已失效时兜底首项」。聊天页两者都需要，
  // 设置页两者都不需要（失效配置必须原样展示，管理员才能看出问题）。
  fallbackWhenUnavailable: {
    type: Boolean,
    default: false
  },
  // 当前选择的会话维度标识（如 thread id）。
  // 切换会话时 model_spec 可能完全相同（两个会话都绑同一个已下架模型），
  // 仅靠 model_spec 无法区分「换了个会话」与「列表重复拉取」，
  // 因此失效回退的幂等去重必须以此为范围，否则第二个失效会话不会回退。
  selectionScope: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['select-model'])
const userStore = useUserStore()
const MODEL_METADATA_NOTICE_DISMISSED_KEY = 'yuxi_model_metadata_notice_dismissed'

// v2 模型数据：每次展开下拉时实时从后端拉取
const v2Models = ref({})
const loadingV2Models = ref(false)
const dropdownOpen = ref(false)
const modelSearchKeyword = ref('')
const modelMetadataBySpec = ref({})
const modelMetadataNoticeDismissed = ref(
  localStorage.getItem(MODEL_METADATA_NOTICE_DISMISSED_KEY) === 'true'
)
// 模型列表是否已成功拉取过至少一次。用于区分「列表还没加载」与「模型确实不在列表里」，
// 前者不能判定为失效，否则首屏竞态会把用户刚选的模型误换成别的模型。
const hasFetchedV2Models = ref(false)
let fetchV2ModelsPromise = null

const dismissModelMetadataNotice = () => {
  modelMetadataNoticeDismissed.value = true
  localStorage.setItem(MODEL_METADATA_NOTICE_DISMISSED_KEY, 'true')
}

const filteredV2Models = computed(() => {
  const keyword = modelSearchKeyword.value.trim().toLowerCase()
  if (!keyword) return v2Models.value

  return Object.entries(v2Models.value).reduce((result, [providerId, providerData]) => {
    const models = (providerData.models || []).filter((model) => {
      return [
        providerId,
        getProviderDisplayName(providerId, providerData),
        model.spec,
        model.model_id,
        model.display_name
      ].some((value) =>
        String(value || '')
          .toLowerCase()
          .includes(keyword)
      )
    })

    if (models.length) {
      result[providerId] = { ...providerData, models }
    }

    return result
  }, {})
})

const hasFilteredModels = computed(() => {
  return Object.values(filteredV2Models.value).some((providerData) => providerData.models?.length)
})

const hasModelMetadata = computed(() => Object.keys(modelMetadataBySpec.value).length > 0)

const getProviderDisplayName = (providerId, providerData = {}) => {
  return (
    providerData.provider_display_name ||
    providerData.display_name ||
    providerData.name ||
    providerId
  )
}

// 拉取 v2 模型列表
const fetchV2Models = async () => {
  if (fetchV2ModelsPromise) return fetchV2ModelsPromise

  loadingV2Models.value = true
  fetchV2ModelsPromise = (async () => {
    try {
      const catalogRequest = loadModelMetadataCatalog().catch((error) => {
        console.warn('Failed to load model metadata catalog:', error)
        return null
      })
      const response = await modelProviderApi.getV2Models('chat')
      if (response.success) {
        v2Models.value = response.data || {}
        const catalog = await catalogRequest
        modelMetadataBySpec.value = catalog
          ? buildModelMetadataBySpec(v2Models.value, catalog.providers)
          : {}
        // 标记列表已就绪：此后才能安全判断当前模型是否已下架
        hasFetchedV2Models.value = true
        // 列表到手后立即做空选择兜底与失效模型回退，避免用户先看到一个已下架的模型名
        applyModelListSideEffects()
      }
    } catch (error) {
      console.warn('Failed to load v2 models:', error)
    } finally {
      loadingV2Models.value = false
      fetchV2ModelsPromise = null
    }
  })()

  return fetchV2ModelsPromise
}

const buildModelMetadataBySpec = (modelsByProvider, providers) => {
  return Object.entries(modelsByProvider).reduce((result, [providerId, providerData]) => {
    for (const model of providerData.models || []) {
      const info = resolveModelDisplayMetadata(providers, providerId, model)
      if (info.matched) result[model.spec] = info
    }
    return result
  }, {})
}

const getModelInfo = (model) => modelMetadataBySpec.value[model.spec] || {}

// 当前选中的模型是否已从供应商侧下架（即不再出现在模型列表接口返回中）。
//
// 这里刻意不用 computed：回退在 fetchV2Models 的成功回调内同步触发，而该回调里
// 刚刚把 hasFetchedV2Models 置为 true。computed 是惰性求值，同一 tick 内读取会拿到
// 依赖尚未失效的旧缓存（isCurrentModelUnavailable 恒为 false），导致回退静默不触发。
// 改成普通函数按当前值现算，读取时机与赋值时机一致。
//
// 列表未加载完成或加载失败时一律视为「尚未确认失效」，避免网络抖动把用户刚选的模型误换掉。
const isCurrentModelUnavailable = () => {
  if (!props.model_spec) return false
  // 只用 hasFetchedV2Models 作为「列表已就绪」的唯一判据，不能再加请求进行中的标记兜底：
  // 判定发生在 fetchV2Models 的成功回调里，而该标记要到 finally 块才复位，
  // 回调执行期间它仍为真，会把本该成立的失效判定挡掉（实测表现为回退静默不触发）。
  // 列表加载失败时 hasFetchedV2Models 不会置位，因此网络抖动不会被误判成模型下架。
  if (!hasFetchedV2Models.value) return false

  return !isModelSpecAvailable(v2Models.value, props.model_spec)
}

// 聊天输入框没有任何默认模型时，使用接口返回的第一个可用模型。
// 该行为由调用方显式开启，避免影响设置页等需要用户手动选择的场景。
// 兜底选择带 autoSelected 标记：调用方据此区分「用户手动选择」与「系统兜底」，
// 只有前者才应写入用户偏好（见 AgentChatComponent.handleModelSelect）。
const selectFirstModelIfNeeded = () => {
  if (!props.autoSelectFirst || props.disabled || props.model_spec) return

  const firstModel = Object.values(v2Models.value)
    .flatMap((providerData) => providerData.models || [])
    .find((model) => model?.spec)
  if (!firstModel?.spec) {
    console.warn('[模型选择] 未找到可用于聊天的模型')
    return
  }

  console.info(`[模型选择] 初始未选择模型，自动选择 ${firstModel.spec}`)
  emit('select-model', firstModel.spec, { autoSelected: true })
}

// 已经为「某个失效 spec」触发过回退的记录：防止列表重复拉取（如刷新缓存、展开下拉）
// 导致对同一个失效模型反复 emit，把用户的选择来回抖动。
//
// 键必须是「会话范围 + 失效 spec」而不是只用 spec：切换会话时组件实例会被复用，
// 若只按 spec 去重，先前会话已回退过 alibaba-cn:qwen3.7-max 后，切到另一个同样绑定
// 该失效模型的会话会被误判为「已处理」而不再回退，界面重新显示已下架的模型名。
const recoveredUnavailableSelections = new Set()

// 选中的模型已被供应商下架时的自动回退。
//
// 背景：模型从供应商 enabled_models 移除后，/models/v2 不再返回它，但本地偏好与会话
// metadata.model_spec 仍指向该 spec，页面会照常显示模型名，直到发送时被后端以 422 拒绝。
// 这里在拿到权威列表后立刻改选可用模型并带 unavailable 标记，让调用方能提示用户。
//
// 仅对显式开启 fallbackWhenUnavailable 的调用方生效（当前只有聊天输入框）：
// 设置页必须原样展示管理员配置的模型，否则失效配置会被静默改写，管理员反而看不出
// 自己配的模型已下架。autoSelected 标记同时避免把兜底结果固化成用户偏好。
const recoverFromUnavailableModel = () => {
  if (!props.fallbackWhenUnavailable || props.disabled) return
  if (!isCurrentModelUnavailable()) return

  const unavailableSpec = props.model_spec
  // 幂等键带上会话范围：同一个失效模型在不同会话上要各回退一次，
  // 只有在同一会话内重复拉列表才跳过。
  const scope = `${props.selectionScope}::${unavailableSpec}`
  if (recoveredUnavailableSelections.has(scope)) return

  const fallbackSpec = resolveFallbackModelSpec(v2Models.value)
  if (!fallbackSpec) {
    // 列表里一个可用模型都没有：保持原状，避免替换成非法值
    console.warn(`[模型选择] 模型 ${unavailableSpec} 已不可用，且没有可用模型可回退`)
    return
  }

  recoveredUnavailableSelections.add(scope)
  console.warn(`[模型选择] 模型 ${unavailableSpec} 已不可用，自动切换到 ${fallbackSpec}`)
  emit('select-model', fallbackSpec, { autoSelected: true, unavailableSpec })
}

// 列表拉取成功后统一处理两件事：空选择兜底首项、失效模型回退。
// 放在 fetchV2Models 的成功回调里，因为这是唯一同时持有「当前 spec」与「权威可用列表」的位置。
const applyModelListSideEffects = () => {
  selectFirstModelIfNeeded()
  recoverFromUnavailableModel()
}

const initializeDefaultModel = async () => {
  // 两个开关任一开启都需要拉取列表：
  // - autoSelectFirst：没有选择时要兜底首项；
  // - fallbackWhenUnavailable：已有选择可能已失效，需要权威列表才能判定。
  // 注意聊天页传的是 auto-select-first="!currentModelSpec"，偏好失效时该值为 false，
  // 若只认 autoSelectFirst 就永远拉不到列表，失效回退形同虚设。
  if (props.disabled) return
  if (!props.autoSelectFirst && !props.fallbackWhenUnavailable) return
  // 副作用（空选择兜底 + 失效模型回退）已在 fetchV2Models 成功回调内统一触发，
  // 此处不重复调用，避免并发去重导致的二次 emit。
  await fetchV2Models()
}

// 下拉展开前先刷新模型列表，避免弹层打开后再因数据加载发生高度跳变。
const handleOpenChange = async (open) => {
  if (props.disabled) {
    dropdownOpen.value = false
    return
  }

  if (!open) {
    dropdownOpen.value = false
    return
  }

  await fetchV2Models()
  if (!props.disabled) {
    dropdownOpen.value = true
  }
}

// 强制刷新缓存
const refreshCache = async () => {
  if (props.disabled || state.refreshingCache) return
  state.refreshingCache = true
  try {
    await modelProviderApi.refreshModelCache()
    // 刷新后重新拉取模型列表
    await fetchV2Models()
  } catch (error) {
    console.error('Failed to refresh cache:', error)
  } finally {
    state.refreshingCache = false
  }
}

const state = reactive({
  currentModelStatus: null,
  checkingStatus: false,
  refreshingCache: false
})

watch(
  () => props.model_spec,
  (spec, previousSpec) => {
    if (spec !== previousSpec) {
      state.currentModelStatus = null
    }
  }
)

// 选择范围（会话）切换时必须重新做一次失效判定。
//
// 切换会话通常不会重新拉取模型列表——fetchV2Models 有并发去重，且列表内容并未变化，
// 因此挂在 fetchV2Models 成功回调里的回退逻辑不会被再次触发。结果是：先打开的会话
// 正常回退，侧栏切到另一个同样绑定已下架模型的会话时却仍显示该模型名。
// 这里在范围变化后直接复用已加载的列表重新判定，无需再次请求。
watch(
  () => props.selectionScope,
  (scope, previousScope) => {
    if (scope === previousScope) return
    if (!hasFetchedV2Models.value) return

    recoverFromUnavailableModel()
  }
)

const resolvedSize = computed(() => props.size || 'small')
const modelSelectClasses = computed(() => ({
  'model-select--nano': resolvedSize.value === 'nano',
  'model-select--middle': resolvedSize.value === 'middle',
  'model-select--large': resolvedSize.value === 'large',
  'model-select--disabled': props.disabled
}))
const buttonSize = computed(() => {
  if (resolvedSize.value === 'large') return 'large'
  if (resolvedSize.value === 'middle') return 'middle'
  return 'small'
})

const displayModelText = computed(() => {
  const spec = props.model_spec
  if (!spec) return props.placeholder

  const modelName = extractModelName(spec)
  if (props.displayName === 'mini') {
    return modelName.includes('/') ? modelName.split('/').pop() : modelName
  }
  if (props.displayName === 'short') return modelName
  return spec
})

const displayModelTitle = computed(() => props.model_spec || props.placeholder)

// 检查当前模型状态
const checkCurrentModelStatus = async () => {
  if (props.disabled) return
  const spec = props.model_spec
  if (!spec) return

  try {
    state.checkingStatus = true
    const response = await modelProviderApi.getModelStatusBySpec(spec)
    if (response.data) {
      state.currentModelStatus = response.data
    } else {
      state.currentModelStatus = null
    }
  } catch (error) {
    console.error(`检查模型 ${spec} 状态失败:`, error)
    state.currentModelStatus = { status: 'error', message: error.message }
  } finally {
    state.checkingStatus = false
  }
}

const modelStatusIcon = computed(() => {
  const status = state.currentModelStatus
  if (!status) return '○'
  if (status.status === 'available') return '✓'
  if (status.status === 'unavailable') return '✗'
  if (status.status === 'error') return '⚠'
  return '○'
})

const getCurrentModelStatusTitle = () => {
  const status = state.currentModelStatus
  if (!status) return '状态未知'

  let statusText = ''
  if (status.status === 'available') statusText = '可用'
  else if (status.status === 'unavailable') statusText = '不可用'
  else if (status.status === 'error') statusText = '错误'

  const message = status.message || '无详细信息'
  return `${statusText}: ${message}`
}

// 选择 v2 模型的方法
const handleSelectV2Model = (spec) => {
  if (props.disabled) return
  emit('select-model', spec)
  dropdownOpen.value = false
}

watch(
  () => [props.autoSelectFirst, props.fallbackWhenUnavailable, props.disabled],
  ([autoSelectFirst, fallbackWhenUnavailable, disabled], previousState) => {
    const [previousAutoSelectFirst, previousFallback, previousDisabled] = previousState || []
    // 任一开关从关变为开时拉一次列表：既覆盖首屏 immediate（previousState 为 undefined），
    // 也覆盖后续切换。偏好失效时 autoSelectFirst 为 false，只有 fallbackWhenUnavailable 能触发。
    const needsList =
      !disabled &&
      (autoSelectFirst || fallbackWhenUnavailable) &&
      (!previousAutoSelectFirst || !previousFallback || previousDisabled)
    if (needsList) {
      void initializeDefaultModel()
    }
  },
  { immediate: true }
)

// 清空选择
const handleClear = () => {
  if (props.disabled) return
  state.currentModelStatus = null
  emit('select-model', '')
  dropdownOpen.value = false
}
</script>

<style lang="less" scoped>
@import '@/assets/css/model-selector-common.less';

// 状态检查按钮
.status-check-button {
  font-size: 12px;
  padding: 0 4px;
}

// 缓存刷新按钮
.cache-refresh-button {
  font-size: 12px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  background-color: transparent;
  border: none;
  outline: none;
  color: var(--gray-400);
  cursor: pointer;
  transition: color 0.15s ease;

  &:hover:not(:disabled) {
    color: var(--gray-600);
  }
}

.model-select--nano {
  max-width: 100%;
  height: 30px;
  padding: 6px 8px;
  border: none;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1;
  color: var(--gray-600);
  background: transparent;
  transition: all 0.2s ease;
  user-select: none;
}

.model-select--nano:hover {
  color: var(--gray-900);
  background: var(--gray-50);
}

.model-select--nano .model-text {
  color: currentColor;
}

.model-clear-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  margin-left: 2px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--gray-400);
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover {
    color: var(--gray-700);
    background: var(--gray-100);
  }
}

.model-select--disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.model-dropdown {
  width: min(300px, calc(100vw - 24px));
  padding: 8px 0;
  overflow: hidden;
  background: var(--gray-0);
  border-radius: 8px;
  box-shadow: 0 0 18px rgba(0, 0, 0, 0.1);
}

.model-search {
  padding: 8px;
}

.model-search :deep(.ant-input-affix-wrapper) {
  border-color: var(--gray-0);
  background: var(--gray-25);
}

.model-search :deep(.ant-input) {
  background: transparent;
}

:deep(.ant-dropdown-menu) {
  &.scrollable-menu {
    max-height: 260px;
    overflow-y: auto;
    box-shadow: none;
  }

  .ant-dropdown-menu-item-group-list {
    margin: 0;
  }

  .ant-dropdown-menu-item,
  .ant-dropdown-menu-submenu-title {
    border-radius: 6px;
  }

  .ant-dropdown-menu-item:hover,
  .ant-dropdown-menu-submenu-title:hover,
  .ant-dropdown-menu-item-active,
  .ant-dropdown-menu-submenu-title-active {
    background: var(--gray-50);
  }

  .ant-dropdown-menu-item-selected,
  .ant-dropdown-menu-item-selected:hover,
  .ant-dropdown-menu-item-selected.ant-dropdown-menu-item-active {
    color: var(--gray-1000);
    background: var(--main-50);
  }
}

.model-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.model-option-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--gray-1000);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-option-signals {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  color: var(--gray-500);
}

.model-context-badge {
  display: inline-flex;
  align-items: center;
  height: 17px;
  padding: 0 5px;
  border-radius: 4px;
  color: var(--gray-600);
  background: var(--gray-100);
  font-size: 10px;
  font-weight: 600;
}

.model-signal-icon {
  display: inline-flex;
  align-items: center;
}

.model-metadata-source {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 7px 10px;
  border-top: 1px solid var(--gray-100);
  color: var(--gray-500);
  background: var(--gray-25);
  font-size: 11px;
  line-height: 16px;

  a {
    color: var(--main-600);
  }
}

.model-metadata-source-content {
  flex: 1;
  min-width: 0;
}

.model-metadata-source-close {
  appearance: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  margin: -2px -3px 0 0;
  padding: 0;
  border: none;
  border-radius: 4px;
  color: var(--gray-500);
  background: transparent;
  cursor: pointer;
  transition:
    color 0.15s ease,
    background-color 0.15s ease;

  &:hover {
    color: var(--gray-800);
    background: var(--gray-100);
  }

  &:focus-visible {
    outline: 2px solid var(--main-400);
    outline-offset: 1px;
  }
}
</style>
