<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 11: 下游推送</div>
    <h2 class="step-h">$&nbsp;下游推送 (全部可选 · 可多选)<span class="term-cursor"></span></h2>

    <div class="term-divider" style="margin-top:8px">gpt-team</div>
    <TermToggle v-model="ts.enabled">启用 gpt-team</TermToggle>
    <div v-if="ts.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="ts.base_url" label="Base URL · base_url" />
      <TermField v-model="ts.username" label="用户名 · username" />
      <TermField v-model="ts.password" label="密码 · password" type="password" />
      <div class="step-actions">
        <TermBtn :loading="tsLoading" @click="testTs">登录测试</TermBtn>
      </div>
      <div v-if="tsResult" class="result-block" :class="`result--${tsResult.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(tsResult.status) }}</span>
          <span>{{ tsResult.message }}</span>
        </div>
      </div>
    </div>

    <div class="term-divider" style="margin-top:20px">CPA</div>
    <TermToggle v-model="cpa.enabled">启用 CPA</TermToggle>
    <div v-if="cpa.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="cpa.base_url" label="Base URL · base_url" />
      <TermField v-model="cpa.admin_key" label="Admin Key · admin_key" type="password" />
      <TermField v-model="cpa.oauth_client_id" label="OAuth Client ID · oauth_client_id" />
      <div class="step-actions">
        <TermBtn :loading="cpaLoading" @click="testCpa">健康检查</TermBtn>
      </div>
      <div v-if="cpaResult" class="result-block" :class="`result--${cpaResult.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(cpaResult.status) }}</span>
          <span>{{ cpaResult.message }}</span>
        </div>
      </div>
    </div>

    <div class="term-divider" style="margin-top:20px">Sub2API</div>
    <TermToggle v-model="sub2api.enabled">启用 Sub2API</TermToggle>
    <div v-if="sub2api.enabled" class="form-stack" style="margin-top:12px">
      <TermField v-model="sub2api.base_url" label="Base URL · base_url" placeholder="https://sub2api.example.com" />
      <TermField v-model="sub2api.api_key" label="API Key · api_key（x-api-key header）" type="password" />
      <TermField
        v-model="sub2api.oauth_client_id"
        label="OAuth Client ID · oauth_client_id（留空时复用 CPA 配置）"
      />
      <div class="form-row-2">
        <TermField v-model="sub2api.concurrency" label="concurrency（并发上限，可选）" type="number" />
        <TermField v-model="sub2api.priority" label="priority（调度优先级，可选）" type="number" />
      </div>
      <div class="form-row-2">
        <TermField
          v-model="sub2api.rate_multiplier"
          label="rate_multiplier（速率倍率，0~N，可选）"
          type="number"
        />
        <TermField v-model="sub2api.proxy_id" label="proxy_id（代理 ID，可选）" type="number" />
      </div>
      <TermField
        v-model="sub2api.group_ids"
        label="group_ids · 逗号分隔（如 1,2,3，可选）"
        placeholder="1,2,3"
      />
      <TermField
        v-model="sub2api.timeout_s"
        label="timeout_s（HTTP 超时秒，留空走默认 20）"
        type="number"
      />
      <div class="step-actions">
        <TermBtn :loading="subLoading" @click="testSub2Api">连通性检查</TermBtn>
      </div>
      <div v-if="subResult" class="result-block" :class="`result--${subResult.status}`">
        <div class="result-head">
          <span class="result-icon">{{ icon(subResult.status) }}</span>
          <span>{{ subResult.message }}</span>
        </div>
        <pre v-if="subResult.details" class="result-details">{{ subResult.details }}</pre>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";
import TermToggle from "../term/TermToggle.vue";

const store = useWizardStore();
const tsInit = store.answers.team_system ?? {};
const cpaInit = store.answers.cpa ?? {};
const subInit = store.answers.sub2api ?? {};

// 开关默认关闭（不读 init.enabled），但其余字段保留 source 同步的值
// 这样用户启用 toggle 时直接看到预填的 url/凭据
const ts = ref({
  enabled: false,
  base_url: tsInit.base_url ?? "http://127.0.0.1:3000",
  username: tsInit.username ?? "admin",
  password: tsInit.password ?? "",
});
const cpa = ref({
  enabled: false,
  base_url: cpaInit.base_url ?? "",
  admin_key: cpaInit.admin_key ?? "",
  oauth_client_id: cpaInit.oauth_client_id ?? "",
});
// sub2api 数字字段在 UI 上以字符串存（TermField 直绑），write_configs 后端
// 会按 _normalize_sub2api 转 int/float/list；空串等同于"未填"。
const sub2api = ref({
  enabled: false,
  base_url: subInit.base_url ?? "",
  api_key: subInit.api_key ?? "",
  oauth_client_id: subInit.oauth_client_id ?? "",
  concurrency: subInit.concurrency ?? "",
  priority: subInit.priority ?? "",
  rate_multiplier: subInit.rate_multiplier ?? "",
  proxy_id: subInit.proxy_id ?? "",
  group_ids: Array.isArray(subInit.group_ids)
    ? subInit.group_ids.join(",")
    : (subInit.group_ids ?? ""),
  timeout_s: subInit.timeout_s ?? "",
});

// 立即同步到 store 覆盖可能从 source 同步过来的 enabled=true，
// 否则 UI 显示关但 wizard state / 导出仍会写 enabled=true
onMounted(() => {
  store.setAnswer("team_system", {});
  store.setAnswer("cpa", {});
  store.setAnswer("sub2api", {});
  store.saveToServer();
});
const tsLoading = ref(false);
const cpaLoading = ref(false);
const subLoading = ref(false);
const tsResult = ref<PreflightResult | null>(null);
const cpaResult = ref<PreflightResult | null>(null);
const subResult = ref<PreflightResult | null>(null);

async function testTs() {
  tsLoading.value = true;
  try {
    tsResult.value = await store.runPreflight("team_system", {
      base_url: ts.value.base_url,
      username: ts.value.username,
      password: ts.value.password,
    });
  } finally { tsLoading.value = false; }
}
async function testCpa() {
  cpaLoading.value = true;
  try {
    cpaResult.value = await store.runPreflight("cpa", {
      base_url: cpa.value.base_url,
      admin_key: cpa.value.admin_key,
    });
  } finally { cpaLoading.value = false; }
}
async function testSub2Api() {
  subLoading.value = true;
  try {
    subResult.value = await store.runPreflight("sub2api", {
      base_url: sub2api.value.base_url,
      api_key: sub2api.value.api_key,
    });
  } finally { subLoading.value = false; }
}
watch([ts, cpa, sub2api], () => {
  store.setAnswer("team_system", ts.value.enabled ? ts.value : {});
  store.setAnswer("cpa", cpa.value.enabled ? cpa.value : {});
  store.setAnswer("sub2api", sub2api.value.enabled ? sub2api.value : {});
  store.saveToServer();
}, { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
</script>

<style scoped>
.form-row-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.result-details {
  margin: 6px 0 0;
  padding: 6px 8px;
  background: var(--bg-base);
  border: 1px dashed var(--border);
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 160px;
  overflow: auto;
}
</style>
