const DEFAULT_SQUAD = [
  1, 497,
  4, 418, 362, 259, 88,
  367, 397, 426, 557, 427,
  194, 379, 165,
];

// Chip labels and copy only -- every chip's points effect is scored by the
// Python service (`chip_effect` / `plan_summary`), never in this file.
const CHIPS = {
  wildcard: { label: "Wildcard", note: "Unlimited free transfers; the new squad is kept.", changesTransfers: true },
  free_hit: { label: "Free Hit", note: "Unlimited free transfers for this Gameweek; the squad then reverts.", changesTransfers: true },
  bench_boost: { label: "Bench Boost", note: "All four bench players score this Gameweek." },
  triple_captain: { label: "Triple Captain", note: "Your captain scores three times instead of twice." },
};

const VIEWS = {
  weekly: "My Team",
  transfers: "Transfers",
  outlook: "Planner",
  settings: "Settings",
};

function normalizeChipStatus(value) {
  return Object.fromEntries(Object.keys(CHIPS).map((chip) => [chip, value?.[chip] === "used" ? "used" : "available"]));
}

function readStoredJson(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "null");
    return value == null ? fallback : value;
  } catch {
    return fallback;
  }
}

function hasStoredSquad() {
  try {
    return localStorage.getItem("touchline-plan") !== null || localStorage.getItem("touchline-squad") !== null;
  } catch {
    return false;
  }
}

function loadPlan() {
  const hadStoredSquad = hasStoredSquad();
  const stored = readStoredJson("touchline-plan", null);
  const legacy = {
    squad: readStoredJson("touchline-squad", DEFAULT_SQUAD),
    selling_prices: readStoredJson("touchline-selling-prices", {}),
    selling_price_is_estimated: readStoredJson("touchline-selling-estimated", false),
    current_setup: readStoredJson("touchline-current-setup", null),
    horizon_length: Math.max(1, Math.min(5, Number(localStorage.getItem("touchline-horizon") || 5) || 3)),
    risk_profile: localStorage.getItem("touchline-risk-profile") || "balanced",
  };
  const source = stored && typeof stored === "object" ? stored : legacy;
  const plan = {
    squad: Array.isArray(source.squad) ? source.squad : legacy.squad,
    bank_tenths: Number.isInteger(source.bank_tenths) ? source.bank_tenths : 0,
    free_transfers: Number.isInteger(source.free_transfers) ? source.free_transfers : 1,
    selling_prices: source.selling_prices && typeof source.selling_prices === "object" ? source.selling_prices : legacy.selling_prices,
    selling_price_is_estimated: Boolean(source.selling_price_is_estimated),
    current_setup: source.current_setup || null,
    pending_transfers: Array.isArray(source.pending_transfers) ? source.pending_transfers : [],
    horizon_length: Math.max(1, Math.min(5, Number(source.horizon_length || legacy.horizon_length) || 5)),
    risk_profile: source.risk_profile || legacy.risk_profile,
    chip_status: normalizeChipStatus(source.chip_status),
    chip: null,
    // Where the squad came from: "team" (Team ID), "demo", or "manual" (an
    // older saved squad). null means a first visit, which shows the connect
    // screen instead of silently planning with the demo squad.
    // The release the saved selling prices were checked against; see loadWorkspace.
    release_id: typeof source.release_id === "string" ? source.release_id : null,
    squad_source: ["team", "demo", "manual"].includes(source.squad_source)
      ? source.squad_source
      : hadStoredSquad ? "manual" : null,
  };
  // A stored chip survives reload only while it is known and still available.
  if (CHIPS[source.chip] && plan.chip_status[source.chip] !== "used") plan.chip = source.chip;
  localStorage.setItem("touchline-plan", JSON.stringify(plan));
  return plan;
}

function readEntryParams() {
  const params = new URLSearchParams(window.location.search);
  const team = (params.get("team") || "").trim();
  const demo = params.get("demo") === "1";
  if (params.has("team") || params.has("demo")) {
    // The landing-page form hands the Team ID over in the URL once; drop it
    // so a reload or a shared link does not re-import the squad.
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.hash}`);
  }
  return team ? { team } : demo ? { demo: true } : null;
}

const state = {
  bootstrap: null,
  plan: loadPlan(),
  // Reviewed role-scenario overrides: {fpl_id, gameweek, xpts}[]. Applied
  // entirely client-side per request -- never persisted server-side or
  // written back to the release, and NOT saved to localStorage (a scenario
  // is deliberately a this-session-only what-if, not private squad state).
  roleScenarioOverrides: [],
  lineups: null,
  transfers: null,
  teamProfile: readStoredJson("touchline-team-profile", null),
  teamNote: "",
  pendingEntry: readEntryParams(),
  alphaToken: sessionStorage.getItem("touchline-alpha-token"),
  publicConfig: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const money = (tenths) => `£${(tenths / 10).toFixed(1)}`;
const points = (value) => Number(value || 0).toFixed(2);
const signed = (value) => `${Number(value || 0) >= 0 ? "+" : ""}${points(value)}`;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

function capitalize(value) {
  const text = String(value ?? "");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function ordinal(value) {
  const number = Math.round(Number(value));
  const tens = number % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" })[number % 10] || "th";
  return `${number}${suffix}`;
}

function persistPlan() {
  localStorage.setItem("touchline-plan", JSON.stringify(state.plan));
}

function effectivePendingCount() {
  return state.plan.pending_transfers.length;
}

function requestBody() {
  return {
    fpl_ids: state.plan.squad,
    bank_tenths: state.plan.bank_tenths,
    free_transfers: state.plan.free_transfers,
    selling_prices: state.plan.selling_prices,
    role_scenario_overrides: state.roleScenarioOverrides,
    current_setup: state.plan.current_setup,
    pending_transfers: state.plan.pending_transfers,
    chip: state.plan.chip,
    chip_status: state.plan.chip_status,
  };
}

async function api(path, options = {}) {
  const accessHeaders = state.alphaToken ? { "X-FPL-Alpha-Token": state.alphaToken } : {};
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...accessHeaders, ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) {
    const retryAfter = response.headers.get("Retry-After");
    const suffix = response.status === 429 && retryAfter ? ` Try again in ${retryAfter}s.` : "";
    const error = new Error(`${payload.detail || `Request failed (${response.status})`}${suffix}`);
    error.status = response.status;
    error.code = payload.code;
    if (response.status === 401 && payload.code === "alpha_access_required") {
      state.alphaToken = null;
      sessionStorage.removeItem("touchline-alpha-token");
      showAccessGate(error.message);
    }
    throw error;
  }
  return payload;
}

function showAccessGate(message = "") {
  const gate = $("#access-gate");
  gate.hidden = false;
  $("#access-message").textContent = message;
  window.setTimeout(() => $("#access-code").focus(), 0);
}

function hideAccessGate() {
  $("#access-gate").hidden = true;
  $("#access-code").value = "";
  $("#access-message").textContent = "";
}

function showError(message) {
  const banner = $("#error-banner");
  banner.textContent = message;
  banner.hidden = !message;
}

function renderPublicConfig() {
  const config = state.publicConfig;
  if (!config) return;
  const support = $("#support-link");
  if (config.support_email) {
    support.href = `mailto:${config.support_email}`;
    support.hidden = false;
  } else {
    support.hidden = true;
  }
  $("#operator-label").textContent = config.operator_name
    ? `Operated by ${config.operator_name}`
    : "Operator details pending; not alpha-ready";
}

function renderDecisionReceipts() {
  const bar = $("#decision-receipts");
  const receipts = [
    ["Lineup & planner", state.lineups?.decision_receipt],
    ["Transfers", state.transfers?.decision_receipt],
  ].filter((row) => row[1]);
  if (!receipts.length) {
    bar.hidden = true;
    bar.innerHTML = "";
    return;
  }
  bar.hidden = false;
  bar.innerHTML = `<strong>Decision receipts</strong><small>The exact inputs and outputs behind each recommendation, as JSON.</small>${receipts.map(([label, receipt]) => `<button class="button secondary small" type="button" data-receipt-id="${receipt.decision_id}">${label} · ${receipt.decision_id.slice(-8)} ↓</button>`).join("")}`;
  bar.querySelectorAll("[data-receipt-id]").forEach((button) => button.addEventListener("click", () => {
    const receipt = receipts.find((row) => row[1].decision_id === button.dataset.receiptId)?.[1];
    if (!receipt) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(receipt, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${receipt.decision_id}.json`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }));
}

