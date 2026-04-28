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
        <div class="message-stack">
          <div v-if="message.steps?.length" class="thinking-drawer">
            <button
              class="thinking-toggle"
              type="button"
              @click="toggleSteps(message.id)"
            >
              <span>{{ message.done ? "处理完成" : "正在处理" }}</span>
              <span class="thinking-meta">{{ message.steps.length }} 步</span>
              <svg
                :class="['chevron', { open: message.stepsOpen }]"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <path d="m6 9 6 6 6-6" />
              </svg>
            </button>
            <div v-show="message.stepsOpen" class="thinking-panel">
              <div
                v-for="step in message.steps"
                :key="step.id"
                :class="['step', step.kind]"
              >
                {{ step.content }}
              </div>
            </div>
          </div>
          <div
            v-if="message.role === 'agent'"
            :class="['bubble', 'markdown-body', { streaming: message.streaming }]"
            v-html="renderMarkdown(message.content)"
          ></div>
          <div v-else :class="['bubble', { streaming: message.streaming }]">
            {{ message.content }}
          </div>
        </div>
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
import DOMPurify from "dompurify";
import MarkdownIt from "markdown-it";
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";

const chatRef = ref(null);
const inputRef = ref(null);
const phoneNum = ref("");
const sessionId = ref("");
const draft = ref("");
const messages = ref([]);
const isBusy = ref(false);
const toast = ref("");
let toastTimer = null;
let messageId = 0;
let stepId = 0;
const streamQueues = new Map();
const markdown = new MarkdownIt({
  breaks: true,
  html: false,
  linkify: true,
  typographer: true,
});

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
  sessionId.value = localStorage.getItem(sessionStorageKey(nextValue)) || "";
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

function renderMarkdown(content) {
  return DOMPurify.sanitize(markdown.render(content || ""), {
    USE_PROFILES: { html: true },
  });
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
    steps: [],
    stepsOpen: false,
    done: false,
    streaming: false,
  });
  scrollToBottom();
  return messageId;
}

function updateMessage(id, content, error = false) {
  const target = messages.value.find((message) => message.id === id);
  if (!target) return;
  target.content = content;
  target.error = error;
  target.streaming = false;
  scrollToBottom();
}

function appendStep(id, content) {
  const target = messages.value.find((message) => message.id === id);
  if (!target) return;
  target.steps.push({
    id: ++stepId,
    kind: "status",
    content,
  });
  target.stepsOpen = true;
  scrollToBottom();
}

function appendThinking(id, content) {
  const text = content.trim();
  if (!text) return;

  const target = messages.value.find((message) => message.id === id);
  if (!target) return;

  const lastStep = target.steps[target.steps.length - 1];
  if (lastStep?.kind === "thinking") {
    lastStep.content += text;
  } else {
    target.steps.push({
      id: ++stepId,
      kind: "thinking",
      content: `模型思考：${text}`,
    });
  }

  target.stepsOpen = true;
  scrollToBottom();
}

function toggleSteps(id) {
  const target = messages.value.find((message) => message.id === id);
  if (!target) return;
  target.stepsOpen = !target.stepsOpen;
}

function markDone(id) {
  const target = messages.value.find((message) => message.id === id);
  if (!target) return;
  target.done = true;
  target.streaming = false;
  target.stepsOpen = false;
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
  const loadingId = appendMessage("agent", "");

  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "true",
      },
      body: JSON.stringify({
        phoneNum: phoneNum.value,
        sessionId: sessionId.value || null,
        message,
      }),
    });

    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }

    assertStreamResponse(response);
    await readStreamResponse(response, loadingId);
  } catch (error) {
    updateMessage(loadingId, error.message, true);
  } finally {
    isBusy.value = false;
    nextTick(() => inputRef.value?.focus());
  }
}

async function readErrorMessage(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const data = await response.json();
    return data.detail || "请求失败";
  }

  const text = await response.text();
  if (text.trim().startsWith("<!DOCTYPE")) {
    return "请求没有到达 Agent API，而是返回了 HTML 页面。请确认后端已启动并重启到最新代码。";
  }

  return text || "请求失败";
}

function assertStreamResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("text/event-stream")) {
    throw new Error(
      "后端没有返回流式响应。请确认 /api/chat/stream 已生效，并重启 uvicorn。",
    );
  }
}

async function readStreamResponse(response, messageId) {
  if (!response.body) {
    throw new Error("当前环境不支持流式响应");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";

    for (const part of parts) {
      handleStreamEvent(part, messageId);
    }
  }

  if (buffer.trim()) {
    handleStreamEvent(buffer, messageId);
  }
}

function handleStreamEvent(rawEvent, messageId) {
  const dataLine = rawEvent
    .split("\n")
    .find((line) => line.startsWith("data:"));
  if (!dataLine) return;

  const payload = JSON.parse(dataLine.slice(5).trim());

  if (payload.type === "status") {
    appendStep(messageId, payload.content);
    return;
  }

  if (payload.type === "session") {
    setSessionId(payload.sessionId);
    return;
  }

  if (payload.type === "delta") {
    enqueueDelta(messageId, payload.content);
    return;
  }

  if (payload.type === "thinking") {
    appendThinking(messageId, payload.content);
    return;
  }

  if (payload.type === "error") {
    updateMessage(messageId, payload.content, true);
    return;
  }

  if (payload.type === "done") {
    finishStream(messageId);
  }
}

function enqueueDelta(messageId, content) {
  if (!content) return;

  if (!streamQueues.has(messageId)) {
    streamQueues.set(messageId, {
      chunks: [],
      running: false,
      done: false,
    });
  }

  const queue = streamQueues.get(messageId);
  queue.chunks.push(content);

  const target = messages.value.find((message) => message.id === messageId);
  if (target) target.streaming = true;

  if (!queue.running) {
    processDeltaQueue(messageId);
  }
}

async function processDeltaQueue(messageId) {
  const queue = streamQueues.get(messageId);
  if (!queue) return;

  queue.running = true;

  while (queue.chunks.length) {
    const chunk = queue.chunks.shift();
    const pieces = chunk.match(/.{1,3}/gs) || [chunk];

    for (const piece of pieces) {
      const target = messages.value.find((message) => message.id === messageId);
      if (!target) break;
      target.content += piece;
      scrollToBottom();
      await sleep(14);
    }
  }

  queue.running = false;
  const shouldMarkDone = queue.done;
  streamQueues.delete(messageId);

  if (shouldMarkDone) {
    markDone(messageId);
  }
}

function finishStream(messageId) {
  const queue = streamQueues.get(messageId);
  if (queue?.running || queue?.chunks.length) {
    queue.done = true;
    return;
  }

  markDone(messageId);
}

function sleep(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function setSessionId(value) {
  const nextValue = String(value || "").trim();
  if (!nextValue || !phoneNum.value) return;
  sessionId.value = nextValue;
  localStorage.setItem(sessionStorageKey(phoneNum.value), nextValue);
}

function sessionStorageKey(value) {
  return `book_agent_session_${value}`;
}
</script>
