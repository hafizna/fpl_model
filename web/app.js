const DEFAULT_SQUAD = [
  1, 497,
  4, 418, 362, 259, 88,
  367, 397, 426, 557, 427,
  194, 379, 165,
];

function readStoredJson(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "null");
    return value == null ? fallback : value;
  } catch {
    return fallback;
  }
}

function loadPlan() {
  const stored = readStoredJson("touchline-plan", null);
  const legacy = {
    squad: readStoredJson("touchline-squad", DEFAULT_SQUAD),
    selling_prices: readStoredJson("touchline-selling-prices", {}),
    selling_price_is_estimated: readStoredJson("touchline-selling-estimated", false),
    current_setup: readStoredJson("touchline-current-setup", null),
    horizon_length: Math.max(1, Math.min(5, Number(localStorage.getItem("touchline-horizon") || 3) || 3)),
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
    horizon_length: Math.max(1, Math.min(5, Number(source.horizon_length || legacy.horizon_length) || 3)),
    risk_profile: source.risk_profile || legacy.risk_profile,
  };
  localStorage.setItem("touchline-plan", JSON.stringify(plan));
  return plan;
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
  teamProfile: JSON.parse(localStorage.getItem("touchline-team-profile") || "null"),
  alphaToken: sessionStorage.getItem("touchline-alpha-token"),
  publicConfig: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const money = (tenths) => `£${(tenths / 10).toFixed(1)}`;
const points = (value) => Number(value || 0).toFixed(2);

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
    ["Weekly + outlook", state.lineups?.decision_receipt],
    ["Transfers", state.transfers?.decision_receipt],
  ].filter((row) => row[1]);
  if (!receipts.length) {
    bar.hidden = true;
    bar.innerHTML = "";
    return;
  }
  bar.hidden = false;
  bar.innerHTML = `<strong>Decision receipts</strong>${receipts.map(([label, receipt]) => `<button class="button secondary" data-receipt-id="${receipt.decision_id}">${label} · ${receipt.decision_id.slice(-8)}</button>`).join("")}`;
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

const RISK_PROFILES = {
  conservative: { label: "Conservative", note: "Prefer clearer upside and fewer speculative moves.", threshold: 2 },
  balanced: { label: "Balanced", note: "Keep the model ranking intact and compare the full shortlist.", threshold: 0 },
  aggressive: { label: "Aggressive", note: "Surface differentials, including lower-confidence upside.", threshold: -2 },
};

function publishedGameweeks() {
  return state.bootstrap?.release?.model_runs?.map((row) => Number(row.gameweek)) || [];
}

function visibleLineups() {
  const lineups = state.lineups?.lineups || [];
  return lineups.slice(0, Math.max(1, Math.min(state.plan.horizon_length, lineups.length)));
}

function renderPlanningControls() {
  if (!RISK_PROFILES[state.plan.risk_profile]) state.plan.risk_profile = "balanced";
  const gameweeks = publishedGameweeks();
  const select = $("#horizon-select");
  if (!select) return;
  const available = Math.min(5, gameweeks.length);
  select.innerHTML = Array.from({ length: 5 }, (_, index) => {
    const length = index + 1;
    const enabled = length <= available;
    return `<option value="${length}" ${length === state.plan.horizon_length ? "selected" : ""} ${enabled ? "" : "disabled"}>${length} GW${length === 1 ? "" : "s"}${enabled ? "" : " — not published"}</option>`;
  }).join("");
  if (state.plan.horizon_length > available) state.plan.horizon_length = available || 1;
  select.value = String(state.plan.horizon_length);
  const first = gameweeks[0];
  const last = gameweeks[Math.min(state.plan.horizon_length, gameweeks.length) - 1];
  $("#horizon-note").textContent = gameweeks.length
    ? `Published now: GW${first}–GW${gameweeks.at(-1)}. Showing ${state.plan.horizon_length} GW${state.plan.horizon_length === 1 ? "" : "s"} (GW${first}–GW${last}).`
    : "No published Gameweek horizon is available yet.";
  $("#horizon-availability").textContent = gameweeks.length ? `${gameweeks.length} GW published` : "";
  const profiles = $("#risk-profiles");
  profiles.innerHTML = Object.entries(RISK_PROFILES).map(([key, profile]) => `<button type="button" class="risk-profile ${state.plan.risk_profile === key ? "selected" : ""}" data-risk-profile="${key}"><strong>${profile.label}</strong><small>${profile.note}</small></button>`).join("");
  $$("[data-risk-profile]").forEach((button) => button.addEventListener("click", () => {
    state.plan.risk_profile = button.dataset.riskProfile;
    persistPlan();
    renderPlanningControls();
    renderTransfers();
  }));
}

