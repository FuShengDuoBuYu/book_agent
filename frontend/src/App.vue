<template>
  <main class="app">
    <header class="topbar">
      <div class="title-block">
        <h1>Book Agent</h1>
        <p>{{ subtitle }}</p>
      </div>
    </header>

    <section v-if="!phoneNum" class="context-banner">
      等待 App 注入用户身份后即可开始对话。
    </section>

    <section ref="chatRef" class="chat" aria-live="polite">
      <div v-if="messages.length === 0" class="empty">
        <h2>今天想看哪笔账？</h2>
        <p>先问一句，体验本地账单 Agent 的对话效果。</p>
        <div class="suggestions">
          <button
            v-for="item in suggestions"
            :key="item"
            type="button"
            @click="useSuggestion(item)"
          >
            {{ item }}
          </button>
        </div>
      </div>

      <article
        v-for="message in messages"
        :key="message.id"
        :class="['message', message.role, { error: message.error }]"
      >
        <div class="bubble">{{ message.content }}</div>
      </article>
    </section>

    <form class="composer" @submit.prevent="sendMessage">
      <textarea
        ref="inputRef"
        v-model="draft"
        rows="1"
        :placeholder="phoneNum ? '发消息' : '等待用户身份'"
        :disabled="isBusy || !phoneNum"
        @input="resizeInput"
        @keydown.enter.exact.prevent="sendMessage"
      />
      <button
        type="submit"
        :disabled="isBusy || !phoneNum || !draft.trim()"
        aria-label="发送"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 19V5" />
          <path d="m5 12 7-7 7 7" />
        </svg>
      </button>
    </form>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";

const chatRef = ref(null);
const inputRef = ref(null);
const phoneNum = ref("");
const draft = ref("");
const messages = ref([]);
const isBusy = ref(false);
const toast = ref("");
let toastTimer = null;
let messageId = 0;

const suggestions = [
  "我这个月花费最多的是什么？",
  "帮我分析一下最近几天的消费情况。",
  "你以后可以帮我做哪些账单分析？",
];

const subtitle = computed(() =>
  phoneNum.value ? "已连接 App 用户身份" : "本地自动记账 Agent",
);

onMounted(() => {
  applyPhoneNum(resolveInitialPhoneNum());
  window.setBookAgentContext = setBookAgentContext;
  window.setBookAgentPhoneNum = (value) => applyPhoneNum(value);
  window.addEventListener("message", handleNativeMessage);
  notifyNativeReady();
  resizeInput();
});

onUnmounted(() => {
  window.removeEventListener("message", handleNativeMessage);
  delete window.setBookAgentContext;
  delete window.setBookAgentPhoneNum;
});

function showToast(text) {
  toast.value = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.value = "";
  }, 2200);
}

function resolveInitialPhoneNum() {
  const params = new URLSearchParams(window.location.search);
  const context = window.__BOOK_AGENT_CONTEXT__ || {};

  return (
    params.get("phoneNum") ||
    params.get("phone") ||
    params.get("userId") ||
    context.phoneNum ||
    context.phone ||
    context.userId ||
    ""
  );
}

function applyPhoneNum(value) {
  const nextValue = String(value || "").trim();
  if (!nextValue) return;
  phoneNum.value = nextValue;
  showToast("用户身份已连接");
  nextTick(() => inputRef.value?.focus());
}

function setBookAgentContext(context) {
  if (typeof context === "string") {
    applyPhoneNum(context);
    return;
  }

  applyPhoneNum(context?.phoneNum || context?.phone || context?.userId);
}

function handleNativeMessage(event) {
  const data = normalizeMessageData(event.data);
  if (!data) return;

  if (data.type && data.type !== "BOOK_AGENT_CONTEXT") return;
  setBookAgentContext(data);
}

function normalizeMessageData(data) {
  if (!data) return null;
  if (typeof data === "string") {
    try {
      return JSON.parse(data);
    } catch {
      return null;
    }
  }

  return data;
}

function notifyNativeReady() {
  const payload = { type: "BOOK_AGENT_READY" };
  window.ReactNativeWebView?.postMessage?.(JSON.stringify(payload));
  window.webkit?.messageHandlers?.bookAgentReady?.postMessage?.(payload);
}

function useSuggestion(text) {
  draft.value = text;
  nextTick(() => {
    resizeInput();
    inputRef.value?.focus();
  });
}

function appendMessage(role, content, error = false) {
  messages.value.push({
    id: ++messageId,
    role,
    content,
    error,
  });
  scrollToBottom();
  return messageId;
}

function updateMessage(id, content, error = false) {
  const target = messages.value.find((message) => message.id === id);
  if (!target) return;
  target.content = content;
  target.error = error;
  scrollToBottom();
}

function scrollToBottom() {
  nextTick(() => {
    if (!chatRef.value) return;
    chatRef.value.scrollTop = chatRef.value.scrollHeight;
  });
}

function resizeInput() {
  nextTick(() => {
    const input = inputRef.value;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 126)}px`;
  });
}

async function sendMessage() {
  const message = draft.value.trim();
  if (!phoneNum.value) {
    showToast("等待 App 注入用户身份");
    return;
  }
  if (!message || isBusy.value) return;

  appendMessage("user", message);
  draft.value = "";
  resizeInput();
  isBusy.value = true;
  const loadingId = appendMessage("agent", "正在思考...");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        phoneNum: phoneNum.value,
        message,
      }),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "请求失败");
    }

    updateMessage(loadingId, data.reply);
  } catch (error) {
    updateMessage(loadingId, error.message, true);
  } finally {
    isBusy.value = false;
    nextTick(() => inputRef.value?.focus());
  }
}
</script>
