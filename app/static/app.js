"use strict";
/* Házikönyvtár – frontend (vanilla JS, build lépés nélkül) */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PAGE = 60;

const state = {
  user: null, meta: null,
  f: { q: "", status: "all", tags: new Set(), tagMode: "any", owner: "", fmt: "", location: "", sort: "",
       read: "", minRating: "0", rmode: "mine" },
  items: [], total: 0, loading: false, reqId: 0,
  current: null,            // megnyitott könyv
  ed: null,                 // szerkesztő állapot
};

/* ---------------------------------------------------------------- API */
async function api(path, opts = {}) {
  const o = { credentials: "same-origin", ...opts };
  if (o.json !== undefined) {
    o.body = JSON.stringify(o.json);
    o.headers = { "Content-Type": "application/json", ...(o.headers || {}) };
    delete o.json;
  }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new Error("Bejelentkezés szükséges"); }
  let data = null;
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("json")) data = await r.json();
  if (!r.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join(", ");
    throw new Error(msg || `Hiba (${r.status})`);
  }
  return data;
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " error" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), isErr ? 5000 : 2500);
}

/* ---------------------------------------------------------------- sheetek + vissza gomb */
const openStack = [];
function openSheet(el) {
  el.classList.remove("hidden");
  openStack.push(el);
  history.pushState({ sheet: el.id }, "");
  document.body.style.overflow = "hidden";
}
function hideSheet(el) {
  el.classList.add("hidden");
  const i = openStack.lastIndexOf(el);
  if (i >= 0) openStack.splice(i, 1);
  if (!openStack.length) document.body.style.overflow = "";
}
function closeTop() {
  if (openStack.length) history.back();
}
window.addEventListener("popstate", () => {
  if (!$("#lightbox").classList.contains("hidden")) { $("#lightbox").classList.add("hidden"); return; }
  const el = openStack[openStack.length - 1];
  if (el) hideSheet(el);
});
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) { e.preventDefault(); closeTop(); }
  else if (e.target.classList.contains("sheet-backdrop")) closeTop();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && openStack.length) closeTop();
});

/* ---------------------------------------------------------------- bejelentkezés */
async function boot() {
  try {
    const me = await api("/api/me");
    if (me.user) { state.user = me.user; return startApp(); }
    showLogin(me.users || []);
  } catch (e) { toast(e.message, true); }
}

function showLogin(users) {
  $("#main").classList.add("hidden");
  $("#login").classList.remove("hidden");
  const box = $("#loginUsers");
  if (users && users.length) {
    box.innerHTML = users.map((u) => `<button type="button" class="btn" data-u="${esc(u)}">${esc(u)}</button>`).join("");
    const last = localStorageGet("hk_last_user");
    const pick = (u) => {
      $$("button", box).forEach((b) => b.classList.toggle("on", b.dataset.u === u));
      box.dataset.sel = u;
      $("#loginPass").focus();
    };
    $$("button", box).forEach((b) => b.onclick = () => pick(b.dataset.u));
    pick(users.includes(last) ? last : users[0]);
  }
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#loginUsers").dataset.sel;
  try {
    const r = await api("/api/login", { method: "POST", json: { name, password: $("#loginPass").value } });
    localStorageSet("hk_last_user", name);
    $("#loginPass").value = "";
    $("#loginErr").textContent = "";
    state.user = r.user;
    startApp();
  } catch (err) { $("#loginErr").textContent = err.message; }
});

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* */ } }

/* ---------------------------------------------------------------- fő nézet */
async function startApp() {
  $("#login").classList.add("hidden");
  $("#main").classList.remove("hidden");
  $("#whoami").textContent = state.user;
  await loadMeta();
  await search();
  refreshLibraryBadge();
  openFromHash();
}

async function loadMeta() {
  const m = await api("/api/meta");
  state.meta = m;
  $("#cAll").textContent = m.counts.all;
  $("#cOwned").textContent = m.counts.owned;
  $("#cWish").textContent = m.counts.wishlist;
  fillSelect($("#fOwner"), m.owners, "Bárki", state.f.owner);
  fillSelect($("#fFormat"), m.formats, "Bármely", state.f.fmt);
  fillSelect($("#fLocation"), m.locations, "Bárhol", state.f.location);
  fillDatalist("#dlAuthors", m.authors);
  fillDatalist("#dlTags", m.tags.map((t) => t.name));
  fillDatalist("#dlPublishers", m.publishers);
  fillDatalist("#dlOwners", m.owners);
  fillDatalist("#dlLocations", m.locations);
  fillDatalist("#dlShops", m.shops);
  renderTagChips();
}

function fillSelect(sel, values, emptyLabel, current) {
  sel.innerHTML = `<option value="">${esc(emptyLabel)}</option>` +
    values.map((v) => `<option ${v === current ? "selected" : ""}>${esc(v)}</option>`).join("");
}
function fillDatalist(id, values) {
  $(id).innerHTML = values.map((v) => `<option value="${esc(v)}">`).join("");
}

function renderTagChips() {
  const tags = state.meta.tags;
  const box = $("#tagChips");
  box.innerHTML = tags.map((t) =>
    `<button class="chip ${state.f.tags.has(t.name) ? "on" : ""}" data-tag="${esc(t.name)}">${esc(t.name)}<small>${t.n}</small></button>`
  ).join("");
  box.classList.toggle("hidden", !tags.length);
}
$("#tagChips").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tag]");
  if (!b) return;
  const t = b.dataset.tag;
  state.f.tags.has(t) ? state.f.tags.delete(t) : state.f.tags.add(t);
  b.classList.toggle("on");
  search();
});