function renderSetupSummary() {
  const container = $("#setup-summary");
  if (!container) return;
  const teamName = state.teamProfile?.name;
  const squadCount = state.plan.squad.filter((id) => playerById(id)).length;
  container.innerHTML = `<div><span>Workspace</span><strong>${teamName || "Default squad"}</strong><small>${squadCount}/15 players loaded · ${state.plan.risk_profile} stance · ${state.plan.horizon_length} GW visible</small></div><button type="button" class="button secondary" id="setup-settings">Adjust settings</button>`;
  $("#setup-settings")?.addEventListener("click", () => navigateTo("settings"));
}

function renderRelease() {
  const release = state.bootstrap.release;
  $("#release-label").textContent = release.label;
  $("#release-meta").textContent = `${release.model_version} · ${new Date(release.planning_as_of).toLocaleString()}`;
  const gws = release.model_runs.map((row) => row.gameweek);
  $("#horizon-label").textContent = `GW${gws[0]}–GW${gws.at(-1)}`;
  renderPlanningControls();
  renderSetupSummary();

  const coverage = release.coverage;
  $("#release-coverage").textContent = coverage
    ? `${coverage.fully_covered_players}/${coverage.total_registered_players} players covered`
    : "";

  const freshness = release.freshness;
  if (freshness) {
    const finalCount = freshness.gameweeks.filter((row) => row.fpl_finality?.is_final).length;
    const summary = `${finalCount}/${freshness.gameweeks.length} GW final`;
    $("#release-freshness").textContent = freshness.passes ? summary : `${summary} · ${freshness.problems.length} freshness issue(s)`;
    $("#release-freshness").style.color = freshness.passes ? "" : "var(--red)";
  } else {
    $("#release-freshness").textContent = "";
  }
}

function renderSquadEditor() {
  const selectedPlayers = state.plan.squad.map(playerById).filter(Boolean);
  const cost = selectedPlayers.reduce((sum, player) => sum + player.price_tenths, 0);
  $("#squad-cost").textContent = `${money(cost)} squad`;
  $("#team-id-note").textContent = state.plan.selling_price_is_estimated
    ? "Loaded from your public Team ID. Selling prices are ESTIMATED from current market price, not FPL's own profit-sharing sale rule — check the FPL app for your exact sell value before making a real transfer."
    : "Loads your public squad by Team ID only — never a password or session cookie. Estimated selling prices (current market price, not FPL's own profit-sharing rule) are used until you adjust them below.";
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
      return `<div class="squad-player"><span class="team-chip">${player.team}</span><select data-player-id="${player.fpl_id}" aria-label="Replace ${player.name}">${options}</select><strong>${money(player.price_tenths)}</strong></div>`;
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
}

