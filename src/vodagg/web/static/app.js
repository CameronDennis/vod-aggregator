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

function signedClock(seconds) {
  return seconds < 0 ? `-${clock(-seconds)}` : clock(seconds);
}

// "12:03", "-1:30" or "95" as seconds of match clock; NaN if it isn't a time.
function parseClock(text) {
  const m = /^\s*(-)?(?:(\d+):)?(\d+(?:\.\d+)?)\s*$/.exec(text);
  if (!m) return NaN;
  const s = Number(m[2] || 0) * 60 + Number(m[3]);
  return m[1] ? -s : s;
}

// Match clock seconds and demo ticks, converted as boon does: the clock reads 0:00 at the match's start tick
// and skips paused ticks. Matches parsed before those were stored go by the first event and no pauses.
function matchClock(detail, rate) {
  const first = detail.events[0];
  const tick0 = detail.match.start_tick ?? (first ? first.tick - (first.clock_s || 0) * rate : 0);
  const pauses = detail.match.pauses || [];
  const pausedBefore = (tick) => pauses.reduce((n, [a, b]) => n + Math.max(0, Math.min(b, Math.ceil(tick) - 1) - a + 1), 0);
  const base = tick0 - pausedBefore(tick0);
  return {
    tick0,
    seconds: (tick) => (tick - pausedBefore(tick) - base) / rate,
    // The first tick at which the clock reads `seconds`; a time inside a pause is its first tick.
    tick: (seconds) => pauses.reduce((t, [a, b]) => (a < t ? t + b - a + 1 : t), base + seconds * rate),
  };
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
  // A sync failure opens where it can be fixed; with no synced matches, the first one does.
  const failed = unsynced.find((m) => m.match_id === matchId) || (matches.length ? null : unsynced[0]);
  const current = failed ? null : matches.find((m) => m.match_id === matchId) || matches[0];
  const shown = failed || current;
  if (shown && (!footageId || !matchId || shown.match_id !== matchId)) {
    history.replaceState(null, "", `#/matches/${item.id}/${shown.match_id}`);
  }
  const detail = current ? await api(`/api/matches/${current.match_id}`) : null;

  const sidebar = h("aside", { class: "sidebar", "aria-label": "Matches" },
    h("label", { class: "label", for: "footage-pick" }, "Footage"),
    h("select", { id: "footage-pick", onchange: (e) => { location.hash = `#/matches/${e.target.value}`; } },
      withMatches.map((f) => h("option", { value: f.id, selected: f.id === item.id },
        f.name || `${f.streamer} · ${when(f.started_at)}`))),
    h("div", { class: "label" }, `${matches.length} synced matches`),
    matches.map((m) => h("a", {
      class: `match-link${m === shown ? " on" : ""}`,
      href: `#/matches/${item.id}/${m.match_id}`,
      "aria-current": m === shown ? "page" : null,
    },
      h("span", { style: { display: "flex", justifyContent: "space-between", gap: "8px" } },
        h("span", { class: "mono" }, m.match_id), h("span", { class: "muted small" }, hms(m.offset_s))),
      h("span", { class: "dim small" }, `${clock(m.duration_s)} long · ${syncedHow(m)}`))),
    unsynced.length ? h("div", { class: "label" }, `${unsynced.length} sync failures`) : null,
    unsynced.map((m) => h("a", {
      class: `match-link${m === shown ? " on" : ""}`,
      href: `#/matches/${item.id}/${m.match_id}`,
      "aria-current": m === shown ? "page" : null,
    },
      h("span", { style: { display: "flex", justifyContent: "space-between", gap: "8px" } },
        h("span", { class: "mono" }, m.match_id), h("span", { class: "muted small" }, hms(m.first_t))),
      h("span", { class: "dim small" }, m.kind === "clock" ? "Couldn't read the clock" : "No replay")))
  );
  show(h("div", { class: "match-layout" }, sidebar,
    failed ? syncFailureView(item, failed) : matchView(item, detail)));
}

// How closely a match is lined up with footage: the fit's uncertainty, or that it was set by hand.
function syncedHow(row) {
  return row.readings === 0 ? "synced by hand" : `synced ±${(row.uncertainty_s || 0).toFixed(2)}s`;
}

// Calls `done` with a job once it has finished, as seen by the header's polling.
function whenJobEnds(jobId, done) {
  const onJobs = (event) => {
    const now = event.detail.find((j) => j.id === jobId);
    if (!now || now.status === "running" || now.status === "waiting") return;
    document.removeEventListener("jobs", onJobs);
    done(now);
  };
  document.addEventListener("jobs", onJobs);
}