let qTimer;
$("#q").addEventListener("input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { state.f.q = $("#q").value; search(); }, 220);
});
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.target.blur(); } });
$("#statusSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#statusSeg button").forEach((x) => x.classList.toggle("on", x === b));
  state.f.status = b.dataset.v;
  search();
});
$("#moreFiltersBtn").onclick = () => $("#moreFilters").classList.toggle("hidden");
[["#fOwner", "owner"], ["#fFormat", "fmt"], ["#fLocation", "location"], ["#fSort", "sort"], ["#fTagMode", "tagMode"],
 ["#fRead", "read"], ["#fRating", "minRating"], ["#fRmode", "rmode"]]
  .forEach(([id, key]) => $(id).addEventListener("change", (e) => {
    state.f[key] = e.target.value;
    if (key === "rmode") localStorageSet("hk_rmode", e.target.value);
    search();
  }));
state.f.rmode = localStorageGet("hk_rmode") === "avg" ? "avg" : "mine";
$("#fRmode").value = state.f.rmode;

function queryString(offset) {
  const f = state.f;
  const p = new URLSearchParams({ q: f.q, status: f.status, tags: [...f.tags].join("|"), tag_mode: f.tagMode,
    owner: f.owner, fmt: f.fmt, location: f.location, sort: f.sort, read: f.read, min_rating: f.minRating,
    rmode: f.rmode, limit: PAGE, offset });
  return p.toString();
}

/* csillagok */
function starsHtml(v, cls = "sm") {
  const pct = v ? Math.max(0, Math.min(100, (v / 5) * 100)) : 0;
  return `<span class="stars ${cls}" aria-label="${v ? fmtRating(v) + " csillag" : "nincs értékelve"}"><span class="fill" style="width:${pct}%"></span></span>`;
}
function fmtRating(v) { return v == null ? "" : String(Math.round(v * 10) / 10).replace(".", ","); }
function shownRating(b) { return state.f.rmode === "avg" ? b.avg_rating : b.my_rating; }
const READ_LABEL = { igen: "Olvastam", folyamatban: "Folyamatban", nem: "Még nem" };

async function search(append = false) {
  const id = ++state.reqId;
  const offset = append ? state.items.length : 0;
  try {
    const r = await api("/api/books?" + queryString(offset));
    if (id !== state.reqId) return;
    state.total = r.total;
    state.items = append ? state.items.concat(r.items) : r.items;
    renderGrid(append ? r.items : null);
  } catch (e) { toast(e.message, true); }
}

function cardHtml(b) {
  const cover = b.cover_thumb
    ? `<img src="${esc(b.cover_thumb)}" loading="lazy" alt="">`
    : `<div class="ph">${esc(b.title)}</div>`;
  const ebook = b.formats.filter((f) => f !== "Papír");
  const r = shownRating(b);
  const st = b.my_status;
  return `<div class="card" data-id="${b.id}">
    <div class="cv">${cover}
      ${b.wishlist ? `<span class="badge">♡ Kívánság</span>` : ""}
      ${st === "igen" ? `<span class="read-badge igen" title="Olvastam">✓</span>` : st === "folyamatban" ? `<span class="read-badge folyamatban" title="Olvasom">◐</span>` : ""}
      ${ebook.length ? `<span class="fmt">${esc(ebook.join(" · "))}</span>` : ""}
    </div>
    <div class="t">${esc(b.title)}</div>
    <div class="a">${esc(b.authors.join(", ") || " ")}</div>
    ${r ? `<div class="meta-line">${starsHtml(r)}<span class="rs">${fmtRating(r)}${state.f.rmode === "avg" && b.rating_count > 1 ? ` (${b.rating_count})` : ""}</span></div>` : ""}
  </div>`;
}

function renderGrid(appended) {
  const g = $("#grid");
  if (appended) g.insertAdjacentHTML("beforeend", appended.map(cardHtml).join(""));
  else g.innerHTML = state.items.length ? state.items.map(cardHtml).join("")
    : `<div class="empty">${state.meta && state.meta.counts.all ? "Nincs találat." : "Még üres a könyvtár. Nyomd meg a ＋ gombot az első könyv felvételéhez!"}</div>`;
  const f = state.f;
  const active = f.q || f.tags.size || f.status !== "all" || f.owner || f.fmt || f.location || f.read || +f.minRating;
  $("#resultInfo").textContent = active ? `${state.total} találat` : `${state.total} könyv`;
  $("#moreBtn").classList.toggle("hidden", state.items.length >= state.total);
}
$("#moreBtn").onclick = () => search(true);
// végtelen görgetés
new IntersectionObserver((ents) => {
  if (ents[0].isIntersecting && state.items.length < state.total) search(true);
}, { rootMargin: "400px" }).observe($("#moreBtn").parentElement);

$("#grid").addEventListener("click", (e) => {
  const c = e.target.closest(".card");
  if (c) openDetail(+c.dataset.id);
});

/* ---------------------------------------------------------------- menü */
$("#menuBtn").onclick = () => openSheet($("#menu"));
$("#logoutBtn").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
};

/* ---------------------------------------------------------------- adatlap */
async function openDetail(id) {
  try {
    const b = await api(`/api/books/${id}`);
    state.current = b;
    renderDetail(b);
    if ($("#detail").classList.contains("hidden")) openSheet($("#detail"));
  } catch (e) { toast(e.message, true); }
}

