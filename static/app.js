"use strict";

const $ = (id) => document.getElementById(id);

let competitorsCache = [];
let toastTimer = null;
let dashboardRequestId = 0;
let articleRequestId = 0;
let pricingRequestId = 0;
let trendRequestId = 0;
let activeScanIds = new Set();
let activeDeleteIds = new Set();

const API_TIMEOUT_MS = 30000;

// API utility
async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), API_TIMEOUT_MS);

  try {
    const headers = { ...(options.headers || {}) };

    if (
      options.body &&
      typeof options.body === "string" &&
      !headers["Content-Type"]
    ) {
      headers["Content-Type"] = "application/json";
    }

    const response = await fetch(path, {
      ...options,
      headers,
      signal: options.signal || controller.signal
    });

    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json")
      ? await response.json().catch(() => ({}))
      : {};

    if (!response.ok) {
      throw new Error(
        data.error ||
        data.message ||
        `Request failed (${response.status})`
      );
    }

    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The request timed out. Please try again.");
    }

    if (error instanceof TypeError) {
      throw new Error("Unable to connect to the server. Check your connection.");
    }

    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

// Safe DOM helpers
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[character]);
}

function escapeAttribute(value) {
  return escapeHtml(value);
}

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""), window.location.origin);

    if (!["http:", "https:"].includes(url.protocol)) {
      return "";
    }

    return url.href;
  } catch (_) {
    return "";
  }
}

function externalLink(url, label, className = "") {
  const safeUrl = safeExternalUrl(url);

  if (!safeUrl) {
    return escapeHtml(label || url || "Unavailable");
  }

  return `<a
    ${className ? `class="${escapeAttribute(className)}"` : ""}
    href="${escapeAttribute(safeUrl)}"
    target="_blank"
    rel="noopener noreferrer"
  >${escapeHtml(label || safeUrl)}</a>`;
}

function setHtml(id, html) {
  const element = $(id);
  if (element) element.innerHTML = html;
}

function setText(id, value) {
  const element = $(id);
  if (element) element.textContent = value ?? "";
}

function setDisabled(id, disabled) {
  const element = $(id);
  if (element) element.disabled = Boolean(disabled);
}

function setLoading(button, loading, loadingText, defaultText) {
  if (!button) return;

  button.disabled = Boolean(loading);
  button.textContent = loading ? loadingText : defaultText;
}

function toast(message) {
  const element = $("toast");
  if (!element) return;

  element.textContent = message;
  element.classList.add("visible");
  element.setAttribute("role", "status");
  element.setAttribute("aria-live", "polite");

  clearTimeout(toastTimer);

  toastTimer = setTimeout(() => {
    element.classList.remove("visible");
  }, 3500);
}

function fmtDate(value) {
  if (!value) return "—";

  const date = new Date(value);

  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString();
}

function statusBadge(status) {
  const value = String(status || "NOT SCANNED");
  const className = value
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, "-");

  return `<span class="status ${escapeAttribute(className)}">${escapeHtml(
    value.replaceAll("_", " ")
  )}</span>`;
}

function showEmptyRow(id, columns, message) {
  setHtml(
    id,
    `<tr><td colspan="${columns}" class="empty">${escapeHtml(message)}</td></tr>`
  );
}

function getCompetitor(id) {
  return competitorsCache.find(
    (competitor) => String(competitor.id) === String(id)
  );
}

