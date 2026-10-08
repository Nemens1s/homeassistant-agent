const loading = document.getElementById("loading");
const content = document.getElementById("notes-content");
const errorBanner = document.getElementById("error-banner");
const pendingList = document.getElementById("pending-list");
const pendingEmpty = document.getElementById("pending-empty");
const historyList = document.getElementById("history-list");

function showError(msg) {
  errorBanner.textContent = msg;
  errorBanner.hidden = false;
}

function localTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function triggerText(note) {
  if (note.trigger_kind === "time") {
    return "at " + note.fire_at_local;
  }
  const states = note.to_state ? note.to_state.split("|").join(" or ") : "any change";
  return note.entity_id + " → " + states;
}

function expiresText(note) {
  if (note.trigger_kind === "time") return note.expires_at_local || "";
  return localTime(note.expires_at);
}

function buildRow(note, pending) {
  const li = document.createElement("li");
  li.className = "note-row";

  const main = document.createElement("div");
  main.className = "note-main";

  const text = document.createElement("span");
  text.className = "note-text";
  text.textContent = note.instruction_original || note.instruction;
  text.title = note.instruction; // the English the agent works with

  const meta = document.createElement("span");
  meta.className = "note-meta";
  let details = `${note.kind} · ${triggerText(note)} · saved ${localTime(note.created_at)}`;
  if (note.action_entity_id) details += ` · runs ${note.action_entity_id}`;
  if (note.reminder) details += ` · reminds "${note.reminder}"`;
  if (pending) {
    details += ` · expires ${expiresText(note)}`;
  } else {
    details += ` · ${note.status}`;
    if (note.result) details += ` → ${note.result}`;
  }
  meta.textContent = details;

  main.append(text, meta);
  li.append(main);

  if (pending) {
    const button = document.createElement("button");
    button.className = "note-delete";
    button.textContent = "Delete";
    button.addEventListener("click", () => onDelete(note, li, button));
    li.append(button);
  }
  return li;
}

async function onDelete(note, row, button) {
  button.disabled = true;
  try {
    const resp = await fetch(`api/notes/${note.id}`, { method: "DELETE" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    row.remove();
    if (pendingList.children.length === 0) pendingEmpty.hidden = false;
  } catch (e) {
    showError(`Could not delete note #${note.id}: ${e.message}`);
    button.disabled = false;
  }
}

async function fetchNotes(status) {
  const resp = await fetch(`api/notes?status=${status}`);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || resp.statusText);
  }
  const body = await resp.json();
  return body.notes;
}

async function load() {
  try {
    const pending = await fetchNotes("pending");
    const all = await fetchNotes("all");
    pending.forEach(note => pendingList.append(buildRow(note, true)));
    pendingEmpty.hidden = pending.length > 0;
    all.filter(note => note.status !== "pending")
       .forEach(note => historyList.append(buildRow(note, false)));
    loading.hidden = true;
    content.hidden = false;
  } catch (e) {
    loading.hidden = true;
    showError(`Could not load notes: ${e.message}`);
  }
}

load();
