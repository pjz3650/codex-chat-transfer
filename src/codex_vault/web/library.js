"use strict";

const $ = (id) => document.getElementById(id);
const view = $("view");
const pending = new Map();
const selected = new Set();
const pageSize = 20;
let sequence = 0;
let hostCapabilities = {};
let activeChat = null;
let busy = false;
let ready = false;
let hostOrigin = "*";
let host;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function notice(text) { $("status").textContent = text; }

function rpc(method, params) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error("O ChatGPT não respondeu a tempo."));
    }, method === "ui/initialize" ? 6000 : 45000);
    pending.set(id, {resolve, reject, timeout});
    host.postMessage({jsonrpc: "2.0", id, method, params}, hostOrigin);
  });
}

function notify(method, params = {}) {
  host.postMessage({jsonrpc: "2.0", method, params}, hostOrigin);
}

window.addEventListener("message", (event) => {
  if (event.source !== host || (hostOrigin !== "*" && event.origin !== hostOrigin)) return;
  const message = event.data;
  if (!message || message.jsonrpc !== "2.0" || !pending.has(message.id)) return;
  const request = pending.get(message.id);
  if (hostOrigin === "*" && event.origin && event.origin !== "null") hostOrigin = event.origin;
  pending.delete(message.id);
  clearTimeout(request.timeout);
  if (message.error) request.reject(new Error(message.error.message || "Falha na comunicação com o ChatGPT."));
  else request.resolve(message.result);
});

async function tool(name, args = {}) {
  const result = await rpc("tools/call", {name, arguments: args});
  if (result.isError) throw new Error(result.content?.find((part) => part.type === "text")?.text || "Falha ao consultar o acervo.");
  if (result.structuredContent) return result.structuredContent;
  return JSON.parse(result.content.find((part) => part.type === "text").text);
}

async function act(action) {
  if (busy || !ready) return;
  busy = true;
  notice("Carregando…");
  view.setAttribute("aria-busy", "true");
  try {
    const message = await action();
    notice(typeof message === "string" ? message : "");
  } catch (error) {
    notice(error.message || "Falha ao consultar o acervo.");
  } finally {
    busy = false;
    view.setAttribute("aria-busy", "false");
  }
}

function button(label, action, className) {
  const item = node("button", label, className);
  item.type = "button";
  item.addEventListener("click", () => act(action));
  return item;
}

function toolbar(title, ...buttons) {
  const bar = node("div", undefined, "toolbar");
  const actions = node("div", undefined, "actions");
  actions.append(...buttons);
  bar.append(node("h2", title), actions);
  return bar;
}

function row(title, meta, action) {
  const wrapper = node("div", undefined, "row");
  const item = button(title, action, "row-button");
  item.append(node("div", meta, "meta"));
  wrapper.append(item);
  return wrapper;
}

function date(value) {
  const parsed = value ? new Date(value) : null;
  return parsed && Number.isFinite(parsed.valueOf()) ? parsed.toLocaleString("pt-BR") : "Data indisponível";
}

function tab(name) {
  for (const id of ["projects", "devices"]) {
    if (id === name) $(`${id}-tab`).setAttribute("aria-current", "page");
    else $(`${id}-tab`).removeAttribute("aria-current");
  }
}

function pagination(offset, next, action) {
  const actions = node("div", undefined, "actions");
  if (offset) actions.append(button("Anterior", () => action(Math.max(0, offset - pageSize))));
  if (next !== null && next !== undefined) actions.append(button("Próxima", () => action(next)));
  return actions;
}

async function projects() {
  const data = await tool("list");
  tab("projects");
  view.replaceChildren(toolbar("Projetos", button("Atualizar", projects)));
  if (!data.projects.length) view.append(node("p", "Nenhum projeto sincronizado. Cadastre uma máquina e solicite a primeira sincronização.", "empty"));
  for (const project of data.projects) {
    const copies = project.copies !== project.unique_chats ? ` · ${project.copies} origens` : "";
    view.append(row(project.name, `${project.unique_chats} conversas${copies}${project.repository ? ` · ${project.repository}` : ""}`, () => chats(project)));
  }
  if (data.pending_devices.length) return `Dispositivos pendentes ou offline: ${data.pending_devices.join(", ")}.`;
}

