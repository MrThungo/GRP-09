// Vanilla JS — replaces the old React islands.

// --- Fast internal navigation: prefetch + instant document swap ---
(function () {
  // Full document swaps briefly expose an unstyled white page while CSS reloads.
  // Keep normal browser navigation for stable dark page transitions.
  const ENABLE_FAST_NAV = false;
  if (!ENABLE_FAST_NAV) return;
  if (!("fetch" in window) || !("history" in window)) return;

  const cache = new Map();
  const TTL = 45000;
  const MAX_ENTRIES = 18;
  let inFlightUrl = "";

  function now() {
    return Date.now();
  }

  function sameOrigin(url) {
    return url.origin === window.location.origin;
  }

  function isModifiedClick(event) {
    return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0;
  }

  function cleanCache() {
    const current = now();
    for (const [url, entry] of cache.entries()) {
      if (current - entry.time > TTL) cache.delete(url);
    }
    while (cache.size > MAX_ENTRIES) {
      cache.delete(cache.keys().next().value);
    }
  }

  function eligibleLink(anchor) {
    if (!anchor || !anchor.href) return null;
    if (anchor.target && anchor.target !== "_self") return null;
    if (anchor.hasAttribute("download") || anchor.dataset.noFastNav !== undefined) return null;
    const url = new URL(anchor.href, window.location.href);
    if (!sameOrigin(url)) return null;
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.pathname === window.location.pathname && url.search === window.location.search) return null;
    if (url.hash && url.pathname === window.location.pathname) return null;
    if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/static/")) return null;
    if (/\.(pdf|csv|xlsx?|docx?|zip|png|jpe?g|gif|webp)$/i.test(url.pathname)) return null;
    return url;
  }

  async function fetchPage(url) {
    cleanCache();
    const key = url.href;
    const cached = cache.get(key);
    if (cached && now() - cached.time <= TTL) return cached;

    const response = await fetch(key, {
      credentials: "same-origin",
      headers: {
        "Accept": "text/html,application/xhtml+xml",
        "X-Fast-Nav": "1",
      },
    });
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok || !contentType.includes("text/html")) {
      throw new Error("Page is not available for fast navigation.");
    }
    const html = await response.text();
    const finalUrl = response.url || key;
    const entry = { html, url: finalUrl, time: now() };
    cache.set(key, entry);
    if (finalUrl !== key) cache.set(finalUrl, entry);
    return entry;
  }

  function setLoading(loading) {
    document.documentElement.classList.toggle("fast-nav-loading", loading);
    document.body?.classList.toggle("fast-nav-loading", loading);
  }

  function swapDocument(entry) {
    window.history.pushState({ fastNav: true }, "", entry.url);
    document.open();
    document.write(entry.html);
    document.close();
  }

  function prefetch(anchor) {
    const url = eligibleLink(anchor);
    if (!url || cache.has(url.href)) return;
    fetchPage(url).catch(() => {});
  }

  document.addEventListener("pointerover", event => {
    const anchor = event.target.closest && event.target.closest("a[href]");
    if (anchor) prefetch(anchor);
  }, { passive: true });

  document.addEventListener("focusin", event => {
    const anchor = event.target.closest && event.target.closest("a[href]");
    if (anchor) prefetch(anchor);
  });

  document.addEventListener("touchstart", event => {
    const anchor = event.target.closest && event.target.closest("a[href]");
    if (anchor) prefetch(anchor);
  }, { passive: true });

  document.addEventListener("click", async event => {
    const anchor = event.target.closest && event.target.closest("a[href]");
    const url = eligibleLink(anchor);
    if (!url || isModifiedClick(event)) return;

    event.preventDefault();
    if (inFlightUrl === url.href) return;
    inFlightUrl = url.href;
    setLoading(true);
    try {
      const entry = await fetchPage(url);
      swapDocument(entry);
    } catch (_) {
      window.location.assign(url.href);
    } finally {
      inFlightUrl = "";
      setLoading(false);
    }
  });

  window.addEventListener("popstate", () => {
    window.location.reload();
  });
})();

// --- Password mask/unmask buttons ---
document.querySelectorAll("[data-toggle-password]").forEach(button => {
  button.addEventListener("click", () => {
    const wrap = button.closest("div");
    const field = wrap && wrap.querySelector("[data-password-field]");
    if (!field) return;
    const showing = field.type === "text";
    field.type = showing ? "password" : "text";
    button.textContent = showing ? "Show" : "Hide";
  });
});