function playerById(id) {
  return state.bootstrap.players.find((player) => player.fpl_id === Number(id));
}

function teamCode(player) {
  return player.team || playerById(player.fpl_id)?.team || "";
}

const RISK_PROFILES = {
  conservative: { label: "Conservative", note: "Only moves with a clear projected gain.", threshold: 2 },
  balanced: { label: "Balanced", note: "Every move the model ranks, best first.", threshold: 0 },
  aggressive: { label: "Aggressive", note: "Also show speculative differentials.", threshold: -2 },
};

function publishedGameweeks() {
  return state.bootstrap?.release?.model_runs?.map((row) => Number(row.gameweek)) || [];
}

function visibleLineups() {
  const lineups = state.lineups?.lineups || [];
  return lineups.slice(0, Math.max(1, Math.min(state.plan.horizon_length, lineups.length)));
}

function renderPlanningControls() {
  // Clamping the horizon needs the published Gameweeks; before the release
  // loads there are none, and clamping would silently shrink the plan to 1.
  if (!state.bootstrap) return;
  if (!RISK_PROFILES[state.plan.risk_profile]) state.plan.risk_profile = "balanced";
  const gameweeks = publishedGameweeks();
  const select = $("#horizon-select");
  if (!select) return;
  const available = Math.min(5, gameweeks.length);
  select.innerHTML = Array.from({ length: 5 }, (_, index) => {
    const length = index + 1;
    const enabled = length <= available;
    return `<option value="${length}" ${length === state.plan.horizon_length ? "selected" : ""} ${enabled ? "" : "disabled"}>${length} Gameweek${length === 1 ? "" : "s"}${enabled ? "" : " — not published yet"}</option>`;
  }).join("");
  if (state.plan.horizon_length > available) state.plan.horizon_length = available || 1;
  select.value = String(state.plan.horizon_length);
  const first = gameweeks[0];
  const last = gameweeks[Math.min(state.plan.horizon_length, gameweeks.length) - 1];
  $("#horizon-note").textContent = gameweeks.length
    ? `Published: GW${first}–GW${gameweeks.at(-1)}. You are planning GW${first}–GW${last}.`
    : "No Gameweeks are published yet.";
  $("#horizon-availability").textContent = gameweeks.length ? `${gameweeks.length} GW published` : "";
  const profiles = $("#risk-profiles");
  profiles.innerHTML = Object.entries(RISK_PROFILES).map(([key, profile]) => `<button type="button" class="risk-profile ${state.plan.risk_profile === key ? "selected" : ""}" data-risk-profile="${key}"><strong>${profile.label}</strong><small>${profile.note}</small></button>`).join("");
  $$("[data-risk-profile]").forEach((button) => button.addEventListener("click", () => {
    state.plan.risk_profile = button.dataset.riskProfile;
    persistPlan();
    renderPlanningControls();
    renderTransfers();
  }));
  renderWildcardControls();
}

function teamDisplayName() {
  if (state.teamProfile?.name) return state.teamProfile.name;
  if (state.teamProfile?.id) return `FPL team ${state.teamProfile.id}`;
  return state.plan.squad_source === "demo" ? "Demo squad" : "Your squad";
}

function renderTeamHeader() {
  const name = teamDisplayName();
  const first = publishedGameweeks()[0];
  $("#team-title").textContent = name;
  const subtitle = $("#team-subtitle");
  if (state.plan.squad_source === "demo" && !state.teamProfile) {
    subtitle.innerHTML = 'You\'re exploring a demo squad. <button type="button" class="text-button" id="focus-team-id">Load your own team</button>';
    $("#focus-team-id").addEventListener("click", () => {
      $("#team-id").scrollIntoView({ behavior: "smooth", block: "center" });
      $("#team-id").focus({ preventScroll: true });
    });
  } else {
    subtitle.textContent = `Your best XI, captain and bench order${first ? ` for GW${first}` : ""}, and what to change before the deadline.`;
  }
  const summary = $("#team-summary");
  if (state.teamProfile) {
    summary.hidden = false;
    summary.innerHTML = `<strong>${escapeHtml(name)}</strong><small>Team ID ${escapeHtml(state.teamProfile.id || "—")}${state.teamNote ? ` · ${escapeHtml(state.teamNote)}` : ""}</small>`;
  } else {
    summary.hidden = true;
    summary.innerHTML = "";
  }
}

function renderRelease() {
  const release = state.bootstrap.release;
  const healthLabels = { production: "Production", shadow: "Model preview", research_only: "Research only" };
  $("#release-label").textContent = healthLabels[String(release.health || "").toLowerCase()] || release.label;
  $("#release-meta").textContent = `${release.model_version} · built ${new Date(release.planning_as_of).toLocaleString()}`;
  const gws = release.model_runs.map((row) => row.gameweek);
  $("#horizon-label").textContent = gws.length > 1 ? `GW${gws[0]}–GW${gws.at(-1)}` : `GW${gws[0]}`;
  renderPlanningControls();
  renderTeamHeader();

  const coverage = release.coverage;
  $("#release-coverage").textContent = coverage
    ? `${coverage.fully_covered_players}/${coverage.total_registered_players} players covered`
    : "";

  const freshness = release.freshness;
  if (freshness) {
    const finalCount = freshness.gameweeks.filter((row) => row.fpl_finality?.is_final).length;
    const summary = `${finalCount}/${freshness.gameweeks.length} GW final`;
    $("#release-freshness").textContent = freshness.passes ? summary : `${summary} · ${freshness.problems.length} freshness issue(s)`;
    $("#release-freshness").classList.toggle("negative", !freshness.passes);
  } else {
    $("#release-freshness").textContent = "";
  }
}

