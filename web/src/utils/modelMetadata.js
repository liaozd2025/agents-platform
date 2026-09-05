let catalogPromise

export const USD_TO_CNY_RATE = 7

// 模型配置保存保持逐模型能力；展示目录补充值不写入执行配置。
export const normalizeModelConfig = (model = {}) => ({
  id: model.id || '',
  display_name: model.display_name || model.name || model.id || '',
  type: model.type && model.type !== 'unknown' ? model.type : 'chat',
  source: model.source || 'remote',
  protocol_override: model.protocol_override || null,
  base_url_override: model.base_url_override || null,
  request_body_overrides:
    model.request_body_overrides &&
    typeof model.request_body_overrides === 'object' &&
    !Array.isArray(model.request_body_overrides)
      ? model.request_body_overrides
      : {},
  context_length: model.context_length ?? null,
  max_completion_tokens: model.max_completion_tokens ?? null,
  input_modalities: model.input_modalities || [],
  reasoning: model.reasoning ?? null,
  dimension: model.dimension || null,
  batch_size: model.batch_size || null,
  supported_parameters: model.supported_parameters || [],
  extra: model.extra || {}
})

export const loadModelMetadataCatalog = () => {
  catalogPromise ||= import('@opencode-ai/models/snapshot').then(({ providers }) => ({ providers }))
  return catalogPromise
}

export const getModelMetadata = (providers, providerId, modelId) => {
  return providers?.[providerId]?.models?.[modelId] || null
}

export const resolveModelDisplayMetadata = (providers, providerId, model = {}) => {
  const catalogModel = getModelMetadata(providers, providerId, model.id || model.model_id)
  const returnedInputModalities =
    model.input_modalities ||
    model.architecture?.input_modalities ||
    model.raw_metadata?.architecture?.input_modalities ||
    []
  const inputModalities = returnedInputModalities.length
    ? returnedInputModalities
    : catalogModel?.modalities?.input || []
  const context = model.context_length || catalogModel?.limit?.context || null
  const cost = normalizeRemotePrice(model.pricing) || catalogModel?.cost

  return {
    matched: !!catalogModel,
    inputModalities,
    context,
    contextLabel: formatModelTokenCount(context),
    isOneMillionContext: context >= 1_000_000 && context < 1_500_000,
    vision: inputModalities.includes('image'),
    price: cost
  }
}

export const formatModelTokenCount = (value) => {
  if (!Number.isFinite(value)) return ''
  if (value >= 1_000_000) return `${trimTrailingZeros((value / 1_000_000).toFixed(1))}M`
  if (value >= 1_000) return `${Math.round(value / 1_000)}K`
  return String(value)
}

export const formatModelPrice = (value) => {
  if (!Number.isFinite(value)) return ''
  const precision = value < 0.01 ? 4 : value < 1 ? 3 : 2
  return trimTrailingZeros(value.toFixed(precision))
}

export const formatModelPriceDisplay = (price, currency = 'USD') => {
  if (!Number.isFinite(price?.input) || !Number.isFinite(price?.output)) return ''
  const rate = currency === 'CNY' ? USD_TO_CNY_RATE : 1
  const symbol = currency === 'CNY' ? '¥' : '$'
  return `${symbol}${formatModelPrice(price.input * rate)} / ${symbol}${formatModelPrice(price.output * rate)}`
}

const normalizeRemotePrice = (pricing) => {
  if (!pricing) return null
  const input = Number.parseFloat(pricing.prompt ?? pricing.prompt_price ?? pricing.input)
  const output = Number.parseFloat(pricing.completion ?? pricing.completion_price ?? pricing.output)
  if (!Number.isFinite(input) || !Number.isFinite(output) || input < 0 || output < 0) {
    return null
  }
  return { input: input * 1_000_000, output: output * 1_000_000 }
}

const trimTrailingZeros = (value) => String(value).replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1')
