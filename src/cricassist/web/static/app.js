// CricAssist web app: sessions in the side panel, a model picker, and a chat
// thread that shows the assistant's tool calls live while it works.
//
// Each chat answers independently: while one is still answering you can open
// another, start a new one, or ask somewhere else. A running answer keeps
// collecting its tool calls and streamed text in `pending`, and is drawn into
// the thread whenever its chat is the one on screen.
"use strict";

const SPLASH_MIN_MS = 2400;
const POLL_MS = 2500;  // checking on an answer that was running before a page reload
const SUGGESTIONS = [
  ["How does Kohli fare against left-arm spin in T20s since 2022?", "Batter vs bowling style"],
  ["How long was Dale Steyn ranked number one in Test cricket?", "What the data can and can't say"],
  ["Tell me about the 2023 World Cup final.", "Full scorecard"],
];

const $ = (sel) => document.querySelector(sel);
const app = $("#app");
const thread = $("#thread");
const input = $("#input");
const sendBtn = $("#send");
const picker = $("#model");
const list = $("#sessionList");
const statusEl = $("#status");

const state = { sessions: [], current: null, models: [], defaultModel: null };
const pending = new Map();  // session id -> { lines, draftHtml, controller, view }
const unread = new Set();   // chats that finished answering while not on screen
let pollTimer = null;

// ---------- Helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) node.append(child);
  return node;
}

function svgUse(id, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  if (cls) svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

function svgTrash() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3");
  svg.append(path);
  return svg;
}