async function sync(name) {
  const response = name ? await tool("pull", {device: name}) : await tool("pull_all");
  const container = node("section");
  container.append(node("h3", "Pedidos de sincronização"));
  for (const job of response.jobs) {
    const line = node("div", undefined, "row");
    const caption = node("p", `${job.device} · ${job.availability === "online" ? "solicitado" : "aguardando máquina"}`);
    line.append(caption, button("Ver andamento", async () => {
      const state = await tool("job_status", {job_id: job.job_id});
      const labels = {pending: "na fila", running: "sincronizando", complete: "concluído", failed: "falhou"};
      caption.textContent = `${job.device} · ${labels[state.state] || state.state}`;
      return state.result?.error || (state.result ? `${state.result.threads_sent} conversas atualizadas.` : "");
    }));
    container.append(line);
  }
  if (!response.jobs.length) container.append(node("p", "Nenhum dispositivo ativo.", "empty"));
  view.querySelector("section")?.remove();
  view.append(container);
  return response.jobs.length ? "Sincronização solicitada." : "";
}

async function devices() {
  const {devices: list} = await tool("devices");
  tab("devices");
  const pullAll = button("Sincronizar todos", () => sync(), "primary");
  pullAll.disabled = !list.some((device) => !device.revoked);
  view.replaceChildren(toolbar("Dispositivos", button("Atualizar", devices), pullAll));
  if (!list.length) view.append(node("p", "Nenhum dispositivo cadastrado. Use o comando de pareamento no serviço local.", "empty"));
  for (const device of list) {
    const line = node("div", undefined, "row");
    const info = node("div");
    info.append(node("h3", device.name), node("p", `${device.revoked ? "Revogado" : device.online ? "Online" : "Offline"} · ${device.last_sync ? `Sincronizado ${date(device.last_sync)}` : "Aguardando primeira sincronização"}`, "meta"));
    const pull = button("Sincronizar", () => sync(device.id));
    pull.disabled = device.revoked;
    line.append(info, pull);
    view.append(line);
  }
}

async function chats(project, offset = 0) {
  const page = await tool("chats", {project_id: project.id, offset, limit: pageSize});
  view.replaceChildren(toolbar(project.name, button("Projetos", projects), button("Exportar", async () => {
    const {downloads} = await tool("export", {project_id: project.id});
    const actions = node("div", undefined, "actions");
    for (const [format, href] of Object.entries(downloads)) {
      actions.append(button(format === "json" ? "Baixar JSON" : "Baixar Markdown", () => rpc("ui/open-link", {url: href})));
    }
    view.querySelector(".toolbar").after(actions);
    return "Links de exportação disponíveis por 10 minutos.";
  })));
  if (!page.data.length) view.append(node("p", "Nenhuma conversa neste projeto.", "empty"));
  for (const chat of page.data) view.append(row(chat.header.title, `${chat.device} · ${date(chat.header.updated_at)}${chat.header.archived ? " · Arquivada" : ""}`, () => read(chat.id, () => chats(project, offset))));
  view.append(pagination(offset, page.next_offset, (next) => chats(project, next)));
}

async function search(query, offset = 0) {
  const page = await tool("search", {query, offset, limit: pageSize});
  tab(null);
  view.replaceChildren(toolbar(`Busca por “${query}”`, button("Projetos", projects)));
  if (!page.data.length) view.append(node("p", "Nenhuma mensagem encontrada.", "empty"));
  for (const hit of page.data) {
    const line = row(hit.title, `${hit.device} · ${date(hit.date)}`, () => read(hit.chat_id, () => search(query, offset), Math.floor(hit.position / pageSize) * pageSize, hit.message_id));
    line.firstElementChild.append(node("p", hit.excerpt, "excerpt"));
    view.append(line);
  }
  view.append(pagination(offset, page.next_offset, (next) => search(query, next)));
}