function fmtSize(n) {
  if (!n) return "";
  return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.ceil(n / 1024) + " KB";
}
function fmtPrice(b) {
  if (b.price == null) return "";
  const v = Number(b.price).toLocaleString("hu-HU", { maximumFractionDigits: 2 });
  return `${v} ${b.currency === "HUF" || !b.currency ? "Ft" : b.currency}`;
}

function renderDetail(b) {
  const rows = [
    ["Kiadó", b.publisher], ["Megjelenés", b.publish_year], ["Kiadás / verzió", b.edition], ["ISBN", b.isbn],
    ["Formátum", b.formats.join(", ")], ["Kié", b.owner], ["Ki szeretné", b.wanted_by], ["Hol található", b.location],
    ["Vásárlás éve", b.purchase_year], ["Bolt", b.shop], ["Ár", fmtPrice(b)],
    ["Link", b.link ? `<a href="${esc(b.link)}" target="_blank" rel="noopener">${esc(b.link.replace(/^https?:\/\/(www\.)?/, "").slice(0, 60))}…</a>` : ""],
    ["Felvette", b.created_by ? `${esc(b.created_by)}, ${esc((b.created_at || "").slice(0, 10))}` : ""],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "");
  const raw = new Set(["Link", "Felvette"]);
  $("#detailBody").innerHTML = `
    <div class="d-top">
      <div class="d-covers">
        ${b.cover ? `<img src="${esc(b.cover)}" alt="Borító" data-zoom>` : ""}
        ${b.back ? `<img class="back" src="${esc(b.back)}" alt="Hátoldal" data-zoom>` : ""}
      </div>
      <div class="d-info">
        <h2>${esc(b.title)}</h2>
        ${b.subtitle ? `<div class="sub">${esc(b.subtitle)}</div>` : ""}
        <div class="auth">${b.authors.map((a) => `<a href="#" data-author="${esc(a)}">${esc(a)}</a>`).join(", ")}</div>
        <div class="d-tags">${b.tags.map((t) => `<span class="chip" data-tagfilter="${esc(t)}">${esc(t)}</span>`).join("")}</div>
        <span class="d-status ${b.wishlist ? "wish" : "own"}">${b.wishlist ? "♡ Kívánságlistán" : "📚 Megvan"}</span>
      </div>
    </div>
    ${mineBoxHtml(b)}
    <dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${raw.has(k) ? v : esc(v)}</dd>`).join("")}</dl>
    ${b.description ? `<div class="section-title">Leírás</div><div class="desc">${esc(b.description)}</div>` : ""}
    ${b.notes ? `<div class="section-title">Megjegyzés</div><div class="desc">${esc(b.notes)}</div>` : ""}
    ${b.files.length ? `<div class="section-title">Digitális fájlok</div><div class="files">${b.files.map((f) =>
      `<div class="file"><span class="fmt-tag">${esc(f.format)}</span>
        <span class="name" title="${esc(f.orig_name)}">${esc(f.orig_name)}</span>
        <span class="muted small">${fmtSize(f.size)}</span>
        ${f.readable ? `<a class="btn tiny primary read-btn" href="${f.format === "EPUB" ? `/olvaso?file=${f.id}&book=${b.id}` : `/api/files/${f.id}?inline=1`}" ${f.format === "PDF" ? 'target="_blank" rel="noopener"' : ""}>📖 Olvasás</a>` : ""}
        <a class="btn tiny" href="/api/files/${f.id}" download title="Letöltés">⬇</a></div>`).join("")}</div>
      ${b.files.some((f) => !f.readable) ? `<p class="muted small">A MOBI/AZW/PRC fájlokat Kindle-re vagy olvasóprogramba töltsd le; böngészőben az EPUB és a PDF olvasható.</p>` : ""}` : ""}
  `;
}

function mineBoxHtml(b) {
  const others = (b.ratings || []).filter((r) => r.user !== state.user);
  const showAll = state.f.rmode === "avg";
  return `<div class="mine-box">
    <div><div class="lbl">Az én értékelésem</div>
      <span id="myStars" data-v="${b.my_rating || 0}">${starsHtml(b.my_rating, "lg")}</span>
      <span class="val" id="myStarsVal">${b.my_rating ? fmtRating(b.my_rating) : "nincs"}</span>
    </div>
    <div><div class="lbl">Elolvastam?</div>
      <div class="seg read" id="myRead">
        ${["nem", "folyamatban", "igen"].map((s) => `<button type="button" data-v="${s}" class="${(b.my_status || "nem") === s ? "on" : ""}">${READ_LABEL[s]}</button>`).join("")}
      </div>
    </div>
    ${showAll ? `<div class="others">
      ${b.avg_rating ? `<div>Átlag: ${starsHtml(b.avg_rating)} <b>${fmtRating(b.avg_rating)}</b> (${b.rating_count} értékelés)</div>` : `<div>Még senki nem értékelte.</div>`}
      ${others.map((r) => `<div><b>${esc(r.user)}:</b> ${r.rating ? starsHtml(r.rating) + " " + fmtRating(r.rating) : "nincs értékelés"} · ${READ_LABEL[r.status || "nem"]}</div>`).join("")}
    </div>` : ""}
  </div>`;
}

async function saveMine(payload) {
  const b = state.current;
  try {
    state.current = await api(`/api/books/${b.id}/me`, { method: "PUT", json: payload });
    renderDetail(state.current);
    const i = state.items.findIndex((x) => x.id === b.id);
    if (i >= 0) {
      Object.assign(state.items[i], { my_rating: state.current.my_rating, my_status: state.current.my_status,
        avg_rating: state.current.avg_rating, rating_count: state.current.rating_count });
      const card = $(`#grid .card[data-id="${b.id}"]`);
      if (card) card.outerHTML = cardHtml(state.items[i]);
    }
  } catch (e) { toast(e.message, true); }
}

