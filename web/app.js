const DEFAULT_SQUAD = [
  1, 497,
  4, 418, 362, 259, 88,
  367, 397, 426, 557, 427,
  194, 379, 165,
];

const state = {
  bootstrap: null,
  selected: JSON.parse(localStorage.getItem("touchline-squad") || "null") || DEFAULT_SQUAD,
  sellingPrices: JSON.parse(localStorage.getItem("touchline-selling-prices") || "null") || {},
  sellingPriceIsEstimated: JSON.parse(localStorage.getItem("touchline-selling-estimated") || "false"),
  currentSetup: JSON.parse(localStorage.getItem("touchline-current-setup") || "null"),
  // Reviewed role-scenario overrides: {fpl_id, gameweek, xpts}[]. Applied
  // entirely client-side per request -- never persisted server-side or
  // written back to the release, and NOT saved to localStorage (a scenario
  // is deliberately a this-session-only what-if, not private squad state).
  roleScenarioOverrides: [],
  lineups: null,
  transfers: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const money = (tenths) => `£${(tenths / 10).toFixed(1)}`;
const points = (value) => Number(value || 0).toFixed(2);

function requestBody() {
  return {
    fpl_ids: state.selected,
    bank_tenths: Math.round(Number($("#bank").value || 0) * 10),
    free_transfers: Number($("#free-transfers").value),
    selling_prices: state.sellingPrices,
    role_scenario_overrides: state.roleScenarioOverrides,
    current_setup: state.currentSetup,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function showError(message) {
  const banner = $("#error-banner");
  banner.textContent = message;
  banner.hidden = !message;
}

function playerById(id) {
  return state.bootstrap.players.find((player) => player.fpl_id === Number(id));
}

function renderRelease() {
  const release = state.bootstrap.release;
  $("#release-label").textContent = release.label;
  $("#release-meta").textContent = `${release.model_version} · ${new Date(release.planning_as_of).toLocaleString()}`;
  const gws = release.model_runs.map((row) => row.gameweek);
  $("#horizon-label").textContent = `GW${gws[0]}–GW${gws.at(-1)}`;

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
  const selectedPlayers = state.selected.map(playerById).filter(Boolean);
  const cost = selectedPlayers.reduce((sum, player) => sum + player.price_tenths, 0);
  $("#squad-cost").textContent = `${money(cost)} squad`;
  $("#team-id-note").textContent = state.sellingPriceIsEstimated
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
      if (state.selected.includes(newId)) {
        showError("That player is already in the squad.");
        event.target.value = String(oldId);
        return;
      }
      state.selected = state.selected.map((id) => id === oldId ? newId : id);
      clearCurrentSetup();
      localStorage.setItem("touchline-squad", JSON.stringify(state.selected));
      renderSquadEditor();
      await runLineups();
    });
  });
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
  state.currentSetup = null;
  localStorage.removeItem("touchline-current-setup");
}