// Dashboard
async function loadDashboard() {
  const requestId = ++dashboardRequestId;
  const refreshButton = $("refreshBtn");

  if (refreshButton) refreshButton.disabled = true;

  try {
    const data = await api("/api/dashboard");

    if (requestId !== dashboardRequestId) return;

    const stats = data.stats || {};
    const competitors = Array.isArray(data.competitors)
      ? data.competitors
      : [];
    const recentEvents = Array.isArray(data.recent_events)
      ? data.recent_events
      : [];

    setText("statCompetitors", stats.competitors ?? 0);
    setText("statArticles", stats.articles_tracked ?? 0);
    setText("statEvents", stats.events_7d ?? 0);
    setText("statScans", stats.scans_run ?? 0);

    competitorsCache = competitors;

    renderCompetitors(competitors);
    renderEvents(recentEvents);

    const articleSelect = $("articleCompetitor");
    const trendSelect = $("trendCompetitor");
    const pricingSelect = $("pricingCompetitor");

    const previousArticleId = articleSelect?.value || "";
    const previousTrendId = trendSelect?.value || "";
    const previousPricingId = pricingSelect?.value || "";

    fillSelectors(competitors);

    const validId = (id) =>
      competitors.some(
        (competitor) => String(competitor.id) === String(id)
      );

    const selectedId = validId(previousArticleId)
      ? previousArticleId
      : validId(previousTrendId)
        ? previousTrendId
        : validId(previousPricingId)
          ? previousPricingId
          : String(competitors[0]?.id || "");

    if (articleSelect) articleSelect.value = selectedId;
    if (trendSelect) trendSelect.value = selectedId;
    if (pricingSelect && !validId(previousPricingId)) {
      pricingSelect.value = selectedId;
    }

    setDisabled("refreshTrendBtn", !selectedId);

    if (selectedId) {
      await Promise.all([
        loadArticles(selectedId),
        loadPricing(selectedId)
      ]);
    } else {
      showEmptyRow("articleRows", 5, "Select a competitor.");
      setHtml(
        "trendRows",
        `<p class="empty">Add a competitor to begin tracking keyword trends.</p>`
      );
      setHtml(
        "pricingList",
        `<p class="empty">Select a competitor.</p>`
      );
    }
  } catch (error) {
    if (requestId === dashboardRequestId) {
      toast(error.message);
    }
  } finally {
    if (requestId === dashboardRequestId && refreshButton) {
      refreshButton.disabled = false;
    }
  }
}

function renderCompetitors(items) {
  setText(
    "competitorCount",
    `${items.length} website${items.length === 1 ? "" : "s"}`
  );

  if (!items.length) {
    showEmptyRow(
      "competitorRows",
      4,
      "No competitors yet. Add one above to get started."
    );
    return;
  }

  setHtml(
    "competitorRows",
    items.map((competitor) => {
      const id = escapeAttribute(competitor.id);
      const name = competitor.name || "?";
      const initial = name.slice(0, 2).toUpperCase();

      const scanning = activeScanIds.has(String(competitor.id));
      const deleting = activeDeleteIds.has(String(competitor.id));

      return `
        <tr>
          <td>
            <div class="competitor-cell">
              <div class="site-avatar">${escapeHtml(initial)}</div>
              <div>
                <div class="site-name">${escapeHtml(name)}</div>
                <div class="site-url">${externalLink(
                  competitor.url,
                  competitor.url
                )}</div>
              </div>
            </div>
          </td>
          <td>${escapeHtml(fmtDate(competitor.last_scan_at))}</td>
          <td>${statusBadge(competitor.last_status)}</td>
          <td>
            <div class="actions">
              <button
                type="button"
                class="btn btn-primary btn-small"
                data-scan="${id}"
                ${scanning || deleting ? "disabled" : ""}
              >${scanning ? "Scanning…" : "Scan now"}</button>
              <button
                type="button"
                class="btn btn-danger btn-small"
                data-delete="${id}"
                title="Delete competitor"
                ${scanning || deleting ? "disabled" : ""}
              >${deleting ? "Deleting…" : "Delete"}</button>
            </div>
          </td>
        </tr>
      `;
    }).join("")
  );
}

function fillSelectors(items) {
  const selects = [
    $("articleCompetitor"),
    $("pricingCompetitor"),
    $("trendCompetitor")
  ].filter(Boolean);

  for (const select of selects) {
    const previous = select.value;

    select.innerHTML =
      `<option value="">Select competitor</option>` +
      items.map((competitor) => `
        <option value="${escapeAttribute(competitor.id)}">
          ${escapeHtml(competitor.name)}
        </option>
      `).join("");

    if (items.some((competitor) => String(competitor.id) === previous)) {
      select.value = previous;
    }
  }
}

