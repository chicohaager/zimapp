/*
 * zimapp web UI.
 *
 * The flow is deliberately two-staged: /api/analyze only reads the source and
 * shows what is in it (services, variables, roles); only /api/generate builds
 * the ZimaOS compose from it. That way you see what the converter found before
 * generating, instead of facing a YAML black box.
 *
 * Errors are always shown, never swallowed: every request goes through post(),
 * which throws on !ok or {error:…}, and every call has a catch branch that
 * writes the message into the corresponding box.
 */

const $ = (id) => document.getElementById(id);

const CATEGORIES = [
  "Backup", "Cloud", "Developer", "Documents", "Entertainment", "Finance",
  "Games", "Home Automation", "Media", "Networking", "Photography",
  "Productivity", "Security", "Social", "Utilities",
];

let state = { source: null, kind: null, services: [], variables: [], yaml: "",
              blueprint: null, blueprints: [],
              fileText: null, fileName: null };

// --- Helpers --------------------------------------------------------------

async function post(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data;
  try {
    data = await resp.json();
  } catch (e) {
    throw new Error(`Server answered with HTTP ${resp.status} and no JSON.`);
  }
  if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function msg(kind, text) {
  const div = document.createElement("div");
  div.className = `msg msg-${kind}`;
  div.textContent = text;
  return div;
}

function msgList(kind, title, items) {
  const div = document.createElement("div");
  div.className = `msg msg-${kind}`;
  const b = document.createElement("b");
  b.textContent = title;
  div.appendChild(b);
  const ul = document.createElement("ul");
  ul.className = "msg-list";
  items.forEach((t) => {
    const li = document.createElement("li");
    li.textContent = t;
    ul.appendChild(li);
  });
  div.appendChild(ul);
  return div;
}

function clear(el) { el.innerHTML = ""; }

function busy(button, on, label) {
  button.disabled = on;
  if (on) {
    button.dataset.label = button.textContent;
    button.innerHTML = `<span class="spinner"></span>${label || "Working…"}`;
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
  }
}

function setStep(n) {
  for (let i = 1; i <= 4; i++) {
    const el = $(`step-${i}`);
    el.classList.toggle("active", i === n);
    el.classList.toggle("done", i < n);
  }
}

// --- 1: analyze -----------------------------------------------------------

async function analyze() {
  const box = $("analyze-msgs");
  clear(box);
  const url = $("src-url").value.trim();
  if (!url && !state.fileText) {
    box.appendChild(msg("err", "Please give a URL, or open a .yml from this computer."));
    return;
  }

  busy($("btn-analyze"), true, "Loading…");
  try {
    // An opened file and a URL would be two sources for one answer. Opening a
    // file clears the URL and vice versa, so there is never a silent winner.
    const data = await post("/api/analyze", state.fileText
      ? { text: state.fileText, filename: state.fileName }
      : { url });
    state.source = url;
    state.kind = data.kind;
    state.services = data.services;
    state.variables = data.variables;

    renderServices(data);
    renderMeta(data);
    renderVars(data.variables);

    $("card-stack").hidden = data.kind !== "compose";
    $("card-meta").hidden = false;
    $("card-vars").hidden = data.variables.length === 0;
    $("card-target").hidden = false;
    $("fld-image").hidden = data.kind !== "dockerfile";

    box.appendChild(msg("ok",
      data.kind === "compose"
        ? `Compose detected: ${data.services.length} service(s) from ${data.source}`
        : `Dockerfile detected (FROM ${data.dockerfile.base || "?"}) — ports: ${
            data.dockerfile.ports.join(", ") || "none"}`));
    if (data.warnings && data.warnings.length) box.appendChild(msgList("warn", "Notes:", data.warnings));
    setStep(2);
  } catch (e) {
    box.appendChild(msg("err", e.message));
  } finally {
    busy($("btn-analyze"), false);
  }
}

function renderServices(data) {
  const list = $("svc-list");
  clear(list);
  data.services.forEach((s) => {
    const card = document.createElement("div");
    card.className = "svc" + (s.name === data.suggested_main ? " is-main" : "");

    const head = document.createElement("div");
    head.className = "svc-head";
    const name = document.createElement("span");
    name.className = "svc-name";
    name.textContent = s.name;
    const badge = document.createElement("span");
    const isMain = s.name === data.suggested_main;
    badge.className = "badge " + (isMain ? "badge-main" : "badge-support");
    badge.textContent = isMain ? "main" : s.role;
    head.append(name, badge);

    const dl = document.createElement("dl");
    const add = (k, v) => {
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = v;
      dl.append(dt, dd);
    };
    add("Image", s.image || (s.build ? "— (build: only)" : "—"));
    add("Ports", s.ports.length ? s.ports.join(", ") : "—");
    add("Volumes", String(s.volumes));
    if (s.depends_on.length) add("depends_on", s.depends_on.join(", "));
    card.append(head, dl);
    list.appendChild(card);
  });
}

function renderMeta(data) {
  $("m-name").value = data.suggested_name || "";
  $("m-title").value = data.suggested_title || "";
  // Only what was really found. Left empty otherwise — the note in the analyze
  // result says which sources were tried, and an empty tile is easier to spot
  // than a reachable placeholder that shows a foreign logo.
  $("m-icon").value = data.suggested_icon || "";

  const main = $("m-main");
  clear(main);
  data.services.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = s.name + (s.role === "support" ? "  (infrastructure)" : "");
    opt.selected = s.name === data.suggested_main;
    main.appendChild(opt);
  });
  if (!data.services.length) {
    const opt = document.createElement("option");
    opt.value = "app"; opt.textContent = "app";
    main.appendChild(opt);
  }

  // After the analysis the fields hold values derived from the source. A selected
  // blueprint is the more specific statement, so it goes on top — including
  // `main`, whose options only exist now.
  if (state.blueprint) {
    const bp = state.blueprints.find((b) => b.name === state.blueprint);
    if (bp) setTimeout(() => applyBlueprintMeta(bp), 0);
  }

  const cat = $("m-category");
  if (!cat.options.length) {
    CATEGORIES.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c; opt.textContent = c;
      opt.selected = c === "Utilities";
      cat.appendChild(opt);
    });
  }
}

