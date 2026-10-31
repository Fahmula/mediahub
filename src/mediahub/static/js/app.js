// MediaHub client behaviour. Kept deliberately small: htmx does the heavy lifting.
// - Live/paused toggle and tab-visibility gating for polled sections ([data-poll])
// - Theme toggle (persisted in a cookie so the server renders the right theme, no flash)
// - Toasts for failed requests
// - Service worker registration (installable app, offline page)
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

  document.addEventListener("DOMContentLoaded", renderLiveToggle);

  // Only available in secure contexts (HTTPS or localhost).
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {
        /* not installable; the app works the same without it */
      });
    });
  }
})();
