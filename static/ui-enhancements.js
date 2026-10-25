(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const THEME_KEY = "competitorpulse-theme";

  // Theme switcher
  const themeToggle = $("#themeToggle");

  function updateThemeButton() {
    if (!themeToggle) return;

    const isLight = document.body.classList.contains("light-theme");

    themeToggle.textContent = isLight ? "☾" : "☼";
    themeToggle.setAttribute(
      "aria-label",
      isLight ? "Switch to dark appearance" : "Switch to light appearance"
    );
    themeToggle.title = isLight
      ? "Switch to dark appearance"
      : "Switch to light appearance";
    themeToggle.setAttribute("aria-pressed", String(isLight));
  }

  try {
    document.body.classList.toggle(
      "light-theme",
      localStorage.getItem(THEME_KEY) === "light"
    );
  } catch (_) {
    // Continue using the current theme if storage is unavailable.
  }

  updateThemeButton();

  themeToggle?.addEventListener("click", () => {
    const isLight = document.body.classList.toggle("light-theme");

    try {
      localStorage.setItem(THEME_KEY, isLight ? "light" : "dark");
    } catch (_) {
      // Theme still works for the current session.
    }

    updateThemeButton();
  });

  // Toast notifications
  let toastTimer;

  function showToast(message) {
    const toast = $("#toast");
    if (!toast) return;

    toast.textContent = message;
    toast.classList.add("visible");
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");

    window.clearTimeout(toastTimer);

    toastTimer = window.setTimeout(() => {
      toast.classList.remove("visible");
    }, 2800);
  }

  // Search and filtering
  const competitorSearch = $("#competitorSearch");
  const globalSearch = $("#globalSearch");
  const activityFilter = $("#activityFilter");

  const normalize = (value) =>
    String(value || "").trim().toLocaleLowerCase();

  function filterRows(containerSelector, query) {
    const container = $(containerSelector);
    if (!container) return;

    const normalized = normalize(query);

    $$("tr", container).forEach((row) => {
      if (row.querySelector(".empty")) {
        row.hidden = Boolean(normalized);
        return;
      }

      row.hidden =
        normalized.length > 0 &&
        !row.textContent.toLocaleLowerCase().includes(normalized);
    });
  }

  function filterCompetitors(query) {
    filterRows("#competitorRows", query);
  }

  function filterArticles(query) {
    filterRows("#articleRows", query);
  }

  function getActivityKind(item) {
    const text = item.textContent.toLocaleLowerCase();

    return String(
      item.dataset.type ||
        item.dataset.eventType ||
        (text.includes("pricing") || text.includes("price")
          ? "pricing"
          : "article")
    ).toLocaleLowerCase();
  }

  function filterEvents(query) {
    const events = $("#eventsList");
    if (!events) return;

    const normalized = normalize(query);
    const selectedType = normalize(activityFilter?.value || "all");

    $$(".event-item", events).forEach((item) => {
      const matchesQuery =
        !normalized ||
        item.textContent.toLocaleLowerCase().includes(normalized);

      const matchesType =
        selectedType === "all" ||
        getActivityKind(item).includes(selectedType);

      item.hidden = !(matchesQuery && matchesType);
    });
  }

  function applyGlobalSearch(query) {
    filterCompetitors(query);
    filterArticles(query);
    filterEvents(query);
  }

  competitorSearch?.addEventListener("input", () => {
    filterCompetitors(competitorSearch.value);
  });

  globalSearch?.addEventListener("input", () => {
    applyGlobalSearch(globalSearch.value);
  });

  activityFilter?.addEventListener("change", () => {
    filterEvents(globalSearch?.value || "");
  });

  // CSV export
  function escapeCSV(value) {
    const text = String(value ?? "").replace(/\s+/g, " ").trim();

    // Reduce spreadsheet formula-injection risk.
    const safeText = /^[\s]*[=+\-@]/.test(text)
      ? `'${text}`
      : text;

    return `"${safeText.replace(/"/g, '""')}"`;
  }

  $("#exportBtn")?.addEventListener("click", () => {
    const table = $("#articleRows");

    if (!table) {
      showToast("Article history table is unavailable.");
      return;
    }

    const rows = $$("tr", table)
      .filter((row) => !row.hidden)
      .map((row) =>
        $$("th, td", row).map((cell) =>
          (cell.innerText || cell.textContent || "")
            .replace(/\s+/g, " ")
            .trim()
        )
      )
      .filter(
        (row) =>
          row.length > 0 &&
          !/select a competitor/i.test(row.join(" "))
      );

    const validRows = rows.filter((row) =>
      row.some((cell) => cell !== "")
    );

    if (validRows.length < 2) {
      showToast(
        "Select a competitor with article history before exporting."
      );
      return;
    }

    const csv =
      "\uFEFF" +
      validRows
        .map((row) => row.map(escapeCSV).join(","))
        .join("\r\n");

    const blob = new Blob([csv], {
      type: "text/csv;charset=utf-8;"
    });

    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");

    link.href = url;
    link.download = `competitorpulse-articles-${new Date()
      .toISOString()
      .slice(0, 10)}.csv`;
    link.style.display = "none";

    document.body.appendChild(link);
    link.click();
    link.remove();

    window.setTimeout(() => URL.revokeObjectURL(url), 1000);

    showToast("Article history CSV exported.");
  });

  // Local clock
  const clock = $("#localClock");

  function updateClock() {
    if (!clock) return;

    clock.textContent = new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit"
    }).format(new Date());
  }

  updateClock();
  window.setInterval(updateClock, 30000);

  // Active navigation indicator
  const navLinks = $$(".nav-item");

  navLinks.forEach((link) => {
    link.addEventListener("click", () => {
      navLinks.forEach((item) => {
        item.classList.remove("active");
        item.removeAttribute("aria-current");
      });

      link.classList.add("active");
      link.setAttribute("aria-current", "page");
    });
  });

  // Reapply filters after dashboard refresh
  $("#refreshBtn")?.addEventListener("click", () => {
    window.setTimeout(() => {
      if (globalSearch?.value) {
        applyGlobalSearch(globalSearch.value);
      } else if (competitorSearch?.value) {
        filterCompetitors(competitorSearch.value);
      }
    }, 250);
  });

  // Trend refresh validation.
  // app.js remains responsible for fetching trend data.
  $("#refreshTrendBtn")?.addEventListener("click", () => {
    if (!$("#trendCompetitor")?.value) {
      showToast("Select a competitor first.");
    }
  });
})();