// --- Password change validation ---
(function () {
  const forms = document.querySelectorAll("[data-password-change-form]");
  if (!forms.length) return;

  const ruleMessages = {
    length: "Password must be at least 8 characters.",
    uppercase: "Password must include at least one uppercase letter.",
    number: "Password must include at least one number.",
    special: "Password must include at least one special character.",
    match: "New passwords do not match.",
    different: "New password must be different from the current password.",
  };

  function checks(newPassword, confirmPassword, currentPassword, requireDifferent) {
    return {
      length: newPassword.length >= 8,
      uppercase: /[A-Z]/.test(newPassword),
      number: /\d/.test(newPassword),
      special: /[^A-Za-z0-9]/.test(newPassword),
      match: Boolean(confirmPassword) && newPassword === confirmPassword,
      different: Boolean(newPassword) && (!requireDifferent || !currentPassword || newPassword !== currentPassword),
    };
  }

  function firstInvalid(result) {
    return ["length", "uppercase", "number", "special", "match", "different"]
      .find(rule => !result[rule]);
  }

  function setRuleState(form, result) {
    Object.entries(result).forEach(([rule, ok]) => {
      form.querySelectorAll(`[data-password-rule="${rule}"]`).forEach(item => {
        item.classList.toggle("text-emerald-300", ok);
        item.classList.toggle("text-slate-500", !ok);
        item.textContent = `${ok ? "OK - " : ""}${item.textContent.replace(/^OK - /, "")}`;
      });
    });
  }

  function setMessage(form, message) {
    const messageEl = form.querySelector("[data-password-validation-message]");
    if (!messageEl) return;
    messageEl.textContent = message || "";
    messageEl.classList.toggle("hidden", !message);
  }

  function validate(form, showMessage) {
    const currentField = form.querySelector("[data-current-password-input], [name='current_password']");
    const newField = form.querySelector("[data-new-password-input], [name='new_password']");
    const confirmField = form.querySelector("[data-confirm-password-input], [name='confirm_password']");
    const submit = form.querySelector("[data-password-submit]");
    if (!newField || !confirmField) return true;

    const currentPassword = currentField ? currentField.value : "";
    const newPassword = newField.value || "";
    const confirmPassword = confirmField.value || "";
    const requireDifferent = form.dataset.requireDifferentPassword === "1";
    const result = checks(newPassword, confirmPassword, currentPassword, requireDifferent);
    const invalidRule = firstInvalid(result);
    const message = invalidRule ? ruleMessages[invalidRule] : "";

    setRuleState(form, result);
    newField.setCustomValidity(
      invalidRule && invalidRule !== "match" ? message : ""
    );
    confirmField.setCustomValidity(
      invalidRule === "match" ? message : ""
    );
    if (submit) submit.setAttribute("aria-disabled", invalidRule ? "true" : "false");
    setMessage(form, showMessage ? message : "");
    return !invalidRule;
  }

  forms.forEach(form => {
    ["input", "change"].forEach(eventName => {
      form.addEventListener(eventName, () => validate(form, false));
    });
    form.addEventListener("submit", event => {
      if (!validate(form, true)) {
        event.preventDefault();
        event.stopPropagation();
        const firstField = form.querySelector("[data-new-password-input], [name='new_password']");
        if (firstField && typeof firstField.reportValidity === "function") {
          firstField.reportValidity();
        }
      }
    });
    validate(form, false);
  });
})();

// --- Flash messages and modal confirmations ---
(function () {
  function flashRegion() {
    let region = document.querySelector("[data-js-flash-region]");
    if (region) return region;

    const host = document.querySelector(".page-container, .auth-card, main") || document.body;
    region = document.createElement("div");
    region.setAttribute("data-js-flash-region", "");
    host.insertBefore(region, host.firstChild);
    return region;
  }

  function flashTone(category) {
    if (category === "error") return "bg-red-950/60 text-red-100 border border-red-800/80";
    if (category === "success") return "bg-emerald-950/60 text-emerald-100 border border-emerald-800/80";
    if (category === "warning") return "bg-amber-950/60 text-amber-100 border border-amber-800/80";
    return "bg-slate-900/80 text-slate-200 border border-slate-800";
  }

  function showFlash(message, category = "info", actions = []) {
    const region = flashRegion();
    region.querySelectorAll("[data-js-flash]").forEach(item => item.remove());

    const flash = document.createElement("div");
    flash.className = `flash-message mb-4 ${flashTone(category)}`;
    flash.setAttribute("data-js-flash", "");

    const body = document.createElement("div");
    body.textContent = message;
    flash.appendChild(body);

    if (actions.length) {
      const actionRow = document.createElement("div");
      actionRow.className = "mt-3 flex flex-wrap gap-2";
      actions.forEach(action => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = action.className || "btn-secondary px-3 py-1.5 text-xs";
        button.textContent = action.label;
        button.addEventListener("click", () => {
          if (action.dismiss !== false) flash.remove();
          if (typeof action.onClick === "function") action.onClick();
        });
        actionRow.appendChild(button);
      });
      flash.appendChild(actionRow);
    }

    region.prepend(flash);
    flash.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return flash;
  }

  window.showFlash = showFlash;

  function confirmLabel(message) {
    const text = String(message || "").trim().toLowerCase();
    if (text.startsWith("restore")) return "Restore";
    if (text.startsWith("revoke")) return "Revoke";
    if (text.startsWith("deactivate")) return "Deactivate";
    if (text.startsWith("delete") || text.startsWith("permanently delete")) return "Delete";
    return "Confirm";
  }

  function isDestructive(label) {
    const text = String(label || "").toLowerCase();
    return text.startsWith("delete") || ["revoke", "deactivate"].includes(text);
  }

  function closeConfirmModal(modal, previousFocus) {
    document.body.classList.remove("confirm-modal-open");
    modal.remove();
    if (previousFocus && typeof previousFocus.focus === "function") {
      previousFocus.focus();
    }
  }

  function showConfirmModal({ message, onConfirm, label }) {
    document.querySelectorAll("[data-confirm-modal]").forEach(item => item.remove());

    const previousFocus = document.activeElement;
    const actionLabel = label || confirmLabel(message);
    const modal = document.createElement("div");
    modal.className = "confirm-modal-backdrop";
    modal.setAttribute("data-confirm-modal", "");
    modal.setAttribute("role", "presentation");

    const dialog = document.createElement("div");
    dialog.className = "confirm-modal";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "confirm-modal-title");
    dialog.setAttribute("aria-describedby", "confirm-modal-message");

    const title = document.createElement("div");
    title.id = "confirm-modal-title";
    title.className = "confirm-modal-title";
    title.textContent = "Confirm action";

    const body = document.createElement("div");
    body.id = "confirm-modal-message";
    body.className = "confirm-modal-message";
    body.textContent = message;

    const actions = document.createElement("div");
    actions.className = "confirm-modal-actions";

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn-secondary px-3 py-2 text-sm";
    cancel.textContent = "Cancel";

    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = `${isDestructive(actionLabel) ? "btn-danger" : "btn-primary"} px-3 py-2 text-sm`;
    confirm.textContent = actionLabel;

    actions.append(cancel, confirm);
    dialog.append(title, body, actions);
    modal.appendChild(dialog);
    document.body.appendChild(modal);
    document.body.classList.add("confirm-modal-open");

    function close() {
      closeConfirmModal(modal, previousFocus);
      document.removeEventListener("keydown", onKeydown, true);
    }

    function onKeydown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    }

    cancel.addEventListener("click", close);
    modal.addEventListener("click", event => {
      if (event.target === modal) close();
    });
    confirm.addEventListener("click", () => {
      close();
      if (typeof onConfirm === "function") onConfirm();
    });
    document.addEventListener("keydown", onKeydown, true);
    cancel.focus();
  }

  document.addEventListener("submit", event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const message = form.dataset.confirmMessage;
    if (!message || form.dataset.confirmed === "true") return;

    event.preventDefault();
    showConfirmModal({
      message,
      label: form.dataset.confirmLabel,
      onConfirm: () => {
        form.dataset.confirmed = "true";
        if (form.requestSubmit) {
          form.requestSubmit();
        } else {
          form.submit();
        }
        setTimeout(() => {
          delete form.dataset.confirmed;
        }, 0);
      },
    });
  });
})();