function renderVars(variables) {
  const list = $("var-list");
  clear(list);
  variables.forEach((v) => {
    const fld = document.createElement("div");
    fld.className = "fld";
    const label = document.createElement("label");
    label.setAttribute("for", `var-${v.name}`);
    label.textContent = v.name;
    if (v.default) {
      const hint = document.createElement("span");
      hint.className = "hint";
      // When both exist, the env file is what gets used — say so, and name the
      // compose default that it overrides, so the value here matches the output.
      hint.textContent = v.from_env_file
        ? ` from ${v.from_env_file.split("/").pop()}: ${v.default}` +
          (v.inline_default ? ` (overrides the compose default ${v.inline_default})` : "")
        : ` default: ${v.default}`;
      label.appendChild(hint);
    } else if (v.secret) {
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = " no default — will be generated if left empty";
      label.appendChild(hint);
    } else {
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = " no default — required";
      label.appendChild(hint);
    }
    const input = document.createElement("input");
    input.type = "text";
    input.id = `var-${v.name}`;
    input.dataset.var = v.name;
    input.placeholder = v.default || (v.secret ? "(will be generated)" : "");
    fld.append(label, input);
    list.appendChild(fld);
  });
}

// --- 2: generate ----------------------------------------------------------

function collectMeta() {
  return {
    name: $("m-name").value.trim(),
    title: $("m-title").value.trim(),
    main: $("m-main").value,
    category: $("m-category").value,
    author: $("m-author").value.trim(),
    icon: $("m-icon").value.trim(),
    tagline: $("m-tagline").value.trim(),
    description: $("m-description").value.trim(),
    index: $("m-index").value.trim() || "/",
    memory: $("m-memory").value.trim(),
    cpus: $("m-cpus").value.trim(),
    image: $("m-image").value.trim(),
    port: $("m-port").value.trim(),
  };
}

function collectVars() {
  const out = {};
  document.querySelectorAll("#var-list input[data-var]").forEach((i) => {
    if (i.value.trim()) out[i.dataset.var] = i.value.trim();
  });
  return out;
}

