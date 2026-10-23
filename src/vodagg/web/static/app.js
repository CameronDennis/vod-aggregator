"use strict";

// Pages are built with h() from data the API returns. Player and streamer
// names come from games and Twitch, so they only ever go in as text.

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

async function api(path, options = {}) {
  const init = { ...options };
  if (init.body !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error((data && data.detail) || `${response.status} ${response.statusText}`);
  return data;
}

function hms(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function when(unix) {
  if (!unix) return "-";
  return new Date(unix * 1000).toISOString().replace("T", " ").slice(5, 16) + "Z";
}

const view = document.getElementById("view");
const state = { picked: new Set(), exportJob: null };

function show(...nodes) {
  view.replaceChildren(...nodes);
}

function failure(error) {
  return h("div", { class: "page" }, h("div", { class: "notice", role: "alert" }, error.message || String(error)));
}

// Routing: #/matches[/footage[/match]], #/library, #/exports

async function route() {
  const [, page = "matches", ...rest] = location.hash.split("?")[0].split("/");
  for (const link of document.querySelectorAll("nav.tabs a")) {
    link.classList.toggle("on", link.dataset.tab === page);
    if (link.dataset.tab === page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  try {
    if (page === "library") await libraryPage();
    else if (page === "exports") await exportsPage();
    else await matchesPage(rest[0] ? Number(rest[0]) : null, rest[1] ? Number(rest[1]) : null);
  } catch (error) {
    show(failure(error));
  }
}

window.addEventListener("hashchange", route);

// Job status in the header, polled while anything runs.

let jobs = [];
async function pollJobs() {
  try {
    jobs = await api("/api/jobs");
  } catch {
    return setTimeout(pollJobs, 5000);
  }
  const running = jobs.find((job) => job.status === "running" || job.status === "waiting");
  const status = document.getElementById("status");
  status.replaceChildren(
    ...(running
      ? [h("span", { class: "dot", "aria-hidden": "true" }),
         h("span", {}, running.message || running.label, running.total ? ` (${running.done}/${running.total})` : "")]
      : [])
  );
  document.dispatchEvent(new CustomEvent("jobs", { detail: jobs }));
  setTimeout(pollJobs, running ? 1500 : 5000);
}

// Matches

async function matchesPage(footageId, matchId) {
  const footage = await api("/api/footage");
  const withMatches = footage.filter((f) => f.synced_matches > 0 || f.unsynced_matches > 0);
  if (!withMatches.length) {
    return show(h("div", { class: "page" }, h("div", { class: "panel empty" },
      "No synced matches yet. ", h("a", { href: "#/library" }, "Add a VOD"), " and it will be scanned for matches.")));
  }
  const item = withMatches.find((f) => f.id === footageId) || withMatches[0];
  const [matches, unsynced] = await Promise.all([
    api(`/api/footage/${item.id}/matches`), api(`/api/footage/${item.id}/unsynced`),
  ]);
  const current = matches.find((m) => m.match_id === matchId) || matches[0];
  if (current && (!footageId || !matchId || current.match_id !== matchId)) {
    history.replaceState(null, "", `#/matches/${item.id}/${current.match_id}`);
  }
  const detail = current ? await api(`/api/matches/${current.match_id}`) : null;

  const sidebar = h("aside", { class: "sidebar", "aria-label": "Matches" },
    h("label", { class: "label", for: "footage-pick" }, "Footage"),
    h("select", { id: "footage-pick", onchange: (e) => { location.hash = `#/matches/${e.target.value}`; } },
      withMatches.map((f) => h("option", { value: f.id, selected: f.id === item.id },
        `${f.streamer} · ${when(f.started_at)}`))),
    h("div", { class: "label" }, `${matches.length} synced matches`),
    matches.map((m) => h("a", {
      class: `match-link${m.match_id === current.match_id ? " on" : ""}`,
      href: `#/matches/${item.id}/${m.match_id}`,
      "aria-current": m.match_id === current.match_id ? "page" : null,
    },
      h("span", { style: { display: "flex", justifyContent: "space-between", gap: "8px" } },
        h("span", { class: "mono" }, m.match_id), h("span", { class: "muted small" }, hms(m.offset_s))),
      h("span", { class: "dim small" }, `${clock(m.duration_s)} long · ±${(m.uncertainty_s || 0).toFixed(2)}s`))),
    unsynced.length ? h("div", { class: "label" }, `${unsynced.length} not synced`) : null,
    unsynced.map((m) => unsyncedMatch(item, m))
  );
  show(h("div", { class: "match-layout" }, sidebar,
    detail ? matchView(item, detail) : h("div", { class: "panel empty" }, "No synced matches in this footage yet. Add a replay to sync one.")));
}

// For a linked streamer with no footage of this match: find their VOD of it, sync it and show the match again.
function findVodButton(matchId, streamerId, streamerName) {
  const button = h("button", { type: "button", class: "small", onclick: async () => {
    button.disabled = true;
    button.textContent = `Finding ${streamerName}'s VOD…`;
    try {
      const { job } = await api("/api/footage/match", { body: { match_id: matchId, streamer_id: streamerId } });
      const onJobs = (event) => {
        const now = event.detail.find((j) => j.id === job.id);
        if (!now || now.status === "running" || now.status === "waiting") return;
        document.removeEventListener("jobs", onJobs);
        if (now.status === "done" && now.result.matches.some((m) => m.synced)) return route();
        button.textContent = now.error || now.result.matches.map((m) => m.error).join(" ") || "Couldn't sync it.";
        button.disabled = false;
      };
      document.addEventListener("jobs", onJobs);
    } catch (error) {
      button.textContent = error.message;
      button.disabled = false;
    }
  } }, `Find ${streamerName}'s VOD`);
  return button;
}

// A match seen in the footage without a replay, with a way to add the replay file by hand.
function unsyncedMatch(item, m) {
  const status = h("span", { class: "dim small" }, m.error || "No replay");
  const file = h("input", { type: "file", accept: ".dem,.bz2,.zst", hidden: true,
    "aria-label": `Replay file for match ${m.match_id}`, onchange: async () => {
      if (!file.files.length) return;
      button.disabled = true;
      status.textContent = "Uploading the replay…";
      try {
        // The file goes as the request body as it is, not as JSON.
        const response = await fetch(`/api/footage/${item.id}/unsynced/${m.match_id}/demo`, { method: "POST", body: file.files[0] });
        const job = await response.json().catch(() => null);
        if (!response.ok) throw new Error((job && job.detail) || `${response.status} ${response.statusText}`);
        status.textContent = "Syncing…";
        const onJobs = (event) => {
          const now = event.detail.find((j) => j.id === job.id);
          if (!now || now.status === "running" || now.status === "waiting") return;
          document.removeEventListener("jobs", onJobs);
          if (now.status === "done") location.hash = `#/matches/${item.id}/${m.match_id}`;
          else { status.textContent = now.error; button.disabled = false; }
        };
        document.addEventListener("jobs", onJobs);
      } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
      }
      file.value = "";
    } });
  const button = h("button", { type: "button", class: "small", onclick: () => file.click() }, "Add replay");
  return h("div", { class: "match-link unsynced" },
    h("span", { style: { display: "flex", justifyContent: "space-between", gap: "8px", alignSelf: "stretch" } },
      h("span", { class: "mono" }, m.match_id), h("span", { class: "muted small" }, hms(m.first_t))),
    status, file, button);
}

function matchView(item, detail) {
  const main = detail.perspectives.find((p) => p.footage_id === item.id);
  const mainIds = new Set(detail.players.filter((p) => p.streamer_id === item.streamer_id).map((p) => p.account_id));
  const players = new Map(detail.players.map((p) => [p.account_id, p]));
  const who = (id) => {
    const p = players.get(id);
    return p ? `${p.player_name} (${p.hero_name})` : "non-player";
  };
  const footageT = (p, tick) => p.offset_s + tick / p.tick_rate;

  // The main streamer's own kills, deaths and ults.
  const events = detail.events.flatMap((e) => {
    if (e.kind === "ult") return mainIds.has(e.actor) ? [{ ...e, view: "ult", other: null }] : [];
    if (mainIds.has(e.actor)) return [{ ...e, view: "kill", other: e.target }];
    if (mainIds.has(e.target)) return [{ ...e, view: "death", other: e.actor }];
    return [];
  });
  const others = detail.perspectives.filter((p) => p.footage_id !== item.id);
  const perspectiveOf = (accountId) => {
    const player = players.get(accountId);
    return player && others.find((p) => p.streamer_id === player.streamer_id);
  };

  // Timeline scale: ticks from the clock's 0:00 to the end of the match.
  const rate = main.tick_rate;
  const first = detail.events[0];
  const tick0 = first ? first.tick - (first.clock_s || 0) * rate : 0;
  const tickEnd = Math.max(tick0 + (detail.match.duration_s || 1) * rate, ...detail.events.map((e) => e.tick));
  const pct = (tick) => `${(100 * (tick - tick0) / (tickEnd - tick0)).toFixed(3)}%`;

  // Players who fought the main streamer, most fights first.
  const fights = new Map();
  for (const e of events) if (e.other !== null && players.has(e.other)) fights.set(e.other, (fights.get(e.other) || 0) + 1);
  const rivals = [...fights.entries()].sort((a, b) => b[1] - a[1]).map(([id]) => id);

  const mainVideo = videoEl(main);
  const otherVideo = { el: null, perspective: null };
  const otherBox = h("div");
  const otherSelect = h("select", { "aria-label": "Second perspective", onchange: (e) => setOther(Number(e.target.value)) },
    others.map((p) => h("option", { value: p.footage_id }, p.streamer)));
  const otherCaption = h("figcaption", {}, others.length ? ["Also watching ", otherSelect] : "Same moment");

  function setOther(footageId, accountId) {
    const p = others.find((o) => o.footage_id === footageId);
    otherVideo.perspective = p || null;
    if (p) {
      otherSelect.value = p.footage_id;
      otherVideo.el = videoEl(p);
      otherBox.replaceChildren(otherVideo.el);
    } else {
      otherVideo.el = null;
      const name = accountId !== undefined ? who(accountId) : "the other players";
      const player = players.get(accountId);
      otherBox.replaceChildren(h("div", { class: "placeholder" },
        h("span", {}, `No footage for ${name}`),
        player && player.streamer_id
          ? findVodButton(detail.match.match_id, player.streamer_id, player.streamer)
          : h("a", { href: "#/library", class: "small" }, "Link a streamer to this player")));
    }
  }
  setOther(others[0] ? others[0].footage_id : null);

  function seek(e) {
    const lead = 3;
    mainVideo.currentTime = Math.max(0, footageT(main, e.tick) - lead);
    if (e.other !== null && e.other !== undefined) {
      const theirs = perspectiveOf(e.other);
      if (theirs) setOther(theirs.footage_id);
      else if (!others.length) setOther(null, e.other);
    }
    if (otherVideo.el && otherVideo.perspective) {
      otherVideo.el.currentTime = Math.max(0, footageT(otherVideo.perspective, e.tick) - lead);
    }
  }

  const playheads = [];
  function playhead() {
    const el = h("div", { class: "playhead", style: { display: "none" } });
    playheads.push(el);
    return el;
  }
  mainVideo.addEventListener("timeupdate", () => {
    const tick = (mainVideo.currentTime - main.offset_s) * rate;
    const inside = tick >= tick0 && tick <= tickEnd;
    for (const el of playheads) {
      el.style.display = inside ? "block" : "none";
      el.style.left = pct(tick);
    }
  });

  function marks(list, perspectiveIds) {
    return list.map((e) => {
      const kind = e.kind === "ult" ? "ult" : perspectiveIds.has(e.actor) ? "kill" : "death";
      const label = `${clock(e.clock_s)} ${kind}`;
      return h("button", { type: "button", class: `mark ${kind}`, style: { left: pct(e.tick) }, title: label,
        "aria-label": label, onclick: () => seek(e) });
    });
  }

  const lanes = [
    h("div", { class: "lane" },
      h("div", { class: "lane-name" }, main.streamer),
      h("div", { class: "track" }, playhead(), marks(events, mainIds))),
    ...rivals.map((id) => {
      const has = Boolean(perspectiveOf(id));
      const list = events.filter((e) => e.other === id);
      return h("div", { class: "lane" },
        h("div", { class: `lane-name${has ? "" : " off"}`, title: who(id) }, players.get(id).player_name, has ? "" : " · no footage"),
        h("div", { class: `track${has ? "" : " off"}` }, playhead(), marks(list, new Set([id]))));
    }),
  ];

  const exportLink = h("a", { class: "button primary", href: "#/exports" });
  function updateExport() {
    const n = events.filter((e) => state.picked.has(e.id)).length;
    exportLink.textContent = n ? `Export ${n} clip${n === 1 ? "" : "s"}` : "Pick events to export";
    exportLink.onclick = (event) => {
      if (!n) return event.preventDefault();
      state.exportDraft = { match_id: detail.match.match_id, streamer_id: item.streamer_id, streamer: main.streamer,
        event_ids: events.filter((e) => state.picked.has(e.id)).map((e) => e.id) };
    };
  }
  if (state.pickedMatch !== detail.match.match_id) {
    state.picked = new Set(events.filter((e) => e.view !== "ult").map((e) => e.id));
    state.pickedMatch = detail.match.match_id;
  }
  updateExport();

  const rows = events.map((e) => {
    const povs = [main.streamer, ...[e.other].filter((id) => id !== null && perspectiveOf(id)).map((id) => perspectiveOf(id).streamer)];
    const box = h("input", { type: "checkbox", id: `ev${e.id}`, checked: state.picked.has(e.id),
      "aria-label": `Clip the ${e.view} at ${clock(e.clock_s)}`,
      onclick: (event) => event.stopPropagation(),
      onchange: (event) => { event.target.checked ? state.picked.add(e.id) : state.picked.delete(e.id); updateExport(); } });
    const row = h("div", { class: "grid ev", onclick: () => {
      for (const r of rows) r.classList.remove("on");
      row.classList.add("on");
      seek(e);
    } },
      box,
      h("span", { class: "mono" }, clock(e.clock_s)),
      h("span", { class: "mono dim" }, hms(footageT(main, e.tick))),
      h("span", { class: `kind-${e.view}` }, e.view),
      h("span", {}, e.view === "ult" ? (e.ability || "").replace(/^ability_/, "").replace(/_/g, " ") : who(e.other)),
      h("span", { class: "muted small" }, povs.join(", ")));
    return row;
  });

  return h("main", { class: "match-main" },
    h("div", { class: "match-title" },
      h("h1", {}, `Match ${detail.match.match_id}`),
      h("span", { class: "dim" }, `${clock(detail.match.duration_s)} · ${main.streamer}'s footage from ${hms(footageT(main, tick0))} · synced ±${(main.uncertainty_s || 0).toFixed(2)}s`),
      h("div", { class: "actions" }, exportLink)),
    h("div", { class: "players" },
      h("figure", { class: "player" }, mainVideo, h("figcaption", {}, `${main.streamer} (main)`)),
      h("figure", { class: "player" }, otherBox, otherCaption)),
    h("section", { class: "panel timeline", "aria-label": "Timeline" },
      h("div", { class: "legend" },
        h("span", {}, h("span", { class: "swatch", style: { background: "var(--kill)" } }), "Kill"),
        h("span", {}, h("span", { class: "swatch", style: { background: "var(--death)" } }), "Death"),
        h("span", {}, h("span", { class: "swatch ult" }), "Ult"),
        h("span", { class: "end" }, `0:00 – ${clock(detail.match.duration_s)}`)),
      lanes),
    h("section", { class: "panel events", "aria-label": "Events" },
      h("div", { class: "grid head" }, h("span", {}, "Clip"), h("span", {}, "Clock"), h("span", {}, "Footage"),
        h("span", {}, "Event"), h("span", {}, "With"), h("span", {}, "Perspectives")),
      rows.length ? rows : h("div", { class: "empty" }, `${main.streamer} has no kills, deaths or ults in this match.`)));
}

function videoEl(perspective) {
  const video = h("video", { controls: true, preload: "metadata", "aria-label": `${perspective.streamer}'s footage` });
  if (perspective.is_hls && !video.canPlayType("application/vnd.apple.mpegurl") && window.Hls && Hls.isSupported()) {
    const hls = new Hls({ maxBufferLength: 20 });
    hls.loadSource(perspective.video);
    hls.attachMedia(video);
  } else {
    video.src = perspective.video;
  }
  return video;
}

// Library

async function libraryPage() {
  let [streamers, footage, unlinked] = await Promise.all([
    api("/api/streamers"), api("/api/footage"), api("/api/players/unlinked"),
  ]);
  const message = h("div", { role: "status", "aria-live": "polite" });
  const say = (text, bad) => message.replaceChildren(text ? h("div", { class: bad ? "notice" : "dim" }, text) : "");
  const attempt = async (work) => {
    try { await work(); await libraryPage(); } catch (error) { say(error.message, true); }
  };

  const vodInput = h("input", { type: "text", id: "vod", placeholder: "https://www.twitch.tv/videos/…" });
  const addBar = h("form", { class: "panel add-bar", "aria-label": "Add footage", onsubmit: (e) => {
    e.preventDefault();
    if (vodInput.value.trim()) attempt(() => api("/api/footage/vod", { body: { video: vodInput.value.trim() } }));
  } },
    h("label", { class: "field grow" }, "Twitch VOD link or id", vodInput),
    h("button", { type: "submit", class: "primary" }, "Add and scan for matches"),
    h("button", { type: "button", onclick: () => { matchForm.toggleAttribute("hidden"); matchInput.focus(); } }, "Add from a match id"),
    h("button", { type: "button", onclick: () => importForm.toggleAttribute("hidden") }, "Import a local file"));

  const matchInput = h("input", { type: "text", inputmode: "numeric", placeholder: "Deadlock match id", "aria-label": "Deadlock match id" });
  // The main streamer is typed, with saved streamers suggested as you go. A new
  // name asks for a Twitch ID and Steam ID so the streamer can be added first.
  const matchWho = h("input", { type: "text", list: "streamer-names", placeholder: "Main streamer", "aria-label": "Main streamer",
    autocomplete: "off", oninput: () => { if (!matchWho.value.trim() || known()) newFields.setAttribute("hidden", ""); } });
  const known = () => streamers.find((s) => s.name.toLowerCase() === matchWho.value.trim().toLowerCase());
  const matchTwitch = h("input", { type: "text", placeholder: "Twitch ID", "aria-label": "Twitch ID for the new streamer" });
  const matchSteam = h("input", { type: "text", inputmode: "numeric", placeholder: "Steam ID", "aria-label": "Steam ID for the new streamer" });
  const newFields = h("span", { class: "inline-form", hidden: true }, matchTwitch, matchSteam);
  const matchForm = h("form", { class: "panel add-bar", hidden: true, "aria-label": "Add footage from a match id", onsubmit: (e) => {
    e.preventDefault();
    const id = matchInput.value.trim();
    if (!/^[1-9]\d*$/.test(id)) return say("Match ids must be numbers.", true);
    const name = matchWho.value.trim();
    if (!name) return say("Type the main streamer's name.", true);
    const existing = known();
    if (!existing) {
      newFields.removeAttribute("hidden");
      if (!matchTwitch.value.trim() || !matchSteam.value.trim()) {
        say(`${name} is a new streamer. Add their Twitch ID and Steam ID, then press go again.`, true);
        return (matchTwitch.value.trim() ? matchSteam : matchTwitch).focus();
      }
      if (!/^\d+$/.test(matchSteam.value.trim())) return say("Steam ids must be numbers.", true);
      if (!Number(matchSteam.value.trim())) return say("Steam id can't be 0.", true);
    }
    attempt(async () => {
      let streamer = existing;
      if (!streamer) {
        streamer = await api("/api/streamers", { body: { name, twitch_login: matchTwitch.value.trim(),
          steam_account_ids: [Number(matchSteam.value.trim())] } });
        streamers.push(streamer);  // So a retry after a failed lookup doesn't add them twice.
      }
      await api("/api/footage/match", { body: { match_id: Number(id), streamer_id: streamer.id } });
    });
  } }, h("div", { class: "grow" }, matchInput), matchWho,
    h("datalist", { id: "streamer-names" }, streamers.map((s) => h("option", { value: s.name }))),
    newFields, h("button", { type: "submit", class: "primary" }, "Find VOD and sync"));

  const importPath = h("input", { type: "text", placeholder: "/path/to/video.mp4", "aria-label": "File path" });
  const importWho = h("select", { "aria-label": "Streamer" }, streamers.map((s) => h("option", { value: s.name }, s.name)));
  const importStart = h("input", { type: "text", placeholder: "Start time (optional, ISO or unix)", "aria-label": "Start time" });
  const importForm = h("form", { class: "panel add-bar", hidden: true, onsubmit: (e) => {
    e.preventDefault();
    attempt(() => api("/api/footage/import", { body: { streamer: importWho.value, path: importPath.value.trim(),
      started: importStart.value.trim() || null } }));
  } }, h("div", { class: "grow" }, importPath), importWho, importStart, h("button", { type: "submit" }, "Import"));

  const newName = h("input", { type: "text", "aria-label": "Streamer name", placeholder: "Name" });
  const newLogin = h("input", { type: "text", "aria-label": "Twitch ID", placeholder: "Twitch ID (if different)" });
  const newSteam = h("input", { type: "text", "aria-label": "Steam account id", placeholder: "Steam id (optional)", inputmode: "numeric" });
  const addStreamer = h("form", { class: "inline-form", hidden: true, style: { padding: "16px", borderBottom: "1px solid var(--line-soft)" },
    onsubmit: (e) => {
      e.preventDefault();
      attempt(() => api("/api/streamers", { body: { name: newName.value.trim(), twitch_login: newLogin.value.trim() || null,
        steam_account_ids: newSteam.value.trim() ? [Number(newSteam.value.trim())] : [] } }));
    } }, newName, newLogin, newSteam, h("button", { type: "submit", class: "small primary" }, "Add"));

  const streamerRows = streamers.map((s) => {
    const alt = h("input", { type: "text", inputmode: "numeric", "aria-label": `New Steam id for ${s.name}`, placeholder: "Steam id" });
    const altForm = h("form", { class: "inline-form", hidden: true, onsubmit: (e) => {
      e.preventDefault();
      const id = alt.value.trim();
      if (!id) { say(""); return altForm.setAttribute("hidden", ""); }
      if (!/^\d+$/.test(id)) return say("Steam ids must be numbers.", true);
      if (!Number(id)) return say("Steam id can't be 0.", true);
      attempt(() => api(`/api/streamers/${s.id}/accounts`, { body: { account_id: Number(id) } }));
    } }, alt, h("button", { type: "submit", class: "small" }, "Add"));
    const editName = h("input", { type: "text", "aria-label": `Name for ${s.name}`, placeholder: "Name", value: s.name });
    const editLogin = h("input", { type: "text", "aria-label": `Twitch ID for ${s.name}`, placeholder: "Twitch ID", value: s.twitch_login || "" });
    const editNotes = h("input", { type: "text", "aria-label": `Notes for ${s.name}`, placeholder: "Notes", value: s.notes || "" });
    const editAccounts = h("input", { type: "text", "aria-label": `Steam ids for ${s.name}`, placeholder: "Steam ids, comma separated",
      value: s.steam_account_ids.join(", ") });
    const editForm = h("form", { class: "inline-form", hidden: true, onsubmit: (e) => {
      e.preventDefault();
      attempt(async () => {
        const ids = editAccounts.value.split(/[\s,]+/).filter(Boolean);
        if (ids.some((id) => !/^\d+$/.test(id))) throw new Error("Steam ids must be numbers.");
        if (ids.some((id) => !Number(id))) throw new Error("Steam id can't be 0.");
        const wanted = new Set(ids.map(Number));
        await api(`/api/streamers/${s.id}`, { method: "PATCH", body: { name: editName.value.trim() || null,
          twitch_login: editLogin.value.trim() || null, notes: editNotes.value.trim() } });
        for (const id of s.steam_account_ids.filter((id) => !wanted.has(id))) {
          await api(`/api/streamers/${s.id}/accounts/${id}`, { method: "DELETE" });
        }
        for (const id of [...wanted].filter((id) => !s.steam_account_ids.includes(id))) {
          await api(`/api/streamers/${s.id}/accounts`, { body: { account_id: id } });
        }
      });
    } }, editName, editLogin, editNotes, editAccounts,
      h("button", { type: "submit", class: "small primary" }, "Save"),
      h("button", { type: "button", class: "small", onclick: () => editForm.setAttribute("hidden", "") }, "Cancel"),
      h("button", { type: "button", class: "small", onclick: () => {
        if (confirm(`Remove ${s.name} and their footage records? Downloaded files are kept.`)) {
          attempt(() => api(`/api/streamers/${s.id}`, { method: "DELETE" }));
        }
      } }, "Remove streamer"));
    return h("div", { class: "streamer" },
      h("div", { style: { display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: "10px" } },
        h("strong", {}, s.name),
        s.twitch_login ? h("a", { class: "small", href: `https://www.twitch.tv/${encodeURIComponent(s.twitch_login)}`, target: "_blank", rel: "noopener" },
          `twitch.tv/${s.twitch_login}`) : null,
        s.notes ? h("span", { class: "muted small" }, s.notes) : null,
        h("button", { type: "button", class: "small", style: { marginLeft: "auto" },
          onclick: () => { editForm.toggleAttribute("hidden"); editName.focus(); } }, "Edit")),
      editForm,
      h("div", { class: "chips" },
        h("span", { class: "muted small" }, "Steam accounts"),
        s.steam_account_ids.map((id) => h("span", { class: "chip" }, String(id),
          h("button", { type: "button", "aria-label": `Remove Steam account ${id}`,
            onclick: () => attempt(() => api(`/api/streamers/${s.id}/accounts/${id}`, { method: "DELETE" })) }, "×"))),
        h("button", { type: "button", class: "small", onclick: () => { altForm.toggleAttribute("hidden"); alt.focus(); } },
          s.steam_account_ids.length ? "+ Add alt account" : "+ Add Steam account")),
      altForm);
  });

  const unlinkedRows = unlinked.map((p) => {
    const pick = h("select", { "aria-label": `Streamer for ${p.player_name}` },
      h("option", { value: "" }, "New streamer…"), streamers.map((s) => h("option", { value: s.id }, s.name)));
    const login = h("input", { type: "text", "aria-label": `Twitch ID for ${p.player_name}`, placeholder: "Twitch ID" });
    pick.addEventListener("change", () => login.toggleAttribute("hidden", Boolean(pick.value)));
    const linkForm = h("div", { class: "inline-form", hidden: true }, pick, login, h("button", { type: "submit", class: "small" }, "Link"));
    return h("form", { "aria-label": `Link ${p.player_name}`, onsubmit: (e) => {
      e.preventDefault();
      attempt(() => pick.value
        ? api(`/api/streamers/${pick.value}/accounts`, { body: { account_id: p.account_id } })
        : api("/api/streamers", { body: { name: login.value.trim(), twitch_login: login.value.trim(),
            steam_account_ids: [p.account_id] } }));
    } },
      h("div", { class: "who" },
        h("span", {}, p.player_name, h("span", { class: "muted" }, " (",
          h("a", { href: `https://statlocker.gg/profile/${p.account_id}`, target: "_blank", rel: "noopener" }, String(p.account_id)), ")")),
        h("button", { type: "button", class: "small", "aria-expanded": "false", onclick: (e) => {
          linkForm.toggleAttribute("hidden");
          e.currentTarget.setAttribute("aria-expanded", String(!linkForm.hidden));
          if (!linkForm.hidden) pick.focus();
        } }, "Add")),
      linkForm);
  });

  const footageRows = (list, all) => list.map((f) => {
    const job = all.find((j) => j.kind === "sync" && j.label === `Scan and sync footage ${f.id}` && j.status !== "done" && j.status !== "failed");
    const progress = job ? h("span", { style: { display: "flex", flexDirection: "column", gap: "6px" } },
      h("span", { class: "small" }, `${job.message || "Waiting"}${job.total ? ` (${job.done}/${job.total})` : ""}`),
      h("span", { class: "bar", role: "img", "aria-label": `${job.done} of ${job.total}` },
        h("span", { style: { width: `${job.total ? 100 * job.done / job.total : 0}%` } })))
      : h("span", { class: "small" }, `${f.synced_matches} synced`);
    return h("div", { class: "grid" },
      h("span", {}, f.streamer),
      h("span", { class: "mono" }, when(f.started_at)),
      h("span", { class: "mono" }, hms(f.duration_s)),
      progress,
      h("span", { class: "cell-actions" },
        h("button", { type: "button", class: "small", disabled: Boolean(job),
          onclick: () => attempt(() => api(`/api/footage/${f.id}/sync`, { body: {} })) }, "Scan"),
        f.synced_matches ? h("a", { class: "button small", href: `#/matches/${f.id}` }, "Open") : null));
  });
  const footageBody = h("div");
  const renderFootage = (list, all) => footageBody.replaceChildren(
    ...(list.length ? footageRows(list, all) : [h("div", { class: "empty" }, "No footage yet.")]));
  renderFootage(footage, jobs);
  // Keep sync progress live, and pick up new matches as syncs finish.
  let finished = jobs.filter((j) => j.kind === "sync" && j.status === "done").length;
  const onJobs = async (event) => {
    if (!footageBody.isConnected) return document.removeEventListener("jobs", onJobs);
    const nowFinished = event.detail.filter((j) => j.kind === "sync" && j.status === "done").length;
    if (nowFinished !== finished) {
      finished = nowFinished;
      footage = await api("/api/footage");
    }
    renderFootage(footage, event.detail);
  };
  document.addEventListener("jobs", onJobs);

  show(h("div", { class: "page" },
    addBar, matchForm, importForm, message,
    h("div", { class: "row" },
      h("section", { class: "panel", "aria-label": "Streamers", style: { flex: "1 1 440px", minWidth: "0" } },
        h("div", { class: "panel-head" }, h("h2", {}, "Streamers"),
          h("button", { type: "button", onclick: () => { addStreamer.toggleAttribute("hidden"); newName.focus(); } }, "Add streamer")),
        addStreamer,
        streamerRows.length ? streamerRows : h("div", { class: "empty" }, "No streamers yet. Adding a VOD adds its streamer."),
        h("div", { class: "unlinked" },
          h("div", { class: "muted small", style: { textTransform: "uppercase", letterSpacing: "0.06em" } }, "Played with your streamers, not linked yet"),
          unlinkedRows.length ? unlinkedRows : h("div", { class: "muted small" }, "Everyone in synced matches is linked or there are no synced matches yet."))),
      h("section", { class: "panel footage-table", "aria-label": "Footage", style: { flex: "999 1 560px", minWidth: "0" } },
        h("div", { class: "panel-head" }, h("h2", {}, "Footage")),
        h("div", { class: "grid head" }, h("span", {}, "Streamer"), h("span", {}, "Started"), h("span", {}, "Length"),
          h("span", {}, "Matches"), h("span", {})),
        footageBody))));
}

// Exports

async function exportsPage() {
  const draft = state.exportDraft;
  const before = h("input", { type: "number", id: "before", value: "8", min: "0", step: "1" });
  const after = h("input", { type: "number", id: "after", value: "4", min: "0", step: "1" });
  const others = h("input", { type: "checkbox", id: "others", checked: true });
  const message = h("div", { role: "status", "aria-live": "polite" });

  const form = draft ? h("form", { class: "panel export-form", "aria-label": "Export settings", onsubmit: async (e) => {
    e.preventDefault();
    try {
      const job = await api("/api/exports", { body: { ...draft, before: Number(before.value), after: Number(after.value),
        others: others.checked } });
      state.exportJob = job.id;
      state.exportDraft = null;
      exportsPage();
    } catch (error) {
      message.replaceChildren(h("div", { class: "notice" }, error.message));
    }
  } },
    h("h1", {}, `Export ${draft.event_ids.length} events from match ${draft.match_id}`),
    h("div", { class: "two" },
      h("label", { class: "field", for: "before" }, "Seconds before event", before),
      h("label", { class: "field", for: "after" }, "Seconds after event", after)),
    h("div", { class: "checks" },
      h("label", {}, h("input", { type: "checkbox", checked: true, disabled: true }), `Main streamer (${draft.streamer})`),
      h("label", { for: "others" }, others, "Every linked streamer in the fight")),
    message,
    h("button", { type: "submit", class: "primary" }, "Start export"))
    : h("div", { class: "panel export-form" }, h("h2", {}, "New export"),
      h("p", { class: "dim", style: { margin: 0 } }, "Pick events in a ", h("a", { href: "#/matches" }, "match"), ", then press Export."));

  const list = h("section", { class: "panel export-list", "aria-label": "Exports" });
  function renderList(all) {
    const exports = all.filter((job) => job.kind === "export");
    list.replaceChildren(
      h("div", { class: "panel-head" }, h("h2", {}, "Exports")),
      ...(exports.length ? exports.map((job) => {
        const clips = (job.result && job.result.clips) || [];
        return h("div", {},
          h("div", { class: "panel-head" },
            h("span", {}, job.label),
            h("span", { class: job.status === "failed" ? "error small" : "muted small" },
              job.error || `${job.message || job.status}${job.total ? ` (${job.done}/${job.total})` : ""}`),
            job.result && job.result.folder ? h("button", { type: "button", class: "small",
              onclick: () => api("/api/open-folder", { body: { path: job.result.folder } }).catch((error) =>
                message.replaceChildren(h("div", { class: "notice" }, error.message))) }, "Open folder") : null),
          clips.map((c) => h("div", { class: "clip" },
            h("div", { class: "what" }, h("span", { class: "mono" }, c.file),
              h("span", { class: "dim small" }, `${c.streamer} · ${hms(c.start)} – ${hms(c.end)}`)),
            h("span", { class: `st-${c.status.split(":")[0]} small` }, c.status))));
      }) : [h("div", { class: "empty" }, "No exports yet in this session.")]));
  }
  renderList(jobs);
  const onJobs = (event) => {
    if (!list.isConnected) return document.removeEventListener("jobs", onJobs);
    renderList(event.detail);
  };
  document.addEventListener("jobs", onJobs);
  show(h("div", { class: "page" }, h("div", { class: "row" }, form, list)));
}

pollJobs();
route();
