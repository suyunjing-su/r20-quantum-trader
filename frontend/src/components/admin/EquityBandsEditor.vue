<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useApi } from '../../composables/useApi';
import { useToast } from '../../composables/useToast';
import { useAuthStore } from '../../stores/auth';
import { Plus, Save, Trash2, RefreshCw } from 'lucide-vue-next';

const props = defineProps<{ domain: 'council' | 'prompt' | 'risk'; targets: any[] }>();
const { api } = useApi();
const toast = useToast();
const auth = useAuthStore();
const rows = ref<any[]>([]);
const mode = ref<'split' | 'unified'>('split');
const unifiedTargetId = ref('');
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const targetOptions = computed(() => (props.targets || []).map((item: any) => ({
  id: String(item.id || item.profile_id || ''),
  name: String(item.name || item.id || item.profile_id || ''),
})).filter((item) => item.id));

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const res = await api<any>(`/api/v1/admin/equity-bands/${props.domain}`);
    rows.value = (res.bands || []).map((row: any) => ({ ...row }));
    mode.value = res.mode === 'unified' ? 'unified' : 'split';
    unifiedTargetId.value = String(res.unified_target_id || '');
  } catch (e: any) {
    error.value = e?.message || '读取资金区间失败';
  } finally {
    loading.value = false;
  }
}
function addRow() {
  const target = targetOptions.value[0];
  rows.value.push({
    id: `band-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`,
    min_equity: 0,
    max_equity: null,
    target_id: target?.id || '',
    enabled: true,
    priority: 0,
  });
}
function removeRow(index: number) { rows.value.splice(index, 1); }
function validateRows(): string {
  if (mode.value === 'unified') {
    if (!unifiedTargetId.value) return '统一模式必须选择一个方案';
    return '';
  }
  if (rows.value.length > 100) return '资金区间最多保存 100 条';
  for (const [i, row] of rows.value.entries()) {
    const min = Number(row.min_equity);
    const max = row.max_equity === '' || row.max_equity == null ? null : Number(row.max_equity);
    if (!Number.isFinite(min) || min < 0 || (max !== null && (!Number.isFinite(max) || max <= min))) return `第 ${i + 1} 条区间范围无效`;
    if (!targetOptions.value.some((option) => option.id === row.target_id)) return `第 ${i + 1} 条方案目标无效`;
  }
  const enabled = rows.value.filter((row) => row.enabled).map((row) => ({ min: Number(row.min_equity), max: row.max_equity === '' || row.max_equity == null ? Infinity : Number(row.max_equity) })).sort((a, b) => a.min - b.min);
  for (let i = 1; i < enabled.length; i++) if (enabled[i].min < enabled[i - 1].max) return '启用的资金区间不能重叠';
  return '';
}
async function save() {
  if (!auth.isSuperadmin || saving.value) return;
  const message = validateRows();
  if (message) { error.value = message; return; }
  saving.value = true;
  error.value = '';
  try {
    const res = await api<any>(`/api/v1/admin/equity-bands/${props.domain}`, {
      method: 'PUT', body: JSON.stringify({
        bands: rows.value,
        mode: mode.value,
        unified_target_id: unifiedTargetId.value || null,
      }),
    });
    rows.value = res.bands || rows.value;
    mode.value = res.mode === 'unified' ? 'unified' : 'split';
    unifiedTargetId.value = String(res.unified_target_id || '');
    toast.ok('资金区间已保存');
  } catch (e: any) {
    error.value = e?.message || '保存资金区间失败';
  } finally { saving.value = false; }
}
watch(() => props.targets, () => { /* Retain stored targets; validation identifies removed profiles. */ }, { deep: true });
onMounted(load);
</script>