function playerCard(player, captainId, viceId) {
  const badge = player.fpl_id === captainId ? "C" : player.fpl_id === viceId ? "V" : "";
  return `<article class="player-card ${player.position.toLowerCase()}">
    ${badge ? `<span class="captain-badge">${badge}</span>` : ""}
    <span class="shirt">${player.team_id}</span>
    <strong>${player.name}</strong>
    <span>${player.position} · ${points(player.xpts)} xPts</span>
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
  const markup = pending.length === 0 ? "" : `<div class="pending-heading"><span>Staged transfers (${pending.length})</span><span>Not committed</span></div><div class="pending-list">${pending.map((row, index) => `<span class="pending-chip">${pendingTransferLabel(row)}<button type="button" data-remove-pending="${index}" aria-label="Remove staged transfer ${pendingTransferLabel(row)}">×</button></span>`).join("")}</div><div class="pending-actions"><small>Lineup and outlook already include these moves.</small><button type="button" class="button primary" data-commit-pending ${state.lineups?.plan_summary ? "" : "disabled"}>Commit to squad</button></div>`;
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
  resetTransfersView("Staged move removed — re-scan to compare from the updated plan.");
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
    `${row.out.name} is staged for ${row.in.name} — lineup and outlook are recalculating.`,
  );
}

async function stagePath(path) {
  if (!path?.transfers?.length) return;
  const moveNames = path.transfers.map((row) => `${row.out_name} → ${row.in_name}`).join(" · ");
  await stageTransfers(
    path.transfers,
    `${moveNames} staged from the ${path.label.toLowerCase()} path — lineup and outlook are recalculating.`,
  );
}

async function commitPendingTransfers() {
  const summary = state.lineups?.plan_summary;
  if (!summary || state.plan.pending_transfers.length === 0) return;
  state.plan.squad = summary.effective_fpl_ids;
  state.plan.bank_tenths = summary.effective_bank_tenths;
  state.plan.free_transfers = summary.effective_free_transfers;
  state.plan.selling_prices = summary.effective_selling_prices;
  state.plan.pending_transfers = [];
  state.plan.current_setup = null;
  localStorage.removeItem("touchline-current-setup");
  persistPlan();
  $("#bank").value = (state.plan.bank_tenths / 10).toFixed(1);
  $("#free-transfers").value = String(state.plan.free_transfers);
  renderSquadEditor();
  resetTransfersView("Transfer committed — re-scan to compare from the committed squad.");
  await runLineups();
}

function resetTransfersView(message = "Squad or scenario changed — run the scan again to compare against the reviewed scenario.") {
  state.transfers = null;
  renderDecisionReceipts();
  renderPendingTransfers();
  $("#transfer-paths").innerHTML = "";
  const container = $("#transfer-results");
  container.className = "transfer-list empty-state";
  container.innerHTML = `<div>${message}</div><button type="button" class="button secondary" id="rescan-transfers">Re-scan transfers</button>`;
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
    banner.innerHTML = `<strong>Reviewed scenario active.</strong> Assuming ${names} blanks. This does not change the underlying release -- it is a what-if, recomputed for this view only. <button type="button" id="clear-scenario" class="button secondary">Back to base release</button>`;
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
        `<button type="button" class="button secondary review-scenario" data-fpl-id="${row.fpl_id}" data-gameweek="${gameweek}">Review: if ${row.player_name} blanks</button>`
    )
    .join(" ");
  banner.hidden = false;
  banner.className = "sensitivity-banner";
  banner.innerHTML = `<strong>Sensitive recommendation.</strong> This lineup depends on ${players || "a rotation-risk player"}'s involvement -- if they blank, the starting XI or captain would change. ${buttons}`;
  $$(".review-scenario").forEach((button) =>
    button.addEventListener("click", () =>
      applyBlankScenario(Number(button.dataset.fplId), Number(button.dataset.gameweek))
    )
  );
}

