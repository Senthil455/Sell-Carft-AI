/* SellCraft AI — front end. No framework, no build step, no network calls
   except to this app's own server on 127.0.0.1. */

const $ = (id) => document.getElementById(id);
const state = {
  photos: [],          // data URLs, downscaled in-browser
  platform: "etsy",
  tone: "warm",
  listing: null,       // last finished listing
  streaming: false,
};

/* ---------------------------------------------------------- tiny helpers */

function toast(msg, ms = 2200) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

async function api(path, body, method) {
  const res = await fetch(path, {
    method: method || (body ? "POST" : "GET"),
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  return res.json();
}

/* ---------------------------------------------------------- status + setup */

async function refreshStatus() {
  const s = $("status");
  try {
    const st = await api("/api/status");
    if (st.ollama) {
      s.className = "pill pill-ok";
      s.textContent = `${st.model} · local`;
      $("offline").classList.add("hidden");
    } else {
      s.className = "pill pill-bad";
      s.textContent = "ollama offline";
      $("offline").classList.remove("hidden");
    }
    const sel = $("model");
    sel.innerHTML = "";
    (st.models || []).forEach((m) => {
      const o = document.createElement("option");
      o.value = m; o.textContent = m;
      if (m === st.model) o.selected = true;
      sel.appendChild(o);
    });
    $("libCount").textContent = st.library_count ?? 0;
  } catch {
    s.className = "pill pill-bad";
    s.textContent = "server unreachable";
  }
}

async function loadSettings() {
  const s = await api("/api/settings");
  if (s.shop_name) $("shop_name").value = s.shop_name;
  if (s.voice) $("voice").value = s.voice;
}

/* ---------------------------------------------------------- photos */

function fileToPhoto(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const max = 1000;
      const scale = Math.min(1, max / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * scale);
      c.height = Math.round(img.height * scale);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      resolve(c.toDataURL("image/jpeg", 0.85)); // downscaled before it even hits your disk
    };
    img.src = url;
  });
}

async function addFiles(files) {
  const room = 4 - state.photos.length;
  const list = [...files].filter((f) => f.type.startsWith("image/")).slice(0, room);
  if (!list.length) {
    if (room <= 0) toast("Four photos is the max — and plenty for Gemma to read.");
    return;
  }
  for (const f of list) state.photos.push(await fileToPhoto(f));
  if (files.length > room) toast("Kept the first 4 photos.");
  renderThumbs();
}

function renderThumbs() {
  $("thumbs").innerHTML = "";
  state.photos.forEach((src, i) => {
    const d = document.createElement("div");
    d.className = "thumb";
    d.innerHTML = `<img src="${src}" alt="photo ${i + 1}" /><button title="remove" aria-label="remove photo">×</button>`;
    d.querySelector("button").onclick = () => { state.photos.splice(i, 1); renderThumbs(); };
    $("thumbs").appendChild(d);
  });
}

/* ---------------------------------------------------------- streaming parse */

const LABELS = ["NOTES", "TITLE", "DESCRIPTION", "TAGS", "KEYWORDS"];