function resetTransfersView() {
  state.transfers = null;
  const container = $("#transfer-results");
  container.className = "transfer-list empty-state";
  container.textContent = "Squad or scenario changed -- run the scan again to compare against the reviewed scenario.";
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
    container.innerHTML = `<div><span>No-chip changes vs your current setup</span><strong>Load your Team ID to compare</strong></div><p>The model needs your submitted XI, captain, vice-captain, and bench order — a squad list alone is not enough.</p>`;
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
  const rating = state.lineups.squad_rating;
  const overallRating = rating?.available ? rating.model_strength.overall_3gw : null;
  $("#outlook-total").classList.remove("skeleton");
  $("#outlook-total").innerHTML = `<div><span>Projected horizon score</span><strong>${points(state.lineups.cumulative_xpts)}</strong><small>captaincy included · cumulative raw xPts</small></div>${overallRating ? `<div class="rating-score"><span>${rating.display_label}</span><strong>${Math.round(overallRating.percentile)}</strong><small>percentile vs ${rating.benchmark.population_size} legal same-budget squads</small></div>` : `<div class="rating-score unavailable"><span>${rating?.display_label || "Model Preview"}</span><strong>—</strong><small>benchmark unavailable; raw xPts is still valid</small></div>`}`;
  const perGameweekRating = new Map(
    rating?.available
      ? rating.model_strength.per_gameweek.map((row) => [row.gameweek, row.percentile])
      : []
  );
  $("#outlook-grid").innerHTML = state.lineups.lineups.map((lineup) => {
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
  state.lineups.lineups.forEach((lineup) => {
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
  const rows = state.selected.map(playerById).filter(Boolean).sort((a, b) => (totals.get(b.fpl_id) || 0) - (totals.get(a.fpl_id) || 0));
  $("#player-outlook").innerHTML = `<div class="table-head"><span>Player</span><span>Fixtures</span><span>Confidence</span><span>3-GW xPts</span></div>${rows.map((player) => {
    const uncertaintyValues = uncertaintyByPlayer.get(player.fpl_id);
    const confidence = uncertaintyValues && uncertaintyValues.length
      ? `±${points(uncertaintyValues.reduce((a, b) => a + b, 0) / uncertaintyValues.length)}`
      : "—";
    return `<div class="table-row outlook-row"><strong>${player.name}<small>${player.team}</small></strong><span class="small">${(fixturesByPlayer.get(player.fpl_id) || []).join(" · ")}</span><span>${confidence}</span><span>${points(totals.get(player.fpl_id))}</span></div>`;
  }).join("")}`;
}

function renderTransfers() {
  const container = $("#transfer-results");
  if (!state.transfers) return;
  const suggestions = state.transfers.suggestions;
  // hit_cost is uniform across one scan -- it comes entirely from the
  // manager's current free-transfer count, not per-suggestion (this app
  // does not evaluate "wait a Gameweek for a free transfer" as an
  // alternative). A hit scenario is therefore an explicit, whole-scan mode,
  // not something to sort per-card.
  const isHitScenario = suggestions.length > 0 && suggestions[0].hit_cost > 0;
  const baselineRating = state.transfers.baseline_squad_rating;
  const baselinePercentile = baselineRating?.available
    ? baselineRating.model_strength.overall_3gw.percentile
    : null;
  const modeBanner = suggestions.length === 0 ? "" : isHitScenario
    ? `<div class="transfer-mode hit">Hit scenario: no free transfer is available, so every suggested move below costs a ${suggestions[0].hit_cost}-point hit. Net gain already accounts for it.</div>`
    : `<div class="transfer-mode free">Free transfer available: every suggested move below costs no hit.</div>`;
  container.className = "transfer-list";
  container.innerHTML = `${modeBanner}<article class="transfer-card hold ${state.transfers.recommendation === "hold" ? "recommended" : ""}"><div><span>Baseline</span><strong>Hold transfer</strong><small>${baselinePercentile == null ? "rating withheld" : `${Math.round(baselinePercentile)}th percentile · ${baselineRating.display_label}`}</small></div><div><span>3-GW xPts</span><strong>${points(state.transfers.baseline_cumulative_xpts)}</strong></div></article>${suggestions.map((row, index) => `<article class="transfer-card ${index === 0 && state.transfers.recommendation === "transfer" ? "recommended" : ""}">
    <div class="transfer-move"><span>${index === 0 ? "Best retained move" : `Alternative ${index + 1}`}</span><strong>${row.out.name} <i>→</i> ${row.in.name}</strong><small>${row.out.position} · bank ${money(row.remaining_bank_tenths)}</small></div>
    <div><span>Net gain</span><strong class="${row.net_xpts_gain >= 0 ? "positive" : "negative"}">${row.net_xpts_gain >= 0 ? "+" : ""}${points(row.net_xpts_gain)}</strong><small>${row.hit_cost ? `includes −${row.hit_cost} hit` : "no hit"}</small></div>
  </article>`).join("")}`;
}

async function loadFromTeamId() {
  const input = $("#team-id");
  const entryId = Number(input.value);
  const button = $("#load-team-id");
  if (!Number.isInteger(entryId) || entryId <= 0) {
    showError("Enter a valid FPL Team ID.");
    return;
  }
  showError("");
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    const resolved = await api(`/api/squad/from-entry/${entryId}`);
    state.selected = resolved.fpl_ids;
    state.sellingPrices = resolved.selling_prices;
    state.sellingPriceIsEstimated = resolved.selling_price_is_estimated;
    state.currentSetup = {
      gameweek: resolved.gameweek,
      starter_fpl_ids: resolved.starter_fpl_ids,
      bench_fpl_ids: resolved.bench_fpl_ids,
      captain_fpl_id: resolved.captain_fpl_id,
      vice_captain_fpl_id: resolved.vice_captain_fpl_id,
    };
    localStorage.setItem("touchline-squad", JSON.stringify(state.selected));
    localStorage.setItem("touchline-selling-prices", JSON.stringify(state.sellingPrices));
    localStorage.setItem("touchline-selling-estimated", JSON.stringify(state.sellingPriceIsEstimated));
    localStorage.setItem("touchline-current-setup", JSON.stringify(state.currentSetup));
    $("#bank").value = (resolved.bank_tenths / 10).toFixed(1);
    renderSquadEditor();
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
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Scan all single transfers";
  }
}

function bindNavigation() {
  const titles = { weekly: "Weekly squad scenario", outlook: "Three-Gameweek outlook", transfers: "Transfer recommendations" };
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => {
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
    $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${button.dataset.view}-view`));
    $("#page-title").textContent = titles[button.dataset.view];
  }));
}

async function init() {
  bindNavigation();
  $("#refresh-lineup").addEventListener("click", runLineups);
  $("#run-transfers").addEventListener("click", runTransfers);
  $("#load-team-id").addEventListener("click", loadFromTeamId);
  $("#team-id").addEventListener("keydown", (event) => { if (event.key === "Enter") loadFromTeamId(); });
  $("#bank").addEventListener("change", runLineups);
  $("#free-transfers").addEventListener("change", resetTransfersView);
  try {
    state.bootstrap = await api("/api/bootstrap");
    if (state.selected.some((id) => !playerById(id))) {
      state.selected = DEFAULT_SQUAD;
      state.sellingPrices = {};
      state.sellingPriceIsEstimated = false;
      clearCurrentSetup();
      showError("A previously loaded squad no longer matches the current release; showing the default squad.");
    }
    const firstGameweek = state.bootstrap.release.model_runs[0].gameweek;
    const setupShapeIsValid = state.currentSetup
      && Array.isArray(state.currentSetup.starter_fpl_ids)
      && Array.isArray(state.currentSetup.bench_fpl_ids);
    const setupIds = setupShapeIsValid
      ? [...state.currentSetup.starter_fpl_ids, ...state.currentSetup.bench_fpl_ids]
      : [];
    if (state.currentSetup && (
      !setupShapeIsValid
      || state.currentSetup.gameweek !== firstGameweek
      || setupIds.length !== 15
      || setupIds.some((id) => !state.selected.includes(id))
    )) clearCurrentSetup();
    renderRelease();
    renderSquadEditor();
    await runLineups();
  } catch (error) {
    showError(error.message);
  }
}

init();
