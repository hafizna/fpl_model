async function loadLegalConfig() {
  const status = document.getElementById("legal-status");
  try {
    const response = await fetch("/api/public-config", { headers: { Accept: "application/json" } });
    const config = await response.json();
    if (!response.ok) throw new Error(config.detail || `Request failed (${response.status})`);
    document.querySelectorAll("[data-operator]").forEach((node) => { node.textContent = config.operator_name || "Belum dikonfigurasi"; });
    document.querySelectorAll("[data-support]").forEach((node) => {
      node.textContent = config.support_email || "Belum dikonfigurasi";
      if (config.support_email && node.tagName === "A") node.href = `mailto:${config.support_email}`;
    });
    document.querySelectorAll("[data-host]").forEach((node) => { node.textContent = config.hosting_provider || "Belum dikonfigurasi"; });
    document.querySelectorAll("[data-region]").forEach((node) => { node.textContent = config.hosting_region || "Belum dikonfigurasi"; });
    document.querySelectorAll("[data-retention]").forEach((node) => { node.textContent = config.log_retention_days ? `${config.log_retention_days} hari` : "Belum dikonfigurasi"; });
    const versionKey = document.body.dataset.legalDocument === "privacy" ? "privacy_notice_version" : "terms_version";
    document.querySelectorAll("[data-version]").forEach((node) => { node.textContent = config[versionKey]; });
    status.textContent = config.ready
      ? "Konfigurasi operasional closed alpha lengkap. Isi tetap harus sesuai hasil legal review operator."
      : "Deployment ini belum siap untuk tester: identitas operator, support, hosting, retensi, atau legal review belum lengkap.";
    status.classList.toggle("ready", config.ready);
  } catch (error) {
    status.textContent = `Konfigurasi operasional tidak dapat dimuat: ${error.message}`;
  }
}

loadLegalConfig();
