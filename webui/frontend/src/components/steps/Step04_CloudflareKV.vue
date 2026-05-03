<template>
  <section class="step-fade-in">
    <div class="term-divider" data-tail="──────────">步骤 04: temp-mail</div>
    <h2 class="step-h">$&nbsp;OTP 接收（temp-mail Admin API）<span class="term-cursor"></span></h2>
    <p class="step-sub">
      使用 <code>cloudflare_temp_email</code> 的 Admin API 创建根域名邮箱，
      并通过 <code>/admin/mails</code> 读取验证码邮件。Step 03 的第一个 zone 会作为基础域名。
    </p>

    <div class="form-stack">
      <TermField
        v-model="form.api_base_url"
        label="API Base URL · api_base_url"
        placeholder="https://mail.example.com"
      />
      <TermField
        v-model="form.admin_auth"
        label="Admin Auth · admin_auth"
        type="password"
        placeholder="x-admin-auth"
      />
      <TermField
        v-model="form.custom_auth"
        label="Custom Auth · custom_auth (可选)"
        type="password"
        placeholder="x-custom-auth"
      />
    </div>

    <div class="step-actions">
      <TermBtn :loading="checking" @click="check">测试 Admin API</TermBtn>
    </div>

    <div v-if="result" class="result-block" :class="`result--${result.status}`" style="margin-top:14px">
      <div class="result-head"><span class="result-icon">{{ icon(result.status) }}</span> {{ result.message }}</div>
      <ul class="result-list">
        <li
          v-for="c in result.checks"
          :key="c.name"
          :class="`row-${c.status}`"
        >
          <span class="row-name">{{ c.name }}</span>
          <span class="row-msg">{{ c.message }}</span>
        </li>
      </ul>
    </div>

    <div v-if="error" class="result-block result--fail" style="margin-top:14px">
      <div class="result-head"><span class="result-icon">✗</span> {{ error }}</div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { useWizardStore } from "../../stores/wizard";
import type { PreflightResult } from "../../api/client";
import TermField from "../term/TermField.vue";
import TermBtn from "../term/TermBtn.vue";

const store = useWizardStore();
const cfAns = (store.answers.cloudflare ?? {}) as any;
const init = (store.answers.temp_mail ?? store.answers.cloudflare_kv ?? {}) as any;

const form = ref({
  api_base_url: init.api_base_url ?? "",
  admin_auth: init.admin_auth ?? "",
  custom_auth: init.custom_auth ?? "",
  enable_random_subdomain: init.enable_random_subdomain ?? false,
});

const domain = computed(() => ((cfAns.zone_names ?? []) as string[])[0] ?? "");

const checking = ref(false);
const result = ref<PreflightResult | null>(store.preflight.temp_mail ?? null);
const error = ref<string>("");

async function check() {
  error.value = "";
  result.value = null;
  if (!form.value.api_base_url.trim()) {
    error.value = "缺 API Base URL";
    return;
  }
  if (!form.value.admin_auth.trim()) {
    error.value = "缺 Admin Auth";
    return;
  }
  if (!domain.value) {
    error.value = "Step 03 还没填 zone_names，先回 Step 03 配基础域名";
    return;
  }

  checking.value = true;
  try {
    const payload = {
      ...form.value,
      domain: domain.value,
    };
    store.setAnswer("temp_mail", payload);
    await store.saveToServer();
    result.value = await store.runPreflight("temp_mail", payload);
  } catch (e: any) {
    error.value = e?.response?.data?.detail || String(e);
  } finally {
    checking.value = false;
  }
}

watch(form, () => {
  const cur = (store.answers.temp_mail ?? {}) as any;
  store.setAnswer("temp_mail", {
    ...cur,
    ...form.value,
    domain: domain.value,
  });
}, { deep: true });

function icon(s: string) {
  return s === "ok" ? "✓" : s === "fail" ? "✗" : s === "warn" ? "▲" : "○";
}
</script>