function starValueFromEvent(el, e) {
  const rect = el.getBoundingClientRect();
  const x = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
  return Math.max(1, Math.ceil((x / rect.width) * 10) / 2);
}

$("#detailBody").addEventListener("pointermove", (e) => {
  const s = e.target.closest("#myStars .stars");
  if (!s || e.pointerType !== "mouse") return;
  const v = starValueFromEvent(s, e);
  $(".fill", s).style.width = (v / 5) * 100 + "%";
  $("#myStarsVal").textContent = fmtRating(v);
});
$("#detailBody").addEventListener("pointerout", (e) => {
  const s = e.target.closest("#myStars .stars");
  if (!s || s.contains(e.relatedTarget)) return;
  const v = state.current.my_rating;
  $(".fill", s).style.width = ((v || 0) / 5) * 100 + "%";
  $("#myStarsVal").textContent = v ? fmtRating(v) : "nincs";
});
$("#detailBody").addEventListener("click", (e) => {
  const s = e.target.closest("#myStars .stars");
  if (s) {
    const v = starValueFromEvent(s, e);
    if (v === state.current.my_rating) {
      if (confirm("Törlöd az értékelésedet?")) saveMine({ clear_rating: true });
      else renderDetail(state.current);
    } else saveMine({ rating: v });
    return;
  }
  const rd = e.target.closest("#myRead button");
  if (rd) { saveMine({ status: rd.dataset.v }); return; }
  const z = e.target.closest("[data-zoom]");
  if (z) { $("#lightbox img").src = z.src; $("#lightbox").classList.remove("hidden"); history.pushState({ lb: 1 }, ""); return; }
  const a = e.target.closest("[data-author]");
  if (a) {
    e.preventDefault();
    $("#q").value = a.dataset.author; state.f.q = a.dataset.author;
    closeTop(); search(); return;
  }
  const t = e.target.closest("[data-tagfilter]");
  if (t) {
    state.f.tags = new Set([t.dataset.tagfilter]);
    renderTagChips(); closeTop(); search();
  }
});
$("#lightbox").onclick = () => history.back();
$("#editBtn").onclick = () => openEditor(state.current);

/* ---------------------------------------------------------------- szerkesztő */
const FORM_FIELDS = ["title", "subtitle", "isbn", "edition", "publisher", "publish_year", "purchase_year", "owner",
  "wanted_by", "location", "shop", "price", "currency", "link", "description", "notes"];

function openEditor(book) {
  const form = $("#bookForm");
  form.reset();
  state.ed = {
    id: book ? book.id : null,
    authors: book ? [...book.authors] : [],
    tags: book ? [...book.tags] : [],
    front: book && book.cover ? { url: book.cover, existing: true } : null,
    back: book && book.back ? { url: book.back, existing: true } : null,
    files: book ? book.files : [],
    pendingFiles: [],
  };
  $("#editorTitle").textContent = book ? "Szerkesztés" : "Új könyv";
  for (const k of FORM_FIELDS) {
    const el = form.elements[k];
    el.value = book && book[k] != null ? book[k] : (k === "currency" ? "HUF" : "");
  }
  if (!book) form.elements.owner.value = state.user !== "Vendég" ? state.user : "";
  form.elements.wishlist.checked = book ? book.wishlist : false;
  $("#fmtBoxes").innerHTML = state.meta.formats.map((f) =>
    `<label><input type="checkbox" value="${esc(f)}" ${(book ? book.formats.includes(f) : f === "Papír") ? "checked" : ""}> ${esc(f)}</label>`).join("");
  $("#deleteBtn").classList.toggle("hidden", !book);
  $("#filesSection").classList.remove("hidden");
  $("#autoFill").open = !book;
  ["#afUrl", "#afIsbn", "#afTitle"].forEach((s) => $(s).value = "");
  $("#afStatus").textContent = ""; $("#afStatus").className = "af-status";
  $("#afCandidates").innerHTML = "";
  if (state.f.status === "wishlist" && !book) form.elements.wishlist.checked = true;
  syncWishlist();
  renderChips(); renderCoverSlots(); renderEdFiles();
  openSheet($("#editor"));
  $(".sheet-body", $("#editor")).scrollTop = 0;
}
$("#addBtn").onclick = () => openEditor(null);

function syncWishlist() {
  const f = $("#bookForm");
  const has = !!f.elements.purchase_year.value;
  f.elements.wishlist.disabled = has;
  if (has) f.elements.wishlist.checked = false;
}
$("#bookForm").elements.purchase_year.addEventListener("input", syncWishlist);

