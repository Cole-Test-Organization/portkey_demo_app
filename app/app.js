"use strict";

const experienceSlots = [
  {
    style: "sales",
    copy: "Bright, fast, and ready to move.",
  },
  {
    style: "hr",
    copy: "Warm, thoughtful, and people-first.",
  },
  {
    style: "devs",
    copy: "Focused, technical, and built for depth.",
  },
];

const roles = {
  sales: {
    label: "Sales",
    description: "Fast, economical help for high-volume customer work.",
    model: "@gemini-example/gemini-3.5-flash-lite",
    experience: experienceSlots[0],
  },
  hr: {
    label: "HR",
    description: "Balanced reasoning for people and policy workflows.",
    model: "@gemini-example/gemini-3.6-flash",
    experience: experienceSlots[1],
  },
  devs: {
    label: "Devs",
    description: "Maximum capability for engineering and complex analysis.",
    model: "@gemini-example/gemini-3.5-flash",
    experience: experienceSlots[2],
  },
};

const state = {
  role: "sales",
  configured: false,
  providerSlug: "gemini-example",
  maxPromptChars: 32000,
  running: false,
  controller: null,
};

const elements = {
  status: document.querySelector("#service-status"),
  profileSwitcher: document.querySelector("#profile-switcher"),
  profileTrigger: document.querySelector("#profile-trigger"),
  profileAvatar: document.querySelector("#profile-avatar"),
  profileRole: document.querySelector("#profile-role"),
  profileMenu: document.querySelector("#profile-menu"),
  profileOptions: document.querySelector("#profile-options"),
  selectedRole: document.querySelector("#selected-role"),
  experienceCopy: document.querySelector("#experience-copy"),
  expectedModel: document.querySelector("#expected-model"),
  prompt: document.querySelector("#prompt"),
  promptCount: document.querySelector("#prompt-count"),
  promptLimit: document.querySelector("#prompt-limit"),
  clear: document.querySelector("#clear-button"),
  send: document.querySelector("#send-button"),
  response: document.querySelector("#response"),
  loadingTemplate: document.querySelector("#loading-template"),
  themeToggle: document.querySelector("#theme-toggle"),
  setupButton: document.querySelector("#setup-button"),
  setupDialog: document.querySelector("#setup-dialog"),
  setupForm: document.querySelector("#setup-form"),
  setupClose: document.querySelector("#setup-close"),
  setupCancel: document.querySelector("#setup-cancel"),
  setupSave: document.querySelector("#setup-save"),
  setupServiceKey: document.querySelector("#setup-service-key"),
  setupProviderSlug: document.querySelector("#setup-provider-slug"),
  setupRoleGroups: [...document.querySelectorAll(".role-setup")],
  setupMessage: document.querySelector("#setup-message"),
  routingOutput: document.querySelector("#routing-output"),
  routingJson: document.querySelector("#routing-json"),
  copyRoutingJson: document.querySelector("#copy-routing-json"),
};

let savedRole = null;
try {
  savedRole = window.localStorage.getItem("ai-role-demo-role");
} catch {
  // Local storage is optional.
}

function setStatus(kind, copy) {
  elements.status.classList.remove("ready", "warning", "error");
  if (kind) elements.status.classList.add(kind);
  elements.status.querySelector("span:last-child").textContent = copy;
}

function setProfileMenu(open) {
  elements.profileMenu.hidden = !open;
  elements.profileTrigger.setAttribute("aria-expanded", String(open));
}

function roleInitials(label) {
  const words = String(label).trim().split(/\s+/).filter(Boolean);
  return words
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase() || "AI";
}

function renderProfileMenu() {
  const fragment = document.createDocumentFragment();

  for (const [role, profile] of Object.entries(roles)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "profile-option";
    button.dataset.role = role;
    button.dataset.style = profile.experience.style;
    button.setAttribute("role", "menuitemradio");
    button.setAttribute("aria-checked", String(role === state.role));

    const avatar = document.createElement("span");
    avatar.className = "profile-avatar";
    avatar.textContent = roleInitials(profile.label);

    const copy = document.createElement("span");
    copy.className = "profile-copy";
    const roleLabel = document.createElement("strong");
    roleLabel.textContent = profile.label;
    copy.append(roleLabel);

    const check = document.createElement("span");
    check.className = "profile-check";
    check.textContent = "✓";
    check.setAttribute("aria-hidden", "true");

    button.append(avatar, copy, check);
    button.addEventListener("click", () => {
      selectRole(role);
      setProfileMenu(false);
      elements.profileTrigger.focus();
    });
    fragment.append(button);
  }

  elements.profileOptions.replaceChildren(fragment);
}