async function generate() {
  const box = $("generate-msgs");
  clear(box);
  busy($("btn-generate"), true, "Generating…");
  try {
    const data = await post("/api/generate", {
      url: state.source,
      text: state.fileText || null,
      filename: state.fileName || null,
      blueprint: state.blueprint || null,
      meta: collectMeta(),
      variables: collectVars(),
      options: {
        autofill_secrets: $("opt-secrets").checked,
        check_ports: $("opt-ports").checked,
        host: $("t-host").value.trim(),
        ssh_user: $("t-ssh").value.trim(),
        // The port check reads the app grid API, which needs the same
        // credentials as the install step further down. They are sent only
        // when the box is ticked and the fields are filled — the server
        // stores nothing either way.
        user: $("opt-ports").checked ? $("i-user").value : "",
        password: $("opt-ports").checked ? $("i-pass").value : "",
      },
    });
    state.yaml = data.yaml;
    // Kept so "Re-validate" can show them again — validation cannot derive them.
    state.warnings = data.warnings || [];
    $("out-yaml").value = data.yaml;
    $("card-result").hidden = false;
    $("card-install").hidden = false;

    renderSecrets(data.generated);
    renderResult(data.problems, data.warnings, data.main, data.web_port);
    setStep(data.problems.length ? 3 : 4);
    $("card-result").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    box.appendChild(msg("err", e.message));
  } finally {
    busy($("btn-generate"), false);
  }
}

function renderSecrets(generated) {
  const names = Object.keys(generated || {});
  const box = $("secrets-box");
  box.hidden = names.length === 0;
  const dl = $("secrets-list");
  clear(dl);
  names.forEach((n) => {
    const dt = document.createElement("dt"); dt.textContent = n;
    const dd = document.createElement("dd"); dd.textContent = generated[n];
    dl.append(dt, dd);
  });
}

function renderResult(problems, warnings, main, webPort) {
  const box = $("result-msgs");
  clear(box);
  if (problems.length) {
    box.appendChild(msgList("err", "Blockers — ZimaOS will not install it like this:", problems));
  } else if (warnings.length) {
    // Green above a list of notes reads as "all good", and then the notes go
    // unread — that is how a compose with four unstartable bind mounts was
    // reported as satisfying every rule (2026-07-21). No blocker found is not
    // the same as nothing left to look at, so the banner says which one it is.
    box.appendChild(msg("warn",
      `No blocker found, but ${warnings.length} ${warnings.length === 1 ? "note" : "notes"} ` +
      `to read before installing. WebUI service: ${main}, host port ${webPort}.`));
  } else {
    box.appendChild(msg("ok",
      `Satisfies every ZimaOS rule checked. WebUI service: ${main}, host port ${webPort}.`));
  }
  if (warnings.length) box.appendChild(msgList("warn", "Notes:", warnings));
}

async function revalidate() {
  const box = $("result-msgs");
  clear(box);
  try {
    const data = await post("/api/validate", { yaml: $("out-yaml").value });
    renderResult(data.problems, data.warnings, data.main || "?", data.port_map || "?");
    // /api/validate only sees the finished YAML, where every source has already
    // been rewritten — it cannot reproduce the conversion notes ("/srv/x → /DATA…",
    // the tmpfs/exit-127 warning). Dropping them turned a warned result into the
    // green "satisfies every rule" banner and certified a compose that dies at the
    // next reboot. They are shown again, labelled for what they are.
    if (state.warnings && state.warnings.length) {
      box.appendChild(msgList("warn",
        "From the conversion (not re-checked — validation only sees the finished YAML):",
        state.warnings));
    }
  } catch (e) {
    box.appendChild(msg("err", e.message));
  }
}

// --- 3: install -----------------------------------------------------------