function renderWeekly() {
  if (!state.lineups) return;
  const lineup = state.lineups.lineups[0];
  const weeklyPercentile = state.lineups.squad_rating?.available
    ? state.lineups.squad_rating.model_strength.per_gameweek.find((row) => row.gameweek === lineup.gameweek)?.percentile
    : null;
  $("#weekly-summary").classList.remove("skeleton");
  $("#weekly-summary").innerHTML = `
    <div><span>GW${lineup.gameweek} xPts${weeklyPercentile == null ? "" : ` · ${Math.round(weeklyPercentile)}th pct`}</span><strong>${points(lineup.total_xpts)}</strong></div>
    <div><span>Formation</span><strong>${lineup.formation}</strong></div>
    <div><span>Captain</span><strong>${lineup.captain.name}</strong></div>
    <div><span>Autosub EV</span><strong>${points(lineup.expected_autosub_value)}</strong></div>`;
  const planSummary = state.lineups.plan_summary || {
    gameweek: lineup.gameweek,
    formation: lineup.formation,
    captain: lineup.captain,
    staged_transfer_count: effectivePendingCount(),
    net_xpts_vs_holding: 0,
  };
  const planDelta = Number(planSummary.net_xpts_vs_holding || 0);
  $("#plan-header").innerHTML = `<span class="plan-title">Plan for GW${planSummary.gameweek}</span><div><span>Formation</span><strong>${planSummary.formation}</strong></div><div><span>Captain</span><strong>${planSummary.captain.name}</strong></div><div><span>Staged transfers</span><strong>${planSummary.staged_transfer_count}</strong></div><div class="plan-delta"><span>Net xPts vs holding</span><strong class="${planDelta >= 0 ? "positive" : "negative"}">${planDelta >= 0 ? "+" : ""}${points(planDelta)}</strong></div>`;
  renderSensitivityBanner(lineup.role_scenario_sensitivity, lineup.gameweek);
  renderMarginalChanges(lineup);
  const rows = ["GK", "DEF", "MID", "FWD"].map((position) => {
    const players = lineup.starters.filter((player) => player.position === position);
    return `<div class="pitch-line ${position.toLowerCase()}">${players.map((player) => playerCard(player, lineup.captain.fpl_id, lineup.vice_captain.fpl_id)).join("")}</div>`;
  });
  $("#pitch").classList.remove("skeleton");
  $("#pitch").innerHTML = rows.join("");
  $("#bench").innerHTML = `<div class="bench-title">Bench order</div>${lineup.bench.map((player, index) => `<div class="bench-player"><span>${index || "GK"}</span><strong>${player.name}</strong><small>${points(player.xpts)} xPts</small></div>`).join("")}`;
}

function renderMarginalChanges(lineup) {
  const container = $("#marginal-changes");
  const comparison = lineup.current_setup_comparison;
  if (!comparison) {
    container.className = "marginal-changes unavailable";
    const stagedNote = state.plan.current_setup && effectivePendingCount() > 0
      ? "Your submitted XI comparison stays anchored to the committed squad; staged moves are shown separately above."
      : "The model needs your submitted XI, captain, vice-captain, and bench order — a squad list alone is not enough.";
    container.innerHTML = `<div><span>No-chip changes vs your current setup</span><strong>${state.plan.current_setup && effectivePendingCount() > 0 ? "Committed setup retained" : "Load your Team ID to compare"}</strong></div><p>${stagedNote}</p>`;
    return;
  }

  const marginal = Number(comparison.marginal_xpts || 0);
  const changeRows = [];
  if (comparison.started.length > 0) {
    changeRows.push(`<li><strong>XI</strong><span>Start ${comparison.started.map((row) => row.name).join(", ")}; bench ${comparison.benched.map((row) => row.name).join(", ")}.</span><small>Best legal formation raises starting-XI score by ${comparison.starting_xpts_gain >= 0 ? "+" : ""}${points(comparison.starting_xpts_gain)} xPts.</small></li>`);
  }
  if (comparison.captain_change) {
    changeRows.push(`<li><strong>Captain</strong><span>${comparison.captain_change.from.name} → ${comparison.captain_change.to.name}</span><small>The higher projected captain adds ${comparison.captain_xpts_gain >= 0 ? "+" : ""}${points(comparison.captain_xpts_gain)} xPts.</small></li>`);
  }
  if (comparison.vice_captain_change) {
    changeRows.push(`<li><strong>Vice</strong><span>${comparison.vice_captain_change.from.name} → ${comparison.vice_captain_change.to.name}</span><small>Next-highest projected starter is the fallback if the captain does not play.</small></li>`);
  }
  if (comparison.bench_order_changed) {
    changeRows.push(`<li><strong>Bench</strong><span>${lineup.bench.map((row) => row.name).join(" → ")}</span><small>Goalkeeper first; outfield substitutes are ordered by projected points for autosubs.</small></li>`);
  }
  if (changeRows.length === 0) {
    changeRows.push("<li><strong>No changes</strong><span>Your submitted XI, captain, vice-captain, and bench order already match the model.</span></li>");
  }

  container.className = "marginal-changes available";
  container.innerHTML = `<div class="marginal-heading"><div><span>No-chip changes vs your current setup</span><strong>${points(comparison.current_total_xpts)} → ${points(comparison.recommended_total_xpts)} xPts</strong></div><b class="${marginal >= 0 ? "positive" : "negative"}">${marginal >= 0 ? "+" : ""}${points(marginal)} xPts</b></div><ul>${changeRows.join("")}</ul>`;
}