// For a linked streamer with no footage of this match: find their VOD of it, sync it and show the match again.
function findVodButton(matchId, streamerId, streamerName) {
  const button = h("button", { type: "button", class: "small", onclick: async () => {
    button.disabled = true;
    button.textContent = `Finding ${streamerName}'s VOD…`;
    try {
      const { job } = await api("/api/footage/match", { body: { match_id: matchId, streamer_id: streamerId } });
      whenJobEnds(job.id, (now) => {
        if (now.status === "done" && now.result.matches.some((m) => m.synced)) return route();
        button.textContent = now.error || now.result.matches.map((m) => m.error).join(" ") || "Couldn't sync it.";
        button.disabled = false;
      });
    } catch (error) {
      button.textContent = error.message;
      button.disabled = false;
    }
  } }, `Find ${streamerName}'s VOD`);
  return button;
}

// A match seen in the footage that couldn't be synced, with ways to fix it. Without a replay: add the file
// by hand or try downloading it again. When the clock couldn't be read: show where the stream has it and
// rescan, or line the match up by hand from one frame.
function syncFailureView(item, m) {
  // Starts a sync job and says how it goes in `status`; once the match is synced the page shows it.
  async function run(start, status, button) {
    button.disabled = true;
    status.textContent = "Syncing…";
    try {
      const job = await start();
      whenJobEnds(job.id, (now) => {
        if (now.status === "done") return route();
        status.textContent = now.error;
        button.disabled = false;
      });
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  }
  const rescanStatus = h("span", { class: "dim small", role: "status" });
  const rescan = h("button", { type: "button", class: "small", onclick: () =>
    run(() => api(`/api/footage/${item.id}/unsynced/${m.match_id}/rescan`, { method: "POST" }), rescanStatus, rescan) }, "Rescan");

  const title = h("div", { class: "match-title" },
    h("h1", {}, `Match ${m.match_id}`),
    h("span", { class: "dim" }, `Seen ${hms(m.first_t)}–${hms(m.last_t)} in ${item.streamer}'s footage, not synced`));
  const error = m.error ? h("div", { class: "notice" }, m.error) : null;

  if (m.kind !== "clock") {
    const file = h("input", { type: "file", accept: ".dem,.bz2,.zst", hidden: true,
      "aria-label": `Replay file for match ${m.match_id}`, onchange: () => {
        if (!file.files.length) return;
        const body = file.files[0];
        file.value = "";
        run(async () => {
          // The file goes as the request body as it is, not as JSON.
          const response = await fetch(`/api/footage/${item.id}/unsynced/${m.match_id}/demo`, { method: "POST", body });
          const job = await response.json().catch(() => null);
          if (!response.ok) throw new Error((job && job.detail) || `${response.status} ${response.statusText}`);
          return job;
        }, rescanStatus, addReplay);
      } });
    const addReplay = h("button", { type: "button", class: "small primary", onclick: () => file.click() }, "Add replay");
    return h("main", { class: "match-main" }, title, error,
      h("section", { class: "panel" },
        h("div", { class: "panel-head" }, h("h2", {}, "No replay")),
        h("div", { class: "fix" },
          h("p", {}, "This match's replay couldn't be downloaded. Add the .dem file if you have it, or rescan to try downloading it again."),
          h("div", { class: "inline-form" }, addReplay, file, rescan, rescanStatus))));
  }

  // Where the clock is: a box drawn on a frame of the match, as fractions of the frame.
  let region = item.clock_region;
  let t = (m.first_t + m.last_t) / 2;
  const img = h("img", { draggable: "false", alt: "" });
  const box = h("div", { class: "clock-box" });
  const frame = h("div", { class: "frame-pick", "aria-label": "Drag a box around the game clock" }, img, box);
  const frameTime = h("span", { class: "mono dim small" });
  const regionStatus = h("span", { class: "dim small", role: "status" });
  const placeBox = () => Object.assign(box.style, { left: `${region[0] * 100}%`, top: `${region[1] * 100}%`,
    width: `${region[2] * 100}%`, height: `${region[3] * 100}%` });
  const loadFrame = () => {
    img.src = `/api/footage/${item.id}/frame?t=${t.toFixed(1)}`;
    img.alt = `${item.streamer}'s footage at ${hms(t)}`;
    frameTime.textContent = hms(t);
    regionStatus.textContent = "";
  };
  img.addEventListener("error", () => { regionStatus.textContent = "Couldn't load this frame."; });
  frame.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    frame.setPointerCapture(e.pointerId);
    const rect = frame.getBoundingClientRect();
    const at = (ev) => [Math.min(Math.max((ev.clientX - rect.left) / rect.width, 0), 1),
      Math.min(Math.max((ev.clientY - rect.top) / rect.height, 0), 1)];
    const [x0, y0] = at(e);
    const before = region;
    const move = (ev) => {
      const [x, y] = at(ev);
      region = [Math.min(x0, x), Math.min(y0, y), Math.abs(x - x0), Math.abs(y - y0)];
      placeBox();
    };
    const up = () => {
      frame.removeEventListener("pointermove", move);
      frame.removeEventListener("pointerup", up);
      // A click without a drag keeps the box there was.
      if (region[2] < 0.005 || region[3] < 0.005) { region = before; placeBox(); }
    };
    frame.addEventListener("pointermove", move);
    frame.addEventListener("pointerup", up);
  });
  const step = (by) => h("button", { type: "button", class: "small", onclick: () => {
    t = Math.min(Math.max(t + by, m.first_t), m.last_t);
    loadFrame();
  } }, by < 0 ? "Earlier" : "Later");
  const save = h("button", { type: "button", class: "small primary", onclick: async () => {
    save.disabled = true;
    regionStatus.textContent = "Reading the clock…";
    try {
      const [left, top, width, height] = region;
      const result = await api(`/api/footage/${item.id}/clock-region`, { method: "PUT", body: { left, top, width, height, t } });
      item.clock_region = result.clock_region;
      regionStatus.textContent = result.clock_s === null
        ? "Saved, but no clock could be read in that box on this frame. Try a tighter box or another frame."
        : `Saved. It reads ${clock(result.clock_s)} here; rescan to sync with it.`;
    } catch (error) {
      regionStatus.textContent = error.message;
    }
    save.disabled = false;
  } }, "Save and test");
  placeBox();
  loadFrame();

  // By hand: the footage paused on the frame where the clock turns over, and what it turns over to.
  const video = videoEl({ streamer: item.streamer, is_hls: !item.local_path,
    video: item.local_path ? `/api/footage/${item.id}/file` : `/api/footage/${item.id}/hls/index.m3u8` });
  video.addEventListener("loadedmetadata", () => { video.currentTime = t; }, { once: true });
  // Twitch streams run at up to 60 fps, so a 60th of a second is at most a frame.
  const nudge = (by, label) => h("button", { type: "button", class: "small", onclick: () => {
    video.pause();
    video.currentTime = Math.max(0, video.currentTime + by);
  } }, label);
  const shown = h("input", { type: "text", placeholder: "12:03", "aria-label": "What the clock reads on this frame" });
  const manualStatus = h("span", { class: "dim small", role: "status" });
  const syncHere = h("button", { type: "button", class: "small primary", onclick: () => {
    const clockS = parseClock(shown.value);
    if (!Number.isInteger(clockS) || clockS < 0) {
      manualStatus.textContent = "Type the clock as it reads on this frame, like 12:03.";
      return;
    }
    run(() => api(`/api/footage/${item.id}/unsynced/${m.match_id}/manual`, { body: { t: video.currentTime, clock_s: clockS } }),
      manualStatus, syncHere);
  } }, "Sync here");

  return h("main", { class: "match-main" }, title, error,
    h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Where the clock is")),
      h("div", { class: "fix" },
        h("p", {}, "The replay was found, but the game clock couldn't be read. Drag a box around the clock, save it, then rescan."),
        frame,
        h("div", { class: "inline-form" }, step(-30), frameTime, step(30), save, regionStatus),
        h("div", { class: "inline-form" }, rescan, rescanStatus))),
    h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Sync by hand")),
      h("div", { class: "fix" },
        h("p", {}, "If it still can't be read: pause on the first frame where the clock shows a new second, type what it shows, then sync here."),
        video,
        h("div", { class: "inline-form" }, nudge(-1, "−1s"), nudge(-1 / 60, "−1 frame"), nudge(1 / 60, "+1 frame"), nudge(1, "+1s"),
          shown, syncHere, manualStatus))));
}