function selectRole(role) {
  if (!roles[role]) return;
  state.role = role;
  const profile = roles[role];

  document.documentElement.dataset.roleStyle = profile.experience.style;
  elements.profileAvatar.textContent = roleInitials(profile.label);
  elements.profileRole.textContent = profile.label;
  elements.profileTrigger.setAttribute(
    "aria-label",
    `${profile.label}. Switch role`,
  );
  elements.selectedRole.textContent = profile.label;
  elements.experienceCopy.textContent = profile.experience.copy;
  elements.expectedModel.textContent = profile.model || "Gateway managed";
  renderProfileMenu();

  try {
    window.localStorage.setItem("ai-role-demo-role", role);
  } catch {
    // Role selection still works without storage.
  }
}

function configureRoles(configuredRoles) {
  const entries = Object.entries(configuredRoles || {}).slice(0, 3);
  if (entries.length !== 3) return;

  for (const role of Object.keys(roles)) delete roles[role];

  entries.forEach(([role, profile], index) => {
    roles[role] = {
      label: profile?.label || role,
      description: profile?.description || "",
      model: profile?.model || "Gateway managed",
      experience: experienceSlots[index],
    };
  });

  const nextRole =
    (savedRole && roles[savedRole] && savedRole) ||
    (roles[state.role] && state.role) ||
    entries[0][0];
  selectRole(nextRole);
}

function applyPublicConfig(config) {
  state.configured = Boolean(config.configured);
  state.providerSlug = config.providerSlug || "gemini-example";
  state.maxPromptChars = Number(config.maxPromptChars) || 32000;
  elements.prompt.maxLength = state.maxPromptChars;
  if (elements.promptLimit) {
    elements.promptLimit.textContent = state.maxPromptChars.toLocaleString();
  }

  configureRoles(config.roles);

  if (state.configured) {
    setStatus("ready", "Ready");
  } else {
    setStatus("warning", "Setup required");
  }
}

function updateCount() {
  elements.promptCount.textContent =
    elements.prompt.value.length.toLocaleString();
}

function setRunning(isRunning) {
  state.running = isRunning;
  elements.send.disabled = isRunning;
  elements.profileTrigger.disabled = isRunning;
  for (const button of elements.profileOptions.querySelectorAll("button")) {
    button.disabled = isRunning;
  }
  elements.send.textContent = isRunning ? "Sending…" : "Send";
}

function renderLoading() {
  elements.response.replaceChildren(
    elements.loadingTemplate.content.cloneNode(true),
  );
}

function renderError(message) {
  const box = document.createElement("div");
  box.className = "error-state";
  box.textContent = message;
  elements.response.replaceChildren(box);
}

