// Landing page: show which Gameweeks the current release covers. Purely
// informational -- if the readiness check fails the line simply stays hidden.
async function showReleaseStatus() {
  const nodes = document.querySelectorAll("[data-release-status]");
  if (nodes.length === 0) return;
  try {
    const response = await fetch("/api/ready", { headers: { Accept: "application/json" } });
    const payload = await response.json();
    const horizon = Array.isArray(payload.horizon) ? payload.horizon : [];
    if (!response.ok || horizon.length === 0) return;
    const range = horizon.length > 1 ? `GW${horizon[0]}–GW${horizon.at(-1)}` : `GW${horizon[0]}`;
    const label = payload.release_health === "production" ? "Production projections" : "Model preview projections";
    nodes.forEach((node) => {
      node.textContent = `${label} are live for ${range}.`;
      node.hidden = false;
    });
  } catch {
    // Leave the line hidden; the rest of the page does not depend on it.
  }
}

showReleaseStatus();