// --- Topbar scroll state (landing page) ---
(function () {
  const bar = document.querySelector("[data-topbar]");
  if (!bar) return;
  const onScroll = () => bar.classList.toggle("is-scrolled", window.scrollY > 24);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
})();

// --- Responsive app navigation ---
(function () {
  const sidebar = document.querySelector("[data-app-sidebar]");
  const toggle = document.querySelector("[data-sidebar-toggle]");
  const overlay = document.querySelector("[data-sidebar-overlay]");
  if (!sidebar || !toggle || !overlay) return;

  const open = () => {
    document.body.classList.add("sidebar-open");
    toggle.setAttribute("aria-expanded", "true");
  };
  const close = () => {
    document.body.classList.remove("sidebar-open");
    toggle.setAttribute("aria-expanded", "false");
  };

  toggle.setAttribute("aria-expanded", "false");
  toggle.addEventListener("click", () => {
    document.body.classList.contains("sidebar-open") ? close() : open();
  });
  overlay.addEventListener("click", close);
  sidebar.querySelectorAll("a").forEach(link => link.addEventListener("click", close));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") close();
  });
})();

// --- Consent test selection ---
(function () {
  const groups = document.querySelectorAll("[data-consent-request]");
  if (!groups.length) return;

  groups.forEach((group) => {
    const all = group.querySelector("[data-consent-all]");
    const items = Array.from(group.querySelectorAll("[data-consent-item]"));
    if (!all || !items.length) return;

    function syncAll() {
      const checked = items.filter((item) => item.checked).length;
      all.checked = checked === items.length;
      all.indeterminate = checked > 0 && checked < items.length;
    }

    all.addEventListener("change", () => {
      items.forEach((item) => {
        item.checked = all.checked;
      });
      all.indeterminate = false;
    });
    items.forEach((item) => item.addEventListener("change", syncAll));
    syncAll();
  });
})();

// --- Notification bell ---
(function () {
  const root = document.querySelector("[data-notification-bell]");
  if (!root) return;
  root.innerHTML = `
    <button type="button" data-bell
      class="relative w-10 h-10 grid place-items-center rounded-lg border border-slate-700 bg-slate-900 hover:bg-slate-800">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
           fill="none" stroke="currentColor" stroke-width="2"
           class="w-5 h-5 text-slate-200">
        <path d="M15 17h5l-1.4-1.4A2 2 0 0 1 18 14V11a6 6 0 1 0-12 0v3a2 2 0 0 1-.6 1.4L4 17h5"/>
        <path d="M9 17a3 3 0 0 0 6 0"/>
      </svg>
      <span data-bell-dot class="hidden absolute top-1 right-1 w-2 h-2 rounded-full bg-rose-500"></span>
    </button>
    <div data-bell-panel
         class="notif-panel absolute right-4 top-12 w-80 max-h-[60vh] overflow-auto
                bg-slate-950 border border-slate-800 rounded-lg text-sm">
      <div class="px-4 py-3 border-b border-slate-800 font-medium">Notifications</div>
      <div data-bell-list class="divide-y divide-slate-800">
        <div class="px-4 py-6 text-slate-500 text-center">Loading…</div>
      </div>
    </div>`;

  const btn = root.querySelector("[data-bell]");
  const panel = root.querySelector("[data-bell-panel]");
  const list = root.querySelector("[data-bell-list]");
  const dot = root.querySelector("[data-bell-dot]");

  btn.addEventListener("click", () => panel.classList.toggle("open"));
  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) panel.classList.remove("open");
  });

  async function load() {
    try {
      const res = await fetch("/api/notifications", { credentials: "same-origin" });
      const items = await res.json();
      if (!Array.isArray(items) || items.length === 0) {
        list.innerHTML = `<div class="px-4 py-6 text-slate-500 text-center">No notifications.</div>`;
        dot.classList.add("hidden");
        return;
      }
      const unread = items.some(i => !i.read);
      dot.classList.toggle("hidden", !unread);
      list.innerHTML = items.map(n => `
        <a href="${n.link || "#"}" data-id="${n.id}"
           class="block px-4 py-3 hover:bg-slate-800 ${n.read ? "opacity-60" : ""}">
          <div class="font-medium">${escape(n.title)}</div>
          ${n.body ? `<div class="text-xs text-slate-400 mt-0.5">${escape(n.body)}</div>` : ""}
          <div class="text-[10px] text-slate-500 mt-1">${new Date(n.created_at).toLocaleString()}</div>
        </a>`).join("");
      list.querySelectorAll("a[data-id]").forEach(a => {
        a.addEventListener("click", () => {
          fetch(`/api/notifications/${a.dataset.id}/read`,
            { method: "POST", credentials: "same-origin" });
        });
      });
    } catch (e) {
      list.innerHTML = `<div class="px-4 py-6 text-rose-400 text-center">Failed to load.</div>`;
    }
  }
  function escape(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }
  load();
  setInterval(() => {
    if (!document.hidden) load();
  }, 30000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load();
  });
})();