function renderResponse(payload, requestedRole) {
  const result = document.createElement("div");
  result.className = "response-result";

  const meta = document.createElement("div");
  meta.className = "response-meta";

  const role = document.createElement("strong");
  role.textContent = roles[requestedRole]?.label || requestedRole;
  meta.append(role);

  if (payload.model) {
    const model = document.createElement("span");
    model.textContent = `Actual · ${payload.model}`;
    meta.append(model);
  }

  if (Number.isFinite(payload.elapsedMs)) {
    const elapsed = document.createElement("span");
    elapsed.textContent = `${(payload.elapsedMs / 1000).toFixed(2)}s`;
    meta.append(elapsed);
  }

  const totalTokens = payload.usage?.total_tokens;
  if (Number.isFinite(totalTokens)) {
    const usage = document.createElement("span");
    usage.textContent = `${totalTokens.toLocaleString()} tokens`;
    meta.append(usage);
  }

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "copy-button";
  copy.textContent = "Copy";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(payload.content);
      copy.textContent = "Copied";
      window.setTimeout(() => {
        copy.textContent = "Copy";
      }, 1200);
    } catch {
      copy.textContent = "Copy failed";
    }
  });
  meta.append(copy);

  const content = document.createElement("pre");
  content.className = "response-content";
  content.textContent = payload.content;

  result.append(meta, content);
  elements.response.replaceChildren(result);
}

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    throw new Error(`Server returned HTTP ${response.status}.`);
  }
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed with HTTP ${response.status}.`);
  }
  return payload;
}

async function runPrompt() {
  const prompt = elements.prompt.value.trim();
  if (!prompt) {
    renderError("Add a prompt before sending.");
    elements.prompt.focus();
    return;
  }
  if (prompt.length > state.maxPromptChars) {
    renderError(
      `Prompt exceeds the ${state.maxPromptChars.toLocaleString()}-character limit.`,
    );
    return;
  }
  if (!state.configured) {
    renderError("Complete Setup before sending a prompt.");
    openSetup();
    return;
  }

  state.controller?.abort();
  const controller = new AbortController();
  const requestedRole = state.role;
  state.controller = controller;
  setRunning(true);
  renderLoading();

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: requestedRole, prompt }),
      signal: controller.signal,
    });
    renderResponse(await parseResponse(response), requestedRole);
  } catch (error) {
    if (error.name !== "AbortError") {
      renderError(error.message || "The request could not be completed.");
    }
  } finally {
    if (state.controller === controller) {
      state.controller = null;
      setRunning(false);
    }
  }
}

function baseModel(model) {
  if (typeof model !== "string") return "";
  return model.startsWith("@") && model.includes("/")
    ? model.split("/", 2)[1]
    : model;
}

function fillSetupForm() {
  elements.setupProviderSlug.value = state.providerSlug;
  Object.values(roles).forEach((profile, index) => {
    const group = elements.setupRoleGroups[index];
    group.querySelector(".setup-role-label").value = profile.label;
    group.querySelector(".setup-role-model").value = baseModel(profile.model);
  });
}

function showSetupMessage(kind, message) {
  elements.setupMessage.hidden = false;
  elements.setupMessage.classList.remove("success", "error");
  elements.setupMessage.classList.add(kind);
  elements.setupMessage.textContent = message;
}

function openSetup() {
  fillSetupForm();
  elements.setupMessage.hidden = true;
  elements.routingOutput.hidden = true;
  if (!elements.setupDialog.open) elements.setupDialog.showModal();
  window.setTimeout(() => elements.setupProviderSlug.focus(), 0);
}

function closeSetup() {
  elements.setupDialog.close();
}

async function saveSetup(event) {
  event.preventDefault();
  if (!elements.setupForm.reportValidity()) return;

  const payload = {
    serviceKey: elements.setupServiceKey.value,
    providerSlug: elements.setupProviderSlug.value,
    roles: elements.setupRoleGroups.map((group) => ({
      label: group.querySelector(".setup-role-label").value,
      model: group.querySelector(".setup-role-model").value,
    })),
  };

  elements.setupSave.disabled = true;
  elements.setupSave.textContent = "Saving…";
  elements.setupMessage.hidden = true;

  try {
    const response = await fetch("/api/onboarding", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await parseResponse(response);
    applyPublicConfig(result.config);
    elements.routingJson.value = JSON.stringify(result.routingConfig, null, 2);
    elements.routingOutput.hidden = false;
    elements.setupServiceKey.value = "";
    showSetupMessage(
      "success",
      "Saved. Copy the generated JSON into your saved routing configuration.",
    );
  } catch (error) {
    showSetupMessage("error", error.message || "Setup could not be saved.");
  } finally {
    elements.setupSave.disabled = false;
    elements.setupSave.textContent = "Save setup";
  }
}

function applyTheme(theme) {
  const selected = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = selected;
  const next = selected === "dark" ? "light" : "dark";
  elements.themeToggle.setAttribute("aria-label", `Switch to ${next} mode`);
  try {
    window.localStorage.setItem("ai-role-demo-theme", selected);
  } catch {
    // Theme still applies for this page view.
  }
}

function initialTheme() {
  try {
    const saved = window.localStorage.getItem("ai-role-demo-theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // Use the operating-system preference.
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

async function loadConfig() {
  try {
    const response = await fetch("/api/config", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    const config = await parseResponse(response);
    applyPublicConfig(config);
    if (!state.configured) openSetup();
  } catch {
    setStatus("error", "Service unavailable");
  }
}

elements.prompt.addEventListener("input", updateCount);
elements.prompt.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    runPrompt();
  }
});
elements.clear.addEventListener("click", () => {
  elements.prompt.value = "";
  updateCount();
  elements.prompt.focus();
});
elements.send.addEventListener("click", runPrompt);

elements.themeToggle.addEventListener("click", () => {
  applyTheme(
    document.documentElement.dataset.theme === "dark" ? "light" : "dark",
  );
});
elements.profileTrigger.addEventListener("click", (event) => {
  event.stopPropagation();
  setProfileMenu(elements.profileMenu.hidden);
});
elements.profileMenu.addEventListener("click", (event) => {
  event.stopPropagation();
});
document.addEventListener("click", () => setProfileMenu(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !elements.profileMenu.hidden) {
    setProfileMenu(false);
    elements.profileTrigger.focus();
  }
});
elements.setupButton.addEventListener("click", openSetup);
elements.setupClose.addEventListener("click", closeSetup);
elements.setupCancel.addEventListener("click", closeSetup);
elements.setupForm.addEventListener("submit", saveSetup);
elements.copyRoutingJson.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(elements.routingJson.value);
    elements.copyRoutingJson.textContent = "Copied";
    window.setTimeout(() => {
      elements.copyRoutingJson.textContent = "Copy JSON";
    }, 1200);
  } catch {
    showSetupMessage("error", "Could not copy automatically. Select the JSON manually.");
  }
});

applyTheme(initialTheme());
selectRole(roles[savedRole] ? savedRole : "sales");
updateCount();
loadConfig();