async function install() {
  const box = $("install-msgs");
  const out = $("install-out");
  clear(box);
  out.hidden = true;
  busy($("btn-install"), true, "Installing…");
  try {
    const data = await post("/api/install", {
      yaml: $("out-yaml").value,
      host: $("t-host").value.trim(),
      user: $("i-user").value,
      password: $("i-pass").value,
    });
    box.appendChild(msg(data.status === 200 ? "ok" : "err",
      data.status === 200
        ? `HTTP 200 — accepted. The installation runs asynchronously; the tile only appears once the images are pulled.`
        : `HTTP ${data.status} — ZimaOS rejected the compose.`));
    out.hidden = false;
    out.textContent = data.body;
    if (data.status === 200) {
      setStep(4);
      runPostCheck();          // "accepted" is not "usable" — go and look
      // Only a blueprint carries expectations — without one there is nothing
      // to verify, so the button stays hidden rather than lying.
      $("btn-verify").hidden = !state.blueprint;
    }
  } catch (e) {
    box.appendChild(msg("err", e.message));
  } finally {
    busy($("btn-install"), false);
  }
}

async function runPostCheck() {
  const box = $("install-msgs");
  const out = $("install-out");
  out.hidden = false;
  out.textContent = "Waiting for the app (HTTP 200 means accepted, not done)…";
  try {
    const data = await post("/api/postcheck", {
      name: $("m-name").value.trim(),
      host: $("t-host").value.trim(),
      ssh_user: $("t-ssh").value.trim(),
      user: $("i-user").value,
      password: $("i-pass").value,
      yaml: $("out-yaml").value,
    });
    out.textContent = data.steps.map((s) =>
      `[${s.ok ? "ok" : "FAIL"}] ${s.step} — ${s.detail}` + (s.hint ? `\n       → ${s.hint}` : "")
    ).join("\n");
    box.appendChild(msg(data.ok ? "ok" : "err",
      data.ok ? "The app is up and reachable."
              : "The app is installed, but not usable as it stands — see below."));
  } catch (e) {
    box.appendChild(msg("err", `Follow-up check failed: ${e.message}`));
  }
}

async function uninstall() {
  const box = $("install-msgs");
  const out = $("install-out");
  clear(box);
  const name = $("m-name").value.trim();
  if (!name) { box.appendChild(msg("err", "No app ID — nothing to uninstall.")); return; }
  if (!confirm(`Remove app '${name}' from ${$("t-host").value} — including /DATA/AppData/${name}?`)) return;

  busy($("btn-uninstall"), true, "Removing…");
  try {
    const data = await post("/api/uninstall", {
      name, host: $("t-host").value.trim(),
      user: $("i-user").value, password: $("i-pass").value,
    });
    box.appendChild(msg(data.status === 200 ? "ok" : "err", `HTTP ${data.status}`));
    out.hidden = false;
    out.textContent = data.body;
  } catch (e) {
    box.appendChild(msg("err", e.message));
  } finally {
    busy($("btn-uninstall"), false);
  }
}

// --- Odds and ends --------------------------------------------------------

