/**
 * 安全页（`SecurityPage.vue`）的显示派生逻辑（结构优化阶段 4·B3 第三十四刀）。
 *
 * 原样搬自 `frontend/src/views/admin/SecurityPage.vue` 的 script setup：
 * `okxLinked` / `mxHealthChips` / `gateExecDirty` / `venueStatus` / `envTextOf` /
 * `envBadge` —— 六段**纯派生**（给定输入与 i18n 函数就有确定输出），
 * 与 Vue 响应式、网络请求、组件状态都无关，因此可以脱离组件单测。
 *
 * ## 与组件内的版本是**同一逻辑**，只是把 `*.value` 换成入参
 *
 * 组件里的写法是 `mx.value?.venues?.[venue]`；这里收成入参 `mx`。
 * 语义**逐字保留**，包括下面这些"看起来可以简化、但不能动"的地方。
 *
 * ## 四处易错点（均原样保留）
 *
 * 1. **"未知"一律 `warn`，不升级为已就绪。** `venueStatus` 在缺少该所数据时
 *    返回 `{ text: statusUnknown, tone: 'warn' }` —— 绝不能因为"没有失败记录"
 *    就当成健康。`envTextOf` 同理（缺数据 → `envUnknown`）。
 * 2. **`mxHealthChips` 的 `total` 是"ok + failed 的键数"，不是"ok 的条数"。**
 *    且缺 `health.venues` 时返回 **`null`**（而不是空数组）—— 前端用
 *    `v-if="mxHealthChips"` 区分"没有数据"与"没有失败"，两者渲染不同。
 * 3. **`gateExecDirty` 的比较对象是 `!!mx.venues.gate.execution_open`** ——
 *    对 `undefined` 取 `!!` 得 `false`。于是"数据未加载"时若 `gateExec=false`，
 *    脏标记为 `false`（不误报"有未保存改动"）。
 * 4. **`envBadge` 对空值兜底成 `DEMO`**（`(env || 'demo').toUpperCase()`），
 *    不是空串。
 *
 * ## 为什么这些值得单独测
 *
 * 三条状态派生（`venueStatus` / `envTextOf` / `mxHealthChips`）直接决定
 * 运维看到的是"绿/黄/红"。它们错的方向如果是**乐观**（把未知当就绪），
 * 会让人以为交易所已连通 —— 这类"UI 说谎"是本仓反复强调的红线。
 */

/** 三所状态徽章的色调。`warn` 是"未知/未配置"的**唯一**归属。 */
export type VenueTone = 'up' | 'warn' | 'down'

/** i18n 取值函数（与 `useI18n().t` 同形；测试里传替身即可）。 */
export type TFn = (path: string, fallback?: string, params?: Record<string, string | number>) => string

export interface VenueLike {
  has_api_key?: boolean
  execution_open?: boolean
  testnet?: boolean
}

export interface MxLike {
  venues?: Record<string, VenueLike | undefined>
  okx_execution_open?: boolean
  health?: { venues?: Record<string, { ok?: unknown[]; failed?: Record<string, unknown>; avg_ms?: number; testnet?: boolean }> }
}

/** OKX 是否已连通：**两个条件都要**（READY 且 mode_configured 为真）。 */
export function deriveOkxLinked(runtime: any): boolean {
  return runtime?.status === 'READY' && runtime?.mode_configured === true
}

/**
 * 多所健康度 chip。
 *
 * - 缺 `health.venues` → **`null`**（不是 `[]`）；
 * - `total` = `ok` 条数 + `failed` 的**键数**；
 * - `avg_ms` 缺省 0；`testnet` 强转布尔。
 */
export function deriveMxHealthChips(mx: MxLike | null | undefined) {
  const venues = mx?.health?.venues
  if (!venues) return null
  return Object.entries(venues).map(([name, v]: [string, any]) => ({
    name,
    ok: (v.ok || []).length,
    total: (v.ok || []).length + Object.keys(v.failed || {}).length,
    avg_ms: v.avg_ms || 0,
    testnet: !!v.testnet,
  }))
}

/** 执行闸开关是否有未保存改动。`undefined` 经 `!!` 归一为 `false`。 */
export function deriveGateExecDirty(gateExec: boolean, mx: MxLike | null | undefined): boolean {
  return gateExec !== !!mx?.venues?.gate?.execution_open
}

/** Binance 执行闸开关是否有未保存改动。 */
export function deriveBinanceExecDirty(binanceExec: boolean, mx: MxLike | null | undefined): boolean {
  return binanceExec !== !!mx?.venues?.binance?.execution_open
}

/** OKX 执行闸开关是否有未保存改动。 */
export function deriveOkxExecDirty(okxExec: boolean, mx: MxLike | null | undefined): boolean {
  return okxExec !== !!mx?.okx_execution_open
}

/** 三所状态徽章：**未知一律 warn，不升级为已就绪**。 */
export function venueStatus(venue: 'binance' | 'gate' | 'okx', mx: MxLike | null | undefined, t: TFn): { text: string; tone: VenueTone } {
  const v = mx?.venues?.[venue]
  if (!v) return { text: t('admin.security.statusUnknown'), tone: 'warn' }
  return v.has_api_key
    ? { text: v.execution_open ? t('admin.security.keyExecOpen') : t('admin.security.keyExecClosed'), tone: 'up' }
    : { text: t('admin.security.publicMarket'), tone: 'warn' }
}

/** 三所资金档位文字：缺数据 → `envUnknown`（与 `venueStatus` 同一条"未知不乐观"规则）。 */
export function envTextOf(
  venue: 'binance' | 'gate',
  sandboxLabel: string,
  mx: MxLike | null | undefined,
  testnetFlags: { binance: boolean; gate: boolean },
  t: TFn,
): string {
  if (!mx?.venues?.[venue]) return t('admin.security.envUnknown')
  return testnetFlags[venue] ? sandboxLabel : t('admin.security.envLive')
}

/** OKX 资金档位文字（三态：live / demo / 其它一律 unknown）。 */
export function okxEnvText(okxEnvironment: unknown, t: TFn): string {
  const env = String(okxEnvironment || '')
  return env === 'live' ? t('admin.security.envLive') : env === 'demo' ? t('admin.security.envDemoOkx') : t('admin.security.envUnknown')
}

/** 环境徽章文字：空值兜底 `DEMO`（不是空串）。 */
export function envBadge(env: string | null | undefined): string {
  return (env || 'demo').toUpperCase()
}