<template>
  <section class="eb card">
    <header class="eb-head">
      <div>
        <h2 class="card-title">资金方案选择</h2>
        <p class="card-sub">统一模式只使用一个方案；分割区间模式按已结算权益选择方案。切换模式不会删除另一模式的配置。</p>
      </div>
      <div class="eb-actions">
        <button type="button" class="btn btn-ghost btn-sm" :disabled="loading" @click="load"><RefreshCw :size="14" />刷新</button>
        <button v-if="auth.isSuperadmin" type="button" class="btn btn-primary btn-sm" :disabled="saving || loading" @click="save"><Save :size="14" />{{ saving ? '保存中…' : '保存区间' }}</button>
      </div>
    </header>
    <p v-if="error" class="eb-error" role="alert">{{ error }}</p>
    <div v-if="loading" class="card-sub">正在读取资金区间…</div>
    <div v-else-if="!auth.isSuperadmin" class="card-sub">仅超级管理员可修改资金区间。</div>
    <div v-else>
      <div class="eb-mode" role="group" aria-label="资金方案模式">
        <button type="button" class="eb-mode-btn" :class="{ 'is-on': mode === 'unified' }" @click="mode = 'unified'">统一模式</button>
        <button type="button" class="eb-mode-btn" :class="{ 'is-on': mode === 'split' }" @click="mode = 'split'">分割区间模式</button>
      </div>

      <div v-if="mode === 'unified'" class="eb-unified">
        <label class="eb-unified-target"><span>统一使用方案</span><select v-model="unifiedTargetId"><option value="" disabled>选择方案</option><option v-for="option in targetOptions" :key="option.id" :value="option.id">{{ option.name }}</option></select></label>
        <p class="card-sub">无论账户权益是多少，委员会/风控都使用这里选定的单一方案。</p>
      </div>

      <template v-else>
        <div v-for="(row, index) in rows" :key="row.id" class="eb-row">
          <label><span>从 (USDT)</span><input v-model.number="row.min_equity" type="number" min="0" step="any" /></label>
          <label><span>至 (不含)</span><input v-model.number="row.max_equity" type="number" min="0" step="any" placeholder="无上限" /></label>
          <label class="eb-target"><span>应用方案</span><select v-model="row.target_id"><option value="" disabled>选择方案</option><option v-for="option in targetOptions" :key="option.id" :value="option.id">{{ option.name }}</option></select></label>
          <label class="eb-enabled"><input v-model="row.enabled" type="checkbox" /><span>启用</span></label>
          <button type="button" class="btn btn-quiet btn-icon btn-sm" title="删除区间" @click="removeRow(index)"><Trash2 :size="14" /></button>
        </div>
        <p v-if="!rows.length" class="card-sub">尚未配置资金区间；未命中区间时继续使用默认/当前激活方案。</p>
        <div class="eb-actions"><button type="button" class="btn btn-ghost btn-sm" :disabled="targetOptions.length === 0" @click="addRow"><Plus :size="14" />添加区间</button></div>
      </template>
      <p v-if="targetOptions.length === 0" class="eb-error">请先保存至少一个可选择方案。</p>
    </div>
  </section>
</template>

<style scoped>
.eb { margin-block: 1rem; }
.eb-head,.eb-actions { display:flex; align-items:center; justify-content:space-between; gap:.6rem; flex-wrap:wrap; }
.eb-mode { display:flex; gap:0; margin:.75rem 0; }
.eb-mode-btn { padding:.45rem .8rem; color:var(--ink-2); background:var(--surface-2); border:1px solid var(--line-1); cursor:pointer; }
.eb-mode-btn:first-child { border-radius:.4rem 0 0 .4rem; }
.eb-mode-btn:last-child { border-radius:0 .4rem .4rem 0; }
.eb-mode-btn.is-on { color:var(--ink-1); background:var(--surface-3); border-color:var(--accent,var(--brand)); }
.eb-unified { display:grid; gap:.5rem; padding:.75rem 0; }
.eb-unified-target { display:grid; gap:.25rem; max-width:420px; color:var(--ink-3); font-size:.72rem; }
.eb-unified-target select { width:100%; padding:.45rem .55rem; color:var(--ink-1); background:var(--surface-2); border:1px solid var(--line-1); border-radius:.4rem; }
.eb-row { display:grid; grid-template-columns: minmax(100px,1fr) minmax(100px,1fr) minmax(160px,2fr) auto auto; align-items:end; gap:.6rem; padding:.7rem 0; border-bottom:1px solid var(--line-1); }
.eb-row label { display:grid; gap:.25rem; color:var(--ink-3); font-size:.72rem; }
.eb-row input:not([type=checkbox]),.eb-row select { width:100%; min-width:0; padding:.45rem .55rem; color:var(--ink-1); background:var(--surface-2); border:1px solid var(--line-1); border-radius:.4rem; }
.eb-enabled { display:flex!important; align-items:center; gap:.4rem!important; min-height:2rem; white-space:nowrap; }
.eb-error { margin:.5rem 0; color:var(--danger-ink,var(--danger)); font-size:.82rem; }
@media(max-width:640px) { .eb-row { grid-template-columns:1fr 1fr; } .eb-target { grid-column:1/-1; } .eb-actions { justify-content:flex-start; } }
</style>