function parseStream(buf) {
  const out = { NOTES: "", TITLE: "", DESCRIPTION: "", TAGS: "" };
  let cur = null;
  for (const raw of buf.split("\n")) {
    const s = raw.trim().replace(/^[*#_\s]+|[*#_\s]+$/g, "");
    const u = s.toUpperCase().replace(/:$/, "");
    if (LABELS.includes(u) && s.length < 24) { cur = u === "KEYWORDS" ? "TAGS" : u; continue; }
    if (cur) out[cur] += raw + "\n";
  }
  for (const k of Object.keys(out)) out[k] = out[k].trim();
  return out;
}

function renderLive(p, done) {
  const notesEl = $("out-notes"), titleEl = $("out-title"), descEl = $("out-description"), tagsEl = $("out-tags");
  notesEl.classList.toggle("empty", !p.NOTES);
  notesEl.textContent = p.NOTES || "reading your photos…";
  titleEl.classList.toggle("empty", !p.TITLE);
  titleEl.textContent = p.TITLE || "—";
  descEl.classList.toggle("empty", !p.DESCRIPTION);
  descEl.textContent = p.DESCRIPTION || "—";
  titleEl.classList.toggle("caret", !done && !!p.TITLE);
  descEl.classList.toggle("caret", !done && !!p.DESCRIPTION);
  const limit = state.platform === "ebay" ? 80 : 140;
  $("title-count").textContent = `${p.TITLE.length} / ${limit}`;
  $("title-count").classList.toggle("over", p.TITLE.length > limit);
  const tags = p.TAGS.split(/[,\n]+/).map((t) => t.trim().replace(/^[-*#\s]+/, "").toLowerCase()).filter(Boolean);
  tagsEl.innerHTML = "";
  tags.forEach((t) => {
    const c = document.createElement("span");
    c.className = "tag-chip" + (state.platform === "etsy" && t.length > 20 ? " long" : "");
    c.textContent = t;
    tagsEl.appendChild(c);
  });
  const cap = state.platform === "ebay" ? 8 : 13;
  $("tag-count").textContent = `${tags.length} / ${cap}`;
  $("tag-count").classList.toggle("over", tags.length > cap);
}

/* ---------------------------------------------------------- generate */

async function generate(opts = {}) {
  if (state.streaming) return;
  if (!state.photos.length && !opts.allowNoPhoto) {
    toast("Add at least one photo — that's what Gemma reads.");
    return;
  }
  state.streaming = true;
  $("write").disabled = true;
  $("regen").disabled = true;

  const facts = {
    materials: $("materials").value, size: $("size").value, price: $("price").value,
    keywords: $("keywords").value, notes: $("notes").value,
  };
  const payload = {
    photos: state.photos, facts, platform: state.platform, tone: state.tone,
    feedback: opts.feedback || "",
    previous: opts.previous || "",
  };
  $("meta").textContent = "writing…";

  const t0 = performance.now();
  renderLive({ NOTES: "", TITLE: "", DESCRIPTION: "", TAGS: "" }, false);
  let buf = "";
  try {
    const res = await fetch("/api/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let carry = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      carry += dec.decode(value, { stream: true });
      const lines = carry.split("\n");
      carry = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.error) { toast(ev.error, 6000); $("meta").textContent = "error"; return; }
        if (ev.t) { buf += ev.t; renderLive(parseStream(buf), false); }
        if (ev.done) {
          state.listing = ev.listing;
          renderLive({
            NOTES: ev.listing.notes, TITLE: ev.listing.title,
            DESCRIPTION: ev.listing.description, TAGS: ev.listing.tags.join(", "),
          }, true);
          const secs = ((performance.now() - t0) / 1000).toFixed(1);
          $("meta").textContent = `${ev.listing.model} · ${secs}s · ${ev.listing.tok_s} tok/s · ${ev.listing.photos} photo${ev.listing.photos === 1 ? "" : "s"}`;
          if (ev.listing.title_truncated) toast("Title was trimmed to fit the character limit.");
          if (ev.listing.feedback === undefined && opts.feedback) ev.listing.feedback = opts.feedback;
        }
      }
    }
  } catch (e) {
    toast("Something broke talking to the local server: " + e.message, 6000);
  } finally {
    state.streaming = false;
    $("write").disabled = false;
    $("regen").disabled = false;
    ["out-title", "out-description"].forEach((id) => $(id).classList.remove("caret"));
  }
}

/* ---------------------------------------------------------- copy / save / library */

function plainText(l) {
  return `${l.title}\n\n${l.description}\n\nTags: ${(l.tags || []).join(", ")}`;
}

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`); }
  catch { toast("Copy failed — your browser blocked the clipboard."); }
}

async function saveListing() {
  if (!state.listing) { toast("Nothing to save yet."); return; }
  const l = state.listing;
  await api("/api/save", {
    ...l, tags: l.tags, facts: {
      materials: $("materials").value, size: $("size").value, price: $("price").value,
      keywords: $("keywords").value, notes: $("notes").value,
    },
    feedback: l.feedback || "",
  });
  toast("Saved to your local library");
  refreshStatus(); loadLibrary();
}

async function loadLibrary() {
  const rows = await api("/api/library");
  const wrap = $("lib");
  wrap.innerHTML = "";
  if (!rows.length) {
    wrap.innerHTML = `<p class="hint">Nothing saved yet. Listings you save live in a SQLite file on this machine.</p>`;
    $("libCount").textContent = "0";
    return;
  }
  $("libCount").textContent = rows.length;
  rows.forEach((r) => {
    const d = document.createElement("div");
    d.className = "lib-card";
    d.innerHTML = `<h4></h4><p></p>
      <div class="row2"><span>${r.platform} · ${r.created_at}</span>
      <button class="lib-del">delete</button></div>`;
    d.querySelector("h4").textContent = r.title || "(untitled)";
    d.querySelector("p").textContent = r.description;
    d.querySelector(".lib-del").onclick = async (e) => {
      e.stopPropagation();
      await api("/api/delete", { id: r.id });
      loadLibrary(); refreshStatus();
    };
    d.onclick = () => {
      state.listing = { ...r, title_truncated: false, tok_s: 0, seconds: 0 };
      renderLive({ NOTES: r.notes, TITLE: r.title, DESCRIPTION: r.description, TAGS: (r.tags || []).join(", ") }, true);
      $("meta").textContent = `loaded from library · ${r.created_at}`;
      document.querySelector(".result").scrollIntoView({ behavior: "smooth", block: "start" });
    };
    wrap.appendChild(d);
  });
}

/* ---------------------------------------------------------- wiring */

function wire() {
  const drop = $("drop"), file = $("file");
  file.onchange = () => { addFiles(file.files); file.value = ""; };
  drop.onclick = () => file.click();
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } };
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
  window.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) addFiles(files);
  });

  document.querySelectorAll(".seg-btn").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".seg-btn").forEach((x) => { x.classList.remove("active"); x.setAttribute("aria-selected", "false"); });
    b.classList.add("active"); b.setAttribute("aria-selected", "true");
    state.platform = b.dataset.platform;
    if (state.listing) renderLive({ NOTES: $("out-notes").textContent, TITLE: $("out-title").textContent, DESCRIPTION: $("out-description").textContent, TAGS: state.listing.tags.join(", ") }, true);
  });
  document.querySelectorAll(".tone").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".tone").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); state.tone = b.dataset.tone;
  });

  $("write").onclick = () => generate();
  $("regen").onclick = () => {
    if (!state.listing) { toast("Write a first draft before regenerating."); return; }
    const feedback = $("feedback").value.trim();
    if (!feedback) { toast("Tell it what to change first — that's the fun part."); return; }
    const prev = plainText(state.listing);
    generate({ feedback, previous: prev });
  };
  $("save").onclick = saveListing;
  $("copyAll").onclick = () => state.listing ? copy(plainText(state.listing), "Listing") : toast("Nothing to copy yet.");
  document.querySelectorAll(".copy").forEach((b) => b.onclick = () => {
    const l = state.listing;
    const which = b.dataset.copy;
    if (!l && which !== "notes") return toast("Nothing to copy yet.");
    if (which === "title") copy($("out-title").textContent, "Title");
    if (which === "description") copy($("out-description").textContent, "Description");
    if (which === "notes") copy($("out-notes").textContent, "Notes");
    if (which === "tags") copy((l?.tags || []).join(", "), "Tags");
  });
  $("saveSettings").onclick = async () => {
    await api("/api/settings", {
      shop_name: $("shop_name").value, voice: $("voice").value,
      model: $("model").value, tone: state.tone,
    });
    toast("Saved — the next listing will use this setup.");
    refreshStatus();
  };
  $("recheck").onclick = refreshStatus;
  $("libWrap").addEventListener("toggle", () => { if ($("libWrap").open) loadLibrary(); });
}

wire();
refreshStatus();
loadSettings();
loadLibrary();
setInterval(refreshStatus, 30000);