function download() {
  const name = ($("m-name").value.trim() || "app") + ".yml";
  const blob = new Blob([$("out-yaml").value], { type: "application/yaml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function copyYaml() {
  const btn = $("btn-copy");
  try {
    await navigator.clipboard.writeText($("out-yaml").value);
    btn.textContent = "Copied";
  } catch (e) {
    // Without clipboard permission (http on a remote host) say honestly what is going on.
    btn.textContent = "Clipboard blocked — please select manually";
  }
  setTimeout(() => { btn.textContent = "Copy"; }, 2500);
}

async function loadDefaults() {
  try {
    const resp = await fetch("/api/defaults");
    const d = await resp.json();
    $("t-host").value = d.host || "";
    $("t-ssh").value = d.ssh_user || "";
    $("i-user").value = d.user || "";
    $("app-version").textContent = d.version || "v2";
    renderBlueprints(d.blueprints || []);
  } catch (e) {
    /* Defaults are convenience, not a blocker — the fields just stay empty. */
  }
}

function renderBlueprints(list) {
  state.blueprints = list;
  if (!list.length) return;                       // no catalogue → no dropdown
  const sel = $("src-blueprint");
  clear(sel);
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "— none (paste a URL below) —";
  sel.appendChild(none);
  list.forEach((bp) => {
    const opt = document.createElement("option");
    opt.value = bp.name;
    opt.textContent = `${bp.title} · ${bp.category}`;
    sel.appendChild(opt);
  });
  $("fld-blueprint").hidden = false;
  // Rebuilding the list resets the <select> to the first option. Without putting
  // the selection back, state.blueprint stayed set while the UI showed "none" —
  // and the next Generate quietly applied a recipe nobody could see.
  if (state.blueprint && list.some((b) => b.name === state.blueprint)) {
    sel.value = state.blueprint;
  } else if (state.blueprint) {
    state.blueprint = null;
    $("blueprint-proof").textContent = "";
  }
  // Assignment, not addEventListener: the list is re-rendered after saving, and
  // stacked listeners would fire the old closures too.
  sel.onchange = (() => {
    const bp = list.find((b) => b.name === sel.value);
    state.blueprint = bp ? bp.name : null;
    // A blueprint IS a source. Setting the URL field by assignment fires no
    // `input` event, so the listener that drops an opened file never ran — file
    // and URL were then live at once and the server mixed them.
    if (bp && state.fileText) clearOpenedFile("Opened file dropped — the blueprint is used now.");
    $("src-url").value = bp ? bp.source : "";
    state.source = bp ? bp.source : null;
    if (bp) applyBlueprintMeta(bp);
    // Say how old the proof is instead of just claiming "tested".
    $("blueprint-proof").textContent = bp
      ? (bp.verified && bp.verified.date
          ? `${bp.expectations} expectation(s) · last verified ${bp.verified.date} on ${bp.verified.host || "?"}`
          : `${bp.expectations} expectation(s) · NEVER VERIFIED`)
      : "";
  });
}

// The form's own defaults used to win over every blueprint value, because the
// server only filled fields the form left empty and the form was never empty
// (hard-coded value= on tagline/memory/cpus/index, pre-selected category/main).
// Filling the fields here instead makes the recipe visible AND editable.
const BLUEPRINT_FIELDS = { title: "m-title", category: "m-category", tagline: "m-tagline",
  description: "m-description", icon: "m-icon", memory: "m-memory", cpus: "m-cpus",
  index: "m-index", main: "m-main", author: "m-author", image: "m-image", port: "m-port" };

function applyBlueprintMeta(bp) {
  Object.entries(BLUEPRINT_FIELDS).forEach(([key, id]) => {
    const el = $(id);
    if (!el || !bp[key]) return;
    // A <select> can only take a value it actually offers (m-main is filled from
    // the analysis, so before Analyze it is still empty).
    if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === bp[key])) return;
    el.value = bp[key];
  });
}

function clearOpenedFile(note) {
  state.fileText = null;
  state.fileName = null;
  $("src-file").value = "";                       // so the same file can be picked again
  $("src-file-note").textContent = note || "";
}

function invalidateFromStep2() {
  // Everything below step 1 describes the previous source. Leaving it on screen
  // invites a Generate that mixes the old analysis with the new source.
  ["card-stack", "card-meta", "card-vars", "card-target", "card-result", "card-install"]
    .forEach((id) => { const el = $(id); if (el) el.hidden = true; });
  state.kind = null;
  state.services = [];
  state.variables = [];
  state.yaml = "";
  state.warnings = [];
  setStep(1);
}

async function openFile(file) {
  const box = $("analyze-msgs");
  clear(box);
  // Deliberately below the server's MAX_BODY (8 MB): the JSON envelope escapes
  // every newline and quote, so the body is bigger than the file. Comparing the
  // raw size against the same 2 MB the server applied to the body meant a 1.9 MB
  // file was accepted here and rejected with 413 there — two contradicting
  // messages and no way to convert the file at all.
  const MAX_OPEN_FILE = 4 * 1024 * 1024;
  if (file.size > MAX_OPEN_FILE) {
    box.appendChild(msg("err",
      `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB — the limit is 4 MB. ` +
      `Nothing was read.`));
    return;
  }
  try {
    const text = await file.text();
    if (!text.trim()) {
      box.appendChild(msg("err", `${file.name} is empty.`));
      clearOpenedFile();
      return;
    }
    state.fileText = text;
    state.fileName = file.name;
    // The URL field would otherwise still show a source that is no longer used.
    $("src-url").value = "";
    $("src-blueprint").value = "";
    state.blueprint = null;
    $("blueprint-proof").textContent = "";
    // state.source still held the previously analyzed URL, and Generate sends it
    // alongside the file — the server then read that project's env_file into this
    // compose. Opening a file starts over: everything below step 1 is stale until
    // Analyze has run again.
    state.source = null;
    invalidateFromStep2();
    $("src-file-note").textContent =
      `Opened: ${file.name} (${(file.size / 1024).toFixed(1)} KB) — the URL field is ignored ` +
      `while a file is open. Type a URL to switch back.`;
    box.appendChild(msg("ok", `${file.name} read. Press Analyze.`));
  } catch (e) {
    box.appendChild(msg("err", `${file.name} could not be read: ${e.message}`));
  }
}

