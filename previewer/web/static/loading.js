/* Loading feedback on every page: the top bar (#loading-bar) shows while a page, form or
   button action takes longer than SHOW_AFTER ms, and the button that started it spins and is
   disabled so it cannot be sent twice. Background requests (htmx polling, lazy "load"
   panels) show nothing. */
(function () {
  "use strict";
  var SHOW_AFTER = 200;
  var GIVE_UP = 60000; // a download or a cancelled navigation never unloads the page
  var root = document.documentElement;
  var pending = 0, showTimer = null, giveUpTimer = null;
  var buttons = new WeakMap();

  function start() {
    pending += 1;
    if (pending > 1) return;
    showTimer = setTimeout(function () { root.classList.add("is-busy"); }, SHOW_AFTER);
  }

  function done() {
    pending = Math.max(0, pending - 1);
    if (pending) return;
    clearTimeout(showTimer);
    root.classList.remove("is-busy");
  }

  function reset() {
    pending = 0;
    clearTimeout(showTimer);
    clearTimeout(giveUpTimer);
    root.classList.remove("is-busy");
    document.querySelectorAll(".is-loading").forEach(idle);
  }

  function busy(btn) {
    if (!btn || btn.classList.contains("is-loading")) return;
    btn.classList.add("is-loading");
    btn.setAttribute("aria-busy", "true");
    // disable after the browser has read the button's name/value for the submission
    setTimeout(function () { if (btn.classList.contains("is-loading")) btn.disabled = true; }, 0);
  }

  function idle(btn) {
    if (!btn) return;
    btn.classList.remove("is-loading");
    btn.removeAttribute("aria-busy");
    btn.disabled = false;
  }

  function leaving() {
    start();
    clearTimeout(giveUpTimer);
    giveUpTimer = setTimeout(reset, GIVE_UP);
  }

  function submitButton(form, submitter) {
    return submitter || form.querySelector("button:not([type=button]):not([type=reset]), input[type=submit]");
  }

  // full-page form posts (htmx forms call preventDefault before this runs)
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.target === "_blank" || form.hasAttribute("hx-post")) return;
    busy(submitButton(form, e.submitter));
    leaving();
  });

  // links to other pages of the dashboard
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download") || a.hasAttribute("hx-get")) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    leaving();
  });

  // htmx actions started by the user (clicks, submits, typing in a search box)
  function userStarted(detail) {
    var ev = detail.requestConfig && detail.requestConfig.triggeringEvent;
    return !!ev && ["load", "revealed", "intersect"].indexOf(ev.type) === -1;
  }

  document.addEventListener("htmx:beforeRequest", function (e) {
    var d = e.detail;
    if (!userStarted(d)) return;
    var elt = d.elt, btn = null;
    if (elt.matches("button, input[type=submit]")) btn = elt;
    else if (elt.tagName === "FORM") btn = submitButton(elt, d.requestConfig.triggeringEvent.submitter);
    buttons.set(d.xhr, btn);
    busy(btn);
    start();
  });

  document.addEventListener("htmx:afterRequest", function (e) {
    var d = e.detail;
    if (!buttons.has(d.xhr)) return;
    var x = d.xhr, navigating = x.getResponseHeader("HX-Redirect") || x.getResponseHeader("HX-Refresh") === "true";
    if (navigating) leaving(); // keep the bar until the next page shows
    else idle(buttons.get(x));
    buttons.delete(x);
    done();
  });

  // back/forward restores the page from the cache with its old busy state
  window.addEventListener("pageshow", reset);
})();