function renderSquadEditor() {
  const selectedPlayers = state.plan.squad.map(playerById).filter(Boolean);
  const cost = selectedPlayers.reduce((sum, player) => sum + player.price_tenths, 0);
  $("#squad-cost").textContent = `${money(cost)} squad value`;
  if (!state.teamNote) {
    $("#team-id-note").textContent = state.plan.selling_price_is_estimated
      ? "Selling prices are estimated from current prices, not FPL's profit-sharing rule. Check the FPL app for your exact sell values before a real transfer."
      : "We read your public squad only — never a password or login.";
  }
  const positionOrder = ["GK", "DEF", "MID", "FWD"];
  $("#squad-editor").classList.remove("skeleton");
  $("#squad-editor").innerHTML = positionOrder.map((position) => {
    const rows = selectedPlayers.filter((player) => player.position === position);
    return `<div class="position-group"><div class="position-label">${position}</div>${rows.map((player) => {
      const options = state.bootstrap.players
        .filter((candidate) => candidate.position === position)
        .sort((a, b) => b.price_tenths - a.price_tenths || a.name.localeCompare(b.name))
        .map((candidate) => `<option value="${candidate.fpl_id}" ${candidate.fpl_id === player.fpl_id ? "selected" : ""}>${candidate.name} · ${candidate.team} · ${money(candidate.price_tenths)}</option>`)
        .join("");
      return `<div class="squad-player"><span class="kit team-chip" data-kit="${player.team}" title="${player.team}"></span><select data-player-id="${player.fpl_id}" aria-label="Replace ${player.name}">${options}</select><strong>${money(player.price_tenths)}</strong></div>`;
    }).join("")}</div>`;
  }).join("");

  $$("#squad-editor select").forEach((select) => {
    select.addEventListener("change", async (event) => {
      const oldId = Number(event.target.dataset.playerId);
      const newId = Number(event.target.value);
      if (state.plan.squad.includes(newId)) {
        showError("That player is already in the squad.");
        event.target.value = String(oldId);
        return;
      }
      state.plan.squad = state.plan.squad.map((id) => id === oldId ? newId : id);
      state.plan.pending_transfers = [];
      clearCurrentSetup();
      persistPlan();
      renderSquadEditor();
      await runLineups();
    });
  });
  renderPendingTransfers();
  renderChipPanel();
}

function chipGameweekLabel() {
  const gameweek = publishedGameweeks()[0];
  return gameweek ? `GW${gameweek}` : "this Gameweek";
}

function renderChipPanel() {
  const container = $("#chip-options");
  if (!container) return;
  const active = state.plan.chip;
  const option = (key, label, note, { used = false } = {}) => `<div class="chip-row ${active === key || (!active && !key) ? "active" : ""} ${used ? "used" : ""}">
      <label><input type="radio" name="active-chip" value="${key}" ${active === key || (!active && !key) ? "checked" : ""} ${used ? "disabled" : ""}><span><strong>${label}</strong>${active === key && key ? `<small>${note}</small>` : ""}</span></label>
      ${key ? `<button type="button" class="chip-status" data-chip-status="${key}" aria-pressed="${used}" aria-label="${label} used this half-season">${used ? "Used" : "Available"}</button>` : ""}
    </div>`;
  container.innerHTML = option("", "No chip", "")
    + Object.entries(CHIPS).map(([key, chip]) => {
      const used = state.plan.chip_status[key] === "used";
      return option(key, chip.label, used ? "Already played this half-season." : chip.note, { used });
    }).join("");
  $("#chip-note").textContent = active
    ? `${CHIPS[active].label} is a what-if for ${chipGameweekLabel()}. Your squad rating stays chip-free.`
    : `Try a chip for ${chipGameweekLabel()}. Tap “Available” to mark a chip you've already played.`;
  container.querySelectorAll('input[name="active-chip"]').forEach((input) => input.addEventListener("change", () => setActiveChip(input.value || null)));
  container.querySelectorAll("[data-chip-status]").forEach((button) => button.addEventListener("click", () => toggleChipStatus(button.dataset.chipStatus)));
  renderWildcardControls();
}

async function setActiveChip(chip) {
  if (chip && state.plan.chip_status[chip] === "used") return;
  if (state.plan.chip === chip) return;
  state.plan.chip = chip;
  persistPlan();
  renderChipPanel();
  resetTransfersView(chip
    ? `${CHIPS[chip].label} is now active for ${chipGameweekLabel()} — scan again to compare moves with the chip played.`
    : "Chip cleared — scan again to compare moves without a chip.");
  await runLineups();
}

async function toggleChipStatus(chip) {
  if (!CHIPS[chip]) return;
  const nowUsed = state.plan.chip_status[chip] !== "used";
  state.plan.chip_status[chip] = nowUsed ? "used" : "available";
  const clearedActive = nowUsed && state.plan.chip === chip;
  if (clearedActive) state.plan.chip = null;
  persistPlan();
  renderChipPanel();
  if (clearedActive) resetTransfersView(`${CHIPS[chip].label} marked used, so it is no longer active — scan again to compare moves without it.`);
  await runLineups();
}

// The server compares a Wildcard over 2+ Gameweeks and needs the post-Wildcard
// roll to end inside the visible horizon; keep the inputs inside that contract.
function wildcardRollLimit() {
  return Math.max(0, state.plan.horizon_length - 1);
}

function renderWildcardControls() {
  const button = $("#run-wildcard");
  const roll = $("#wc-roll");
  if (!button || !roll) return;
  const used = state.plan.chip_status.wildcard === "used";
  const tooShort = state.plan.horizon_length < 2;
  const limit = wildcardRollLimit();
  roll.max = String(limit);
  roll.value = String(Math.max(0, Math.min(limit, Math.floor(Number(roll.value) || 0))));
  roll.disabled = tooShort;
  button.disabled = used || tooShort;
  $("#wildcard-status").textContent = used
    ? "Your Wildcard is marked used for this half-season. Mark it available in the Chips panel to compare."
    : tooShort
      ? "A Wildcard comparison needs at least two visible Gameweeks. Widen the horizon in Settings."
      : "";
}

function fixtureLabel(player) {
  const fixtures = player.fixtures || [];
  if (fixtures.length === 0) return "no fixture";
  return fixtures.map((row) => `${row.opponent} (${row.is_home ? "H" : "A"})`).join(", ");
}

function playerCard(player, captainId, viceId, captainBadge = "C") {
  const badge = player.fpl_id === captainId ? captainBadge : player.fpl_id === viceId ? "V" : "";
  const badgeClass = badge === "TC" ? "triple" : badge === "V" ? "vice" : "";
  const team = teamCode(player);
  const fixture = fixtureLabel(player);
  return `<article class="pitch-player player-card ${player.position.toLowerCase()}" title="${escapeHtml(`${player.name} · ${team} · ${fixture} · ${points(player.xpts)} xPts`)}">
    ${badge ? `<span class="captain-badge ${badgeClass}">${badge}</span>` : ""}
    <span class="kit" data-kit="${team}"></span>
    <strong class="name">${player.name}</strong>
    <span class="meta"><span>${fixture}</span><b>${Number(player.xpts || 0).toFixed(1)}</b></span>
  </article>`;
}

function clearCurrentSetup() {
  state.plan.current_setup = null;
  persistPlan();
  localStorage.removeItem("touchline-current-setup");
}

function pendingTransferLabel(row) {
  const outgoing = playerById(row.out_fpl_id)?.name || `#${row.out_fpl_id}`;
  const incoming = playerById(row.in_fpl_id)?.name || `#${row.in_fpl_id}`;
  return `${outgoing} → ${incoming}`;
}