// Activity events
function renderEvents(items) {
  const container = $("eventsList");
  if (!container) return;

  if (!items.length) {
    container.innerHTML =
      `<p class="empty">No events yet. Run a scan to begin monitoring.</p>`;
    return;
  }

  container.innerHTML = items.map((event) => {
    const source = event.url
      ? ` · ${externalLink(event.url, "Open source ↗")}`
      : "";

    const type = String(
      event.type || event.event_type || "article"
    ).toLowerCase();

    return `
      <div
        class="event event-item"
        data-type="${escapeAttribute(type)}"
      >
        <span class="event-dot"></span>
        <div>
          <div class="event-title">${escapeHtml(event.summary)}</div>
          <div class="event-meta">
            ${escapeHtml(event.competitor_name)}
            · ${escapeHtml(fmtDate(event.created_at))}
            ${source}
          </div>
        </div>
      </div>
    `;
  }).join("");
}

// Trend metrics
function formatVolume(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString();
}

function formatMetric(value, digits = 2) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function sparklinePath(series) {
  const points = (Array.isArray(series) ? series : []).filter(
    (point) =>
      point.value != null &&
      point.value !== "" &&
      Number.isFinite(Number(point.value))
  );

  if (points.length < 2) return "";

  const values = points.map((point) => Number(point.value));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;

  return values.map((value, index) => {
    const x = index * 120 / (values.length - 1);
    const y = 24 - ((value - min) / range) * 18;

    return `${index === 0 ? "M" : "L"} ${x} ${y}`;
  }).join(" ");
}