function matchView(item, detail) {
  const main = detail.perspectives.find((p) => p.footage_id === item.id);
  const mainIds = new Set(detail.players.filter((p) => p.streamer_id === item.streamer_id).map((p) => p.account_id));
  const players = new Map(detail.players.map((p) => [p.account_id, p]));
  const mainTeam = (detail.players.find((p) => mainIds.has(p.account_id)) || {}).team;
  // A player linked to a streamer goes by the streamer's saved name, with their in-game name after it.
  // A name made only of invisible characters shows as the hero instead.
  // Matches parsed before names were recorded have none.
  const nameOf = (p) => {
    const raw = p.player_name || "";
    const ingame = raw.replace(/[\p{C}\s]/gu, "") ? raw : `(${p.hero_name})`;
    return p.streamer && p.streamer.toLowerCase() !== raw.toLowerCase()
      ? `${p.streamer} [${ingame}]` : p.streamer || ingame;
  };
  const who = (id) => {
    const p = players.get(id);
    return p ? `${nameOf(p)} (${p.hero_name})` : "non-player";
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

  // Timeline scale: the clock's 0:00 to the end of the match, with two minutes either side
  // so you can jump to just before and just after it.
  const rate = main.tick_rate;
  const mc = matchClock(detail, rate);
  const tick0 = mc.tick0;
  const tickEnd = Math.max(mc.tick(detail.match.duration_s || 1), ...detail.events.map((e) => e.tick));
  const pad = 120;
  const viewStart = tick0 - pad * rate;
  const viewEnd = tickEnd + pad * rate;
  const along = (tick) => (tick - viewStart) / (viewEnd - viewStart);
  const pct = (tick) => `${(100 * along(tick)).toFixed(3)}%`;

  // Every enemy, those who fought the main streamer most first.
  const fights = new Map();
  for (const e of events) if (e.other !== null && players.has(e.other)) fights.set(e.other, (fights.get(e.other) || 0) + 1);
  const rivals = detail.players.filter((p) => p.team !== mainTeam).map((p) => p.account_id)
    .sort((a, b) => (fights.get(b) || 0) - (fights.get(a) || 0));

  const mainVideo = videoEl(main);
  const otherVideo = { el: null, perspective: null };

  // Playing either video starts the other at the same moment of the match, muted;
  // pausing either pauses both.
  const partner = (el) => (el === mainVideo ? otherVideo.el : mainVideo);
  const perspectiveFor = (el) => (el === mainVideo ? main : otherVideo.perspective);
  function playTogether(el) {
    el.addEventListener("play", () => {
      const other = partner(el);
      if (!other || !other.paused) return;
      const from = perspectiveFor(el);
      other.currentTime = Math.max(0, footageT(perspectiveFor(other), (el.currentTime - from.offset_s) * from.tick_rate));
      el.muted = false;
      other.muted = true;
      other.play().catch(() => {});
    });
    el.addEventListener("pause", () => {
      const other = partner(el);
      if (other && !other.paused) other.pause();
    });
  }
  playTogether(mainVideo);
  const otherBox = h("div");
  const mainTeamOf = (p) => detail.players.some((q) => q.streamer_id === p.streamer_id && q.team === mainTeam);
  const option = (p) => h("option", { value: p.footage_id }, p.streamer);
  const otherSelect = h("select", { "aria-label": "Second perspective", onchange: (e) => setOther(Number(e.target.value)) },
    others.some(mainTeamOf) ? h("optgroup", { label: "Teammates" }, others.filter(mainTeamOf).map(option)) : null,
    others.some((p) => !mainTeamOf(p)) ? h("optgroup", { label: "Enemies" }, others.filter((p) => !mainTeamOf(p)).map(option)) : null);
  const otherCaption = h("figcaption", {}, others.length ? ["Also watching ", otherSelect] : "Same moment");

  function setOther(footageId, accountId) {
    const p = others.find((o) => o.footage_id === footageId);
    if (p && p === otherVideo.perspective) return;  // Already showing: keep the loaded video.
    otherVideo.perspective = p || null;
    if (p) {
      otherSelect.value = p.footage_id;
      otherVideo.el = videoEl(p);
      playTogether(otherVideo.el);
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

  // `focus` is whose perspective to show beside the main one: the other player in the event unless given.
  function seek(e, focus = e.other) {
    const lead = 3;
    mainVideo.currentTime = Math.max(0, footageT(main, e.tick) - lead);
    if (focus !== null && focus !== undefined) {
      const theirs = perspectiveOf(focus);
      if (theirs) setOther(theirs.footage_id);
      else if (!others.length) setOther(null, focus);
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
    const inside = tick >= viewStart && tick <= viewEnd;
    for (const el of playheads) {
      el.style.display = inside ? "block" : "none";
      el.style.left = pct(tick);
    }
  });

  function marks(list, perspectiveIds, focus) {
    return list.map((e) => {
      const kind = e.kind === "ult" ? "ult" : perspectiveIds.has(e.actor) ? "kill" : "death";
      const what = e.kind === "ult"
        ? `${who(e.actor)} used ${(e.ability || "their ult").replace(/^ability_/, "").replace(/_/g, " ")}`
        : `${who(e.actor)} killed ${who(e.target)}`;
      const label = `${clock(e.clock_s)} ${what}`;
      // Tooltips near either end of the track open inwards so they stay on the page.
      const at = along(e.tick);
      const side = at < 0.3 ? " tip-start" : at > 0.7 ? " tip-end" : "";
      return h("button", { type: "button", class: `mark ${kind}${side}`, style: { left: pct(e.tick) }, "data-tip": label,
        "aria-label": label, onclick: () => seek(e, focus === undefined ? e.other : focus) });
    });
  }

  const lane = (id, list, focus) => {
    const has = Boolean(perspectiveOf(id));
    return h("div", { class: "lane" },
      h("div", { class: `lane-name${has ? "" : " off"}`, title: who(id) }, nameOf(players.get(id)), has ? "" : " · no footage"),
      h("div", { class: `track${has ? "" : " off"}` }, playhead(), marks(list, new Set([id]), focus)));
  };
  // Teammates' own kills, deaths and ults; clicking one shows that teammate's perspective.
  const teammates = detail.players.filter((p) => p.team === mainTeam && !mainIds.has(p.account_id))
    .sort((a, b) => Boolean(perspectiveOf(b.account_id)) - Boolean(perspectiveOf(a.account_id)));
  const teammateEvents = (id) => detail.events.filter((e) => e.actor === id || (e.kind !== "ult" && e.target === id));
  // A clock ruler over the lanes: click or drag on it to jump both videos to that moment of the match.
  const seconds = mc.seconds(tickEnd);
  const step = seconds <= 20 * 60 ? 120 : seconds <= 60 * 60 ? 300 : 600;
  const rulerTicks = [];
  for (let s = -pad; s <= seconds + pad; s += 60) {
    const major = s % step === 0 || s === -pad;
    const at = along(mc.tick(s));
    const edge = at < 0.03 ? " start" : at > 0.97 ? " end" : "";
    rulerTicks.push(h("span", { class: `rule${major ? " major" : ""}${edge}`, style: { left: pct(mc.tick(s)) } },
      major ? h("span", { class: "rule-label" }, signedClock(s)) : null));
  }
  // The match itself, shaded so the padding either side reads as before and after it.
  rulerTicks.push(h("span", { class: "ruler-match", style: { left: pct(tick0), width: `${100 * (along(tickEnd) - along(tick0))}%` } }));
  const hoverTip = h("span", { class: "ruler-tip", hidden: true });
  const ruler = h("div", { class: "track ruler", role: "slider", tabindex: "0", "aria-label": "Match time",
    "aria-valuemin": String(-pad), "aria-valuemax": String(Math.round(seconds + pad)) }, rulerTicks, playhead(), hoverTip);
  const rulerAt = (event) => {
    const box = ruler.getBoundingClientRect();
    return Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
  };
  // While hovering, show the match time under the pointer, kept inside the ruler.
  ruler.addEventListener("pointermove", (event) => {
    const at = rulerAt(event);
    hoverTip.textContent = signedClock(mc.seconds(viewStart + at * (viewEnd - viewStart)));
    hoverTip.hidden = false;
    hoverTip.style.left = `${at * 100}%`;
    hoverTip.style.transform = `translateX(${-at * 100}%)`;
  });
  ruler.addEventListener("pointerleave", () => { hoverTip.hidden = true; });
  const jumpTo = (event) => {
    const tick = viewStart + rulerAt(event) * (viewEnd - viewStart);
    mainVideo.currentTime = Math.max(0, footageT(main, tick));
    if (otherVideo.el && otherVideo.perspective) otherVideo.el.currentTime = Math.max(0, footageT(otherVideo.perspective, tick));
  };
  ruler.addEventListener("pointerdown", (event) => { ruler.setPointerCapture(event.pointerId); jumpTo(event); });
  ruler.addEventListener("pointermove", (event) => { if (ruler.hasPointerCapture(event.pointerId)) jumpTo(event); });

  const lanes = [
    h("div", { class: "lane" }, h("div", { class: "lane-name muted small" }, "Match clock"), ruler),
    h("div", { class: "lane" },
      h("div", { class: "lane-name" }, main.streamer),
      h("div", { class: "track" }, playhead(), marks(events, mainIds))),
    rivals.length ? h("div", { class: "lane-group" }, `Enemies · fights with ${main.streamer}`) : null,
    ...rivals.map((id) => lane(id, events.filter((e) => e.other === id))),
    teammates.length ? h("div", { class: "lane-group" }, "Teammates") : null,
    ...teammates.map((p) => lane(p.account_id, teammateEvents(p.account_id), p.account_id)),
  ];

  const exportLink = h("a", { class: "button primary", href: "#/exports" });
  function updateExport() {
    const n = events.filter((e) => state.picked.has(e.id)).length;
    exportLink.textContent = n ? `Export ${n} clip${n === 1 ? "" : "s"}` : "Pick events to export";
    exportLink.onclick = (event) => {
      if (!n) return event.preventDefault();
      state.exportDraft = { match_id: detail.match.match_id, streamer_id: item.streamer_id, streamer: main.streamer,
        footage_id: item.id, event_ids: events.filter((e) => state.picked.has(e.id)).map((e) => e.id) };
    };
  }

  // One clip with its own start and end, in match clock time. Picking an event's
  // Clip button fills it in around that event; "Use playhead" takes the main video's time.
  const clipStart = h("input", { type: "text", id: "clip-start", placeholder: "m:ss", inputmode: "numeric" });
  const clipEnd = h("input", { type: "text", id: "clip-end", placeholder: "m:ss", inputmode: "numeric" });
  const clipOthers = h("input", { type: "checkbox", id: "clip-others", checked: true });
  const clipMessage = h("div", { role: "status", "aria-live": "polite" });
  let clipEvent = null;
  const playheadClock = () => mc.seconds((mainVideo.currentTime - main.offset_s) * rate);
  const usePlayhead = (input) => h("button", { type: "button", class: "small",
    onclick: () => { input.value = signedClock(playheadClock()); clipEvent = null; } }, "Use playhead");
  for (const input of [clipStart, clipEnd]) input.addEventListener("input", () => { clipEvent = null; });
  const clipForm = h("form", { class: "panel clip-form", "aria-label": "Export one clip", onsubmit: async (event) => {
    event.preventDefault();
    const start = parseClock(clipStart.value), end = parseClock(clipEnd.value);
    if (Number.isNaN(start) || Number.isNaN(end)) return clipMessage.replaceChildren(h("div", { class: "notice" }, "Write times as m:ss, like 12:03."));
    if (end <= start) return clipMessage.replaceChildren(h("div", { class: "notice" }, "The clip has to end after it starts."));
    try {
      const job = await api("/api/exports", { body: { match_id: detail.match.match_id, streamer_id: item.streamer_id,
        others: clipOthers.checked, windows: [{ start_tick: mc.tick(start), end_tick: mc.tick(end),
          event_id: clipEvent, label: `${signedClock(start)}_to_${signedClock(end)}`.replace(/:/g, "-") }] } });
      state.exportJob = job.id;
      location.hash = "#/exports";
    } catch (error) {
      clipMessage.replaceChildren(h("div", { class: "notice" }, error.message));
    }
  } },
    h("h2", {}, "Export one clip"),
    h("div", { class: "inline-form" },
      h("label", { class: "field", for: "clip-start" }, "Start", clipStart), usePlayhead(clipStart),
      h("label", { class: "field", for: "clip-end" }, "End", clipEnd), usePlayhead(clipEnd)),
    h("label", { for: "clip-others", class: "small" }, clipOthers, " Also from other streamers' footage"),
    clipMessage,
    h("div", {}, h("button", { type: "submit", class: "primary" }, "Export clip")));
  const clipAround = (e) => {
    const s = mc.seconds(e.tick);
    clipStart.value = signedClock(s - 8);
    clipEnd.value = signedClock(s + 4);
    clipEvent = e.id;
    clipMessage.replaceChildren();
    clipForm.scrollIntoView({ behavior: "smooth", block: "center" });
    clipStart.focus({ preventScroll: true });
  };
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
      h("span", { class: "muted small povs" }, povs.join(", "),
        h("button", { type: "button", class: "small", "aria-label": `Clip the ${e.view} at ${clock(e.clock_s)} on its own`,
          onclick: (event) => { event.stopPropagation(); seek(e); clipAround(e); } }, "Clip")));
    return row;
  });

  const playersBox = h("div", { class: "players" },
    h("figure", { class: "player" }, mainVideo, h("figcaption", {}, `${main.streamer} (main)`)),
    h("figure", { class: "player" }, otherBox, otherCaption));

  return h("main", { class: "match-main" },
    h("div", { class: "match-title" },
      h("h1", {}, `Match ${detail.match.match_id}`),
      h("span", { class: "dim" }, `${clock(detail.match.duration_s)} · ${main.streamer}'s footage from ${hms(footageT(main, tick0))} · ${syncedHow(main)}`),
      h("div", { class: "actions" }, exportLink)),
    playersBox,
    playersResize(playersBox),
    h("section", { class: "panel timeline", "aria-label": "Timeline" },
      h("div", { class: "legend" },
        h("span", {}, h("span", { class: "swatch", style: { background: "var(--kill)" } }), "Kill"),
        h("span", {}, h("span", { class: "swatch", style: { background: "var(--death)" } }), "Death"),
        h("span", {}, h("span", { class: "swatch ult" }), "Ult"),
        h("span", { class: "end" }, `0:00 – ${clock(detail.match.duration_s)}`)),
      lanes),
    clipForm,
    h("section", { class: "panel events", "aria-label": "Events" },
      h("div", { class: "grid head" }, h("span", {}, "Clip"), h("span", {}, "Clock"), h("span", {}, "Footage"),
        h("span", {}, "Event"), h("span", {}, "With"), h("span", {}, "Perspectives")),
      rows.length ? rows : h("div", { class: "empty" }, `${main.streamer} has no kills, deaths or ults in this match.`)));
}