function renderPendingTransfers() {
  const pending = state.plan.pending_transfers;
  const summary = state.lineups?.plan_summary;
  const commitBlocked = summary?.commit_allowed === false;
  const actionNote = commitBlocked
    ? `Free Hit moves apply to GW${summary.squad_reverts_after_gameweek} only and the squad reverts, so there is nothing to commit.`
    : state.plan.chip === "wildcard"
      ? "No hits while Wildcard is active. Committing marks your Wildcard used."
      : "Your lineup and planner already include these moves.";
  const markup = pending.length === 0 ? "" : `<div class="pending-heading"><span>Staged transfers (${pending.length})</span><span>Not made in FPL yet</span></div><div class="pending-list">${pending.map((row, index) => `<span class="pending-chip">${pendingTransferLabel(row)}<button type="button" data-remove-pending="${index}" aria-label="Remove staged transfer ${pendingTransferLabel(row)}">×</button></span>`).join("")}</div><div class="pending-actions"><small>${actionNote}</small><button type="button" class="button primary small" data-commit-pending ${summary && !commitBlocked ? "" : "disabled"}>Commit to squad</button></div>`;
  $$("#squad-pending-transfers, #transfers-pending-transfers").forEach((container) => {
    container.innerHTML = markup;
    container.querySelectorAll("[data-remove-pending]").forEach((button) => button.addEventListener("click", () => removePendingTransfer(Number(button.dataset.removePending))));
    container.querySelector("[data-commit-pending]")?.addEventListener("click", commitPendingTransfers);
  });
}

async function removePendingTransfer(index) {
  if (!Number.isInteger(index) || !state.plan.pending_transfers[index]) return;
  state.plan.pending_transfers.splice(index, 1);
  persistPlan();
  renderPendingTransfers();
  resetTransfersView("Staged move removed — scan again to compare from the updated plan.");
  await runLineups();
}

async function stageTransfers(transfers, message) {
  const staged = (transfers || []).map((row) => ({
    out_fpl_id: row.out_fpl_id ?? row.out?.fpl_id,
    in_fpl_id: row.in_fpl_id ?? row.in?.fpl_id,
  }));
  if (staged.length === 0 || staged.some((row) => !row.out_fpl_id || !row.in_fpl_id)) return;
  state.plan.pending_transfers.push(...staged);
  persistPlan();
  renderPendingTransfers();
  resetTransfersView(message);
  await runLineups();
}

async function stageTransfer(row) {
  if (!row?.out?.fpl_id || !row?.in?.fpl_id) return;
  await stageTransfers(
    [row],
    `${row.out.name} → ${row.in.name} is staged. Your lineup and planner now include it.`,
  );
}

async function stagePath(path) {
  if (!path?.transfers?.length) return;
  const moveNames = path.transfers.map((row) => `${row.out_name} → ${row.in_name}`).join(" · ");
  await stageTransfers(
    path.transfers,
    `${moveNames} staged from “${path.label}”. Your lineup and planner now include it.`,
  );
}

async function commitPendingTransfers() {
  const summary = state.lineups?.plan_summary;
  if (!summary || summary.commit_allowed === false || state.plan.pending_transfers.length === 0) return;
  const playedWildcard = summary.chip === "wildcard";
  state.plan.squad = summary.effective_fpl_ids;
  state.plan.bank_tenths = summary.effective_bank_tenths;
  state.plan.free_transfers = summary.effective_free_transfers;
  state.plan.selling_prices = summary.effective_selling_prices;
  state.plan.pending_transfers = [];
  state.plan.current_setup = null;
  if (playedWildcard) {
    // Committing a Wildcard rebuild spends the chip; say so rather than
    // leaving it silently active.
    state.plan.chip_status.wildcard = "used";
    state.plan.chip = null;
  }
  localStorage.removeItem("touchline-current-setup");
  persistPlan();
  $("#bank").value = (state.plan.bank_tenths / 10).toFixed(1);
  $("#free-transfers").value = String(state.plan.free_transfers);
  renderSquadEditor();
  resetTransfersView(playedWildcard
    ? "Wildcard committed and marked used — free transfers were kept. Scan again to compare from the new squad."
    : "Transfer committed — scan again to compare from your updated squad.");
  await runLineups();
}

function transfersEmptyState(message) {
  return `<div><strong>${message}</strong>Compare holding, a free transfer, a −4 hit and rolling, then the best single moves.</div><button type="button" class="button primary" id="rescan-transfers">Find my best transfers</button>`;
}

function resetTransfersView(message = "Your squad or scenario changed — scan again to compare from the current plan.") {
  state.transfers = null;
  renderDecisionReceipts();
  renderPendingTransfers();
  $("#transfer-paths").innerHTML = "";
  const container = $("#transfer-results");
  container.className = "transfer-list empty-state";
  container.innerHTML = transfersEmptyState(message);
  $("#rescan-transfers")?.addEventListener("click", runTransfers);
}

async function applyBlankScenario(fplId, gameweek) {
  state.roleScenarioOverrides = [
    ...state.roleScenarioOverrides.filter(
      (row) => !(row.fpl_id === fplId && row.gameweek === gameweek)
    ),
    { fpl_id: fplId, gameweek, xpts: 0.0 },
  ];
  resetTransfersView();
  await runLineups();
}

async function clearRoleScenarioOverrides() {
  state.roleScenarioOverrides = [];
  resetTransfersView();
  await runLineups();
}

function renderSensitivityBanner(sensitivity, gameweek) {
  const banner = $("#sensitivity-banner");
  if (state.roleScenarioOverrides.length > 0) {
    banner.hidden = false;
    banner.className = "sensitivity-banner scenario-active";
    const names = state.roleScenarioOverrides
      .map((row) => playerById(row.fpl_id)?.name || `#${row.fpl_id}`)
      .join(", ");
    banner.innerHTML = `<strong>What-if active:</strong> assuming ${names} does not play. The projections themselves are unchanged — this view is recalculated for you only. <button type="button" id="clear-scenario" class="button secondary small">Back to the normal projections</button>`;
    $("#clear-scenario").addEventListener("click", clearRoleScenarioOverrides);
    return;
  }
  if (!sensitivity || sensitivity.label !== "sensitive") {
    banner.hidden = true;
    banner.className = "";
    banner.innerHTML = "";
    return;
  }
  const flagged = sensitivity.scenarios_that_change_the_recommendation;
  const players = flagged.map((row) => row.player_name).join(", ");
  const buttons = flagged
    .map(
      (row) =>
        `<button type="button" class="button secondary small review-scenario" data-fpl-id="${row.fpl_id}" data-gameweek="${gameweek}">What if ${row.player_name} doesn't play?</button>`
    )
    .join(" ");
  banner.hidden = false;
  banner.className = "sensitivity-banner";
  banner.innerHTML = `<strong>Fragile pick.</strong> This lineup depends on ${players || "a rotation-risk player"} playing — if they don't, your best XI or captain would change. ${buttons}`;
  $$(".review-scenario").forEach((button) =>
    button.addEventListener("click", () =>
      applyBlankScenario(Number(button.dataset.fplId), Number(button.dataset.gameweek))
    )
  );
}