/* chip-inputok (szerzők, címkék) */
function renderChips() {
  $$(".chip-input").forEach((box) => {
    const field = box.dataset.field;
    $$(".chip", box).forEach((c) => c.remove());
    const input = $("input", box);
    state.ed[field].forEach((v, i) => {
      const c = document.createElement("span");
      c.className = "chip";
      c.innerHTML = `${esc(v)}<button type="button" aria-label="Törlés" data-i="${i}">×</button>`;
      box.insertBefore(c, input);
    });
  });
}
function addChip(box) {
  const input = $("input", box);
  const field = box.dataset.field;
  const list = state.ed[field];
  for (const v of input.value.split(field === "tags" ? /[,;]/ : /;/).map((s) => s.trim()).filter(Boolean)) {
    if (!list.some((x) => x.toLowerCase() === v.toLowerCase())) list.push(v);
  }
  input.value = "";
  renderChips();
}
$$(".chip-input").forEach((box) => {
  const input = $("input", box);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || (e.key === "," && box.dataset.field === "tags")) { e.preventDefault(); addChip(box); }
    else if (e.key === "Backspace" && !input.value && state.ed[box.dataset.field].length) {
      state.ed[box.dataset.field].pop(); renderChips();
    }
  });
  input.addEventListener("change", () => { if (input.value.trim()) addChip(box); }); // datalist választás
  input.addEventListener("blur", () => { if (input.value.trim()) addChip(box); });
  box.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-i]");
    if (b) { state.ed[box.dataset.field].splice(+b.dataset.i, 1); renderChips(); }
    else input.focus();
  });
});

/* borítók */
function renderCoverSlots() {
  for (const side of ["front", "back"]) {
    const el = $(side === "front" ? "#edFront" : "#edBack");
    const v = state.ed[side];
    el.innerHTML = v ? `<img src="${esc(v.url)}" alt="">` : `<span>${side === "front" ? "Borító" : "Hátoldal"}</span>`;
  }
}
async function uploadTemp(file, side, doLookup) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("side", side);
  fd.append("lookup", doLookup ? "1" : "0");
  return api("/api/lookup/image", { method: "POST", body: fd });
}
$$("[data-img]").forEach((inp) => inp.addEventListener("change", async () => {
  const file = inp.files[0]; inp.value = "";
  if (!file) return;
  const side = inp.dataset.img;
  try {
    const r = await uploadTemp(file, side, false);
    state.ed[side] = { url: r._tmp_url, tmp: r._tmp };
    renderCoverSlots();
  } catch (e) { toast(e.message, true); }
}));
$$("[data-rmimg]").forEach((b) => b.addEventListener("click", async () => {
  const side = b.dataset.rmimg;
  const cur = state.ed[side];
  if (!cur) return;
  if (cur.existing) {
    if (!confirm("Biztosan törlöd a képet?")) return;
    try { await api(`/api/books/${state.ed.id}/image?side=${side}`, { method: "DELETE" }); }
    catch (e) { return toast(e.message, true); }
  }
  state.ed[side] = null;
  renderCoverSlots();
}));

/* fájlok */
function renderEdFiles() {
  const ed = state.ed;
  $("#edFiles").innerHTML = ed.files.map((f) =>
    `<div class="file"><span class="fmt-tag">${esc(f.format)}</span><span class="name">${esc(f.orig_name)}</span>
      <span class="muted small">${fmtSize(f.size)}</span>
      <button type="button" class="btn tiny ghost" data-rmfile="${f.id}">Törlés</button></div>`).join("") +
    ed.pendingFiles.map((f, i) =>
    `<div class="file"><span class="fmt-tag">új</span><span class="name">${esc(f.name)}</span>
      <span class="muted small">${fmtSize(f.size)}</span>
      <button type="button" class="btn tiny ghost" data-rmpending="${i}">✕</button></div>`).join("");
  $("#filesSection .muted").textContent = ed.id ? "" : "A fájlok a mentéskor töltődnek fel.";
}
$("#edFileInput").addEventListener("change", async (e) => {
  const files = [...e.target.files]; e.target.value = "";
  if (!state.ed.id) { state.ed.pendingFiles.push(...files); renderEdFiles(); return; }
  for (const f of files) await uploadBookFile(state.ed.id, f);
});
async function uploadBookFile(id, file) {
  const fd = new FormData(); fd.append("file", file);
  toast(`Feltöltés: ${file.name}…`);
  try {
    const b = await api(`/api/books/${id}/files`, { method: "POST", body: fd });
    if (state.ed && state.ed.id === id) { state.ed.files = b.files; renderEdFiles(); }
    toast(`Feltöltve: ${file.name}`);
    return b;
  } catch (err) { toast(`${file.name}: ${err.message}`, true); }
}
$("#edFiles").addEventListener("click", async (e) => {
  const rm = e.target.closest("[data-rmfile]");
  if (rm) {
    if (!confirm("Biztosan törlöd a fájlt? A fájl a lemezről is törlődik.")) return;
    try {
      const b = await api(`/api/files/${rm.dataset.rmfile}`, { method: "DELETE" });
      state.ed.files = b.files; renderEdFiles();
    } catch (err) { toast(err.message, true); }
  }
  const rp = e.target.closest("[data-rmpending]");
  if (rp) { state.ed.pendingFiles.splice(+rp.dataset.rmpending, 1); renderEdFiles(); }
});