// --- Floating portal assistant ---
(function () {
  const root = document.querySelector("[data-floating-chatbot]");
  if (!root) return;
  document.body?.classList.add("has-floating-chatbot");

  const toggle = root.querySelector("[data-floating-chatbot-toggle]");
  const closeButton = root.querySelector("[data-floating-chatbot-close]");
  const fullscreenButton = root.querySelector("[data-floating-chatbot-fullscreen]");
  const panel = root.querySelector("[data-floating-chatbot-panel]");
  const status = root.querySelector("[data-floating-chatbot-status]");
  const messages = root.querySelector("[data-floating-chatbot-messages]");
  const form = root.querySelector("[data-floating-chatbot-form]");
  const input = root.querySelector("[data-floating-chatbot-input]");
  const localEndpoint = root.dataset.localEndpoint;
  const twilioSessionEndpoint = root.dataset.twilioSessionEndpoint;
  const assistantName = root.dataset.assistantName || "NMB Lab";
  let twilioConversation = null;
  let twilioIdentity = "";
  let booted = false;

  function setFullscreen(fullscreen) {
    root.classList.toggle("fullscreen", fullscreen);
    document.body?.classList.toggle("chatbot-fullscreen-open", fullscreen && root.classList.contains("open"));
    if (fullscreenButton) {
      fullscreenButton.textContent = fullscreen ? "Exit" : "Full";
      fullscreenButton.setAttribute("aria-pressed", fullscreen ? "true" : "false");
      fullscreenButton.setAttribute(
        "aria-label",
        fullscreen ? "Exit assistant fullscreen" : "Expand assistant to fullscreen"
      );
    }
    scrollToBottom();
  }

  function setOpen(open) {
    root.classList.toggle("open", open);
    toggle?.setAttribute("aria-expanded", open ? "true" : "false");
    panel?.setAttribute("aria-hidden", open ? "false" : "true");
    if (!open) setFullscreen(false);
    document.body?.classList.toggle("chatbot-fullscreen-open", open && root.classList.contains("fullscreen"));
    if (open) {
      boot();
      setTimeout(() => input?.focus(), 80);
    }
  }

  function setStatus(text) {
    if (status) status.textContent = text || "";
  }

  function scrollToBottom() {
    if (messages) messages.scrollTop = messages.scrollHeight;
  }

  function appendMessage(author, body, links) {
    if (!messages) return;
    const isPatient = author === "You";
    const bubble = document.createElement("div");
    bubble.className = `chatbot-message ${isPatient ? "patient" : "assistant"}`;

    const label = document.createElement("div");
    label.className = "chatbot-message-label";
    label.textContent = author;
    bubble.appendChild(label);

    const text = document.createElement("div");
    text.className = "chatbot-message-body";
    text.textContent = body || "";
    bubble.appendChild(text);

    if (Array.isArray(links) && links.length) {
      const actions = document.createElement("div");
      actions.className = "chatbot-message-actions";
      links.forEach(link => {
        if (!link || !link.url || !link.label) return;
        const anchor = document.createElement("a");
        anchor.href = link.url;
        anchor.textContent = link.label;
        anchor.dataset.noFastNav = "";
        actions.appendChild(anchor);
      });
      bubble.appendChild(actions);
    }

    messages.appendChild(bubble);
    scrollToBottom();
  }

  function appendTwilioMessage(message) {
    const author = message.author === twilioIdentity ? "You" : assistantName;
    appendMessage(author, message.body || "", []);
  }

  async function bootTwilio() {
    if (!window.Twilio || !window.Twilio.Conversations || !twilioSessionEndpoint) {
      return false;
    }
    const response = await fetch(twilioSessionEndpoint, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "application/json" },
    });
    const session = await response.json();
    if (!response.ok || !session.configured) return false;

    twilioIdentity = session.identity || "";
    const client = new Twilio.Conversations.Client(session.token);
    await new Promise((resolve, reject) => {
      client.on("initialized", resolve);
      client.on("initFailed", reject);
    });
    twilioConversation = await client.getConversationBySid(session.conversation_sid);
    const page = await twilioConversation.getMessages(20);
    if (page.items.length) {
      messages.innerHTML = "";
      page.items.forEach(appendTwilioMessage);
    }
    twilioConversation.on("messageAdded", appendTwilioMessage);
    setStatus("Twilio Conversations");
    return true;
  }

  async function boot() {
    if (booted) return;
    booted = true;
    try {
      const ready = await bootTwilio();
      if (!ready) setStatus("Secure portal mode");
    } catch (_) {
      twilioConversation = null;
      setStatus("Secure portal mode");
    }
  }

  async function sendLocal(text) {
    appendMessage("You", text, []);
    const response = await fetch(localEndpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ message: text }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      appendMessage(assistantName, data.error || "I could not complete that task.", []);
      return;
    }
    appendMessage(assistantName, data.reply || "Done.", data.links || []);
  }

  async function sendMessage(text) {
    if (twilioConversation) {
      await twilioConversation.sendMessage(text);
      return;
    }
    await sendLocal(text);
  }

  toggle?.addEventListener("click", () => setOpen(!root.classList.contains("open")));
  fullscreenButton?.addEventListener("click", () => {
    setFullscreen(!root.classList.contains("fullscreen"));
    setTimeout(() => input?.focus(), 80);
  });
  closeButton?.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && root.classList.contains("open")) {
      if (root.classList.contains("fullscreen")) {
        setFullscreen(false);
      } else {
        setOpen(false);
      }
    }
  });

  root.querySelectorAll("[data-floating-chatbot-prompt]").forEach(button => {
    button.addEventListener("click", () => {
      input.value = button.dataset.floatingChatbotPrompt || "";
      form?.requestSubmit();
    });
  });

  form?.addEventListener("submit", async event => {
    event.preventDefault();
    const text = (input?.value || "").trim();
    if (!text) return;
    input.value = "";
    input.disabled = true;
    try {
      await boot();
      await sendMessage(text);
    } catch (_) {
      appendMessage(assistantName, "I could not complete that task. Please try again.", []);
    } finally {
      input.disabled = false;
      input.focus();
    }
  });
})();