function renderWeekly() {
  if (!state.lineups) return;
  const lineup = state.lineups.lineups[0];
  const rating = state.lineups.squad_rating;
  const overall = rating?.available ? rating.model_strength.overall_3gw : null;
  const chipLine = lineup.chip_effect ? `Includes ${lineup.chip_effect.label} ${signed(lineup.chip_effect.delta_xpts)}` : "Best XI with captain";
  $("#weekly-summary").classList.remove("skeleton");
  $("#weekly-summary").innerHTML = `
    <div class="stat primary"><span>GW${lineup.gameweek} projected points</span><strong>${points(lineup.total_xpts)}</strong><small>${chipLine}</small></div>
    <div class="stat"><span>Squad rating</span><strong class="rating-value">${overall ? `${ordinal(overall.percentile)} percentile` : "—"}</strong><small>${overall ? `vs legal squads on your budget · ${rating.display_label}` : "Rating withheld for this release"}</small></div>
    <div class="stat"><span>Captain</span><strong>${lineup.captain.name}</strong><small>Vice: ${lineup.vice_captain.name}</small></div>`;
  const planSummary = state.lineups.plan_summary || {
    gameweek: lineup.gameweek,
    formation: lineup.formation,
    captain: lineup.captain,
    staged_transfer_count: effectivePendingCount(),
    net_xpts_vs_holding: 0,
  };
  const planDelta = Number(planSummary.net_xpts_vs_holding || 0);
  const chipEffect = Number(planSummary.chip_effect_xpts || 0);
  const chipCell = planSummary.chip
    ? `<div class="plan-chip"><span>Chip</span><strong class="chip-badge">${planSummary.chip_label}</strong>${chipEffect ? `<small>+${points(chipEffect)} xPts</small>` : ""}</div>`
    : "";
  const hitCell = planSummary.pending_hit_cost ? `<div><span>Hits</span><strong class="negative">−${planSummary.pending_hit_cost}</strong></div>` : "";
  const revertNote = planSummary.squad_reverts_after_gameweek
    ? `<p class="plan-note">Free Hit squad for GW${planSummary.squad_reverts_after_gameweek} only; later Gameweeks use your committed squad.</p>`
    : "";
  const planIsIdle = !planSummary.staged_transfer_count && !planSummary.chip && !planSummary.pending_hit_cost;
  $("#plan-header").innerHTML = planIsIdle ? "" : `<span class="plan-title">Plan for GW${planSummary.gameweek}</span><div><span>Staged transfers</span><strong>${planSummary.staged_transfer_count}</strong></div>${chipCell}${hitCell}<div class="plan-delta"><span>Net xPts vs holding</span><strong class="${planDelta >= 0 ? "positive" : "negative"}">${planDelta >= 0 ? "+" : ""}${points(planDelta)}</strong></div>${revertNote}`;
  renderSensitivityBanner(lineup.role_scenario_sensitivity, lineup.gameweek);
  renderMarginalChanges(lineup);
  const chip = lineup.chip_effect?.chip;
  const captainBadge = chip === "triple_captain" ? "TC" : "C";
  $("#lineup-title").textContent = `Best XI for GW${lineup.gameweek} · ${lineup.formation}`;
  const rows = ["GK", "DEF", "MID", "FWD"].map((position) => {
    const players = lineup.starters.filter((player) => player.position === position);
    return `<div class="pitch-line ${position.toLowerCase()}">${players.map((player) => playerCard(player, lineup.captain.fpl_id, lineup.vice_captain.fpl_id, captainBadge)).join("")}</div>`;
  });
  $("#pitch").classList.remove("skeleton");
  $("#pitch").innerHTML = rows.join("");
  const benchTitle = chip === "bench_boost"
    ? "Bench<small>Bench Boost: all four score</small>"
    : `Bench<small>In substitution order · autosub value ${signed(lineup.expected_autosub_value)}</small>`;
  $("#bench").innerHTML = `<div class="bench-title">${benchTitle}</div>${lineup.bench.map((player, index) => `<div class="bench-player"><span class="kit" data-kit="${teamCode(player)}"></span><strong>${player.name}</strong><small><span class="bench-order">${index ? `${index}.` : "GK"}</span> ${points(player.xpts)} xPts</small></div>`).join("")}`;
}

function renderMarginalChanges(lineup) {
  const container = $("#marginal-changes");
  const comparison = lineup.current_setup_comparison;
  if (!comparison && !state.plan.current_setup && !state.teamProfile) {
    // A demo or hand-built squad has no submitted XI to compare with.
    container.className = "marginal-changes";
    container.hidden = true;
    container.innerHTML = "";
    return;
  }
  container.hidden = false;
  if (!comparison) {
    container.className = "marginal-changes unavailable";
    const stagedNote = state.plan.current_setup && effectivePendingCount() > 0
      ? "The comparison with your submitted team stays on your committed squad; staged moves are shown in the plan bar above."
      : "Your loaded picks are for a different Gameweek than the projections, so there's no submitted XI to compare with yet.";
    container.innerHTML = `<div><span class="label">No-chip changes vs your current setup</span><strong>${state.plan.current_setup && effectivePendingCount() > 0 ? "Committed setup kept" : "Not compared yet"}</strong></div><p>${stagedNote}</p>`;
    return;
  }

  const marginal = Number(comparison.marginal_xpts || 0);
  const changeRows = [];
  if (comparison.started.length > 0) {
    changeRows.push(`<li><strong>XI</strong><span>Start ${comparison.started.map((row) => row.name).join(", ")}; bench ${comparison.benched.map((row) => row.name).join(", ")}.</span><small>The best legal formation adds ${signed(comparison.starting_xpts_gain)} xPts to your starting XI.</small></li>`);
  }
  if (comparison.captain_change) {
    changeRows.push(`<li><strong>Captain</strong><span>${comparison.captain_change.from.name} → ${comparison.captain_change.to.name}</span><small>The higher-projected captain adds ${signed(comparison.captain_xpts_gain)} xPts.</small></li>`);
  }
  if (comparison.vice_captain_change) {
    changeRows.push(`<li><strong>Vice</strong><span>${comparison.vice_captain_change.from.name} → ${comparison.vice_captain_change.to.name}</span><small>Your next-best starter covers if the captain doesn't play.</small></li>`);
  }
  if (comparison.bench_order_changed) {
    changeRows.push(`<li><strong>Bench</strong><span>${lineup.bench.map((row) => row.name).join(" → ")}</span><small>Goalkeeper first, then outfield subs by projected points for autosubs.</small></li>`);
  }
  if (changeRows.length === 0) {
    changeRows.push("<li><strong>All set</strong><span>Your submitted XI, captain, vice-captain and bench order already match the best pick.</span></li>");
  }

  container.className = "marginal-changes available";
  container.innerHTML = `<div class="marginal-heading"><div><h2>What to change before the deadline</h2><span>No-chip changes vs your current setup · ${points(comparison.current_total_xpts)} → ${points(comparison.recommended_total_xpts)} xPts</span></div><b class="${marginal >= 0 ? "positive" : "negative"}">${signed(marginal)} xPts</b></div><ul>${changeRows.join("")}</ul>`;
}

