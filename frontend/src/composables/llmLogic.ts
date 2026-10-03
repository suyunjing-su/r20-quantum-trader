/**
 * LLM 配置页的**纯逻辑**（结构优化阶段 4·B3 第五十七刀）。
 *
 * ## 为什么抽这里而不是拆成多个 composable
 *
 * `useLlmConfig.ts`（686 行）的 docstring 提过"按 provider / model / failover
 * 三域再拆"。但实测后判断**不能那样拆**：那三域的动作函数都直接读写同一批
 * `ref`（`cfg` / `providerForm` / `modelForm` / `selectedProvider` …），
 * 拆成独立 composable 会各建一份新状态 —— `views/admin/llm/injection.ts`
 * 的注释已经解释过这个陷阱（"看起来能跑、实际全错"）。
 *
 * 故本刀只抽**真正无状态**的那部分：输入进、值出，不碰任何 `ref`。
 * 这些函数与原实现逐字等价，只是把"从 ref 取值"的动作留给调用方。
 *
 * ## 抽出来的最大收益：消掉一处**逐字重复**
 *
 * `importRemoteModel` 与 `importAllFilteredRemoteModels` 里各写了一份
 * **完全相同**的模型 payload 字面量（11 个字段的回落链）。
 * 实测两端字节一致 —— 典型的"改一处漏一处"结构。现统一到
 * `buildRemoteModelPayload()`。
 *
 * ## ⚠️ 行为契约（必须与原实现逐字一致）
 *
 * - `providerIdFromName` 的净化是 `[^a-z0-9_-] → '_'`，**先 trim + 转小写**。
 * - `effortOptions` 的 `max` / `xhigh` 用 **`unshift`** 插到**最前面**
 *   （不是追加），顺序即下拉框顺序。
 * - `supportsExtremeEffort` 的命中词是 `gpt-6`/`gpt-5`/`o3`/`o4`/`ultra`/`max`，
 *   全部对**小写**模型 id 做 `includes`。
 * - `buildRemoteModelPayload` 的 `description` 回落文案是
 *   `'从远端一键自动收录'`（**不是**空串），且 `slice(0, 100)`。
 */

/** 推理档位下拉项。`label` 由调用方通过 `labelOf` 注入（保持 i18n 在组件层）。 */
export interface EffortOption {
  value: string
  label: string
}

/** 档位全集，顺序即原实现的数组顺序。 */
const BASE_EFFORTS = ['high', 'medium', 'low', 'minimal', 'none', 'auto'] as const

/** 极致推演档位 —— `unshift` 插到最前。 */
const EXTREME_EFFORTS = ['max', 'xhigh'] as const

/** i18n key 前缀，与原实现一致。 */
const EFFORT_KEY: Record<string, string> = {
  high: 'admin.llm.effortHigh',
  medium: 'admin.llm.effortMedium',
  low: 'admin.llm.effortLow',
  minimal: 'admin.llm.effortMinimal',
  none: 'admin.llm.effortNone',
  auto: 'admin.llm.effortAuto',
  max: 'admin.llm.effortMax',
  xhigh: 'admin.llm.effortXhigh',
}

/**
 * 该模型是否支持极致推演档位（`max` / `xhigh`）。
 *
 * 原注释：支持 GPT-6、GPT-5 以及未来全系前沿具备极值推演能力的旗舰模型。
 */
export function supportsExtremeEffort(modelId: string | null | undefined): boolean {
  const mid = (modelId || '').toLowerCase()
  return (
    mid.includes('gpt-6') ||
    mid.includes('gpt-5') ||
    mid.includes('o3') ||
    mid.includes('o4') ||
    mid.includes('ultra') ||
    mid.includes('max')
  )
}

/**
 * 按模型族给出可选推理档位。
 *
 * ⚠️ 有些档位在后端 `STANDARD_REASONING_EFFORTS` 里存在（`auto`/`minimal`），
 * 下拉框**缺了会让已存这些值的模型无法回显** —— 故全集必须完整。
 *
 * @param modelId 模型 id（会转小写判断）
 * @param labelOf i18n 取值函数（`t`），保持本模块不依赖 vue / i18n
 */
export function effortOptions(
  modelId: string | null | undefined,
  labelOf: (key: string) => string,
): EffortOption[] {
  const options: EffortOption[] = BASE_EFFORTS.map((value) => ({
    value,
    label: labelOf(EFFORT_KEY[value]),
  }))
  if (supportsExtremeEffort(modelId)) {
    options.unshift(
      ...EXTREME_EFFORTS.map((value) => ({ value, label: labelOf(EFFORT_KEY[value]) })),
    )
  }
  return options
}