function fixtureLabel(player) {
  const fixtures = player.fixtures || [];
  if (fixtures.length === 0) return "no fixture";
  return fixtures.map((row) => `${row.is_home ? "" : "@"}${row.opponent}`).join(", ");
}

function renderOutlook() {
  if (!state.lineups) return;
  const horizonLineups = visibleLineups();
  const rating = state.lineups.squad_rating;
  const overallRating = rating?.available ? rating.model_strength.overall_3gw : null;
  $("#outlook-total").classList.remove("skeleton");
  const visibleTotal = horizonLineups.reduce((sum, row) => sum + Number(row.total_xpts || 0), 0);
  $("#outlook-total").innerHTML = `<div><span>Projected horizon score</span><strong>${points(visibleTotal)}</strong><small>captaincy included · ${state.plan.horizon_length}-GW visible slice</small></div>${overallRating ? `<div class="rating-score"><span>${rating.display_label}</span><strong>${Math.round(overallRating.percentile)}</strong><small>release benchmark (full published horizon)</small></div>` : `<div class="rating-score unavailable"><span>${rating?.display_label || "Model Preview"}</span><strong>—</strong><small>benchmark unavailable; raw xPts is still valid</small></div>`}`;
  if (overallRating) $("#outlook-total").insertAdjacentHTML("beforeend", `<small class="outlook-benchmark-note">percentile benchmark is for the full published horizon.</small>`);
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
      <p>${lineup.formation} · ${lineup.captain.name} (C) · ${fixtureLabel(lineup.captain)}</p>
      <p class="gw-percentile">${percentile == null ? "Rating withheld" : `${Math.round(percentile)}th percentile`}</p>
      <p class="bench-depth">Bench depth <b>${points(benchTotal)}</b> xPts</p>
    </article>`;
  }).join("");

  const detail = $("#outlook-rating-detail");
  detail.classList.remove("skeleton");
  if (!rating?.available) {
    detail.className = "rating-detail unavailable";
    detail.innerHTML = `<strong>Why there is no rating yet</strong><p>${rating?.explanation || "No benchmark contract was returned."}</p>`;
  } else {
    const uncertainty = rating.projection_uncertainty.cumulative_rss;
    const releaseGate = rating.release_gate.production_approved ? "Production approved" : `${rating.release_gate.health} release`;
    detail.className = "rating-detail available";
    detail.innerHTML = `<div><span>Model strength</span><strong>${Math.round(overallRating.percentile)}th percentile</strong></div><div><span>Data confidence</span><strong>${rating.data_confidence.state}</strong></div><div><span>Projection uncertainty</span><strong>${uncertainty == null ? "—" : `±${points(uncertainty)}`}</strong></div><div><span>Squad rules</span><strong>${rating.squad_rule_health.state}</strong></div><p>${releaseGate}. ${rating.explanation}</p>`;
  }

  const totals = new Map();
  const fixturesByPlayer = new Map();
  const uncertaintyByPlayer = new Map();
  horizonLineups.forEach((lineup) => {
    [...lineup.starters, ...lineup.bench].forEach((player) => {
      totals.set(player.fpl_id, (totals.get(player.fpl_id) || 0) + Number(player.xpts || 0));
      const existing = fixturesByPlayer.get(player.fpl_id) || [];
      fixturesByPlayer.set(player.fpl_id, [...existing, `GW${lineup.gameweek} ${fixtureLabel(player)}`]);
      if (player.uncertainty != null) {
        const values = uncertaintyByPlayer.get(player.fpl_id) || [];
        uncertaintyByPlayer.set(player.fpl_id, [...values, player.uncertainty]);
      }
    });
  });
  const rows = state.plan.squad.map(playerById).filter(Boolean).sort((a, b) => (totals.get(b.fpl_id) || 0) - (totals.get(a.fpl_id) || 0));
  $("#player-outlook").innerHTML = `<div class="table-head"><span>Player</span><span>Fixtures</span><span>Confidence</span><span>${state.plan.horizon_length}-GW xPts</span></div>${rows.map((player) => {
    const uncertaintyValues = uncertaintyByPlayer.get(player.fpl_id);
    const confidence = uncertaintyValues && uncertaintyValues.length
      ? `±${points(uncertaintyValues.reduce((a, b) => a + b, 0) / uncertaintyValues.length)}`
      : "—";
    return `<div class="table-row outlook-row"><strong>${player.name}<small>${player.team}</small></strong><span class="small">${(fixturesByPlayer.get(player.fpl_id) || []).join(" · ")}</span><span>${confidence}</span><span>${points(totals.get(player.fpl_id))}</span></div>`;
  }).join("")}`;
}

function renderTransferPaths() {
  const container = $("#transfer-paths");
  const paths = state.transfers?.paths || [];
  const recommendedPathId = state.transfers?.recommended_path_id;
  container.innerHTML = paths.map((path) => {
    const moves = path.transfers.map((row) => `${row.out_name} <i>→</i> ${row.in_name}`).join(" · ");
    const detail = moves || path.note || "No transfer this Gameweek.";
    const isRecommended = path.id === recommendedPathId;
    return `<article class="transfer-path ${isRecommended ? "recommended" : ""}" data-path-id="${path.id}">
      <div><span>${isRecommended ? "Recommended path" : "Path"}</span><strong>${path.label}</strong><small>${detail}</small></div>
      <div><span>Net xPts</span><strong>${points(path.net_xpts)}</strong><small class="${path.delta_xpts_vs_hold >= 0 ? "positive" : "negative"}">${path.delta_xpts_vs_hold >= 0 ? "+" : ""}${points(path.delta_xpts_vs_hold)} vs hold${path.hit ? ` · includes −${path.hit} hit` : ""}</small></div>
      ${path.transfers.length ? `<button type="button" class="button ${isRecommended ? "primary" : "secondary"}" data-stage-path="${path.id}">Stage this</button>` : ""}
    </article>`;
  }).join("");
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
  container.className = "transfer-list";
  container.innerHTML = `<div class="transfer-mode profile">${profile.label} stance: ${profile.note}</div>${suggestions.length === 0 ? `<div class="empty-state">No retained single-move alternatives for this stance. The path comparison above still shows Hold and Roll.</div>` : suggestions.map((row, index) => `<article class="transfer-card">
    <div class="transfer-move"><span>${index === 0 ? "Best retained move" : `Alternative ${index + 1}`}</span><strong>${row.out.name} <i>→</i> ${row.in.name}</strong><small>${row.out.position} · bank ${money(row.remaining_bank_tenths)}</small><button type="button" class="button secondary" data-apply-out="${row.out.fpl_id}" data-apply-in="${row.in.fpl_id}" data-lineup-changed="${row.lineup_changed ? "true" : "false"}">Apply move</button></div>
    <div><span>Net gain</span><strong class="${row.net_xpts_gain >= 0 ? "positive" : "negative"}">${row.net_xpts_gain >= 0 ? "+" : ""}${points(row.net_xpts_gain)}</strong><small>${row.hit_cost ? `includes −${row.hit_cost} hit` : "no hit"}</small></div>
  </article>`).join("")}`;
  container.querySelectorAll("[data-apply-out]").forEach((button) => button.addEventListener("click", () => {
    const row = state.transfers.suggestions.find((candidate) => candidate.out.fpl_id === Number(button.dataset.applyOut) && candidate.in.fpl_id === Number(button.dataset.applyIn));
    stageTransfer(row);
  }));
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

async function loadFromTeamId() {
  const input = $("#team-id");
  const reference = parseEntryReference(input.value);
  const entryId = reference?.entryId;
  const button = $("#load-team-id");
  if (!Number.isInteger(entryId) || entryId <= 0) {
    showError("Enter a valid numeric FPL Team ID or paste a public fantasy.premierleague.com/entry/<id> URL. Team-name search is not exposed by FPL's public API.");
    return;
  }
  showError("");
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
    state.teamProfile = resolved.entry || { id: entryId, name: null };
    localStorage.setItem("touchline-team-profile", JSON.stringify(state.teamProfile));
    persistPlan();
    $("#bank").value = (state.plan.bank_tenths / 10).toFixed(1);
    const teamSummary = $("#team-summary");
    teamSummary.hidden = false;
    teamSummary.innerHTML = `<strong>${state.teamProfile.name || `FPL Team ${entryId}`}</strong><small>Team ID ${entryId} · public picks loaded for GW${resolved.gameweek}${setupMatchesHorizon ? "" : ` · lineup comparison starts at GW${horizonStart}`}</small>`;
    renderSetupSummary();
    renderSquadEditor();
    $("#team-id-note").textContent = setupMatchesHorizon
      ? "Public squad loaded. Selling prices are estimated from current market prices."
      : `Public squad loaded from GW${resolved.gameweek}. The release starts at GW${horizonStart}, so the squad is available for planning but no submitted-XI comparison is shown yet.`;
    await runLineups();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Load squad";
  }
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
    button.textContent = "Scan all single transfers";
  }
}

function navigateTo(viewName) {
  const titles = { setup: "Workspace setup", weekly: "Lineup recommendation", outlook: "Planning outlook", transfers: "Transfer suggestions", settings: "Risk & horizon settings" };
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === viewName));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${viewName}-view`));
  $("#page-title").textContent = titles[viewName] || "FPL Decision Lab";
  renderPlanningControls();
  renderSetupSummary();
}