function renderOutlook() {
  if (!state.lineups) return;
  const horizonLineups = visibleLineups();
  const rating = state.lineups.squad_rating;
  const overallRating = rating?.available ? rating.model_strength.overall_3gw : null;
  const first = horizonLineups[0]?.gameweek;
  const last = horizonLineups.at(-1)?.gameweek;
  $("#outlook-total").classList.remove("skeleton");
  const visibleTotal = horizonLineups.reduce((sum, row) => sum + Number(row.total_xpts || 0), 0);
  $("#outlook-total").innerHTML = `<div><span>Projected horizon score</span><strong>${points(visibleTotal)}</strong><small>GW${first}${last !== first ? `–GW${last}` : ""} · best XI and captain each week</small></div>${overallRating ? `<div class="rating-score"><span>${rating.display_label} rating</span><strong>${ordinal(overallRating.percentile)}</strong><small>percentile over the full published horizon</small></div>` : `<div class="rating-score unavailable"><span>${rating?.display_label || "Model Preview"} rating</span><strong>—</strong><small>No benchmark yet; the points are still valid</small></div>`}`;
  if (overallRating && state.lineups.chip_scenario) $("#outlook-total").insertAdjacentHTML("beforeend", `<small class="outlook-benchmark-note">Chip what-if: the rating scores your squad without the chip.</small>`);
  const perGameweekRating = new Map(
    rating?.available
      ? rating.model_strength.per_gameweek.map((row) => [row.gameweek, row.percentile])
      : []
  );
  $("#outlook-grid").innerHTML = horizonLineups.map((lineup) => {
    const benchTotal = lineup.bench.reduce((sum, player) => sum + Number(player.xpts || 0), 0);
    const percentile = perGameweekRating.get(lineup.gameweek);
    return `<article class="gw-card">
      <span>GW${lineup.gameweek}</span>
      <strong>${points(lineup.total_xpts)}</strong>
      <p>${lineup.formation} · ${lineup.captain.name} (C)${lineup.captain.fixtures?.length ? ` vs ${fixtureLabel(lineup.captain)}` : ""}</p>
      ${lineup.chip_effect ? `<p class="chip-effect"><span class="chip-badge">${lineup.chip_effect.label}</span>${lineup.chip_effect.delta_xpts ? ` +${points(lineup.chip_effect.delta_xpts)} xPts` : ""}</p>` : ""}
      <p class="gw-percentile">${percentile == null ? "Rating withheld" : `${ordinal(percentile)} percentile`}</p>
      <p class="bench-depth">Bench <b>${points(benchTotal)}</b> xPts</p>
    </article>`;
  }).join("");

  const detail = $("#outlook-rating-detail");
  detail.classList.remove("skeleton");
  if (!rating?.available) {
    detail.className = "rating-detail unavailable";
    detail.innerHTML = `<strong>Why there is no rating yet</strong><p>${rating?.explanation || "No benchmark contract was returned."}</p>`;
  } else {
    const uncertainty = rating.projection_uncertainty.cumulative_rss;
    const releaseGate = rating.release_gate.production_approved ? "Production approved." : `${rating.release_gate.health === "shadow" ? "Model preview" : rating.release_gate.health} release — not yet approved for production.`;
    detail.className = "rating-detail available";
    detail.innerHTML = `<div><span>Squad strength</span><strong>${ordinal(overallRating.percentile)} percentile</strong></div><div><span>Data confidence</span><strong>${capitalize(rating.data_confidence.state)}</strong></div><div><span>Projection uncertainty</span><strong>${uncertainty == null ? "—" : `±${points(uncertainty)}`}</strong></div><div><span>Squad rules</span><strong>${capitalize(rating.squad_rule_health.state)}</strong></div><p>${releaseGate} ${rating.explanation}</p>`;
  }

  const totals = new Map();
  const fixturesByPlayer = new Map();
  const uncertaintyByPlayer = new Map();
  horizonLineups.forEach((lineup) => {
    [...lineup.starters, ...lineup.bench].forEach((player) => {
      totals.set(player.fpl_id, (totals.get(player.fpl_id) || 0) + Number(player.xpts || 0));
      const byGameweek = fixturesByPlayer.get(player.fpl_id) || new Map();
      byGameweek.set(lineup.gameweek, player.fixtures || []);
      fixturesByPlayer.set(player.fpl_id, byGameweek);
      if (player.uncertainty != null) {
        const values = uncertaintyByPlayer.get(player.fpl_id) || [];
        uncertaintyByPlayer.set(player.fpl_id, [...values, player.uncertainty]);
      }
    });
  });
  const rows = state.plan.squad.map(playerById).filter(Boolean).sort((a, b) => (totals.get(b.fpl_id) || 0) - (totals.get(a.fpl_id) || 0));
  const tickerGameweeks = horizonLineups.map((lineup) => lineup.gameweek);
  const ticker = (player) => `<div class="fixture-ticker">${tickerGameweeks.map((gameweek) => {
    const fixtures = fixturesByPlayer.get(player.fpl_id)?.get(gameweek) || [];
    if (fixtures.length === 0) return `<span class="blank" title="GW${gameweek}: no fixture">—</span>`;
    const label = fixtures.map((row) => `${row.opponent} ${row.is_home ? "H" : "A"}`).join(" + ");
    return `<span class="${fixtures.every((row) => row.is_home) ? "home" : ""}" title="GW${gameweek}: ${label}">${label}</span>`;
  }).join("")}</div>`;
  $("#player-outlook").innerHTML = `<div class="table-head"><span>Player</span><div class="fixture-ticker">${tickerGameweeks.map((gameweek) => `<span>GW${gameweek}</span>`).join("")}</div><span>Uncertainty</span><span>${state.plan.horizon_length}-GW xPts</span></div>${rows.map((player) => {
    const uncertaintyValues = uncertaintyByPlayer.get(player.fpl_id);
    const confidence = uncertaintyValues && uncertaintyValues.length
      ? `±${points(uncertaintyValues.reduce((a, b) => a + b, 0) / uncertaintyValues.length)}`
      : "—";
    return `<div class="table-row outlook-row"><strong>${player.name}<small>${player.team} · ${player.position}</small></strong>${ticker(player)}<span>${confidence}</span><span>${points(totals.get(player.fpl_id))}</span></div>`;
  }).join("")}`;
}

function renderTransferPaths() {
  const container = $("#transfer-paths");
  const paths = state.transfers?.paths || [];
  const recommendedPathId = state.transfers?.recommended_path_id;
  if (paths.length === 0) {
    container.innerHTML = "";
    return;
  }
  const gameweek = state.transfers?.horizon?.[0];
  container.innerHTML = `<h2 class="section-label">Your options${gameweek ? ` for GW${gameweek}` : ""}</h2><div class="path-list">${paths.map((path) => {
    const moves = path.transfers.map((row) => `${row.out_name} <i>→</i> ${row.in_name}`).join(" · ");
    const detail = moves || path.note || "No transfer this Gameweek.";
    const isRecommended = path.id === recommendedPathId;
    return `<article class="transfer-path ${isRecommended ? "recommended" : ""}" data-path-id="${path.id}">
      <div><span>${isRecommended ? "Recommended" : "Option"}</span><strong>${path.label}</strong><small>${detail}</small></div>
      <div><span>Net xPts</span><strong>${points(path.net_xpts)}</strong><small class="${path.delta_xpts_vs_hold >= 0 ? "positive" : "negative"}">${signed(path.delta_xpts_vs_hold)} vs hold${path.hit ? ` · includes −${path.hit} hit` : ""}</small></div>
      ${path.transfers.length ? `<button type="button" class="button ${isRecommended ? "primary" : "secondary"} small" data-stage-path="${path.id}">Stage this</button>` : ""}
    </article>`;
  }).join("")}</div>`;
  container.querySelectorAll("[data-stage-path]").forEach((button) => button.addEventListener("click", () => {
    stagePath(paths.find((path) => path.id === button.dataset.stagePath));
  }));
}

