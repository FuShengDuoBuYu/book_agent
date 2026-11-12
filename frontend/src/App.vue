<template>
  <main class="app">
    <header class="topbar">
      <div class="title-block">
        <h1>AI 账单</h1>
        <p>{{ subtitle }}</p>
      </div>
      <nav class="conversation-actions" aria-label="对话操作">
        <button ref="historyButton" type="button" :disabled="isBusy || historyLoading || !hasSession" @click="openHistory">历史对话</button>
        <button type="button" :disabled="isBusy || historyLoading || !hasSession" @click="newConversation">新对话</button>
      </nav>
    </header>

    <section class="mode-panel" aria-label="账本模式">
      <div class="mode-switch">
        <button
          type="button"
          :class="{ active: bookMode === '个人版' }"
          :disabled="isBusy || !hasSession"
          @click="setBookMode('个人版')"
        >
          个人版
        </button>
        <button
          type="button"
          :class="{ active: bookMode === '家庭版' }"
          :disabled="isBusy || !familyAvailable"
          @click="setBookMode('家庭版')"
        >
          家庭版
        </button>
      </div>
    </section>

    <section v-if="!hasSession" class="context-banner" role="status">
      {{ sessionStatus }}
    </section>
    <section v-if="historyError" class="context-banner" role="alert">
      {{ historyError }}
      <button type="button" :disabled="historyLoading" @click="refreshConversations('', !sessionId)">重试</button>
    </section>

    <section ref="chatRef" class="chat" aria-live="polite">
      <p v-if="historyLoading" class="history-status" role="status">正在读取历史对话…</p>
      <button v-if="messageCursor" class="older-messages" type="button" :disabled="historyLoading || isBusy" @click="loadOlderMessages">加载更早的消息</button>
      <div v-if="hasSession && !historyLoading && messages.length === 0" class="empty">
        <h2>想了解哪方面的收支？</h2>
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

    <dialog ref="historyDialog" class="history-dialog" @click="closeHistoryOnBackdrop">
      <div class="history-heading">
        <h2>历史对话</h2>
        <button type="button" @click="closeHistory">关闭</button>
      </div>
      <p class="history-status">{{ bookMode }}</p>
      <p v-if="historyError" class="history-status" role="alert">{{ historyError }}
        <button type="button" :disabled="historyLoading" @click="refreshConversations()">重试</button>
      </p>
      <p v-else-if="!conversations.length && !historyLoading" class="history-status">这个账本还没有历史对话。</p>
      <button v-for="conversation in conversations" :key="conversation.id" type="button"
        class="conversation-row" :class="{ selected: conversation.id === sessionId }"
        :disabled="historyLoading || isBusy" @click="selectConversation(conversation.id)">
        <span>{{ conversation.title }}</span>
        <time>{{ formatHistoryTime(conversation.updatedAt) }}</time>
      </button>
      <button v-if="conversationCursor" class="older-messages" type="button" :disabled="historyLoading" @click="refreshConversations(conversationCursor)">加载更多对话</button>
    </dialog>

    <form class="composer" @submit.prevent="sendMessage">
      <textarea
        ref="inputRef"
        v-model="draft"
        rows="1"
        :placeholder="hasSession ? '发消息' : '等待验证'"
          :disabled="isBusy || historyLoading || !hasSession"
        @input="resizeInput"
        @keydown.enter.exact.prevent="sendMessage"
      />
      <button
        type="submit"
        :disabled="isBusy || !hasSession || !draft.trim()"
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

// 这里维护的是整个聊天页的核心状态：用户身份、当前会话、消息列表和流式渲染队列。
const chatRef = ref(null);
const inputRef = ref(null);
const hasSession = ref(false);
const familyAvailable = ref(false);
const sessionStatus = ref("正在验证账本会话…");
const sessionId = ref("");
const bookMode = ref("个人版");
const draft = ref("");
const messages = ref([]);
const conversations = ref([]);
const conversationCursor = ref(null);
const messageCursor = ref(null);
const historyLoading = ref(false);
const historyError = ref("");
const historyDialog = ref(null);
const historyButton = ref(null);
const startNewSession = ref(false);
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
  "分析最近半年的收支变化和主要原因。",
  "最近有没有异常开销或重复记账？",
  "结合历史数据，预估下个月的收入、支出和结余。",
];

const subtitle = computed(() =>
  hasSession.value ? bookMode.value : "自动记账",
);

onMounted(() => {
  loadSession();
  resizeInput();
});

onUnmounted(() => clearTimeout(toastTimer));

function showToast(text) {
  toast.value = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.value = "";
  }, 2200);
}

async function loadSession() {
  try {
    const response = await fetch("/api/session", { cache: "no-store" });
    if (!response.ok) {
      sessionStatus.value = await readErrorMessage(response);
      return;
    }
    const context = await response.json();
    familyAvailable.value = context.familyAvailable === true;
    hasSession.value = true;
    await refreshConversations("", true);
  } catch {
    sessionStatus.value = "暂时无法连接账本服务，请从 App 重新进入。";
  }
}

async function setBookMode(value) {
  if (bookMode.value === value || isBusy.value || historyLoading.value) return;
  bookMode.value = value;
  sessionId.value = "";
  messages.value = [];
  conversations.value = [];
  messageCursor.value = null;
  conversationCursor.value = null;
  startNewSession.value = false;
  await refreshConversations("", true);
  showToast(`已切换到${value}`);
}

