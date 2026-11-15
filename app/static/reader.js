"use strict";
/* EPUB olvasó (epub.js). Az olvasási pozíciót felhasználónként a szerver menti. */

const $ = (s) => document.querySelector(s);
const params = new URLSearchParams(location.search);
const fileId = params.get("file");
const bookId = params.get("book");
const store = {
  get(k, d) { try { const v = localStorage.getItem("hk_reader_" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("hk_reader_" + k, JSON.stringify(v)); } catch { /* */ } },
};
const THEMES = ["light", "sepia", "dark"];
const COLORS = {
  light: { body: { color: "#2a2420", background: "#fbf8f3" } },
  sepia: { body: { color: "#4a3b2a", background: "#f4ecd8" } },
  dark: { body: { color: "#e8e0d8", background: "#1b1815" }, a: { color: "#d39a6a" } },
};
let theme = store.get("theme", matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
let fontSize = store.get("font", 100);
let book, rendition, saveTimer, lastCfi = null;

function fail(msg) {
  $("#msg").innerHTML = msg;
  $("#msg").style.display = "flex";
}

if (bookId) $("#backBtn").href = "/#book-" + bookId;

async function api(path, opts) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (r.status === 401) { location.href = "/"; throw new Error("login"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Hiba (${r.status})`);
  return r.json();
}

function applyTheme() {
  document.body.classList.remove(...THEMES);
  document.body.classList.add(theme);
  if (rendition) rendition.themes.select(theme);
}

async function start() {
  if (!fileId) return fail("Nincs megadva fájl.");
  let info;
  try { info = await api(`/api/files/${fileId}/info`); }
  catch (e) { return fail(e.message); }
  document.title = info.title + " – Olvasó";
  $("#title").textContent = info.title;
  if (!bookId) $("#backBtn").href = "/#book-" + info.book_id;
  if (info.format !== "EPUB") {
    location.replace(`/api/files/${fileId}?inline=1`);
    return;
  }
  applyTheme();
  book = ePub(`/api/files/${fileId}?inline=1`, { openAs: "epub" });
  rendition = book.renderTo("viewer", { width: "100%", height: "100%", spread: "auto", flow: "paginated", allowScriptedContent: false });
  for (const t of THEMES) rendition.themes.register(t, COLORS[t]);
  rendition.themes.select(theme);
  rendition.themes.fontSize(fontSize + "%");

  try {
    await rendition.display(info.position || undefined);
  } catch (e) {
    try { await rendition.display(); } catch (e2) { return fail("A könyvet nem sikerült megnyitni: " + e2.message); }
  }
  $("#msg").style.display = "none";

  book.loaded.navigation.then((nav) => {
    const list = $("#tocList");
    const add = (items, sub) => items.forEach((it) => {
      const a = document.createElement("a");
      a.href = "#"; a.textContent = (it.label || "").trim(); if (sub) a.className = "sub";
      a.onclick = (e) => { e.preventDefault(); rendition.display(it.href); toggleToc(false); };
      list.appendChild(a);
      if (it.subitems && it.subitems.length) add(it.subitems, true);
    });
    add(nav.toc, false);
    if (!nav.toc.length) list.innerHTML = '<p style="padding:0 16px;color:var(--muted)">Nincs tartalomjegyzék.</p>';
  });

  // százalékhoz helyek generálása a háttérben (nagy könyvnél pár másodperc)
  book.ready.then(() => book.locations.generate(1600)).then(() => showProgress(rendition.currentLocation())).catch(() => {});

  rendition.on("relocated", (loc) => {
    showProgress(loc);
    lastCfi = loc.start.cfi;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(savePosition, 1500);
  });
  rendition.on("keyup", onKey);
  rendition.on("click", onTap);
  // érintéses lapozás
  let tx = null, ty = null;
  rendition.on("touchstart", (e) => { tx = e.changedTouches[0].screenX; ty = e.changedTouches[0].screenY; });
  rendition.on("touchend", (e) => {
    if (tx === null) return;
    const dx = e.changedTouches[0].screenX - tx, dy = e.changedTouches[0].screenY - ty;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) { dx < 0 ? rendition.next() : rendition.prev(); }
    tx = null;
  });
}

function showProgress(loc) {
  if (!loc || !loc.start) return;
  let txt = "";
  if (book.locations.length()) {
    const p = book.locations.percentageFromCfi(loc.start.cfi);
    txt = Math.round(p * 100) + "%";
  }
  const page = loc.start.displayed ? `${loc.start.displayed.page}/${loc.start.displayed.total}. oldal a fejezetben` : "";
  $("#progress").textContent = [txt, page].filter(Boolean).join(" · ");
}

async function savePosition() {
  if (!lastCfi) return;
  let percent = null;
  if (book.locations.length()) percent = Math.round(book.locations.percentageFromCfi(lastCfi) * 1000) / 10;
  try {
    await fetch(`/api/files/${fileId}/position`, {
      method: "PUT", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position: lastCfi, percent }),
    });
  } catch { /* offline: legközelebb */ }
}
window.addEventListener("pagehide", savePosition);
document.addEventListener("visibilitychange", () => { if (document.hidden) savePosition(); });

function onKey(e) {
  if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " ") rendition.next();
  if (e.key === "ArrowLeft" || e.key === "PageUp") rendition.prev();
  if (e.key === "Escape") toggleToc(false);
}
document.addEventListener("keyup", onKey);

function onTap(e) {
  // a szöveg harmadai: bal = vissza, jobb = előre, közép = menük ki/be
  const w = e.view ? e.view.innerWidth : window.innerWidth;
  const x = e.clientX % w;
  if (x < w * 0.28) rendition.prev();
  else if (x > w * 0.72) rendition.next();
  else document.body.classList.toggle("hide-ui");
}

function toggleToc(open) {
  $("#toc").classList.toggle("open", open ?? !$("#toc").classList.contains("open"));
}

$("#prev").onclick = () => rendition && rendition.prev();
$("#next").onclick = () => rendition && rendition.next();
$("#tocBtn").onclick = () => toggleToc();
$("#bigger").onclick = () => { fontSize = Math.min(200, fontSize + 10); store.set("font", fontSize); rendition && rendition.themes.fontSize(fontSize + "%"); };
$("#smaller").onclick = () => { fontSize = Math.max(60, fontSize - 10); store.set("font", fontSize); rendition && rendition.themes.fontSize(fontSize + "%"); };
$("#themeBtn").onclick = () => { theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]; store.set("theme", theme); applyTheme(); };
document.addEventListener("click", (e) => { if (!e.target.closest("#toc") && !e.target.closest("#tocBtn")) toggleToc(false); });

start();