function renderTransfers() {
  const container = $("#transfer-results");
  if (!state.transfers) return;
  renderPendingTransfers();
  renderTransferPaths();
  const profile = RISK_PROFILES[state.plan.risk_profile] || RISK_PROFILES.balanced;
  const suggestions = state.transfers.suggestions.filter((row) => row.net_xpts_gain >= profile.threshold);
  const chip = CHIPS[state.transfers.chip];
  const chipMode = chip
    ? `<div class="transfer-mode chip">Scored with ${chip.label} played in GW${state.transfers.horizon[0]}. ${chip.changesTransfers ? "Every move is free and saved free transfers are kept." : chip.note}</div>`
    : "";
  container.className = "transfer-list";
  container.innerHTML = `<h2 class="section-label">Best single moves</h2>${chipMode}<div class="transfer-mode profile">${profile.label} stance: ${profile.note} <button type="button" class="text-button" data-goto="settings">Change</button></div>${suggestions.length === 0 ? `<div class="empty-state">No single moves pass the ${profile.label.toLowerCase()} stance. The options above still compare holding and rolling.</div>` : suggestions.map((row, index) => `<article class="transfer-card">
    <div class="transfer-move"><span>${index === 0 ? "Best move" : `Alternative ${index}`}</span><strong>${row.out.name} <i>→</i> ${row.in.name}</strong><small>${row.out.position} · ${money(row.remaining_bank_tenths)} left in the bank</small></div>
    <div><span>Net gain</span><strong class="${row.net_xpts_gain >= 0 ? "positive" : "negative"}">${signed(row.net_xpts_gain)}</strong><small>${row.hit_cost ? `includes −${row.hit_cost} hit` : "no hit"}</small></div>
    <button type="button" class="button ${index === 0 ? "primary" : "secondary"} small" data-apply-out="${row.out.fpl_id}" data-apply-in="${row.in.fpl_id}" data-lineup-changed="${row.lineup_changed ? "true" : "false"}">Stage move</button>
  </article>`).join("")}`;
  container.querySelectorAll("[data-apply-out]").forEach((button) => button.addEventListener("click", () => {
    const row = state.transfers.suggestions.find((candidate) => candidate.out.fpl_id === Number(button.dataset.applyOut) && candidate.in.fpl_id === Number(button.dataset.applyIn));
    stageTransfer(row);
  }));
  bindGotoButtons(container);
}

function parseEntryReference(raw) {
  const value = String(raw || "").trim();
  if (/^\d+$/.test(value)) return { entryId: Number(value), gameweek: null };
  const match = value.match(/(?:entry|team)\/(\d+)/i) || value.match(/\b\d{4,}\b/);
  if (!match) return null;
  const gameweekMatch = value.match(/(?:event|gw|gameweek)[\/_-]?(\d+)/i);
  const gameweek = gameweekMatch ? Number(gameweekMatch[1]) : null;
  return { entryId: Number(match[1] || match[0]), gameweek: Number.isInteger(gameweek) && gameweek >= 1 && gameweek <= 38 ? gameweek : null };
}

