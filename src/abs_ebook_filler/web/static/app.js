// Cover lightbox + rapid mode. Loaded on every page; each feature no-ops where its elements are absent.
(function () {
  "use strict";

  // ---------- sticky top bar height ----------
  // The bar's height changes (filters wrap, pre-search progress appears), so keep --topbar-h in sync;
  // the sticky book panel and scroll offsets use it to stay clear of the bar.
  const topbar = document.getElementById("topbar");
  if (topbar) {
    const setH = () => document.documentElement.style.setProperty("--topbar-h", topbar.offsetHeight + "px");
    setH();
    if ("ResizeObserver" in window) new ResizeObserver(setH).observe(topbar);
    else window.addEventListener("resize", setH);
  }

  // ---------- cover lightbox ----------
  const lightbox = document.getElementById("lightbox");
  document.addEventListener("click", (e) => {
    const img = e.target.closest("img.zoomable");
    if (!img || !lightbox) return;
    e.preventDefault();
    e.stopPropagation();
    const big = lightbox.querySelector("img");
    big.src = img.dataset.zoom || img.src;
    lightbox.showModal();
  }, true);
  if (lightbox) lightbox.addEventListener("click", () => lightbox.close());

  // ---------- toast ----------
  const toastEl = document.getElementById("toast");
  let toastTimer;
  function toast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 3500);
  }

  // ---------- rapid mode ----------
  const rapidBox = document.getElementById("rapid");
  const ELIGIBLE = new Set(["missing", "failed"]);
  const prefetched = new Set();

  function loadRapid() {
    try { return localStorage.getItem("rapid") === "1"; } catch { return false; }
  }
  function saveRapid(on) {
    try { localStorage.setItem("rapid", on ? "1" : "0"); } catch { /* storage unavailable */ }
  }
  function rapidOn() { return !!(rapidBox && rapidBox.checked); }

  if (rapidBox) {
    rapidBox.checked = loadRapid();
    document.body.classList.toggle("rapid-on", rapidBox.checked);
    rapidBox.addEventListener("change", () => {
      saveRapid(rapidBox.checked);
      document.body.classList.toggle("rapid-on", rapidBox.checked);
      const cur = currentItemId();
      if (rapidBox.checked && cur) prefetchAfter(cur);
    });
  }

  function currentItemId() {
    const art = document.querySelector("#panel article[data-item-id]");
    return art ? art.dataset.itemId : null;
  }

  function nextRow(itemId) {
    const rows = Array.from(document.querySelectorAll("tbody tr[data-item-id]"));
    let start = rows.findIndex((r) => r.dataset.itemId === itemId);
    for (let i = start + 1; i < rows.length; i++) {
      if (ELIGIBLE.has(rows[i].dataset.status)) return rows[i];
    }
    return null;
  }

  function markActive(itemId) {
    document.querySelectorAll("tbody tr.active").forEach((r) => r.classList.remove("active"));
    const row = document.getElementById("row-" + itemId);
    if (row) {
      row.classList.add("active");
      row.scrollIntoView({ block: "nearest" });
    }
  }

  function prefetchAfter(itemId) {
    const nxt = nextRow(itemId);
    if (!nxt || prefetched.has(nxt.dataset.itemId)) return;
    prefetched.add(nxt.dataset.itemId);
    htmx.ajax("POST", "/book/" + encodeURIComponent(nxt.dataset.itemId) + "/prefetch",
              { target: "#toast", swap: "none" });
  }

  function openNext(itemId) {
    const nxt = nextRow(itemId);
    if (!nxt) {
      toast("End of the list 🎉");
      return;
    }
    htmx.ajax("GET", "/book/" + encodeURIComponent(nxt.dataset.itemId), { target: "#panel", swap: "innerHTML" });
    nxt.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  document.body.addEventListener("htmx:afterSettle", (e) => {
    if (!e.detail.target || e.detail.target.id !== "panel") return;
    const id = currentItemId();
    if (!id) return;
    markActive(id);
    if (rapidOn()) prefetchAfter(id);
  });

  const ACTION_RE = /^\/book\/([^/]+)\/(download\/\d+|skip)$/;
  document.body.addEventListener("htmx:afterRequest", (e) => {
    if (!rapidOn() || !e.detail.successful) return;
    const path = (e.detail.pathInfo && e.detail.pathInfo.requestPath) || "";
    const m = path.match(ACTION_RE);
    if (!m) return;
    const itemId = decodeURIComponent(m[1]);
    const row = document.getElementById("row-" + itemId);
    const title = row ? row.querySelector(".t").textContent.replace("⚡", "").trim() : "book";
    toast((m[2] === "skip" ? "Skipped: " : "Queued: ") + title);
    // Let htmx finish swapping the panel/row before moving on.
    setTimeout(() => openNext(itemId), 50);
  });

  // ---------- auto-download ----------
  // When a book's results include a release scored exactly 100 (the list only ever shows enabled
  // sources), click its Download button for you. Only for books still "missing": a failed book's
  // best match is usually the release that just failed, so re-queuing it would loop. With rapid
  // mode on, the download click moves on to the next book as usual, so runs of perfect matches
  // go through hands-free and it stops at the first book that needs you.
  const autoBox = document.getElementById("auto-dl");
  const AUTO_DELAY_MS = 900; // long enough to see what's happening (and untick the switch)
  const autoTried = new Set(); // one auto-download per book per page load

  if (autoBox) {
    try { autoBox.checked = localStorage.getItem("autoDownload") === "1"; } catch { /* ignore */ }
    autoBox.addEventListener("change", () => {
      try { localStorage.setItem("autoDownload", autoBox.checked ? "1" : "0"); } catch { /* ignore */ }
      if (autoBox.checked) maybeAutoDownload(); // results already on screen count too
    });
  }
  function autoOn() { return !!(autoBox && autoBox.checked); }

  function maybeAutoDownload() {
    if (!autoOn()) return;
    const art = document.querySelector("#panel article[data-item-id]");
    if (!art || art.dataset.status !== "missing") return;
    const itemId = art.dataset.itemId;
    if (autoTried.has(itemId)) return;
    const btn = art.querySelector("#cands button.dl-btn[data-score='100']");
    if (!btn) return; // nothing perfect -> wait for the user
    autoTried.add(itemId);
    toast("Auto-downloading (score 100): " + (btn.dataset.title || "release"));
    setTimeout(() => {
      // Bail if the switch was turned off or the panel moved to another book meanwhile.
      if (!autoOn() || !document.body.contains(btn) || currentItemId() !== itemId || btn.disabled) return;
      btn.click();
    }, AUTO_DELAY_MS);
  }

  document.body.addEventListener("htmx:afterSettle", (e) => {
    if (e.detail.target && e.detail.target.id === "cands") maybeAutoDownload();
  });
})();
