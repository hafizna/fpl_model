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

function renderSensitivityBanner(sensitivity) {
  const banner = $("#sensitivity-banner");
  if (!sensitivity || sensitivity.label !== "sensitive") {
    banner.hidden = true;
    banner.className = "";
    banner.innerHTML = "";
    return;
  }
  const players = sensitivity.scenarios_that_change_the_recommendation
    .map((row) => row.player_name)
    .join(", ");
  banner.hidden = false;
  banner.className = "sensitivity-banner";
  banner.innerHTML = `<strong>Sensitive recommendation.</strong> This lineup depends on ${players || "a rotation-risk player"}'s involvement -- if they blank, the starting XI or captain would change. Review before treating this as an unconditional best option.`;
}

function renderWeekly() {
  if (!state.lineups) return;
  const lineup = state.lineups.lineups[0];
  $("#weekly-summary").classList.remove("skeleton");
  $("#weekly-summary").innerHTML = `
    <div><span>GW${lineup.gameweek} xPts</span><strong>${points(lineup.total_xpts)}</strong></div>
    <div><span>Formation</span><strong>${lineup.formation}</strong></div>
    <div><span>Captain</span><strong>${lineup.captain.name}</strong></div>
    <div><span>Autosub EV</span><strong>${points(lineup.expected_autosub_value)}</strong></div>`;
  renderSensitivityBanner(lineup.role_scenario_sensitivity);
  const rows = ["GK", "DEF", "MID", "FWD"].map((position) => {
    const players = lineup.starters.filter((player) => player.position === position);
    return `<div class="pitch-line ${position.toLowerCase()}">${players.map((player) => playerCard(player, lineup.captain.fpl_id, lineup.vice_captain.fpl_id)).join("")}</div>`;
  });
  $("#pitch").classList.remove("skeleton");
  $("#pitch").innerHTML = rows.join("");
  $("#bench").innerHTML = `<div class="bench-title">Bench order</div>${lineup.bench.map((player, index) => `<div class="bench-player"><span>${index || "GK"}</span><strong>${player.name}</strong><small>${points(player.xpts)} xPts</small></div>`).join("")}`;
}

function renderOutlook() {
  if (!state.lineups) return;
  $("#outlook-total").classList.remove("skeleton");
  $("#outlook-total").innerHTML = `<span>Projected horizon score</span><strong>${points(state.lineups.cumulative_xpts)}</strong><small>captaincy included · raw mean xPts</small>`;
  $("#outlook-grid").innerHTML = state.lineups.lineups.map((lineup) => `<article class="gw-card"><span>GW${lineup.gameweek}</span><strong>${points(lineup.total_xpts)}</strong><p>${lineup.formation} · ${lineup.captain.name} (C)</p></article>`).join("");

  const totals = new Map();
  state.lineups.lineups.forEach((lineup) => {
    [...lineup.starters, ...lineup.bench].forEach((player) => totals.set(player.fpl_id, (totals.get(player.fpl_id) || 0) + player.xpts));
  });
  const rows = state.selected.map(playerById).filter(Boolean).sort((a, b) => (totals.get(b.fpl_id) || 0) - (totals.get(a.fpl_id) || 0));
  $("#player-outlook").innerHTML = `<div class="table-head"><span>Player</span><span>Position</span><span>3-GW xPts</span></div>${rows.map((player) => `<div class="table-row"><strong>${player.name}<small>${player.team}</small></strong><span>${player.position}</span><span>${points(totals.get(player.fpl_id))}</span></div>`).join("")}`;
}

function renderTransfers() {
  const container = $("#transfer-results");
  if (!state.transfers) return;
  const suggestions = state.transfers.suggestions;
  container.className = "transfer-list";
  container.innerHTML = `<article class="transfer-card hold ${state.transfers.recommendation === "hold" ? "recommended" : ""}"><div><span>Baseline</span><strong>Hold transfer</strong></div><div><span>3-GW xPts</span><strong>${points(state.transfers.baseline_cumulative_xpts)}</strong></div></article>${suggestions.map((row, index) => `<article class="transfer-card ${index === 0 && state.transfers.recommendation === "transfer" ? "recommended" : ""}">
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
    localStorage.setItem("touchline-squad", JSON.stringify(state.selected));
    localStorage.setItem("touchline-selling-prices", JSON.stringify(state.sellingPrices));
    localStorage.setItem("touchline-selling-estimated", JSON.stringify(state.sellingPriceIsEstimated));
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
  $("#free-transfers").addEventListener("change", () => { state.transfers = null; });
  try {
    state.bootstrap = await api("/api/bootstrap");
    if (state.selected.some((id) => !playerById(id))) {
      state.selected = DEFAULT_SQUAD;
      state.sellingPrices = {};
      state.sellingPriceIsEstimated = false;
      showError("A previously loaded squad no longer matches the current release; showing the default squad.");
    }
    renderRelease();
    renderSquadEditor();
    await runLineups();
  } catch (error) {
    showError(error.message);
  }
}

init();