/* mentés */
$("#bookForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $$(".chip-input").forEach((box) => { if ($("input", box).value.trim()) addChip(box); });
  const form = e.target;
  const ed = state.ed;
  const data = {};
  for (const k of FORM_FIELDS) data[k] = form.elements[k].value.trim() || null;
  for (const k of ["publish_year", "purchase_year"]) data[k] = data[k] ? parseInt(data[k], 10) : null;
  data.price = data.price ? parseFloat(String(data.price).replace(",", ".")) : null;
  data.wishlist = form.elements.wishlist.checked;
  data.authors = ed.authors;
  data.tags = ed.tags;
  data.formats = $$("#fmtBoxes input:checked").map((i) => i.value);
  if (ed.front && ed.front.tmp) data.cover_tmp = ed.front.tmp;
  else if (ed.front && ed.front.remote) data.cover_url = ed.front.remote;
  if (ed.back && ed.back.tmp) data.back_tmp = ed.back.tmp;
  const btns = [$("#saveBtn"), $("button[type=submit]", form)];
  btns.forEach((b) => b.disabled = true);
  try {
    const b = ed.id
      ? await api(`/api/books/${ed.id}`, { method: "PUT", json: data })
      : await api("/api/books", { method: "POST", json: data });
    ed.id = b.id;
    for (const f of ed.pendingFiles) await uploadBookFile(b.id, f);
    ed.pendingFiles = [];
    toast("Mentve ✓");
    hideSheet($("#editor")); history.back();
    await loadMeta();
    await search();
    state.current = await api(`/api/books/${b.id}`);
    renderDetail(state.current);
    if ($("#detail").classList.contains("hidden")) setTimeout(() => openSheet($("#detail")), 50);
  } catch (err) { toast(err.message, true); }
  finally { btns.forEach((b) => b.disabled = false); }
});
$("#saveBtn").addEventListener("click", (e) => { e.preventDefault(); $("#bookForm").requestSubmit(); });

$("#deleteBtn").onclick = async () => {
  const ed = state.ed;
  if (!ed.id || !confirm("Biztosan törlöd ezt a könyvet? A borítók és a feltöltött fájlok is törlődnek.")) return;
  try {
    await api(`/api/books/${ed.id}`, { method: "DELETE" });
    toast("Törölve");
    // szerkesztő + adatlap bezárása
    hideSheet($("#editor")); hideSheet($("#detail"));
    history.go(-2);
    await loadMeta(); await search();
  } catch (e) { toast(e.message, true); }
};

/* ---------------------------------------------------------------- automatikus kitöltés */
function afBusy(msg) { const s = $("#afStatus"); s.textContent = msg; s.className = "af-status busy"; }
function afDone(msg, warn) { const s = $("#afStatus"); s.textContent = msg; s.className = "af-status" + (warn ? " err" : ""); }

function applyLookup(r) {
  const form = $("#bookForm");
  const ed = state.ed;
  let n = 0;
  const set = (k, v) => {
    if (v === null || v === undefined || v === "") return;
    const el = form.elements[k];
    if (el && !el.value) { el.value = v; n++; }
  };
  set("title", r.title); set("subtitle", r.subtitle); set("isbn", r.isbn); set("publisher", r.publisher);
  set("publish_year", r.publish_year); set("description", r.description); set("shop", r.shop);
  set("link", r.link); set("price", r.price); set("edition", r.edition);
  if (r.currency) form.elements.currency.value = r.currency;
  if (r.authors && r.authors.length && !ed.authors.length) { ed.authors = [...r.authors]; n++; }
  if (r.tags && r.tags.length && !ed.tags.length) { ed.tags = r.tags.slice(0, 4); n++; }
  if (r._tmp) {
    ed[r._side === "back" ? "back" : "front"] = { url: r._tmp_url, tmp: r._tmp };
  }
  if (r.cover_url && !(ed.front && (ed.front.tmp || ed.front.existing))) {
    ed.front = { url: r.cover_url, remote: r.cover_url }; n++;
  }
  // linkkel érkező könyv: ha nincs vásárlás, legyen kívánságlistán
  if (r.link && !form.elements.purchase_year.value && !ed.id && r._from === "url") form.elements.wishlist.checked = true;
  renderChips(); renderCoverSlots(); syncWishlist();
  return n;
}

async function runLookup(label, fn) {
  afBusy(label);
  $("#afCandidates").innerHTML = "";
  try {
    const r = await fn();
    const n = applyLookup(r);
    const src = r._source ? ` (${r._source})` : "";
    if (r._warning && !r.title) afDone(r._warning, true);
    else afDone(n ? `✓ ${n} mező kitöltve${src}. Ellenőrizd, és mentsd el!` : `Nem találtam új adatot${src}.`, !n);
  } catch (e) { afDone(e.message, true); }
}

$("#afUrlBtn").onclick = () => {
  const url = $("#afUrl").value.trim();
  if (!url) return;
  runLookup("Oldal letöltése és elemzése…", async () => ({ ...(await api("/api/lookup/url", { method: "POST", json: { url } })), _from: "url" }));
};
$("#afUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#afUrlBtn").click(); } });
$("#afUrl").addEventListener("paste", () => setTimeout(() => $("#afUrlBtn").click(), 50));
$("#afIsbnBtn").onclick = () => {
  const isbn = $("#afIsbn").value.trim();
  if (!isbn) return;
  runLookup("Keresés ISBN alapján…", () => api("/api/lookup/isbn", { method: "POST", json: { isbn } }));
};
$("#afIsbn").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#afIsbnBtn").click(); } });