// A handle under the videos: drag it down to make them bigger, up to make them smaller,
// double-click to fill the page again. The size is remembered in this browser.
function playersResize(box) {
  const apply = (width) => box.style.setProperty("--players-width", width ? `${Math.round(width)}px` : "100%");
  try { apply(Number(localStorage.getItem("playersWidth")) || null); } catch { /* storage off: full width */ }
  const save = (width) => { try { width ? localStorage.setItem("playersWidth", String(Math.round(width))) : localStorage.removeItem("playersWidth"); } catch { /* ignore */ } };
  let start = null;
  const handle = h("button", { type: "button", class: "players-resize", "aria-label": "Resize the videos",
    title: "Drag to resize the videos, double-click to reset",
    onpointerdown: (event) => {
      handle.setPointerCapture(event.pointerId);
      start = { y: event.clientY, width: box.getBoundingClientRect().width };
    },
    onpointermove: (event) => {
      if (!start || !handle.hasPointerCapture(event.pointerId)) return;
      // Two 16:9 videos side by side: each pixel taller is about 3.5 pixels wider.
      const width = Math.max(320, Math.min(box.parentElement.clientWidth, start.width + (event.clientY - start.y) * 32 / 9));
      apply(width);
      save(width);
    },
    onpointerup: () => { start = null; },
    ondblclick: () => { apply(null); save(null); },
    onkeydown: (event) => {
      const step = event.key === "ArrowDown" ? 60 : event.key === "ArrowUp" ? -60 : 0;
      if (!step) return;
      event.preventDefault();
      const width = Math.max(320, Math.min(box.parentElement.clientWidth, box.getBoundingClientRect().width + step));
      apply(width);
      save(width);
    } });
  return handle;
}