function wordmark() {
  return $("#wordmark").content.firstElementChild.cloneNode(true);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function describeCall(call) {
  const args = Object.entries(call.args || {})
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(", ");
  return `${call.name}(${args})`;
}

function modelLabel(id) {
  return (state.models.find((m) => m.id === id) || { label: id }).label;
}

function isCurrent(id) {
  return state.current?.id === id;
}

function isRunning(id) {
  return pending.has(id) || Boolean(state.sessions.find((s) => s.id === id)?.running);
}

function scrollToEnd() {
  thread.scrollTop = thread.scrollHeight;
}

// ---------- Rendering ----------

function renderSessions() {
  list.replaceChildren();
  if (!state.sessions.length) {
    list.append(el("li", { class: "empty" }, "No chats yet"));
    return;
  }
  for (const s of state.sessions) {
    const del = el("button", { class: "del", type: "button", "aria-label": `Delete chat: ${s.title}` }, svgTrash());
    del.addEventListener("click", (e) => { e.stopPropagation(); deleteSession(s.id); });
    const badge = isRunning(s.id)
      ? el("span", { class: "badge running", title: "Answering…" }, svgUse("ball"))
      : unread.has(s.id) ? el("span", { class: "badge unread", title: "New answer" }) : null;
    const item = el("li", { class: isCurrent(s.id) ? "active" : "", title: s.title },
      el("span", { class: "title" }, s.title));
    if (badge) item.append(badge);
    item.append(del);
    item.addEventListener("click", () => openSession(s.id));
    list.append(item);
  }
}

function renderWelcome() {
  const chips = el("div", { class: "chips" });
  for (const [question, hint] of SUGGESTIONS) {
    chips.append(el("button", { class: "chip", type: "button", onclick: () => ask(question) }, question, el("small", {}, hint)));
  }
  thread.replaceChildren(el("div", { class: "welcome" },
    wordmark(),
    el("p", {}, "Ask about players, teams, venues and records. Every figure comes from ball-by-ball data."),
    chips));
}

function userMessage(text) {
  return el("div", { class: "msg user" }, el("div", { class: "bubble" }, text));
}

function botShell() {
  const avatar = el("div", { class: "avatar" });
  avatar.append(svgUse("ball"));
  const content = el("div", { class: "content" });
  return { node: el("div", { class: "msg bot" }, avatar, content), content };
}

function toolsDetails(calls) {
  if (!calls?.length) return null;
  const ul = el("ul");
  for (const call of calls) ul.append(el("li", { class: call.is_error ? "err" : "" }, describeCall(call)));
  return el("details", { class: "tools" },
    el("summary", {}, `Used ${calls.length} stats tool${calls.length === 1 ? "" : "s"}`), ul);
}

function botMessage(message) {
  const { node, content } = botShell();
  content.innerHTML = message.html;
  content.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  const meta = el("div", { class: "meta" });
  const tools = toolsDetails(message.tool_calls);
  if (tools) meta.append(tools);
  if (message.model) meta.append(el("span", { class: "model-tag" }, modelLabel(message.model)));
  content.append(meta);
  return node;
}

function errorMessage(text) {
  const { node, content } = botShell();
  node.classList.add("error");
  content.textContent = text;
  return node;
}

// The bubble of an answer in progress, built from what has arrived so far.
function pendingView(p) {
  const { node, content } = botShell();
  content.classList.add("working");
  const dots = el("div", { class: "dots" }, el("span"), el("span"), el("span"));
  const live = el("ul", { class: "live" });
  const draft = el("div", { class: "draft" });
  content.append(dots, live, draft);
  const view = { node, content, dots, live, draft };
  for (const line of p.lines) live.append(lineItem(line));
  setDraft(view, p.draftHtml);
  return view;
}

function lineItem(line) {
  return el("li", { class: line.error ? "err" : "" }, line.text);
}

function setDraft(view, html) {
  view.draft.innerHTML = html;
  view.dots.hidden = Boolean(html);
  view.content.classList.toggle("streaming", Boolean(html));
}

function stillAnsweringView() {
  const { node, content } = botShell();
  content.classList.add("working");
  content.append(el("div", { class: "dots" }, el("span"), el("span"), el("span")),
    el("ul", { class: "live" }, el("li", {}, "Still answering this question…")));
  return node;
}

function renderMessages(session) {
  if (!session.messages.length && !isRunning(session.id)) return renderWelcome();
  thread.replaceChildren();
  for (const m of session.messages) {
    if (m.role === "user") thread.append(userMessage(m.text));
    else if (m.role === "assistant") thread.append(botMessage(m));
    else if (m.role === "error") thread.append(errorMessage(m.text));
    else if (m.role === "notice") thread.append(el("div", { class: "notice" }, m.text));
  }
  const p = pending.get(session.id);
  if (p) {
    p.view = pendingView(p);
    thread.append(p.view.node);
  } else if (session.running) {
    thread.append(stillAnsweringView());  // started before a page reload: wait for it
    schedulePoll(session.id);
  }
  scrollToEnd();
}

function renderPicker() {
  picker.replaceChildren();
  const groups = {};
  for (const m of state.models) {
    const label = m.provider === "claude" ? "Claude" : "Gemini";
    groups[label] ||= el("optgroup", { label });
    const option = el("option", { value: m.id }, m.label + (m.available ? "" : " (no API key)"));
    if (!m.available) option.disabled = true;
    groups[label].append(option);
  }
  Object.values(groups).forEach((g) => picker.append(g));
  picker.value = state.current?.model || state.defaultModel;
}

// Only the chat on screen decides whether you can type: other chats may be busy.
function updateComposer() {
  const busy = Boolean(state.current && isRunning(state.current.id));
  input.disabled = busy;
  picker.disabled = busy;
  sendBtn.disabled = busy || !input.value.trim();
  const others = [...pending.keys()].filter((id) => !isCurrent(id)).length;
  statusEl.textContent = busy ? "Answering…" : others ? `${others} other chat${others === 1 ? "" : "s"} answering` : "";
}

// ---------- Sessions ----------

async function refreshSessions() {
  state.sessions = await api("/api/sessions");
  renderSessions();
}

function detachView() {
  for (const p of pending.values()) p.view = null;  // the thread is about to be redrawn
  clearTimeout(pollTimer);
}

function newChat() {
  detachView();
  state.current = null;
  history.replaceState(null, "", location.pathname);
  picker.value = picker.value || state.defaultModel;
  renderSessions();
  renderWelcome();
  updateComposer();
  closeNav();
  input.focus();
}

async function openSession(id) {
  const session = await api(`/api/sessions/${id}`);
  detachView();
  state.current = session;
  unread.delete(id);
  history.replaceState(null, "", `#${session.id}`);  // a refresh comes back to this chat
  picker.value = session.model;
  const known = state.sessions.find((s) => s.id === id);
  if (known) known.running = session.running;
  renderSessions();
  renderMessages(session);
  updateComposer();
  closeNav();
  if (!isRunning(id)) input.focus();
}

async function deleteSession(id) {
  pending.get(id)?.controller.abort();
  pending.delete(id);
  unread.delete(id);
  await api(`/api/sessions/${id}`, { method: "DELETE" });
  if (isCurrent(id)) {
    detachView();
    state.current = null;
    history.replaceState(null, "", location.pathname);
    renderWelcome();
  }
  await refreshSessions();
  updateComposer();
}

// After a page reload, an answer may still be running on the server with no
// stream attached here: check back until it is done, then show it.
function schedulePoll(id) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!isCurrent(id) || pending.has(id)) return;
    try {
      const session = await api(`/api/sessions/${id}`);
      if (session.running) return schedulePoll(id);
      await refreshSessions();
      if (isCurrent(id)) await openSession(id);
    } catch { /* deleted or server gone: stop checking */ }
  }, POLL_MS);
}