async function saveBlueprint() {
  const box = $("result-msgs");
  const name = $("m-name").value.trim();
  const url = $("src-url").value.trim();
  if (!url) {
    box.appendChild(msg("err", "A blueprint keeps the source URL, not the generated " +
      "compose — so there is nothing to save without one."));
    return;
  }
  busy($("btn-save-bp"), true, "Saving…");
  try {
    let data;
    try {
      data = await post("/api/blueprint/save", { url, meta: collectMeta(), variables: collectVars() });
    } catch (e) {
      // Overwriting is a decision, not a default — ask before replacing a recipe.
      if (!/already exists/.test(e.message)) throw e;
      if (!confirm(`A blueprint '${name}' already exists here. Replace it?`)) {
        box.appendChild(msg("warn", "Nothing was written."));
        return;
      }
      data = await post("/api/blueprint/save",
        { url, meta: collectMeta(), variables: collectVars(), overwrite: true });
    }
    renderBlueprints(data.blueprints || []);
    box.appendChild(msgList("warn", "Saved as blueprint:", data.warnings || []));
  } catch (e) {
    box.appendChild(msg("err", e.message));
  } finally {
    busy($("btn-save-bp"), false, "Save as blueprint");
  }
}

async function verifyInstall() {
  const box = $("install-msgs");
  const out = $("install-out");
  clear(box);
  busy($("btn-verify"), true, "Verifying…");
  try {
    const data = await post("/api/verify", {
      name: state.blueprint,
      host: $("t-host").value.trim(),
      user: $("i-user").value,
      password: $("i-pass").value,
    });
    const lines = data.results.map(
      (r) => `[${r.ok ? "ok" : "FAIL"}] ${r.url} — ${r.detail}`);
    out.hidden = false;
    out.textContent = lines.join("\n");
    box.appendChild(msg(data.passed === data.total ? "ok" : "err",
      data.passed === data.total
        ? `All ${data.total} expectation(s) hold against ${data.base}.`
        : `${data.total - data.passed} of ${data.total} expectation(s) failed — this app is NOT verified.`));
  } catch (e) {
    box.appendChild(msg("err", e.message));
  } finally {
    busy($("btn-verify"), false);
  }
}

$("btn-analyze").addEventListener("click", analyze);
$("src-url").addEventListener("keydown", (e) => { if (e.key === "Enter") analyze(); });
$("btn-generate").addEventListener("click", generate);
$("btn-validate").addEventListener("click", revalidate);
$("btn-download").addEventListener("click", download);
$("btn-copy").addEventListener("click", copyYaml);
$("btn-save-bp").addEventListener("click", saveBlueprint);
$("btn-open").addEventListener("click", () => $("src-file").click());
$("src-file").addEventListener("change", (e) => {
  if (e.target.files && e.target.files[0]) openFile(e.target.files[0]);
});
$("src-url").addEventListener("input", () => {
  // Typing a URL is the decision to use it — drop the other sources rather than
  // letting them compete silently.
  const typed = $("src-url").value.trim();
  if (state.fileText && typed) {
    clearOpenedFile("Opened file dropped — the URL above is used now.");
  }
  if (state.blueprint && typed) {
    const bp = state.blueprints.find((b) => b.name === state.blueprint);
    if (!bp || bp.source !== typed) {
      state.blueprint = null;
      $("src-blueprint").value = "";
      $("blueprint-proof").textContent = "blueprint dropped — the URL above is used now.";
    }
  }
});
$("btn-install").addEventListener("click", install);
$("btn-uninstall").addEventListener("click", uninstall);
$("btn-verify").addEventListener("click", verifyInstall);
loadDefaults();