function renderTrendRows(rows) {
  if (!rows || !rows.length) {
    setHtml(
      "trendRows",
      `<p class="empty">No stored keyword data for this competitor yet. Run a scan or refresh data to request provider results.</p>`
    );
    return;
  }

  setHtml(
    "trendRows",
    rows.map((row) => {
      const keyword =
        row.volume_keyword || row.keyword || "Keyword not generated";

      const trendsKeyword = row.trends_keyword || keyword;

      const series = Array.isArray(row.trends_series)
        ? row.trends_series
        : [];

      const volumeSeries = Array.isArray(row.volume_series)
        ? row.volume_series
        : [];

      const volumeStatus =
        row.volume_status || row.keyword_status || "NOT_CHECKED";

      const trendsStatus = row.trends_status || "NOT_CHECKED";
      const provider = row.volume_source || "SeoData";

      const volumeError = row.volume_error || row.keyword_error || "";
      const trendsError = row.trends_error || "";

      const trendScore = row.trends_interest == null
        ? "—"
        : formatMetric(row.trends_interest, 1);

      const path = sparklinePath(series);
      const volumePath = sparklinePath(volumeSeries);

      const articleUrl = safeExternalUrl(row.url);
      const articleLabel = row.title || row.url || "Source article";

      const articleLink = articleUrl
        ? externalLink(articleUrl, articleLabel, "trend-article")
        : `<span class="trend-article">${escapeHtml(articleLabel)}</span>`;

      const chart = path
        ? `
          <svg
            viewBox="0 0 120 28"
            preserveAspectRatio="none"
            role="img"
            aria-label="Google Trends interest history"
          >
            <path
              d="${path}"
              stroke="#169b62"
              stroke-width="2.2"
              fill="none"
              stroke-linecap="round"
              stroke-linejoin="round"
            ></path>
          </svg>
        `
        : `<span class="trend-no-chart" title="${escapeAttribute(
            trendsError || "Google Trends did not return a historical series."
          )}">No series</span>`;

      const volumeChart = volumePath
        ? `
          <svg
            class="volume-spark"
            viewBox="0 0 120 28"
            preserveAspectRatio="none"
            role="img"
            aria-label="Monthly search volume history"
          >
            <path
              d="${volumePath}"
              stroke="#6179d8"
              stroke-width="1.8"
              fill="none"
              stroke-linecap="round"
              stroke-linejoin="round"
            ></path>
          </svg>
        `
        : "";

      const similarKeywords =
        Array.isArray(row.similar_keywords) && row.similar_keywords.length
          ? `
            <div class="trend-similar">
              <b>Related keywords:</b>
              ${row.similar_keywords.slice(0, 5).map((item) =>
                `${escapeHtml(item.keyword)} (${formatVolume(item.volume)})`
              ).join(" · ")}
            </div>
          `
          : "";

      const volumeMessageClass = volumeStatus === "NO_DATA"
        ? "trend-source"
        : "trend-error";

      return `
        <div class="trend-row">
          <div class="trend-keyword" title="${escapeAttribute(
            row.title || keyword
          )}">
            <span>${escapeHtml(keyword)}</span>
            ${articleLink}
          </div>

          <div class="trend-volume">
            ${formatVolume(row.search_volume)}
            <span class="trend-source">
              ${escapeHtml(
                row.country
                  ? String(row.country).toUpperCase()
                  : "Monthly searches"
              )}
            </span>
            ${volumeChart}
          </div>

          <div class="trend-metric">
            ${formatMetric(row.cpc)}
            <span class="trend-source">CPC</span>
          </div>

          <div class="trend-metric">
            ${formatMetric(row.competition)}
            <span class="trend-source">Competition</span>
          </div>

          <div class="trend-interest">
            ${trendScore}
            <span class="trend-source">
              Term: ${escapeHtml(trendsKeyword)}
            </span>
            <span class="trend-status ${
              trendsStatus === "SUCCESS" ? "good" : "muted"
            }">
              Google Trends: ${escapeHtml(trendsStatus.replaceAll("_", " "))}
            </span>
            ${
              trendsError
                ? `<span class="trend-error" title="${escapeAttribute(
                    trendsError
                  )}">${escapeHtml(trendsError)}</span>`
                : ""
            }
          </div>

          <div class="trend-provider">
            <span class="trend-status ${
              volumeStatus === "SUCCESS" ? "good" : "muted"
            }">
              ${escapeHtml(volumeStatus.replaceAll("_", " "))}
            </span>
            <span class="trend-source">
              ${escapeHtml(provider)}${row.provider_cached ? " · cached" : ""}
            </span>
            ${
              volumeError
                ? `<span class="${volumeMessageClass}" title="${escapeAttribute(
                    volumeError
                  )}">${escapeHtml(volumeError)}</span>`
                : ""
            }
          </div>

          <div class="trend-spark">${chart}</div>

          ${similarKeywords}
        </div>
      `;
    }).join("")
  );
}

// Add competitor
const addForm = $("addForm");

addForm?.addEventListener("submit", async (event) => {
  event.preventDefault();

  const message = $("formMessage");
  const submitButton = addForm.querySelector('[type="submit"]');

  if (message) {
    message.className = "message";
    message.textContent = "Adding competitor…";
  }

  if (submitButton) submitButton.disabled = true;

  try {
    const competitor = await api("/api/competitors", {
      method: "POST",
      body: JSON.stringify({
        name: $("competitorName")?.value.trim(),
        url: $("competitorUrl")?.value.trim()
      })
    });

    if ($("competitorName")) $("competitorName").value = "";
    if ($("competitorUrl")) $("competitorUrl").value = "";

    if (message) {
      message.className = "message";
      message.textContent =
        "Competitor added. Run the first scan to establish a baseline.";
    }

    await loadDashboard();

    const id = String(competitor.id);

    if ($("articleCompetitor")) $("articleCompetitor").value = id;
    if ($("trendCompetitor")) $("trendCompetitor").value = id;
    if ($("pricingCompetitor")) $("pricingCompetitor").value = id;

    setDisabled("refreshTrendBtn", !id);

    await Promise.all([
      loadArticles(id),
      loadPricing(id)
    ]);

    toast("Competitor added. Run its first scan to fetch available data.");
  } catch (error) {
    if (message) {
      message.className = "message error";
      message.textContent = error.message;
    } else {
      toast(error.message);
    }
  } finally {
    if (submitButton) submitButton.disabled = false;
  }
});

// Competitor actions
const competitorRows = $("competitorRows");

competitorRows?.addEventListener("click", async (event) => {
  const target = event.target;

  if (!(target instanceof Element)) return;

  const scanButton = target.closest("[data-scan]");
  const deleteButton = target.closest("[data-delete]");

  if (scanButton) {
    const id = String(scanButton.dataset.scan);

    if (activeScanIds.has(id) || activeDeleteIds.has(id)) return;

    activeScanIds.add(id);
    renderCompetitors(competitorsCache);

    try {
      const result = await api(`/api/competitors/${encodeURIComponent(id)}/scan`, {
        method: "POST"
      });

      const keywordCount = result.keyword_results?.length ?? 0;
      const newArticleCount = result.new_articles?.length ?? 0;
      const pricingCount = result.pricing_changes?.length ?? 0;

      let summary =
        `${result.status || "Scan complete"}: ${result.message || "Scan finished."} ` +
        `Keyword lookups: ${keywordCount}. ` +
        `New articles: ${newArticleCount}. ` +
        `Pricing changes: ${pricingCount}.`;

      if (result.alert) {
        summary += ` Slack: ${
          result.alert.sent ? "sent" : result.alert.reason || "not sent"
        }.`;
      }

      toast(summary);

      const selectedId = id;

      await loadDashboard();

      if ($("articleCompetitor")) $("articleCompetitor").value = selectedId;
      if ($("pricingCompetitor")) $("pricingCompetitor").value = selectedId;
      if ($("trendCompetitor")) $("trendCompetitor").value = selectedId;

      setDisabled("refreshTrendBtn", !selectedId);

      await Promise.all([
        loadArticles(selectedId),
        loadPricing(selectedId)
      ]);
    } catch (error) {
      toast(error.message);
    } finally {
      activeScanIds.delete(id);
      renderCompetitors(competitorsCache);
    }

    return;
  }

  if (deleteButton) {
    const id = String(deleteButton.dataset.delete);

    if (activeScanIds.has(id) || activeDeleteIds.has(id)) return;

    if (!window.confirm("Delete this competitor and its stored history?")) {
      return;
    }

    activeDeleteIds.add(id);
    renderCompetitors(competitorsCache);

    try {
      await api(`/api/competitors/${encodeURIComponent(id)}`, {
        method: "DELETE"
      });

      toast("Competitor deleted.");
      await loadDashboard();
    } catch (error) {
      toast(error.message);
    } finally {
      activeDeleteIds.delete(id);
      renderCompetitors(competitorsCache);
    }
  }
});

// Refresh dashboard
$("refreshBtn")?.addEventListener("click", loadDashboard);

// Article competitor selection
$("articleCompetitor")?.addEventListener("change", (event) => {
  const id = event.target.value;

  if ($("trendCompetitor")) $("trendCompetitor").value = id;

  setDisabled("refreshTrendBtn", !id);

  if (id) {
    loadArticles(id);
  } else {
    showEmptyRow("articleRows", 5, "Select a competitor.");
    renderTrendRows([]);
  }
});

// Trend competitor selection
$("trendCompetitor")?.addEventListener("change", (event) => {
  const id = event.target.value;

  if ($("articleCompetitor")) $("articleCompetitor").value = id;

  setDisabled("refreshTrendBtn", !id);

  if (id) {
    loadArticles(id);
  } else {
    showEmptyRow("articleRows", 5, "Select a competitor.");
    renderTrendRows([]);
  }
});

// Pricing competitor selection
$("pricingCompetitor")?.addEventListener("change", (event) => {
  const id = event.target.value;

  if (id) {
    loadPricing(id);
  } else {
    setHtml(
      "pricingList",
      `<p class="empty">Select a competitor.</p>`
    );
  }
});

// Refresh keyword/trend data
$("refreshTrendBtn")?.addEventListener("click", async () => {
  const id = $("trendCompetitor")?.value;
  if (!id) {
    toast("Select a competitor first.");
    return;
  }

  const button = $("refreshTrendBtn");
  const requestId = ++trendRequestId;

  setLoading(button, true, "Requesting…", "Refresh data");

  try {
    const result = await api(
      `/api/competitors/${encodeURIComponent(id)}/trends/refresh`,
      { method: "POST" }
    );

    if (requestId !== trendRequestId) return;

    await loadArticles(id);

    const results = Array.isArray(result.results) ? result.results : [];

    const seodataLimited = results.some(
      (item) => item.volume_status === "RATE_LIMITED"
    );

    const trendsLimited = results.some(
      (item) => item.trends_status === "RATE_LIMITED"
    );

    if (seodataLimited || trendsLimited) {
      const providers = [
        seodataLimited ? "SeoData" : "",
        trendsLimited ? "Google Trends" : ""
      ].filter(Boolean).join(" and ");

      toast(
        `${providers} rate-limited the request. Check provider status for details.`
      );
    } else {
      toast(
        `Refreshed ${result.refreshed ?? results.length} keyword lookup(s). Values are shown only when returned by a provider.`
      );
    }
  } catch (error) {
    if (requestId === trendRequestId) toast(error.message);
  } finally {
    if (requestId === trendRequestId) {
      setLoading(
        button,
        false,
        "Requesting…",
        "Refresh data"
      );
      button.disabled = !$("trendCompetitor")?.value;
    }
  }
});

// Load article history
async function loadArticles(id) {
  if (!id) {
    showEmptyRow("articleRows", 5, "Select a competitor.");
    renderTrendRows([]);
    return;
  }

  const requestId = ++articleRequestId;

  try {
    const rows = await api(
      `/api/competitors/${encodeURIComponent(id)}/articles`
    );

    if (requestId !== articleRequestId) return;

    if (!Array.isArray(rows) || !rows.length) {
      showEmptyRow("articleRows", 5, "No article metadata stored yet.");
      renderTrendRows([]);
      return;
    }

    setHtml(
      "articleRows",
      rows.map((article) => {
        const title = article.title || article.url || "Untitled article";
        const url = safeExternalUrl(article.url);

        const link = url
          ? externalLink(url, title, "article-link")
          : escapeHtml(title);

        const interest = article.trends_interest == null
          ? "—"
          : formatMetric(article.trends_interest);

        return `
          <tr>
            <td>
              ${link}
              <div class="site-url">${escapeHtml(article.url || "")}</div>
            </td>
            <td>${escapeHtml(article.published_at || "Not found")}</td>
            <td>${escapeHtml(article.keyword || "—")}</td>
            <td>${interest}</td>
            <td>${escapeHtml(fmtDate(article.first_seen_at))}</td>
          </tr>
        `;
      }).join("")
    );

    renderTrendRows(rows);
  } catch (error) {
    if (requestId === articleRequestId) toast(error.message);
  }
}

// Load pricing history
async function loadPricing(id) {
  if (!id) {
    setHtml(
      "pricingList",
      `<p class="empty">Select a competitor.</p>`
    );
    return;
  }

  const requestId = ++pricingRequestId;

  try {
    const rows = await api(
      `/api/competitors/${encodeURIComponent(id)}/pricing`
    );

    if (requestId !== pricingRequestId) return;

    if (!Array.isArray(rows) || !rows.length) {
      setHtml(
        "pricingList",
        `<p class="empty">No readable pricing values found yet. Pricing may be dynamic or unavailable to this scraper.</p>`
      );
      return;
    }

    setHtml(
      "pricingList",
      rows.map((price) => {
        const source = price.source_url
          ? `<br>${externalLink(price.source_url, "Source page ↗")}`
          : "";

        return `
          <article class="price-card">
            <h4>${escapeHtml(price.plan_name || "Pricing plan")}</h4>
            <div class="price">${escapeHtml(price.price_text || "Price unavailable")}</div>
            <div class="price-meta">
              ${escapeHtml(price.billing_period || "Billing period not detected")}
              <br>
              Observed: ${escapeHtml(fmtDate(price.observed_at))}
              ${source}
            </div>
          </article>
        `;
      }).join("")
    );
  } catch (error) {
    if (requestId === pricingRequestId) toast(error.message);
  }
}

// Start the dashboard
loadDashboard();