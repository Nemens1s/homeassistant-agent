const loading = document.getElementById("loading");
const actionsList = document.getElementById("actions-list");
const errorBanner = document.getElementById("error-banner");
const automationsSection = document.getElementById("automations-section");
const automationsList = document.getElementById("automations-list");
const scriptsSection = document.getElementById("scripts-section");
const scriptsList = document.getElementById("scripts-list");

function showError(msg) {
  errorBanner.textContent = msg;
  errorBanner.hidden = false;
}

function buildRow(action) {
  const li = document.createElement("li");
  li.className = "action-row" + (action.enabled ? "" : " disabled");
  li.dataset.entityId = action.entity_id;

  const name = document.createElement("span");
  name.className = "action-name";
  name.textContent = action.name || action.entity_id;

  const label = document.createElement("label");
  label.className = "toggle";
  label.title = action.enabled ? "Disable" : "Enable";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = action.enabled;
  checkbox.addEventListener("change", () => onToggle(action, checkbox, li));

  const track = document.createElement("span");
  track.className = "toggle-track";

  label.append(checkbox, track);
  li.append(name, label);
  return li;
}

async function onToggle(action, checkbox, row) {
  checkbox.disabled = true;
  const newEnabled = checkbox.checked;
  try {
    const resp = await fetch(`api/actions/${action.entity_id}/toggle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: newEnabled }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    action.enabled = newEnabled;
    row.classList.toggle("disabled", !newEnabled);
  } catch (e) {
    showError(`Failed to toggle ${action.name || action.entity_id}: ${e.message}`);
    checkbox.checked = !newEnabled;
  } finally {
    checkbox.disabled = false;
  }
}

async function load() {
  try {
    const resp = await fetch("api/actions");
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    const { actions } = await resp.json();

    const automations = actions.filter(a => a.type === "automation");
    const scripts = actions.filter(a => a.type === "script");

    if (automations.length > 0) {
      automations.forEach(a => automationsList.append(buildRow(a)));
      automationsSection.hidden = false;
    }
    if (scripts.length > 0) {
      scripts.forEach(a => scriptsList.append(buildRow(a)));
      scriptsSection.hidden = false;
    }

    loading.hidden = true;
    actionsList.hidden = false;
  } catch (e) {
    loading.hidden = true;
    showError(`Could not load actions: ${e.message}`);
  }
}

load();
