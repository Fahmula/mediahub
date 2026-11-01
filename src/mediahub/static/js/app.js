// MediaHub client behaviour. Kept deliberately small: htmx does the heavy lifting.
// - Live/paused toggle and tab-visibility gating for polled sections ([data-poll])
// - Theme toggle (persisted in a cookie so the server renders the right theme, no flash)
// - Toasts for failed requests
// - Service worker registration (installable app, offline page)
// - Push notification subscription (Settings, and repairing it on page load)
(() => {
  "use strict";

  const LIVE_KEY = "mh-live";
  let live = true;
  try {
    live = localStorage.getItem(LIVE_KEY) !== "0";
  } catch (_) {
    /* storage unavailable: default to live */
  }

  const isActive = () => live && document.visibilityState === "visible";

  function refreshAll() {
    document.querySelectorAll("[data-poll]").forEach((el) => htmx.trigger(el, "mh:refresh"));
  }

  function renderLiveToggle() {
    document.querySelectorAll("[data-live-toggle]").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(live));
      btn.title = live ? "Pause live updates" : "Resume live updates";
      btn.querySelector("[data-live-on]")?.toggleAttribute("hidden", !live);
      btn.querySelector("[data-live-off]")?.toggleAttribute("hidden", live);
    });
    document.getElementById("live-paused-banner")?.toggleAttribute("hidden", live);
  }

  function toast(message, tone = "error") {
    const host = document.getElementById("toast");
    if (!host) return;
    const el = document.createElement("div");
    el.className = `alert alert-${tone} alert-soft text-sm shadow-lg`;
    el.textContent = message;
    host.replaceChildren(el);
    setTimeout(() => el.remove(), 4000);
  }

  // ----- Notifications -------------------------------------------------------------------
  const PUSH_KEY = "mh-push"; // { endpoint, session }: what the server last heard from us
  const PUSH_API = "/settings/notifications";
  const SW_READY_TIMEOUT_MS = 4000;
  let pushRegistration = null; // kept so the click can subscribe at once: see enablePush

  const pushSupported = () =>
    window.isSecureContext &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window;

  const csrfToken = () => {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers"))["X-CSRF-Token"] || "";
    } catch (_) {
      return "";
    }
  };
  // Enough to notice a new login, without keeping the token itself.
  const sessionMark = () => csrfToken().slice(0, 8);

  function readPushMark() {
    try {
      return JSON.parse(localStorage.getItem(PUSH_KEY));
    } catch (_) {
      return null;
    }
  }

  function writePushMark(endpoint) {
    try {
      if (endpoint) localStorage.setItem(PUSH_KEY, JSON.stringify({ endpoint, session: sessionMark() }));
      else localStorage.removeItem(PUSH_KEY);
    } catch (_) {
      /* without it, a lost subscription is only repaired from Settings */
    }
  }

  class SignedOut extends Error {}

  async function pushRequest(path, options = {}) {
    const res = await fetch(`${PUSH_API}${path}`, {
      ...options,
      headers: { Accept: "application/json", "X-CSRF-Token": csrfToken(), ...options.headers },
    });
    if (res.redirected) throw new SignedOut();
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong on the server.");
    return data;
  }

  const pushPost = (path, body) =>
    pushRequest(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

  function pushError(err) {
    if (err instanceof SignedOut) location.href = "/login?next=/settings";
    else toast(err.message);
  }

  function keyBytes(key) {
    const padded = `${key}${"=".repeat((4 - (key.length % 4)) % 4)}`.replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  }

  function workerReady() {
    const timeout = new Promise((resolve) => setTimeout(resolve, SW_READY_TIMEOUT_MS, null));
    return Promise.race([navigator.serviceWorker.ready, timeout]);
  }

  // A subscription made with a signing key the server has since replaced can't receive anything.
  function usesKey(subscription, publicKey) {
    const current = subscription.options?.applicationServerKey;
    if (!current) return true;
    const stored = new Uint8Array(current);
    const wanted = keyBytes(publicKey);
    return stored.length === wanted.length && stored.every((byte, i) => byte === wanted[i]);
  }

  const needsHomeScreen = () => {
    const ios =
      /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const installed = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
    return ios && !installed;
  };

  async function pushState(panel) {
    if (!window.isSecureContext) return "https";
    if (needsHomeScreen()) return "install";
    if (!pushSupported()) return "unsupported";
    pushRegistration = await workerReady();
    if (!pushRegistration) return "unsupported";
    if (Notification.permission === "denied") return "denied";
    const subscription = await pushRegistration.pushManager.getSubscription();
    if (!subscription) {
      writePushMark(null);
      return "off";
    }
    const { subscribed } = usesKey(subscription, panel.dataset.publicKey)
      ? await pushPost("/touch", { endpoint: subscription.endpoint })
      : { subscribed: false };
    if (!subscribed) {
      await subscription.unsubscribe();
      writePushMark(null);
      return "off";
    }
    writePushMark(subscription.endpoint);
    return "on";
  }

  async function renderPush() {
    const panel = document.querySelector("[data-push-panel]");
    if (!panel) return;
    let state = "off";
    try {
      state = await pushState(panel);
    } catch (err) {
      pushError(err);
    }
    panel.querySelectorAll("[data-push-state]").forEach((el) => {
      el.toggleAttribute("hidden", el.dataset.pushState !== state);
    });
  }

  const refreshPushDevices = () => {
    const list = document.getElementById("push-devices");
    if (list) htmx.trigger(list, "mh:refresh");
  };

  async function enablePush(panel) {
    let subscription = null;
    try {
      // Straight from the click, with nothing slow first: iOS drops the request otherwise. This
      // is also what asks for permission.
      subscription = await pushRegistration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: keyBytes(panel.dataset.publicKey),
      });
      await pushPost("/subscribe", subscription.toJSON());
      writePushMark(subscription.endpoint);
    } catch (err) {
      await subscription?.unsubscribe().catch(() => {});
      if (Notification.permission === "default") toast("Notifications weren't turned on.", "warning");
      else if (Notification.permission === "granted") pushError(err);
    }
    await renderPush();
    refreshPushDevices();
  }

  async function disablePush() {
    try {
      const subscription = await pushRegistration.pushManager.getSubscription();
      if (subscription) {
        await pushPost("/unsubscribe", { endpoint: subscription.endpoint });
        await subscription.unsubscribe();
      }
      writePushMark(null);
    } catch (err) {
      pushError(err);
    }
    await renderPush();
    refreshPushDevices();
  }

  async function testPush() {
    try {
      const subscription = await pushRegistration.pushManager.getSubscription();
      if (!subscription) return;
      const { ok, error } = await pushPost("/test", { endpoint: subscription.endpoint });
      if (ok) toast("Test sent", "success");
      else toast(`The push service refused it: ${error}`);
    } catch (err) {
      pushError(err);
    }
    refreshPushDevices();
  }

  // Browsers can replace or drop a subscription with no notice, and a user can sign in again.
  async function syncPush() {
    const mark = readPushMark();
    if (!mark || !document.body.hasAttribute("data-signed-in") || !pushSupported()) return;
    if (Notification.permission !== "granted") return;
    const registration = await workerReady();
    if (!registration) return;
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      const { public_key: publicKey } = await pushRequest("/bootstrap");
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: keyBytes(publicKey),
      });
    }
    if (subscription.endpoint !== mark.endpoint) {
      await pushPost("/subscribe", { ...subscription.toJSON(), old_endpoint: mark.endpoint });
    } else if (mark.session !== sessionMark()) {
      const { subscribed } = await pushPost("/touch", { endpoint: subscription.endpoint });
      if (!subscribed) {
        await subscription.unsubscribe();
        writePushMark(null);
        return;
      }
    }
    writePushMark(subscription.endpoint);
  }

  document.addEventListener("click", (evt) => {
    const liveBtn = evt.target.closest("[data-live-toggle]");
    if (liveBtn) {
      live = !live;
      try {
        localStorage.setItem(LIVE_KEY, live ? "1" : "0");
      } catch (_) {
        /* ignore */
      }
      renderLiveToggle();
      if (live) refreshAll();
      return;
    }
    const themeBtn = evt.target.closest("[data-theme-toggle]");
    if (themeBtn) {
      const root = document.documentElement;
      const light = root.dataset.theme !== "mediahub-light";
      root.dataset.theme = light ? "mediahub-light" : "mediahub";
      document.querySelector('meta[name="theme-color"]')?.setAttribute("content", light ? "#f1f4f9" : "#0b1220");
      document.cookie = `mh_theme=${light ? "light" : "dark"}; path=/; max-age=31536000; samesite=lax`;
      return;
    }
    // Back to the page the user came from, when it's one of ours; otherwise the link's own href.
    if (evt.target.closest("[data-back]") && document.referrer.startsWith(`${location.origin}/`)) {
      evt.preventDefault();
      history.back();
      return;
    }
    const pushPanel = evt.target.closest("[data-push-panel]");
    if (pushPanel) {
      if (evt.target.closest("[data-push-enable]")) enablePush(pushPanel);
      else if (evt.target.closest("[data-push-disable]")) disablePush();
      else if (evt.target.closest("[data-push-test]")) testPush();
    }
    const copyBtn = evt.target.closest("[data-copy]");
    if (copyBtn) {
      const input = document.getElementById(copyBtn.dataset.copy);
      if (!input) return;
      input.select();
      // The Clipboard API needs a secure context (HTTPS or localhost); otherwise the text
      // stays selected for a manual copy.
      const failed = () => toast("Couldn't copy: the text is selected, copy it manually", "warning");
      if (!navigator.clipboard) return failed();
      navigator.clipboard.writeText(input.value).then(() => toast("Copied", "success"), failed);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (isActive()) refreshAll();
  });

  // A refresh would close a menu the user has open inside the section.
  const menuOpenIn = (elt) => {
    const focused = document.activeElement;
    return focused instanceof Element && elt.contains(focused) && focused.closest(".dropdown") !== null;
  };

  // Polled sections load once regardless, then only refresh while live and visible.
  document.addEventListener("htmx:beforeRequest", (evt) => {
    const elt = evt.detail.elt;
    if (
      elt instanceof Element &&
      elt.hasAttribute("data-poll") &&
      elt.dataset.loaded &&
      (!isActive() || menuOpenIn(elt))
    ) {
      evt.preventDefault();
    }
  });

  document.addEventListener("htmx:afterRequest", (evt) => {
    const elt = evt.detail.elt;
    if (elt instanceof Element && elt.hasAttribute("data-poll") && evt.detail.successful) {
      elt.dataset.loaded = "1";
    }
  });

  // The server doesn't know the viewer's time zone.
  htmx.onLoad((root) => {
    root.querySelectorAll?.("time[data-local]").forEach((el) => {
      el.textContent = new Date(el.dateTime).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    });
  });

  let offline = false;
  document.addEventListener("htmx:sendError", () => {
    if (!offline) toast("Can't reach MediaHub. Retrying…", "warning");
    offline = true;
  });
  document.addEventListener("htmx:afterOnLoad", () => {
    offline = false;
  });
  document.addEventListener("htmx:responseError", (evt) => {
    const status = evt.detail.xhr.status;
    if (status === 403) toast("Your session changed. Reload the page and try again.");
    else if (status >= 500) toast("Something went wrong on the server.");
  });

  // Removing this device from the list has to flip its switch too.
  document.addEventListener("htmx:afterSettle", (evt) => {
    if (evt.detail.target?.id === "push-devices" || evt.detail.elt?.id === "push-devices") renderPush();
  });

  document.addEventListener("DOMContentLoaded", renderLiveToggle);

  // Only available in secure contexts (HTTPS or localhost).
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {
        /* not installable; the app works the same without it */
      });
      renderPush().then(() => syncPush()).catch(() => {
        /* tried again on the next page load */
      });
    });
  }
})();