// ---------- Asking ----------

async function ask(text) {
  text = text.trim();
  if (!text || (state.current && isRunning(state.current.id))) return;
  input.value = "";
  autosize();

  let id;
  try {
    if (!state.current) {
      state.current = await api("/api/sessions", { method: "POST", body: { model: picker.value } });
      state.current.messages = [];
      history.replaceState(null, "", `#${state.current.id}`);
      thread.replaceChildren();
      await refreshSessions();
    }
    id = state.current.id;
  } catch (err) {
    thread.append(errorMessage(err.message));
    return;
  }

  const p = { lines: [], draftHtml: "", controller: new AbortController(), view: null };
  pending.set(id, p);
  if (thread.querySelector(".welcome")) thread.replaceChildren();
  thread.append(userMessage(text));
  p.view = pendingView(p);
  thread.append(p.view.node);
  scrollToEnd();
  renderSessions();
  updateComposer();

  // Every update goes to the pending record, and to the screen only while this chat is open.
  const addLine = (line) => {
    p.lines.push(line);
    if (p.view && isCurrent(id)) { p.view.live.append(lineItem(line)); scrollToEnd(); }
  };
  const finish = (node) => {
    pending.delete(id);
    if (p.view && isCurrent(id)) { p.view.node.replaceWith(node); scrollToEnd(); }
    else unread.add(id);
    const known = state.sessions.find((s) => s.id === id);
    if (known) known.running = false;
    renderSessions();
    updateComposer();
  };

  try {
    const res = await fetch(`/api/sessions/${id}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal: p.controller.signal,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Request failed (${res.status})`);
    }
    await readEvents(res, {
      session: (s) => {
        const known = state.sessions.find((x) => x.id === s.id);
        if (known && known.title !== s.title) { known.title = s.title; renderSessions(); }
      },
      tool: (call) => addLine({ text: "· " + describeCall(call), error: call.is_error }),
      wait: (w) => addLine({ text: w.message }),
      draft: (d) => {
        // The answer as it streams in; an empty draft means the model went back to its tools.
        p.draftHtml = d.html;
        if (p.view && isCurrent(id)) { setDraft(p.view, d.html); scrollToEnd(); }
      },
      answer: (m) => finish(botMessage(m)),
      error: (m) => finish(errorMessage(m.text)),
    });
    if (pending.has(id)) finish(errorMessage("The connection closed before the answer arrived."));
  } catch (err) {
    if (err.name === "AbortError") return;  // the chat was deleted
    if (pending.has(id)) finish(errorMessage(err.message));
  }
}

// Server-Sent Events over a POST response: "event: name\ndata: {...}\n\n".
async function readEvents(res, handlers) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data && handlers[event]) handlers[event](JSON.parse(data));
    }
  }
}

// ---------- Inputs ----------

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
  updateComposer();
}

function closeNav() { app.classList.remove("nav-open"); }

input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    ask(input.value);
  }
});
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
$("#newChat").addEventListener("click", newChat);
$("#menu").addEventListener("click", () => app.classList.toggle("nav-open"));
$("#scrim").addEventListener("click", closeNav);

picker.addEventListener("change", async () => {
  if (!state.current) return;  // a new chat uses the picked model when it starts
  try {
    await api(`/api/sessions/${state.current.id}`, { method: "PATCH", body: { model: picker.value } });
    await openSession(state.current.id);
  } catch (err) {
    thread.append(errorMessage(err.message));
  }
});

// ---------- Start ----------

async function start() {
  document.querySelectorAll("[data-wordmark]").forEach((slot) => slot.append(wordmark()));
  const minimum = new Promise((resolve) => setTimeout(resolve, SPLASH_MIN_MS));
  try {
    const [models] = await Promise.all([api("/api/models"), refreshSessions(), minimum]);
    state.models = models.models;
    state.defaultModel = models.default;
    renderPicker();
    const fromUrl = location.hash.slice(1);
    if (fromUrl && state.sessions.some((s) => s.id === fromUrl)) await openSession(fromUrl);
    else renderWelcome();
  } catch (err) {
    await minimum;
    thread.replaceChildren(errorMessage(`Could not reach the CricAssist server: ${err.message}`));
  }
  updateComposer();
  app.hidden = false;
  requestAnimationFrame(() => $("#splash").classList.add("done"));
  setTimeout(() => $("#splash").remove(), 900);
  input.focus();
}

start();