function videoEl(perspective) {
  const video = h("video", { controls: true, preload: "metadata", "aria-label": `${perspective.streamer}'s footage` });
  // When Twitch won't serve a stream, say why under the video, from the server's error.
  const explain = async () => {
    if (video.nextElementSibling && video.nextElementSibling.matches(".video-error")) return;
    const response = await fetch(perspective.video).catch(() => null);
    const data = response && !response.ok ? await response.json().catch(() => null) : null;
    video.after(h("div", { class: "notice video-error", role: "alert" }, (data && data.detail) || "Twitch wouldn't play this video."));
  };
  if (perspective.is_hls && !video.canPlayType("application/vnd.apple.mpegurl") && window.Hls && Hls.isSupported()) {
    const hls = new Hls({ maxBufferLength: 20 });
    hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) explain(); });
    hls.loadSource(perspective.video);
    hls.attachMedia(video);
  } else {
    video.src = perspective.video;
    if (perspective.is_hls) video.addEventListener("error", explain);
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
    const job = all.find((j) => j.kind === "sync" && j.footage_id === f.id && j.status !== "done" && j.status !== "failed");
    const progress = job ? h("span", { style: { display: "flex", flexDirection: "column", gap: "6px" } },
      h("span", { class: "small" }, `${job.message || "Waiting"}${job.total ? ` (${job.done}/${job.total})` : ""}`),
      h("span", { class: "bar", role: "img", "aria-label": `${job.done} of ${job.total}` },
        h("span", { style: { width: `${job.total ? 100 * job.done / job.total : 0}%` } })))
      : h("span", { class: "small" }, `${f.synced_matches} synced`);
    // Renaming swaps the name for a box: Enter saves, Escape cancels, an empty name goes back to the streamer's.
    const nameCell = renaming === f.id
      ? h("form", { class: "rename", onsubmit: (e) => {
          e.preventDefault();
          renaming = null;
          attempt(() => api(`/api/footage/${f.id}`, { method: "PATCH", body: { name: e.target.elements.name.value } }));
        } },
        h("input", { type: "text", name: "name", value: f.name || "", placeholder: f.streamer, "aria-label": "Footage name",
          onkeydown: (e) => { if (e.key === "Escape") { renaming = null; renderFootage(footage, jobs); } } }))
      : h("span", { class: "footage-name" }, f.name || f.streamer, f.name ? h("span", { class: "muted small" }, f.streamer) : null);
    return h("div", { class: "grid" },
      nameCell,
      h("span", { class: "mono" }, when(f.started_at)),
      h("span", { class: "mono" }, hms(f.duration_s)),
      progress,
      h("span", { class: "cell-actions" },
        h("button", { type: "button", class: "small", disabled: Boolean(job),
          onclick: () => attempt(() => api(`/api/footage/${f.id}/sync`, { body: {} })) }, "Scan"),
        f.synced_matches ? h("a", { class: "button small", href: `#/matches/${f.id}` }, "Open") : null,
        h("button", { type: "button", class: "small", onclick: () => {
          renaming = f.id;
          renderFootage(footage, jobs);
          const box = footageBody.querySelector(".rename input");
          box.focus();
          box.select();
        } }, "Rename")));
  });
  let renaming = null;
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
    if (renaming === null) renderFootage(footage, event.detail);  // Don't wipe a name being typed.
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
    h("div", { class: "inline-form" },
      h("button", { type: "submit", class: "primary" }, "Export all"),
      h("button", { type: "button", onclick: () => reviewClips(draft, Number(before.value), Number(after.value), others.checked)
        .catch((error) => message.replaceChildren(h("div", { class: "notice" }, error.message))) }, "Review each clip")))
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