$("#afTitleBtn").onclick = async () => {
  const q = $("#afTitle").value.trim();
  if (!q) return;
  afBusy("Keresés…");
  try {
    const r = await api("/api/lookup/search", { method: "POST", json: { q } });
    state.ed.cands = r.items;
    afDone(r.items.length ? "Válaszd ki a megfelelőt:" : "Nincs találat. Próbáld ISBN-nel vagy linkkel.", !r.items.length);
    $("#afCandidates").innerHTML = r.items.map((c, i) => `<button type="button" class="cand" data-cand="${i}">
      ${c.cover_url ? `<img src="${esc(c.cover_url)}" alt="" loading="lazy">` : `<span class="noimg"></span>`}
      <span><div class="ct">${esc(c.title)}${c.subtitle ? " – " + esc(c.subtitle) : ""}</div>
      <div class="ca">${esc((c.authors || []).join(", "))} ${c.publish_year ? "· " + c.publish_year : ""} ${c.publisher ? "· " + esc(c.publisher) : ""} ${c._source ? "· <i>" + esc(c._source) + "</i>" : ""}</div></span>
    </button>`).join("");
  } catch (e) { afDone(e.message, true); }
};
$("#afTitle").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#afTitleBtn").click(); } });
$("#afCandidates").addEventListener("click", (e) => {
  const b = e.target.closest("[data-cand]");
  if (!b) return;
  const c = { ...state.ed.cands[+b.dataset.cand] };
  delete c.link; // Google Books link nem vásárlási link
  const n = applyLookup(c);
  $("#afCandidates").innerHTML = "";
  afDone(`✓ ${n} mező kitöltve. Ellenőrizd, és mentsd el!`);
});

for (const [id, side] of [["#afFront", "front"], ["#afBack", "back"]]) {
  $(id).addEventListener("change", (e) => {
    const file = e.target.files[0]; e.target.value = "";
    if (!file) return;
    runLookup(state.meta.ai ? "Kép feldolgozása (vonalkód, AI felismerés)… ez eltarthat fél percig" : "Kép feldolgozása (vonalkód, szövegfelismerés)…",
      () => uploadTemp(file, side, true));
  });
}

/* ---------------------------------------------------------------- e-könyv import a közös mappából */
const imp = { which: "new", groups: [], sel: new Set(), edits: {}, timer: null, wasBusy: false };

async function refreshLibraryBadge() {
  try {
    const s = await api("/api/library/status");
    $("#importBtn").classList.toggle("hidden", !s.enabled);
    $("#importBadge").textContent = s.enabled && s.new ? s.new : "";
    return s;
  } catch { return null; }
}

$("#importBtn").onclick = () => {
  hideSheet($("#menu"));
  history.replaceState({ sheet: "importer" }, "");
  $("#importer").classList.remove("hidden");
  openStack.push($("#importer"));
  imp.sel.clear(); imp.edits = {};
  loadImport();
  pollImport();
};

async function loadImport() {
  $("#impList").innerHTML = `<p class="muted">Betöltés…</p>`;
  try {
    const r = await api("/api/library/groups?which=" + imp.which);
    imp.groups = r.items;
    renderImport();
  } catch (e) { $("#impList").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

function impVisible() {
  const q = $("#impFilter").value.trim().toLowerCase();
  if (!q) return imp.groups;
  return imp.groups.filter((g) => (g.dir + " " + g.title + " " + g.authors.join(" ") + " " + g.files.map((f) => f.name).join(" "))
    .toLowerCase().includes(q));
}

function renderImport() {
  const groups = impVisible();
  const byDir = new Map();
  for (const g of groups) { if (!byDir.has(g.dir)) byDir.set(g.dir, []); byDir.get(g.dir).push(g); }
  if (!groups.length) {
    $("#impList").innerHTML = `<p class="muted">${imp.which === "new" ? "Nincs új, importálható fájl. Nyomd meg az Átvizsgálás gombot, ha új könyveket tettél a mappába." : "Nincs elrejtett fájl."}</p>`;
  } else {
    const open = !!$("#impFilter").value.trim();
    $("#impList").innerHTML = [...byDir.entries()].map(([dir, gs]) => {
      const all = gs.every((g) => imp.sel.has(g.key));
      return `<details class="imp-dir" data-dir="${esc(dir)}" ${open ? "open" : ""}>
        <summary><input type="checkbox" data-seldir ${all ? "checked" : ""} aria-label="Mappa kijelölése">
          <span class="path">📁 ${esc(dir || "(gyökér)")}</span><span class="n">${gs.length} könyv</span></summary>
        <div class="rows">${open ? gs.map(impRowHtml).join("") : ""}</div></details>`;
    }).join("");
  }
  $("#impHide").textContent = imp.which === "new" ? "Elrejtés" : "Visszaállítás";
  $("#impGo").classList.toggle("hidden", imp.which !== "new");
  updateImpSel();
}

function impRowHtml(g) {
  const e = imp.edits[g.key] || {};
  return `<div class="imp-row" data-key="${esc(g.key)}">
    <input type="checkbox" data-sel ${imp.sel.has(g.key) ? "checked" : ""} aria-label="Kijelölés">
    <div class="fields">
      <input data-f="title" value="${esc(e.title ?? g.title)}" placeholder="Cím">
      <input data-f="authors" value="${esc((e.authors ?? g.authors).join(", "))}" placeholder="Szerző(k), vesszővel" list="dlAuthors">
    </div>
    <div class="files-line">${g.files.map((f) => `<span class="fmt-tag">${esc(f.format)}</span>`).join("")}
      <span class="fname" title="${esc(g.files.map((f) => f.name).join("\n"))}">${esc(g.files[0].name)}</span>
      <span>${fmtSize(g.size)}</span>${g.has_meta ? `<span title="A fájlban leírás/ISBN is van">ℹ</span>` : ""}</div>
  </div>`;
}

function updateImpSel() {
  $("#impSel").textContent = `${imp.sel.size} kijelölve`;
  $("#impGo").disabled = !imp.sel.size;
  $("#impHide").disabled = !imp.sel.size;
}

$("#impList").addEventListener("toggle", (e) => {
  const d = e.target;
  if (!d.classList || !d.classList.contains("imp-dir") || !d.open) return;
  const rows = $(".rows", d);
  if (!rows.children.length) {
    rows.innerHTML = impVisible().filter((g) => g.dir === d.dataset.dir).map(impRowHtml).join("");
  }
}, true);
$("#impList").addEventListener("click", (e) => {
  const cb = e.target.closest("[data-seldir]");
  if (cb) {
    e.stopPropagation();
    const d = cb.closest(".imp-dir");
    const gs = impVisible().filter((g) => g.dir === d.dataset.dir);
    gs.forEach((g) => cb.checked ? imp.sel.add(g.key) : imp.sel.delete(g.key));
    $$("[data-sel]", d).forEach((x) => x.checked = cb.checked);
    updateImpSel();
  }
});
$("#impList").addEventListener("change", (e) => {
  const row = e.target.closest(".imp-row");
  if (!row) return;
  const key = row.dataset.key;
  if (e.target.matches("[data-sel]")) {
    e.target.checked ? imp.sel.add(key) : imp.sel.delete(key);
    const d = row.closest(".imp-dir");
    $("[data-seldir]", d).checked = $$("[data-sel]", d).every((x) => x.checked);
    updateImpSel();
  }
});
$("#impList").addEventListener("input", (e) => {
  const row = e.target.closest(".imp-row");
  if (!row || !e.target.dataset.f) return;
  const g = imp.groups.find((x) => x.key === row.dataset.key);
  const ed = imp.edits[g.key] = imp.edits[g.key] || {};
  if (e.target.dataset.f === "title") ed.title = e.target.value;
  else ed.authors = e.target.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
  if (!imp.sel.has(g.key)) { imp.sel.add(g.key); $("[data-sel]", row).checked = true; updateImpSel(); }
});
let impFilterTimer;
$("#impFilter").addEventListener("input", () => { clearTimeout(impFilterTimer); impFilterTimer = setTimeout(renderImport, 250); });
$("#impWhich").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#impWhich button").forEach((x) => x.classList.toggle("on", x === b));
  imp.which = b.dataset.v; imp.sel.clear();
  loadImport();
});