async function read(chat, back, offset = 0, highlight) {
  const page = await tool("read", {chat_id: chat, offset, limit: pageSize});
  if (activeChat !== chat) selected.clear();
  activeChat = chat;
  view.replaceChildren(toolbar(page.header.title, button("Voltar", back), button("Continuar no ChatGPT", async () => {
    const context = await tool("continue_context", {chat_id: chat, message_ids: [...selected]});
    $("context-text").value = context.prompt;
    const supported = Boolean(hostCapabilities.experimental?.["openai/message"] || hostCapabilities.extensions?.["openai/message"]);
    $("open-draft").disabled = !supported;
    $("context-status").textContent = supported ? "Revise o contexto antes de abrir a nova conversa." : "Copie o contexto para uma nova conversa.";
    $("context-dialog").showModal();
  }, "primary")));
  view.append(node("p", `${page.device} · ${page.header.workspace} · ${date(page.header.updated_at)}`, "meta"));
  const list = node("ul", undefined, "messages");
  const roles = {user: "Você", assistant: "Codex", tool: "Ferramenta", system: "Sistema", unknown: "Registro"};
  for (const item of page.items) {
    const line = node("li", undefined, item.id === highlight ? "highlight" : undefined);
    const label = node("label");
    if (["user", "assistant"].includes(item.role) && item.text) {
      const check = node("input");
      check.type = "checkbox";
      check.checked = selected.has(item.id);
      check.setAttribute("aria-label", `Incluir mensagem de ${roles[item.role]} no contexto`);
      check.addEventListener("change", () => {
        if (!check.checked) selected.delete(item.id);
        else if (selected.size < 24) selected.add(item.id);
        else { check.checked = false; notice("Selecione até 24 mensagens."); }
      });
      label.append(check);
    }
    label.append(node("strong", `${roles[item.role] || "Registro"} · ${date(item.timestamp)}`));
    line.append(label, node("p", item.text || item.kind, "excerpt"));
    if (item.truncated) line.append(node("p", "Trecho parcial. Use a exportação para ver o registro completo.", "meta"));
    list.append(line);
  }
  view.append(list, pagination(offset, page.next_offset, (next) => read(chat, back, next)));
  if (!page.items.length) view.append(node("p", "Nenhum item nesta página.", "empty"));
  list.querySelector(".highlight")?.scrollIntoView({block: "nearest"});
}

$("projects-tab").addEventListener("click", () => act(projects));
$("devices-tab").addEventListener("click", () => act(devices));
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const query = $("query").value.trim();
  if (query) act(() => search(query));
});
$("close-dialog").addEventListener("click", () => $("context-dialog").close());
$("copy-context").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("context-text").value);
    $("context-status").textContent = "Contexto copiado.";
  } catch {
    $("context-text").select();
    $("context-status").textContent = "Use o atalho de copiar para copiar o texto selecionado.";
  }
});
$("open-draft").addEventListener("click", async () => {
  const button = $("open-draft");
  button.disabled = true;
  try {
    const result = await rpc("ui/message", {
      role: "user", content: [{type: "text", text: $("context-text").value}],
      _meta: {"openai/message": {target: "new", send: false}},
    });
    if (result?.isError) throw new Error("O ChatGPT não abriu o rascunho.");
    $("context-dialog").close();
    notice("Rascunho solicitado ao ChatGPT.");
  } catch (error) {
    $("context-status").textContent = `${error.message} Você pode copiar o contexto.`;
  } finally {
    button.disabled = false;
  }
});

async function start() {
  host = window.parent;
  if (host === window) {
    view.replaceChildren(node("p", "Abra o acervo pelo plugin Codex Vault no ChatGPT Work.", "empty"));
    view.setAttribute("aria-busy", "false");
    return;
  }
  try {
    const result = await rpc("ui/initialize", {
      protocolVersion: "2026-01-26", appInfo: {name: "Codex Vault", version: "0.1.0"}, appCapabilities: {},
    });
    hostCapabilities = result.hostCapabilities || {};
    notify("ui/notifications/initialized");
    ready = true;
    await act(projects);
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => notify("ui/notifications/size-changed", {height: document.body.scrollHeight})).observe(document.body);
    }
  } catch (error) {
    view.replaceChildren(node("p", error.message, "empty"));
    view.setAttribute("aria-busy", "false");
  }
}

start();
