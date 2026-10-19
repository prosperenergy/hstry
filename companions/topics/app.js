"use strict";
const $ = id => document.getElementById(id);
let overview, active = "", context, offset = 0, selected = new Set(), detailNext = null, detailChat = null, viewVersion = 0;
const node = (tag, text, className) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (className) e.className = className; return e; };
function notice(text) { $("notice").textContent = text; }
async function api(path, body) {
  const res = await fetch(path, {method: body ? "POST" : "GET", headers: {...(body ? {"Content-Type": "application/json"} : {})}, ...(body ? {body: JSON.stringify(body)} : {})});
  const data = await res.json(); if (!res.ok) throw new Error(data.error || "Request failed"); return data;
}
function action(text, fn, secondary = true) { const b = node("button", text, secondary ? "secondary" : ""); b.type = "button"; b.onclick = () => Promise.resolve().then(fn).catch(e => notice(e.message)); return b; }
function link(text, url) { const a = node("a", text, "button"); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; }
function topicName() { return overview.topics.find(t => t.id === active)?.name || (active === "unfiled" ? "Needs a topic" : "All conversations"); }
async function loadOverview() {
  overview = await api("/api/status");
  $("topics").replaceChildren();
  for (const t of [{id: "", name: "All conversations", entries: overview.entries}, ...overview.topics, {id: "unfiled", name: "Needs a topic", entries: overview.unfiled}]) {
    const b = action(t.name, () => choose(t.id)); b.append(node("span", t.entries)); if (t.id === active) b.classList.add("active"); $("topics").append(b);
  }
  $("coverage").textContent = `${overview.entries.toLocaleString()} imported entries · ${overview.messages.toLocaleString()} message records · ${overview.reference_entries} reference entries. Account-wide coverage is not established.`;
  $("freshness").textContent = overview.last_sync ? `Last checked ${new Date(overview.last_sync.checked_at).toLocaleString()}. ${overview.automatic_refresh ? "Imported history changes are checked every 30 seconds. No model calls." : "Refresh is manual; no startup indexing."}` : "Refresh history to begin.";
  if (overview.local_import?.enabled && overview.automatic_refresh) $("freshness").textContent += " Local Codex and Claude Code sessions refresh every 5 minutes while this workspace runs.";
  if (overview.local_import?.last_error) notice(overview.local_import.last_error);
}
async function choose(id) {
  active = id; context = null; selected = new Set(); offset = 0; $("search").value = "";
  const version = ++viewVersion;
  await loadOverview(); if (version !== viewVersion) return;
  $("heading").textContent = topicName(); $("topic-panel").hidden = !id || id === "unfiled";
  if (id && id !== "unfiled") {
    const data = await api("/api/context?topic=" + encodeURIComponent(id)); if (version !== viewVersion) return; context = data;
    selected = new Set(context.brief_evidence); $("brief").value = context.brief;
    $("brief-state").textContent = context.brief_needs_review ? "Source history changed since this brief was saved. Review it before relying on it." : context.brief_saved_at ? `Brief saved ${new Date(context.brief_saved_at).toLocaleString()}.` : "No current brief saved yet.";
    $("canonical").textContent = context.canonical ? `${context.canonical.title}${context.canonical.present ? "" : " — missing from the latest import"}` : "Choose the conversation below where you want to keep working.";
    $("continue").replaceChildren();
    if (context.canonical?.present && context.canonical.source_url) $("continue").append(link("Open original conversation", context.canonical.source_url));
    else if (context.canonical) $("continue").append(node("p", "Original app link unavailable; source messages are available below.", "muted"));
    if (context.native_destination) $("continue").append(link("Open topic project", context.native_destination.project_url));
    $("native-sources").replaceChildren();
    for (const source of context.native_items || []) {
      const row = node("div", null, "actions");
      const a = link(source.title, source.url); a.classList.add("secondary"); row.append(a);
      row.append(action("Continue here", async () => { await api("/api/native-canonical", {topic_id: active, url: source.url}); await choose(active); notice("Saved this original conversation as the place to continue the topic."); }));
      const label = node("label"); const check = document.createElement("input"); check.type = "checkbox"; check.checked = selected.has(source.url); check.onchange = () => check.checked ? selected.add(source.url) : selected.delete(source.url); label.append(check, document.createTextNode(" Source for brief")); row.append(label);
      $("native-sources").append(row);
    }
    if ((context.native_items || []).length) $("native-sources").append(node("p", "Original app destinations verified by observation; their full transcripts may not yet be imported.", "muted"));
  }
  await listChats(false, version);
}
function renderChat(chat) {
  const card = node("article", null, "chat"); card.append(node("h3", chat.title));
  const badges = node("div"); badges.append(node("span", chat.source, "badge"));
  if (chat.reference_only) badges.append(node("span", "Reference / note", "badge"));
  if (chat.kind) badges.append(node("span", chat.kind === "suggested" ? "Suggested topic match" : "Topic confirmed", "badge"));
  if (chat.id === context?.canonical?.id) badges.append(node("span", "Continue here", "badge"));
  card.append(badges);
  const hits = chat.evidence?.messages || [];
  const phrases = [...new Set([...(chat.evidence?.title_terms || []), ...hits.flatMap(m => m.terms)])];
  card.append(node("p", `${chat.message_count} message records${phrases.length ? " · Matched: " + phrases.join(", ") : ""}`, "evidence"));
  const actions = node("div", null, "actions");
  actions.append(action("Read source", () => expand(chat.id, 0)));
  if (chat.source_url) actions.append(link("Open original", chat.source_url));
  if (active && active !== "unfiled") {
    if (chat.can_continue) actions.append(action("Continue topic here", async () => { await api("/api/canonical", {topic_id: active, chat_id: chat.id}); await choose(active); notice("Saved the conversation to continue. Native app grouping is still pending."); }));
    actions.append(action("Confirm topic", async () => { await api("/api/assign", {topic_id: active, chat_id: chat.id, kind: "included"}); await choose(active); }));
    actions.append(action("Remove from topic", async () => { await api("/api/assign", {topic_id: active, chat_id: chat.id, kind: "excluded"}); await choose(active); }));
    const label = node("label"); const check = document.createElement("input"); check.type = "checkbox"; check.checked = selected.has(chat.id); check.onchange = () => check.checked ? selected.add(chat.id) : selected.delete(chat.id); label.append(check, document.createTextNode(" Source for brief")); actions.append(label);
  } else {
    const select = document.createElement("select"); select.setAttribute("aria-label", "Assign conversation to topic");
    for (const t of overview.topics) { const option = node("option", t.name); option.value = t.id; select.append(option); }
    actions.append(select, action("Add to topic", async () => { await api("/api/assign", {topic_id: select.value, chat_id: chat.id, kind: "included"}); await choose(active); }));
  }
  card.append(actions); return card;
}
async function listChats(more = false, version = viewVersion) {
  const data = await api(`/api/chats?topic=${encodeURIComponent(active)}&offset=${more ? offset : 0}`); if (version !== viewVersion) return;
  if (!more) $("chats").replaceChildren();
  data.chats.forEach(chat => $("chats").append(renderChat(chat)));
  if (!data.chats.length && !more) $("chats").append(node("p", "No conversations in this view. Add a topic or review all conversations.", "muted"));
  offset = data.next_offset; $("more").hidden = offset === null; $("result-count").textContent = ""; $("list-heading").textContent = "Conversations";
}
async function expand(id, start) {
  const data = await api(`/api/expand?chat=${encodeURIComponent(id)}&start=${start}`);
  if (!start) $("detail-content").replaceChildren();
  data.messages.forEach(m => { const e = node("div", null, "message"); e.append(node("strong", `${m.role} · message ${m.idx} · ${m.id}`), document.createTextNode(m.content + (m.truncated ? "\n[Long message truncated in this view]" : ""))); $("detail-content").append(e); });
  detailChat = id; detailNext = data.next_start; $("more-messages").hidden = detailNext === null;
  $("detail-title").textContent = "Source messages · historical evidence"; if (!$("detail").open) $("detail").showModal();
}
$("search-form").onsubmit = async event => {
  event.preventDefault(); const version = ++viewVersion;
  try {
    const data = await api(`/api/search?q=${encodeURIComponent($("search").value)}&topic=${encodeURIComponent(active === "unfiled" ? "" : active)}`); if (version !== viewVersion) return;
    $("chats").replaceChildren(); $("more").hidden = true; $("list-heading").textContent = "Matching source messages";
    data.results.forEach(r => { const card = node("article", null, "chat"); card.append(node("h3", r.title || "Untitled conversation"), node("p", r.source_id, "muted"), node("p", r.excerpt)); card.append(action("Read this source", () => expand(r.conversation_id, r.idx))); $("chats").append(card); });
    $("result-count").textContent = `${data.results.length} results (up to 20)`;
    if (!data.results.length) $("chats").append(node("p", "No matching messages in the imported history.", "muted"));
  } catch (e) { notice(e.message); }
};
$("clear-search").onclick = () => { $("search").value = ""; listChats().catch(e => notice(e.message)); };
$("more").onclick = () => listChats(true).catch(e => notice(e.message));
$("close-detail").onclick = () => $("detail").close();
$("more-messages").onclick = () => expand(detailChat, detailNext).catch(e => notice(e.message));
$("refresh").onclick = async () => { $("refresh").disabled = true; notice("Checking imported history…"); try { const r = await api("/api/sync", {}); await choose(active); notice(`Checked ${r.entries} entries: ${r.changed} changed, ${r.unchanged} unchanged. ${r.seconds}s; no model calls.`); } catch (e) { notice(e.message); } finally { $("refresh").disabled = false; } };
$("new-topic").onclick = async () => { const name = prompt("Topic name"); if (!name) return; const terms = prompt("Words or phrases that identify this topic, separated by commas"); if (!terms) return; try { const r = await api("/api/topic", {name, terms: terms.split(",")}); await api("/api/sync", {}); await choose(r.topic_id); } catch (e) { notice(e.message); } };
$("save-brief").onclick = async () => { try { await api("/api/brief", {topic_id: active, brief: $("brief").value, evidence: [...selected]}); await choose(active); notice("Saved the topic brief with its source references."); } catch (e) { notice(e.message); } };
$("copy-context").onclick = async () => { try { const data = await api("/api/context?topic=" + encodeURIComponent(active)); await navigator.clipboard.writeText(JSON.stringify(data, null, 2)); notice("Copied the saved topic context. Paste it into the AI conversation you want to continue."); } catch (e) { notice(e.message); } };
$("native-plan").onclick = async () => { try { const data = await api("/api/native-plan?topic=" + encodeURIComponent(active)); notice(data.applied ? `ChatGPT project observed ${new Date(data.observed_at).toLocaleString()}. ${data.observed_chats} chat memberships recorded. ${data.reason}` : `${data.reason} Status: ${data.status}.`); } catch (e) { notice(e.message); } };
choose("").catch(e => notice(e.message));
setInterval(() => { if (!document.hidden) loadOverview().catch(() => {}); }, 60000);