async function historyRequest(path, cursor = "") {
  const query = new URLSearchParams({ mode: bookMode.value });
  if (cursor) query.set("cursor", cursor);
  const response = await fetch(`${path}?${query}`, { cache: "no-store" });
  if (!response.ok) {
    const detail = await readErrorMessage(response);
    if (response.status === 401) {
      hasSession.value = false;
      sessionStatus.value = detail;
      closeHistory();
    }
    throw new Error(detail);
  }
  return response.json();
}

async function refreshConversations(cursor = "", restore = false) {
  if (historyLoading.value || isBusy.value || !hasSession.value) return;
  historyLoading.value = true;
  historyError.value = "";
  try {
    const data = await historyRequest("/api/conversations", cursor);
    conversations.value = cursor ? [...conversations.value, ...data.items] : data.items;
    conversationCursor.value = data.nextCursor;
    if (restore && data.items.length) await readConversation(data.items[0].id);
  } catch (error) {
    historyError.value = error.message || "历史对话暂时无法读取。";
  } finally {
    historyLoading.value = false;
  }
}

async function readConversation(id, cursor = "") {
  const data = await historyRequest(`/api/conversations/${encodeURIComponent(id)}`, cursor);
  const restored = data.messages.map((message) => ({
    id: message.id,
    role: message.role === "assistant" ? "agent" : "user",
    content: message.content,
    done: true,
    steps: [],
  }));
  const priorHeight = chatRef.value?.scrollHeight || 0;
  messages.value = cursor ? [...restored, ...messages.value] : restored;
  sessionId.value = data.sessionId;
  startNewSession.value = false;
  messageCursor.value = data.nextCursor;
  if (cursor) {
    await nextTick();
    if (chatRef.value) chatRef.value.scrollTop = chatRef.value.scrollHeight - priorHeight;
  } else {
    scrollToBottom();
  }
}

async function selectConversation(id) {
  if (historyLoading.value || isBusy.value) return;
  historyLoading.value = true;
  historyError.value = "";
  try {
    await readConversation(id);
    closeHistory();
  } catch (error) {
    historyError.value = error.message;
    showToast(error.message);
  } finally {
    historyLoading.value = false;
  }
}

async function loadOlderMessages() {
  if (historyLoading.value || isBusy.value || !messageCursor.value) return;
  historyLoading.value = true;
  try {
    await readConversation(sessionId.value, messageCursor.value);
  } catch (error) {
    historyError.value = error.message;
  } finally {
    historyLoading.value = false;
  }
}

function openHistory() {
  historyDialog.value?.showModal();
  refreshConversations();
}

function closeHistory() {
  historyDialog.value?.close();
  historyButton.value?.focus();
}

function closeHistoryOnBackdrop(event) {
  if (event.target === historyDialog.value) closeHistory();
}

function newConversation() {
  if (isBusy.value || historyLoading.value) return;
  sessionId.value = "";
  messages.value = [];
  messageCursor.value = null;
  draft.value = "";
  startNewSession.value = true;
  historyError.value = "";
  resizeInput();
  nextTick(() => inputRef.value?.focus());
}

function formatHistoryTime(value) {
  return value ? new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "";
}

function renderMarkdown(content) {
  // 模型回复支持 Markdown，但必须先消毒，避免把 HTML 直接注入页面。
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
  // 这里是前端主流程入口：发请求、校验 SSE、消费流、更新消息状态。
  const message = draft.value.trim();
  if (!hasSession.value) {
    showToast(sessionStatus.value);
    return;
  }
  if (!message || isBusy.value || historyLoading.value) return;

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
      },
      body: JSON.stringify({
        sessionId: sessionId.value || null,
        newSession: startNewSession.value,
        mode: bookMode.value,
        message,
      }),
    });

    if (!response.ok) {
      const detail = await readErrorMessage(response);
      if (response.status === 401) {
        hasSession.value = false;
        familyAvailable.value = false;
        sessionId.value = "";
        sessionStatus.value = detail;
      } else if (response.status === 403 && bookMode.value === "家庭版") {
        familyAvailable.value = false;
      }
      throw new Error(detail);
    }

    assertStreamResponse(response);
    await readStreamResponse(response, loadingId);
  } catch (error) {
    updateMessage(loadingId, error.message, true);
  } finally {
    isBusy.value = false;
    await refreshConversations();
    nextTick(() => inputRef.value?.focus());
  }
}

async function readErrorMessage(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const data = await response.json();
    return typeof data.detail === "string" ? data.detail : "请求失败";
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
    // 一旦这里报错，通常说明请求没命中流式接口，而是被静态页面或旧后端接住了。
    throw new Error(
      "后端没有返回流式响应。请确认 /api/chat/stream 已生效，并重启 uvicorn。",
    );
  }
}

async function readStreamResponse(response, messageId) {
  if (!response.body) {
    throw new Error("当前环境不支持流式响应");
  }

  // SSE 在 fetch 里表现为可读流，这里手动按事件分隔符 \n\n 切包。
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
  // 后端每种事件类型都映射到不同 UI 行为：状态栏、会话 ID、增量文本、错误、结束。
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
    // queue 用来做“打字机效果”，避免流式 chunk 一次性整块塞进页面。
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
    // 这里故意切成更小片段，视觉上会更像模型正在逐字输出。
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
  if (value && hasSession.value) {
    sessionId.value = value;
    startNewSession.value = false;
  }
}
</script>