$("#scanBtn").onclick = async () => {
  try { await api("/api/library/scan", { method: "POST" }); pollImport(); }
  catch (e) { toast(e.message, true); }
};
$("#impHide").onclick = async () => {
  const ids = imp.groups.filter((g) => imp.sel.has(g.key)).flatMap((g) => g.files.map((f) => f.id));
  if (!ids.length) return;
  try {
    await api("/api/library/ignore", { method: "POST", json: { file_ids: ids, ignored: imp.which === "new" } });
    imp.sel.clear(); await loadImport(); pollImport();
  } catch (e) { toast(e.message, true); }
};
$("#impGo").onclick = async () => {
  const items = imp.groups.filter((g) => imp.sel.has(g.key)).map((g) => {
    const e = imp.edits[g.key] || {};
    return { file_ids: g.files.map((f) => f.id), title: (e.title ?? g.title).trim(), authors: e.authors ?? g.authors };
  });
  if (!items.length) return;
  try {
    await api("/api/library/import", { method: "POST", json: { items, owner: $("#impOwner").value.trim() } });
    imp.sel.clear(); updateImpSel();
    pollImport();
  } catch (e) { toast(e.message, true); }
};

async function pollImport() {
  clearTimeout(imp.timer);
  const s = await refreshLibraryBadge();
  if (!s) return;
  const el = $("#impStatus");
  $("#impNew").textContent = s.new ?? ""; $("#impIgn").textContent = s.ignored ?? "";
  const busy = s.scanning || s.importing;
  if (s.scanning) { el.className = "af-status busy"; el.textContent = `Mappa átvizsgálása… ${s.scan_done}/${s.scan_total || "?"} fájl`; }
  else if (s.importing) { el.className = "af-status busy"; el.textContent = `Importálás… ${s.import_done}/${s.import_total} könyv (borítók kinyerése)`; }
  else {
    el.className = "af-status";
    el.textContent = `${s.imported} fájl importálva · ${s.new} új · ${s.ignored} elrejtett` +
      (s.missing ? ` · ${s.missing} hiányzó (törölték/áthelyezték)` : "") +
      (s.last_scan ? ` · utolsó átvizsgálás: ${s.last_scan}` : "") +
      (s.import_error ? ` · hiba: ${s.import_error}` : "") + (s.scan_error ? ` · hiba: ${s.scan_error}` : "");
  }
  if (imp.wasBusy && !busy) { await loadImport(); await loadMeta(); await search(); toast("Kész ✓"); }
  imp.wasBusy = busy;
  $("#scanBtn").disabled = busy; $("#impGo").disabled = busy || !imp.sel.size;
  if (!$("#importer").classList.contains("hidden")) imp.timer = setTimeout(pollImport, busy ? 1500 : 8000);
}

/* közvetlen link egy könyvre (pl. az olvasóból vissza): /#book-12 */
function openFromHash() {
  const m = location.hash.match(/^#book-(\d+)$/);
  if (m) { history.replaceState(null, "", "/"); openDetail(+m[1]); }
}

boot();