function bindNavigation() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigateTo(button.dataset.view)));
  $("#setup-next")?.addEventListener("click", () => navigateTo("weekly"));
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
      showError("A previously loaded squad no longer matches the current release; showing the default squad.");
    }
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
    if (state.teamProfile) {
      const teamSummary = $("#team-summary");
      teamSummary.hidden = false;
      teamSummary.innerHTML = `<strong>${state.teamProfile.name || "Saved FPL team"}</strong><small>Team ID ${state.teamProfile.id || "—"}</small>`;
    }
    renderSetupSummary();
    await runLineups();
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
  button.disabled = true;
  button.textContent = "Checkingâ€¦";
  state.alphaToken = code;
  sessionStorage.setItem("touchline-alpha-token", code);
  try {
    await loadWorkspace();
  } finally {
    button.disabled = false;
    button.textContent = "Open workspace";
  }
}

async function init() {
  bindNavigation();
  $("#refresh-lineup").addEventListener("click", runLineups);
  $("#run-transfers").addEventListener("click", runTransfers);
  $("#load-team-id").addEventListener("click", loadFromTeamId);
  $("#team-id").addEventListener("keydown", (event) => { if (event.key === "Enter") loadFromTeamId(); });
  $("#bank").addEventListener("change", () => {
    state.plan.bank_tenths = Math.round(Number($("#bank").value || 0) * 10);
    persistPlan();
    resetTransfersView("Bank changed — re-scan to compare from the updated plan.");
    runLineups();
  });
  $("#free-transfers").addEventListener("change", () => {
    state.plan.free_transfers = Number($("#free-transfers").value);
    persistPlan();
    resetTransfersView("Free-transfer count changed — re-scan to compare from the updated plan.");
    runLineups();
  });
  $("#horizon-select")?.addEventListener("change", (event) => {
    state.plan.horizon_length = Number(event.target.value);
    persistPlan();
    renderPlanningControls();
    renderOutlook();
    renderSetupSummary();
  });
  $("#access-form").addEventListener("submit", submitAccessCode);
  await loadPublicConfig();
  await loadWorkspace();
}

init();