// Advanced mass export: every picked event as its own clip, each with a start and end
// you can change and preview in the main streamer's video before exporting.
async function reviewClips(draft, before, after, others) {
  const detail = await api(`/api/matches/${draft.match_id}`);
  const main = detail.perspectives.find((p) => p.footage_id === draft.footage_id)
    || detail.perspectives.find((p) => p.streamer_id === draft.streamer_id);
  const rate = main.tick_rate;
  const mc = matchClock(detail, rate);
  const names = new Map(detail.players.map((p) => [p.account_id, p.streamer || p.player_name]));
  const mainIds = new Set(detail.players.filter((p) => p.streamer_id === draft.streamer_id).map((p) => p.account_id));
  const preview = videoEl(main);
  let stopAt = null;
  preview.addEventListener("timeupdate", () => { if (stopAt !== null && preview.currentTime >= stopAt) { preview.pause(); stopAt = null; } });
  const message = h("div", { role: "status", "aria-live": "polite" });

  const picked = detail.events.filter((e) => draft.event_ids.includes(e.id));
  const items = picked.map((e) => {
    const s = mc.seconds(e.tick);
    const what = e.kind === "ult" ? `${names.get(e.actor)} ult`
      : mainIds.has(e.actor) ? `kill on ${names.get(e.target) || "non-player"}` : `death to ${names.get(e.actor) || "non-player"}`;
    const include = h("input", { type: "checkbox", checked: true, "aria-label": `Export the clip at ${clock(e.clock_s)}` });
    const start = h("input", { type: "text", value: signedClock(s - before), "aria-label": `Start of the clip at ${clock(e.clock_s)}` });
    const end = h("input", { type: "text", value: signedClock(s + after), "aria-label": `End of the clip at ${clock(e.clock_s)}` });
    const row = h("div", { class: "review-row" }, include,
      h("span", { class: "mono" }, clock(e.clock_s)), h("span", {}, what), start, h("span", { class: "muted" }, "to"), end,
      h("button", { type: "button", class: "small", onclick: () => {
        const from = parseClock(start.value), to = parseClock(end.value);
        if (Number.isNaN(from) || Number.isNaN(to)) return message.replaceChildren(h("div", { class: "notice" }, "Write times as m:ss, like 12:03."));
        preview.currentTime = Math.max(0, main.offset_s + mc.tick(from) / rate);
        stopAt = main.offset_s + mc.tick(to) / rate;
        preview.play().catch(() => {});
      } }, "Preview"));
    return { e, include, start, end, row };
  });

  const exportReviewed = async () => {
    const windows = [];
    for (const { e, include, start, end } of items) {
      if (!include.checked) continue;
      const from = parseClock(start.value), to = parseClock(end.value);
      if (Number.isNaN(from) || Number.isNaN(to) || to <= from) {
        return message.replaceChildren(h("div", { class: "notice" }, `Check the times for the clip at ${clock(e.clock_s)}.`));
      }
      windows.push({ start_tick: mc.tick(from), end_tick: mc.tick(to), event_id: e.id });
    }
    if (!windows.length) return message.replaceChildren(h("div", { class: "notice" }, "Tick at least one clip to export."));
    try {
      const job = await api("/api/exports", { body: { match_id: draft.match_id, streamer_id: draft.streamer_id, others, windows } });
      state.exportJob = job.id;
      state.exportDraft = null;
      exportsPage();
    } catch (error) {
      message.replaceChildren(h("div", { class: "notice" }, error.message));
    }
  };

  show(h("div", { class: "page" }, h("section", { class: "panel review", "aria-label": "Review clips" },
    h("div", { class: "panel-head" }, h("h1", {}, `Review ${items.length} clips from match ${draft.match_id}`),
      h("button", { type: "button", class: "small", onclick: () => exportsPage() }, "Back")),
    h("figure", { class: "player" }, preview, h("figcaption", {}, `${main.streamer} · Preview plays from the clip's start and stops at its end`)),
    h("div", { class: "review-list" }, items.map((i) => i.row)),
    message,
    h("div", { class: "review-actions" }, h("button", { type: "button", class: "primary", onclick: exportReviewed }, "Export ticked clips")))));
}

pollJobs();
route();