async function loadFromTeamId({ input = $("#team-id"), button = $("#load-team-id") } = {}) {
  const reference = parseEntryReference(input.value);
  const entryId = reference?.entryId;
  if (!Number.isInteger(entryId) || entryId <= 0) {
    showError("Enter a valid FPL Team ID (numbers only) or paste your fantasy.premierleague.com/entry/… link. FPL does not allow searching by team name.");
    return false;
  }
  showError("");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    const params = new URLSearchParams({ include_profile: "true" });
    if (reference.gameweek) params.set("gameweek", String(reference.gameweek));
    const resolved = await api(`/api/squad/from-entry/${entryId}?${params.toString()}`);
    state.plan.squad = resolved.fpl_ids;
    state.plan.selling_prices = resolved.selling_prices;
    state.plan.selling_price_is_estimated = resolved.selling_price_is_estimated;
    const horizonStart = Number(state.bootstrap.release.model_runs[0].gameweek);
    const setupMatchesHorizon = resolved.gameweek === horizonStart;
    state.plan.current_setup = setupMatchesHorizon ? {
      gameweek: resolved.gameweek,
      starter_fpl_ids: resolved.starter_fpl_ids,
      bench_fpl_ids: resolved.bench_fpl_ids,
      captain_fpl_id: resolved.captain_fpl_id,
      vice_captain_fpl_id: resolved.vice_captain_fpl_id,
    } : null;
    state.plan.pending_transfers = [];
    state.plan.bank_tenths = resolved.bank_tenths;
    state.plan.squad_source = "team";
    state.teamProfile = resolved.entry || { id: entryId, name: null };
    state.teamNote = setupMatchesHorizon
      ? `GW${resolved.gameweek} picks loaded`
      : `GW${resolved.gameweek} picks loaded; projections start at GW${horizonStart}`;
    localStorage.setItem("touchline-team-profile", JSON.stringify(state.teamProfile));
    persistPlan();
    $("#bank").value = (state.plan.bank_tenths / 10).toFixed(1);
    $("#team-id").value = "";
    renderTeamHeader();
    renderSquadEditor();
    $("#team-id-note").textContent = setupMatchesHorizon
      ? "Selling prices are estimated from current prices. Adjust the bank if your FPL app shows a different value."
      : `Your squad is from GW${resolved.gameweek} but the projections start at GW${horizonStart}, so there's no comparison with your submitted XI yet.`;
    renderConnectState();
    navigateTo("weekly");
    resetTransfersView("New squad loaded — find the best transfers for it.");
    await runLineups();
    return true;
  } catch (error) {
    showError(error.message);
    return false;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function useDemoSquad() {
  state.plan.squad_source = "demo";
  persistPlan();
  renderTeamHeader();
  renderConnectState();
  navigateTo("weekly");
  await runLineups();
}

function needsTeam() {
  return state.plan.squad_source === null && !state.teamProfile;
}

function renderConnectState() {
  const required = needsTeam();
  document.body.classList.toggle("needs-team", required);
  $("#connect-view").hidden = !required;
  if (required) window.setTimeout(() => $("#connect-team-id").focus(), 0);
}

async function runLineups() {
  showError("");
  $("#refresh-lineup").disabled = true;
  try {
    state.lineups = await api("/api/recommend/lineups", { method: "POST", body: JSON.stringify(requestBody()) });
    localStorage.setItem("touchline-last-squad-rating", JSON.stringify(state.lineups.squad_rating));
    renderWeekly();
    renderOutlook();
    renderPendingTransfers();
    renderDecisionReceipts();
  } catch (error) {
    showError(error.message);
  } finally {
    $("#refresh-lineup").disabled = false;
  }
}

async function runTransfers() {
  const button = $("#run-transfers");
  button.disabled = true;
  button.textContent = "Scanning…";
  showError("");
  try {
    state.transfers = await api("/api/recommend/transfers?top_n=8", { method: "POST", body: JSON.stringify(requestBody()) });
    renderTransfers();
    renderDecisionReceipts();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Find my best transfers";
  }
}

function navigateTo(viewName, { updateHash = true } = {}) {
  const view = VIEWS[viewName] ? viewName : "weekly";
  $$(".nav-item").forEach((item) => {
    const active = item.dataset.view === view;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $$(".view").forEach((section) => section.classList.toggle("active", section.id === `${view}-view`));
  document.title = `${VIEWS[view]} — Touchline`;
  if (updateHash) {
    const hash = view === "weekly" ? "" : `#${view}`;
    if (window.location.hash !== hash) window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${hash}`);
  }
  renderPlanningControls();
}

function bindGotoButtons(root = document) {
  root.querySelectorAll("[data-goto]").forEach((button) => button.addEventListener("click", () => {
    navigateTo(button.dataset.goto);
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (button.dataset.autorun === "transfers" && !state.transfers) runTransfers();
  }));
}

function bindNavigation() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigateTo(button.dataset.view)));
  bindGotoButtons();
  window.addEventListener("hashchange", () => navigateTo(window.location.hash.slice(1), { updateHash: false }));
}

async function loadWorkspace() {
  try {
    state.bootstrap = await api("/api/bootstrap");
    hideAccessGate();
    if (state.plan.squad.some((id) => !playerById(id))) {
      state.plan.squad = DEFAULT_SQUAD;
      state.plan.selling_prices = {};
      state.plan.selling_price_is_estimated = false;
      state.plan.pending_transfers = [];
      clearCurrentSetup();
      persistPlan();
      if (state.plan.squad_source !== null) showError("Your saved squad no longer matches the current projections, so the demo squad is shown. Load your Team ID again to update it.");
    }
    // Selling prices saved in this browser were estimated from an earlier
    // release's market prices. After a refresh a price can fall below the saved
    // value, which the server rightly rejects, so drop them and fall back to
    // the current prices instead of breaking every request.
    const releaseId = state.bootstrap.release.release_id;
    let notice = "";
    if (state.plan.release_id !== releaseId && Object.keys(state.plan.selling_prices || {}).length) {
      state.plan.selling_prices = {};
      state.plan.selling_price_is_estimated = true;
      if (state.plan.squad_source !== null) notice = "Projections were updated, so your saved selling prices were reset to current prices. Load your Team ID again to refresh them.";
    }
    state.plan.release_id = releaseId;
    persistPlan();
    const firstGameweek = state.bootstrap.release.model_runs[0].gameweek;
    const setupShapeIsValid = state.plan.current_setup
      && Array.isArray(state.plan.current_setup.starter_fpl_ids)
      && Array.isArray(state.plan.current_setup.bench_fpl_ids);
    const setupIds = setupShapeIsValid
      ? [...state.plan.current_setup.starter_fpl_ids, ...state.plan.current_setup.bench_fpl_ids]
      : [];
    if (state.plan.current_setup && (
      !setupShapeIsValid
      || state.plan.current_setup.gameweek !== firstGameweek
      || setupIds.length !== 15
      || setupIds.some((id) => !state.plan.squad.includes(id))
    )) clearCurrentSetup();
    $("#bank").value = (state.plan.bank_tenths / 10).toFixed(1);
    $("#free-transfers").value = String(state.plan.free_transfers);
    renderRelease();
    renderSquadEditor();
    navigateTo(window.location.hash.slice(1), { updateHash: false });

    // A Team ID or demo request handed over from the landing page.
    const entry = state.pendingEntry;
    state.pendingEntry = null;
    if (entry?.team) {
      $("#connect-team-id").value = entry.team;
      $("#team-id").value = entry.team;
      if (await loadFromTeamId()) return;
    } else if (entry?.demo) {
      state.plan.squad_source = "demo";
      persistPlan();
      renderTeamHeader();
    }
    renderConnectState();
    if (!needsTeam()) await runLineups();
    // runLineups clears the banner when it starts, so show the notice after it.
    if (notice && $("#error-banner").hidden) showError(notice);
  } catch (error) {
    if (error.code !== "alpha_access_required") showError(error.message);
  }
}

async function loadPublicConfig() {
  try {
    state.publicConfig = await api("/api/public-config");
    renderPublicConfig();
  } catch (error) {
    showError(`Operator/support configuration unavailable: ${error.message}`);
  }
}

async function submitAccessCode(event) {
  event.preventDefault();
  const code = $("#access-code").value.trim();
  if (code.length < 16) {
    $("#access-message").textContent = "The access code must contain at least 16 characters.";
    return;
  }
  const button = $("#access-submit");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Checking…";
  state.alphaToken = code;
  sessionStorage.setItem("touchline-alpha-token", code);
  try {
    await loadWorkspace();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function runWildcardComparison() {
  const button = $("#run-wildcard");
  const output = $("#wildcard-results");
  button.disabled = true;
  output.textContent = "Comparing legal squads and transfer paths…";
  try {
    const payload = await api("/api/recommend/wildcard", { method: "POST", body: JSON.stringify({
      ...requestBody(), chip: null, horizon_length: state.plan.horizon_length,
      roll_after_wildcard: Math.min(wildcardRollLimit(), Math.max(0, Math.floor(Number($("#wc-roll").value) || 0))),
      terminal_ft_value: Number($("#wc-ft-value").value),
    }) });
    output.replaceChildren();
    for (const path of payload.paths) {
      const card = document.createElement("section");
      card.className = "wildcard-path";
      const title = document.createElement("h4");
      title.textContent = `${path.name}: ${points(path.net_xpts)} net xPts · ${path.terminal_free_transfers} free transfer${path.terminal_free_transfers === 1 ? "" : "s"} left at the end`;
      card.append(title);
      const assumptions = document.createElement("p");
      assumptions.textContent = `Score including the value you gave saved free transfers: ${points(path.objective)}.`;
      card.append(assumptions);
      if (path.squad_fpl_ids) {
        const squad = document.createElement("p");
        squad.textContent = `Squad: ${path.squad_fpl_ids.map((id) => playerById(id)?.name || `#${id}`).join(", ")}`;
        card.append(squad);
      }
      for (const step of path.steps) {
        const row = document.createElement("div");
        row.className = "wildcard-step";
        const action = document.createElement("strong");
        const moves = step.transfers.map((move) => `${playerById(move.out)?.name || move.out} → ${playerById(move.in)?.name || move.in}`).join("; ");
        action.textContent = `GW${step.gameweek}: ${step.action}${moves ? ` — ${moves}` : ""}`;
        const detail = document.createElement("small");
        detail.textContent = `${points(step.net_xpts)} net xPts · free transfers ${step.free_transfers_before} → ${step.free_transfers_after} · hit ${step.hit_cost} · bank ${money(step.bank_tenths)} · captain ${playerById(step.captain_fpl_id)?.name || step.captain_fpl_id}`;
        row.append(action, detail);
        card.append(row);
      }
      output.append(card);
    }
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = payload.limitations.join(" ");
    output.append(note);
  } catch (error) {
    output.textContent = error.message;
  } finally {
    renderWildcardControls();
  }
}

async function init() {
  bindNavigation();
  $("#refresh-lineup").addEventListener("click", runLineups);
  $("#run-transfers").addEventListener("click", runTransfers);
  $("#run-wildcard").addEventListener("click", runWildcardComparison);
  $("#load-team-id").addEventListener("click", () => loadFromTeamId());
  $("#team-id").addEventListener("keydown", (event) => { if (event.key === "Enter") loadFromTeamId(); });
  $("#connect-form").addEventListener("submit", (event) => {
    event.preventDefault();
    loadFromTeamId({ input: $("#connect-team-id"), button: $("#connect-submit") });
  });
  $("#connect-demo").addEventListener("click", useDemoSquad);
  $("#bank").addEventListener("change", () => {
    state.plan.bank_tenths = Math.round(Number($("#bank").value || 0) * 10);
    persistPlan();
    resetTransfersView("Bank changed — scan again to compare from the updated plan.");
    runLineups();
  });
  $("#free-transfers").addEventListener("change", () => {
    state.plan.free_transfers = Number($("#free-transfers").value);
    persistPlan();
    resetTransfersView("Free transfers changed — scan again to compare from the updated plan.");
    runLineups();
  });
  $("#horizon-select").addEventListener("change", (event) => {
    state.plan.horizon_length = Number(event.target.value);
    persistPlan();
    renderPlanningControls();
    renderOutlook();
  });
  $("#wc-roll").addEventListener("change", renderWildcardControls);
  $("#access-form").addEventListener("submit", submitAccessCode);
  await loadPublicConfig();
  await loadWorkspace();
}

init();