// --- Live messages ---
(function () {
  const root = document.querySelector("[data-chat-root]");
  if (!root) return;

  const contactsRoot = root.querySelector("[data-chat-contacts]");
  const emptyState = root.querySelector("[data-chat-empty]");
  const chatWindow = root.querySelector("[data-chat-window]");
  const thread = root.querySelector("[data-chat-thread]");
  const form = root.querySelector("[data-chat-form]");
  const input = root.querySelector("[data-chat-input]");
  const status = root.querySelector("[data-chat-status]");
  const headerName = root.querySelector("[data-chat-header-name]");
  const headerPresence = root.querySelector("[data-chat-header-presence]");
  const headerPresenceDot = root.querySelector("[data-chat-header-presence-dot]");
  const sendButton = form && form.querySelector("button[type='submit']");

  let selectedId = root.dataset.initialUser || "";
  let lastSignature = "";
  let loadingThread = false;

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, char => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function contactLinks() {
    return Array.from(root.querySelectorAll("[data-chat-contact]"));
  }

  function findByData(name, id) {
    return Array.from(root.querySelectorAll(`[${name}]`))
      .find(item => item.getAttribute(name) === id);
  }

  function setStatus(text) {
    if (status) status.textContent = text || "";
  }

  function setWindowVisible(visible) {
    emptyState?.classList.toggle("hidden", visible);
    chatWindow?.classList.toggle("hidden", !visible);
  }

  function setActiveContact() {
    contactLinks().forEach(link => {
      const active = link.dataset.userId === selectedId;
      link.classList.toggle("border-sky-700", active);
      link.classList.toggle("bg-slate-900", active);
      link.classList.toggle("border-transparent", !active);
    });
  }

  function updateUnreadBadge(id, count) {
    const badge = findByData("data-chat-unread", id);
    if (!badge) return;
    const unread = id === selectedId ? 0 : Number(count || 0);
    badge.textContent = unread > 99 ? "99+" : String(unread);
    badge.classList.toggle("hidden", unread <= 0);
  }

  function updatePresence(id, online, label) {
    const presence = findByData("data-chat-presence", id);
    const dot = findByData("data-chat-presence-dot", id);
    if (presence) presence.textContent = label || "Offline";
    if (dot) {
      dot.classList.toggle("bg-emerald-400", Boolean(online));
      dot.classList.toggle("bg-slate-600", !online);
    }
  }

  function updateHeader(contact) {
    if (!contact) return;
    if (headerName) headerName.textContent = contact.name || "";
    if (headerPresence) headerPresence.textContent = contact.presence_label || "Offline";
    if (headerPresenceDot) {
      headerPresenceDot.classList.toggle("bg-emerald-400", Boolean(contact.online));
      headerPresenceDot.classList.toggle("bg-slate-600", !contact.online);
    }
  }

  function messageHtml(message) {
    const mine = Boolean(message.mine);
    const bubble = mine
      ? "bg-sky-700 text-white"
      : "border border-slate-800 bg-slate-900 text-slate-100";
    const meta = mine ? "text-sky-100/80" : "text-slate-500";
    const readState = mine ? ` - ${message.read ? "Read" : "Sent"}` : "";
    return `
      <div class="flex ${mine ? "justify-end" : "justify-start"}">
        <div class="max-w-[82%] rounded-lg px-3 py-2 ${bubble}">
          <div class="whitespace-pre-wrap break-words text-sm leading-5">${escapeHtml(message.body)}</div>
          <div class="mt-1 text-[10px] ${meta}">${escapeHtml(message.created_label)}${readState}</div>
        </div>
      </div>`;
  }

  function renderMessages(messages, silent) {
    const signature = messages.map(message => `${message.id}:${message.read_at || ""}`).join("|");
    if (signature === lastSignature && silent) return;

    if (!messages.length) {
      thread.innerHTML = `<div class="py-10 text-center text-sm text-slate-500">No messages yet. Start the conversation.</div>`;
      lastSignature = signature;
      return;
    }

    const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 96;
    thread.innerHTML = messages.map(messageHtml).join("");
    if (!silent || nearBottom || signature !== lastSignature) {
      thread.scrollTop = thread.scrollHeight;
    }
    lastSignature = signature;
  }

  async function loadContacts() {
    try {
      const response = await fetch("/messages/api/contacts", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) return;
      const data = await response.json();
      (data.contacts || []).forEach(contact => {
        updateUnreadBadge(contact.id, contact.unread_count);
        updatePresence(contact.id, contact.online, contact.presence_label);
        if (contact.id === selectedId) updateHeader(contact);
      });
    } catch (_) {}
  }

  async function loadThread(silent = false) {
    if (!selectedId || loadingThread) return;
    loadingThread = true;
    try {
      const response = await fetch(`/messages/api/thread/${encodeURIComponent(selectedId)}`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        if (!silent && window.showFlash) window.showFlash("This conversation is no longer available.", "error");
        return;
      }
      const data = await response.json();
      updateHeader(data.contact);
      renderMessages(data.messages || [], silent);
      updateUnreadBadge(selectedId, 0);
      setStatus("Updated just now");
    } catch (_) {
      if (!silent && window.showFlash) window.showFlash("Messages could not be loaded.", "error");
    } finally {
      loadingThread = false;
    }
  }

  function selectContact(id, updateUrl = true) {
    if (!id) return;
    selectedId = id;
    lastSignature = "";
    root.dataset.initialUser = id;
    setWindowVisible(true);
    setActiveContact();
    updateUnreadBadge(id, 0);
    if (thread) {
      thread.innerHTML = `<div class="py-10 text-center text-sm text-slate-500">Loading conversation...</div>`;
    }
    if (updateUrl) {
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set("with", id);
      window.history.replaceState(null, "", nextUrl);
    }
    loadThread(false);
    input?.focus();
  }

  contactsRoot?.addEventListener("click", event => {
    const link = event.target.closest("[data-chat-contact]");
    if (!link) return;
    event.preventDefault();
    selectContact(link.dataset.userId);
  });

  form?.addEventListener("submit", async event => {
    event.preventDefault();
    if (!selectedId || !input) return;

    const body = input.value.trim();
    if (!body) return;

    if (sendButton) sendButton.disabled = true;
    try {
      const response = await fetch(`/messages/api/thread/${encodeURIComponent(selectedId)}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (window.showFlash) window.showFlash(data.error || "Message could not be sent.", "error");
        return;
      }
      input.value = "";
      await loadThread(false);
      await loadContacts();
    } catch (_) {
      if (window.showFlash) window.showFlash("Message could not be sent.", "error");
    } finally {
      if (sendButton) sendButton.disabled = false;
      input?.focus();
    }
  });

  input?.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form?.requestSubmit();
    }
  });

  setWindowVisible(Boolean(selectedId));
  setActiveContact();
  if (selectedId) loadThread(false);
  loadContacts();

  setInterval(() => {
    if (!document.hidden && selectedId) loadThread(true);
  }, 5000);
  setInterval(() => {
    if (!document.hidden) loadContacts();
  }, 10000);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    loadContacts();
    if (selectedId) loadThread(true);
  });
})();

// --- Blocked-user heartbeat: instantly logs the user out + shows a modal alert ---
(function () {
  const modal = document.querySelector("[data-blocked-modal]");
  if (!modal) return;
  let triggered = false;
  async function check() {
    if (triggered) return;
    try {
      const r = await fetch("/api/me", { credentials: "same-origin", cache: "no-store" });
      if (r.status === 401) return; // not logged in
      const data = await r.json();
      if (data && data.blocked) {
        triggered = true;
        modal.classList.remove("hidden");
        setTimeout(async () => {
          await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
          window.location.href = "/auth/login";
        }, 1800);
      }
    } catch (e) { /* network blip */ }
  }
  check();
  setInterval(() => {
    if (!document.hidden) check();
  }, 15000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) check();
  });
})();

// --- South African ID date-of-birth autofill ---
(function () {
  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  function dobFromSaId(value) {
    const digits = String(value || "").replace(/\D/g, "");
    if (digits.length < 6) return "";

    const yy = Number(digits.slice(0, 2));
    const mm = Number(digits.slice(2, 4));
    const dd = Number(digits.slice(4, 6));
    const currentYear = new Date().getFullYear();
    const year = 2000 + yy <= currentYear ? 2000 + yy : 1900 + yy;
    const date = new Date(year, mm - 1, dd);

    if (
      date.getFullYear() !== year ||
      date.getMonth() !== mm - 1 ||
      date.getDate() !== dd
    ) {
      return "";
    }

    return `${year}-${pad2(mm)}-${pad2(dd)}`;
  }

  document.querySelectorAll("[data-sa-id-source]").forEach(idField => {
    const form = idField.closest("form");
    const dobField = form && form.querySelector("[data-sa-dob-target]");
    if (!dobField) return;
    let lastAutoDob = "";

    function updateDob() {
      const dob = dobFromSaId(idField.value);
      if (dob) {
        dobField.value = dob;
        lastAutoDob = dob;
      } else if (dobField.value === lastAutoDob) {
        dobField.value = "";
        lastAutoDob = "";
      }
    }

    idField.addEventListener("input", updateDob);
    idField.addEventListener("change", updateDob);
    idField.addEventListener("blur", updateDob);
    updateDob();
  });
})();

// --- Searchable dropdown multi-selects ---
(function () {
  document.querySelectorAll("[data-searchable-multi]").forEach(root => {
    const search = root.querySelector("[data-searchable-multi-search]");
    const summary = root.querySelector("[data-searchable-multi-summary]");
    const summaryText = root.querySelector("[data-searchable-multi-text]");
    const options = Array.from(root.querySelectorAll("[data-searchable-multi-option]"));
    const empty = root.querySelector("[data-searchable-multi-empty]");
    const details = root.querySelector("details");
    if (!summaryText || !summary) return;

    function selectedLabels() {
      return options
        .filter(option => option.querySelector("input[type='checkbox']")?.checked)
        .map(option => option.dataset.label || option.textContent.trim());
    }

    function updateSummary() {
      const labels = selectedLabels();
      if (labels.length === 0) {
        summaryText.textContent = summary.dataset.placeholder || "Select options";
      } else if (labels.length <= 2) {
        summaryText.textContent = labels.join(", ");
      } else {
        summaryText.textContent = `${labels.length} selected`;
      }
    }

    function filterOptions() {
      const q = (search?.value || "").trim().toLowerCase();
      let visible = 0;
      options.forEach(option => {
        const match = !q || (option.dataset.search || "").includes(q);
        option.hidden = !match;
        if (match) visible += 1;
      });
      if (empty) empty.classList.toggle("hidden", visible !== 0);
    }

    search?.addEventListener("input", filterOptions);
    root.querySelectorAll("input[type='checkbox']").forEach(input => {
      input.addEventListener("change", updateSummary);
    });
    details?.addEventListener("toggle", () => {
      if (!details.open) return;
      search?.focus();
      search?.select();
    });

    updateSummary();
    filterOptions();
  });
})();

// --- Result verification: reject only after details were viewed ---
(function () {
  document.querySelectorAll("[data-result-review]").forEach(details => {
    const card = details.closest("[data-result-card]");
    const form = card && card.querySelector("[data-result-review-form]");
    const viewedInput = form && form.querySelector("[data-viewed-result-input]");
    const returnButton = form && form.querySelector("[data-return-result-button]");
    if (!form || !viewedInput || !returnButton) return;

    function markViewed() {
      viewedInput.value = "1";
      returnButton.disabled = false;
      returnButton.removeAttribute("title");
    }

    details.addEventListener("toggle", () => {
      if (details.open) markViewed();
    });
    details.addEventListener("click", () => {
      if (details.open) markViewed();
    });
  });
})();

// --- Responsive charts ---
(function () {
  function applyChartDefaults() {
    if (typeof window.applyNmbChartDefaults === "function") {
      window.applyNmbChartDefaults();
    }
  }

  function baseHeight(canvas) {
    const requested = Number(canvas.getAttribute("height")) || 160;
    if (requested <= 110) return "clamp(15rem, 58vw, 20rem)";
    if (requested <= 140) return "clamp(16rem, 58vw, 22rem)";
    if (requested <= 180) return "clamp(17rem, 62vw, 24rem)";
    return "clamp(18rem, 66vw, 26rem)";
  }

  function chartForCanvas(canvas) {
    const Chart = window.Chart;
    if (!Chart) return null;
    if (typeof Chart.getChart === "function") return Chart.getChart(canvas) || null;
    if (Chart.instances) {
      return Object.values(Chart.instances).find(chart => chart.canvas === canvas) || null;
    }
    return null;
  }

  function normalizeCanvas(canvas) {
    if (!(canvas instanceof HTMLCanvasElement)) return;
    if (!canvas.id || !canvas.id.toLowerCase().includes("chart")) return;

    const chart = chartForCanvas(canvas);
    let frame = canvas.parentElement;
    if (!frame || !frame.classList.contains("chart-frame")) {
      if (chart) return;
      frame = document.createElement("div");
      frame.className = "chart-frame";
      canvas.parentNode.insertBefore(frame, canvas);
      frame.appendChild(canvas);
    }
    frame.style.setProperty("--chart-height", baseHeight(canvas));
  }

  function normalizeCharts() {
    applyChartDefaults();
    document.querySelectorAll('canvas[id*="Chart"]').forEach(normalizeCanvas);
  }

  applyChartDefaults();
  normalizeCharts();
  window.addEventListener("load", normalizeCharts);
  window.addEventListener("resize", () => {
    window.clearTimeout(normalizeCharts._resizeTimer);
    normalizeCharts._resizeTimer = window.setTimeout(normalizeCharts, 120);
  }, { passive: true });
  [80, 350, 900].forEach(delay => window.setTimeout(normalizeCharts, delay));

  if ("MutationObserver" in window) {
    let normalizeTimer;
    new MutationObserver(records => {
      let shouldNormalize = false;
      records.forEach(record => {
        record.addedNodes.forEach(node => {
          if (!(node instanceof Element)) return;
          if (node.matches?.('canvas[id*="Chart"]') || node.querySelector?.('canvas[id*="Chart"]')) {
            shouldNormalize = true;
          }
        });
      });
      if (!shouldNormalize) return;
      window.clearTimeout(normalizeTimer);
      normalizeTimer = window.setTimeout(normalizeCharts, 80);
    }).observe(document.body || document.documentElement, { childList: true, subtree: true });
  }

  window.normalizeResponsiveCharts = normalizeCharts;
})();

// --- Responsive table wrappers ---
(function () {
  function enhanceWrapper(wrapper) {
    wrapper.classList.add("responsive-table", "overflow-x-auto");
    if (!wrapper.hasAttribute("tabindex")) wrapper.tabIndex = 0;
    if (!wrapper.hasAttribute("aria-label")) wrapper.setAttribute("aria-label", "Scrollable table");
  }

  document.querySelectorAll("main table").forEach(table => {
    if (table.closest("[data-no-responsive-table]")) return;
    const parent = table.parentElement;
    if (!parent) return;
    if (parent.classList.contains("overflow-x-auto") || parent.classList.contains("responsive-table")) {
      enhanceWrapper(parent);
      return;
    }

    const wrapper = document.createElement("div");
    wrapper.className = "responsive-table overflow-x-auto";
    enhanceWrapper(wrapper);
    parent.insertBefore(wrapper, table);
    wrapper.appendChild(table);
  });
})();

// --- Client-side table pagination ---
(function () {
  const DEFAULT_PAGE_SIZE = 10;
  const PAGE_SIZES = [10, 25, 50, 100];
  const paginatedTables = new Map();

  function tableRows(table) {
    const body = table.tBodies && table.tBodies[0];
    return body ? Array.from(body.rows) : [];
  }

  function shouldPaginate(table) {
    if (table.dataset.noPagination !== undefined) return false;
    if (table.closest("[data-no-pagination]")) return false;
    if (!table.tBodies || !table.tBodies[0]) return false;
    return tableRows(table).length > DEFAULT_PAGE_SIZE;
  }

  function restorePaginationRows(rows) {
    rows.forEach(row => {
      if (row.dataset.paginationHidden !== "true") return;
      row.style.display = row.dataset.paginationDisplay || "";
      delete row.dataset.paginationHidden;
      delete row.dataset.paginationDisplay;
    });
  }

  function visibleRows(rows) {
    return rows.filter(row => row.style.display !== "none" && !row.hidden);
  }

  function insertAfterTable(table, controls) {
    const parent = table.parentElement;
    const target = parent && parent.classList.contains("overflow-x-auto") ? parent : table;
    target.insertAdjacentElement("afterend", controls);
  }

  function makeButton(label, className) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (className) button.className = className;
    return button;
  }

  function initTablePagination(table) {
    if (!shouldPaginate(table) || paginatedTables.has(table)) return;

    const controls = document.createElement("div");
    controls.className = "table-pagination";

    const status = document.createElement("div");
    status.className = "table-pagination-status";

    const sizeWrap = document.createElement("label");
    sizeWrap.className = "table-pagination-size";
    const sizeLabel = document.createElement("span");
    sizeLabel.textContent = "Rows";
    const sizeSelect = document.createElement("select");
    PAGE_SIZES.forEach(size => {
      const option = document.createElement("option");
      option.value = String(size);
      option.textContent = String(size);
      sizeSelect.appendChild(option);
    });
    sizeWrap.append(sizeLabel, sizeSelect);

    const pageButtons = document.createElement("div");
    pageButtons.className = "table-pagination-pages";

    const controlsWrap = document.createElement("div");
    controlsWrap.className = "table-pagination-controls";
    const prevButton = makeButton("Prev");
    const nextButton = makeButton("Next");
    controlsWrap.append(prevButton, pageButtons, nextButton);
    controls.append(status, sizeWrap, controlsWrap);

    const state = {
      page: 1,
      pageSize: Number(table.dataset.pageSize || DEFAULT_PAGE_SIZE),
      controls,
      status,
      sizeSelect,
      pageButtons,
      prevButton,
      nextButton,
    };

    if (!PAGE_SIZES.includes(state.pageSize)) state.pageSize = DEFAULT_PAGE_SIZE;
    sizeSelect.value = String(state.pageSize);

    function renderPageButtons(pageCount) {
      pageButtons.replaceChildren();
      const maxButtons = 5;
      let start = Math.max(1, state.page - Math.floor(maxButtons / 2));
      const end = Math.min(pageCount, start + maxButtons - 1);
      start = Math.max(1, end - maxButtons + 1);

      for (let page = start; page <= end; page++) {
        const button = makeButton(String(page));
        if (page === state.page) {
          button.setAttribute("aria-current", "page");
          button.disabled = true;
        }
        button.addEventListener("click", () => {
          state.page = page;
          refresh({ preservePage: true });
        });
        pageButtons.appendChild(button);
      }
    }

    function refresh(options = {}) {
      const rows = tableRows(table);
      restorePaginationRows(rows);

      const availableRows = visibleRows(rows);
      const total = availableRows.length;
      const pageCount = Math.max(1, Math.ceil(total / state.pageSize));

      if (options.reset) state.page = 1;
      if (!options.preservePage && !options.reset) state.page = Math.min(state.page, pageCount);
      state.page = Math.min(Math.max(state.page, 1), pageCount);

      controls.hidden = false;
      if (total <= state.pageSize) {
        status.textContent = total ? `Showing 1-${total} of ${total}` : "No rows";
        prevButton.disabled = true;
        nextButton.disabled = true;
        if (total) {
          renderPageButtons(1);
        } else {
          pageButtons.replaceChildren();
        }
        return;
      }

      const start = (state.page - 1) * state.pageSize;
      const end = start + state.pageSize;

      availableRows.forEach((row, index) => {
        if (index >= start && index < end) return;
        row.dataset.paginationHidden = "true";
        row.dataset.paginationDisplay = row.style.display || "";
        row.style.display = "none";
      });

      status.textContent = `Showing ${start + 1}-${Math.min(end, total)} of ${total}`;
      prevButton.disabled = state.page <= 1;
      nextButton.disabled = state.page >= pageCount;
      renderPageButtons(pageCount);
    }

    prevButton.addEventListener("click", () => {
      state.page -= 1;
      refresh({ preservePage: true });
    });
    nextButton.addEventListener("click", () => {
      state.page += 1;
      refresh({ preservePage: true });
    });
    sizeSelect.addEventListener("change", () => {
      state.pageSize = Number(sizeSelect.value) || DEFAULT_PAGE_SIZE;
      refresh({ reset: true });
    });

    state.refresh = refresh;
    paginatedTables.set(table, state);
    insertAfterTable(table, controls);
    refresh({ reset: true });
  }

  window.refreshTablePagination = function (table, options = {}) {
    const state = paginatedTables.get(table);
    if (state) state.refresh(options);
  };

  window.refreshAllTablePagination = function (options = {}) {
    paginatedTables.forEach(state => state.refresh(options));
  };

  const schedulePagination = window.requestIdleCallback
    ? callback => window.requestIdleCallback(callback, { timeout: 700 })
    : callback => window.setTimeout(callback, 0);

  schedulePagination(() => {
    document.querySelectorAll("main table").forEach(initTablePagination);
  });
})();

// --- Capture form: live abnormal-flag preview ---
document.querySelectorAll("[data-flag-preview]").forEach(out => {
  const id = out.getAttribute("data-flag-preview");
  const lo = parseFloat(out.getAttribute("data-low"));
  const hi = parseFloat(out.getAttribute("data-high"));
  const input = document.querySelector(`[data-capture-input="${id}"]`);
  if (!input) return;
  const update = () => {
    const v = parseFloat(input.value);
    if (!isFinite(v)) { out.textContent = ""; return; }
    if (isFinite(lo) && v < lo) { out.textContent = "↓ low (will flag)"; out.className = "mt-2 text-xs text-amber-400"; }
    else if (isFinite(hi) && v > hi) { out.textContent = "↑ high (will flag)"; out.className = "mt-2 text-xs text-amber-400"; }
    else { out.textContent = "✓ within reference range"; out.className = "mt-2 text-xs text-emerald-400"; }
  };
  input.addEventListener("input", update);
  update();
});