/** id 为空时由 name 派生：trim → 小写 → 非 `[a-z0-9_-]` 换 `_`。 */
export function providerIdFromName(name: string | null | undefined): string {
  return String(name ?? '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, '_')
}

/**
 * 供应商列表按关键词过滤（name / type / group / id，大小写无关）。
 *
 * ⚠️ `p.name` **不加**存在性守卫 —— 与原实现逐字一致（无 name 的供应商会抛错）。
 * 抽离阶段以等价性优先：加守卫是行为变更，属独立决策。
 * 另：无关键词时返回**原数组引用**（不是拷贝），与原实现一致。
 */
export function filterProviders(providers: any[] | null | undefined, query: string): any[] {
  if (!providers) return []
  const q = (query || '').trim().toLowerCase()
  if (!q) return providers
  return providers.filter(
    (p: any) =>
      p.name.toLowerCase().includes(q) ||
      (p.type && p.type.toLowerCase().includes(q)) ||
      (p.group && p.group.toLowerCase().includes(q)) ||
      (p.id && p.id.toLowerCase().includes(q)),
  )
}

/** 远端模型列表按关键词过滤（id / name，大小写无关）。 */
export function filterRemoteModels(models: any[] | null | undefined, query: string): any[] {
  if (!models) return []
  const q = (query || '').trim().toLowerCase()
  if (!q) return models
  return models.filter(
    (m: any) =>
      m.id.toLowerCase().includes(q) || (m.name && m.name.toLowerCase().includes(q)),
  )
}

/** 备用模型候选 = 全部模型**排除**当前主模型。 */
export function fallbackOptionsFor(cfg: any): any[] {
  return (cfg?.models || []).filter((m: any) => m.id !== cfg?.active_model_id)
}

/** 模型显示名：`名称 · 供应商`（无供应商时只显示名称；查不到时原样返回 id）。 */
export function modelDisplayName(models: any[] | null | undefined, id: string): string {
  const m = (models || []).find((x: any) => x.id === id)
  if (!m) return id
  return `${m.name || m.id}${m.provider_name ? ` · ${m.provider_name}` : ''}`
}

/**
 * 切换数组里某元素的存在性（有则删、无则加）。
 *
 * ⚠️ **原地修改**（与原实现一致：`splice` / `push` 直接作用于 `ref` 里的数组，
 * 故 vue 能追踪到变化）。返回同一个数组引用。
 */
export function toggleInArray(arr: any[], value: any): any[] {
  const idx = arr.indexOf(value)
  if (idx > -1) arr.splice(idx, 1)
  else arr.push(value)
  return arr
}

/** 交换 `idx` 与 `idx + dir` 两个位置（越界则原样返回）。 */
export function moveInArray(arr: any[], idx: number, dir: -1 | 1): any[] {
  const target = idx + dir
  if (target < 0 || target >= arr.length) return arr
  ;[arr[idx], arr[target]] = [arr[target], arr[idx]]
  return arr
}

/** api_format 切换后的目标路径与 `response_api` 开关。 */
export interface ApiFormatEffect {
  apiPath: string
  responseApiEnabled: boolean
}

const DEFAULT_PATH: Record<string, string> = {
  claude_messages: '/messages',
  openai_responses: '/responses',
  openai_chat: '/chat/completions',
}

/**
 * 切换 `api_format` 时该把 `api_path` 改成什么。
 *
 * ⚠️ 原实现的规则：**只有当前路径是"标准三选一之一"或为空时才改** ——
 * 用户手填的自定义路径不得被覆盖。`response_api_enabled` 则无条件跟随
 * （`claude_messages` / `openai_chat` → false，`openai_responses` → true）。
 */
export function apiFormatEffect(format: string, currentPath: string | null | undefined): ApiFormatEffect {
  const known = Object.values(DEFAULT_PATH)
  const path = currentPath || ''
  const isStandardOrEmpty = !path || known.includes(path)
  const target = DEFAULT_PATH[format]
  return {
    apiPath: isStandardOrEmpty && target ? target : path,
    responseApiEnabled: format === 'openai_responses',
  }
}

/**
 * 远端模型 → 收录用的服务端 payload。
 *
 * ⚠️ 这段字面量原先在 `importRemoteModel` 与 `importAllFilteredRemoteModels`
 * 里**各写了一份、逐字相同**（本刀实测确认）。现只此一处。
 */
export function buildRemoteModelPayload(
  m: any,
  provider: any,
  /** i18n 取值函数（`t`）。与 `effortOptions` 的 `labelOf` 同一约定：
   *  本模块不依赖 vue / i18n，「从远端一键自动收录」这句**用户可见文案**
   *  由调用方注入 —— 此前写死中文，英文界面下会直接显示中文。 */
  t: (key: string) => string,
): Record<string, any> {
  return {
    id: m.id,
    name: m.name || m.id,
    provider_id: provider.id,
    provider_name: provider.name,
    base_url: provider.base_url,
    api_format: m.api_format || provider.api_format || 'openai_chat',
    reasoning_type: m.reasoning_type || 'auto',
    reasoning_effort: m.default_effort || 'high',
    capabilities: m.capabilities || ['chat'],
    context_length: m.context_length,
    max_tokens: m.max_tokens,
    description: m.description ? m.description.slice(0, 100) : t('admin.llm.remoteAutoCollected'),
  }
}

/** 激活模型的 payload。 */
export function buildActivatePayload(
  modelId: string,
  providerId: string,
  reasoningEffort: string,
): Record<string, any> {
  return { model_id: modelId, provider_id: providerId, reasoning_effort: reasoningEffort }
}

/** 删除供应商确认框里的级联提示（原实现里 `cascade` 变量的拼法）。 */
export function providerDeleteCascadeHint(
  provider: any,
  t: (key: string, fallback?: string, params?: Record<string, string | number>) => string,
): string {
  const n = provider?.models_count ?? provider?.models?.length ?? 0
  return n > 0 ? t('admin.llm.cascadeModelsDeleted', undefined, { n }) : ''
}
