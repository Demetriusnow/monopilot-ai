(() => {
  "use strict";

  const SESSION_KEYS = {
    GEMINI: "monopilot_gemini_key",
    MONO: "monopilot_mono_token",
  };

  const CHART_PALETTE = [
    "#fa5255",
    "#22c55e",
    "#f59e0b",
    "#3b82f6",
    "#a855f7",
    "#06b6d4",
    "#ec4899",
    "#14b8a6",
    "#84cc16",
    "#64748b",
  ];

  const FORECAST_LABELS = {
    on_track: { text: "В межах плану", cls: "status-on-track" },
    warning: { text: "Близько до ліміту", cls: "status-warning" },
    over_budget: { text: "Перевищення", cls: "status-over-budget" },
  };

  const state = {
    days: 30,
    health: null,
    dashboard: null,
    budgets: [],
    history: [],
    isSending: false,
    categoriesChart: null,
    dailyChart: null,
  };

  const $ = (id) => document.getElementById(id);

  function getSessionKey(name) {
    try {
      return window.sessionStorage.getItem(name) || "";
    } catch {
      return "";
    }
  }

  function setSessionKey(name, value) {
    try {
      const trimmed = (value || "").trim();
      if (trimmed) {
        window.sessionStorage.setItem(name, trimmed);
      } else {
        window.sessionStorage.removeItem(name);
      }
    } catch {
      // Ignore storage access errors in restricted frames
    }
  }

  function clearSessionKeys() {
    setSessionKey(SESSION_KEYS.GEMINI, "");
    setSessionKey(SESSION_KEYS.MONO, "");
  }

  /**
   * Escape HTML entities strictly before any markdown or DOM formatting.
   */
  function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replaceAll("\u0000", "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function formatNumber(value, decimals = 2) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "0.00";
    return new Intl.NumberFormat("uk-UA", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }).format(num);
  }

  function formatUAH(value, showPlus = false) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "0.00 ₴";
    const prefix = showPlus && num > 0 ? "+" : "";
    return `${prefix}${formatNumber(num, 2)} ₴`;
  }

  function formatPreview(preview) {
    if (preview === null || preview === undefined) return "null";
    if (typeof preview === "string") return preview;
    try {
      return JSON.stringify(preview, null, 2);
    } catch {
      return String(preview);
    }
  }

  /**
   * XSS-safe markdown renderer:
   * 1. Escapes HTML entities across the entire raw string first.
   * 2. Isolates fenced code blocks and inline code with null-byte tokens.
   * 3. Converts **bold**, headings, and bullet/numbered lists safely.
   */
  function renderMarkdownSafe(rawText) {
    const escaped = escapeHtml(rawText || "");
    const codeBlocks = [];
    const inlineCodes = [];

    let text = escaped.replace(/```([a-zA-Z0-9_-]*)\r?\n?([\s\S]*?)```/g, (_, _lang, code) => {
      const idx = codeBlocks.length;
      const cleanCode = code.replace(/^\r?\n|\r?\n$/g, "");
      codeBlocks.push(`<pre class="md-code-block"><code>${cleanCode}</code></pre>`);
      return `\u0000CB_${idx}\u0000`;
    });

    text = text.replace(/`([^`\n]+)`/g, (_, code) => {
      const idx = inlineCodes.length;
      inlineCodes.push(`<code class="md-inline-code">${code}</code>`);
      return `\u0000IC_${idx}\u0000`;
    });

    const formatInline = (line) =>
      line.replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>");

    const splitTableRow = (rowLine) => {
      const stripped = rowLine.trim().replace(/^\|/, "").replace(/\|$/, "");
      return stripped.split("|").map((cell) => cell.trim());
    };

    const isTableSeparator = (rowLine) =>
      /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(rowLine || "");

    const lines = text.split(/\r?\n/);
    const htmlParts = [];
    let listType = null;
    let paragraphLines = [];

    const flushParagraph = () => {
      if (paragraphLines.length > 0) {
        htmlParts.push(`<p>${paragraphLines.map(formatInline).join("<br>")}</p>`);
        paragraphLines = [];
      }
    };

    const flushList = () => {
      if (listType) {
        htmlParts.push(`</${listType}>`);
        listType = null;
      }
    };

    for (let i = 0; i < lines.length; i += 1) {
      const rawLine = lines[i];
      const trimmed = rawLine.trim();

      if (!trimmed) {
        flushParagraph();
        flushList();
        continue;
      }

      if (trimmed.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flushParagraph();
        flushList();
        const headerCells = splitTableRow(trimmed);
        const bodyRows = [];
        let j = i + 2;
        while (j < lines.length && lines[j].trim() && lines[j].trim().includes("|")) {
          bodyRows.push(splitTableRow(lines[j].trim()));
          j += 1;
        }
        const theadHtml = `<thead><tr>${headerCells
          .map((cell) => `<th>${formatInline(cell)}</th>`)
          .join("")}</tr></thead>`;
        const tbodyHtml = bodyRows.length
          ? `<tbody>${bodyRows
              .map(
                (cells) =>
                  `<tr>${headerCells
                    .map((_, colIdx) => `<td>${formatInline(cells[colIdx] || "")}</td>`)
                    .join("")}</tr>`
              )
              .join("")}</tbody>`
          : "";
        htmlParts.push(`<div class="md-table-wrap"><table class="md-table">${theadHtml}${tbodyHtml}</table></div>`);
        i = j - 1;
        continue;
      }

      const blockOnlyMatch = trimmed.match(/^\u0000CB_(\d+)\u0000$/);
      if (blockOnlyMatch) {
        flushParagraph();
        flushList();
        htmlParts.push(trimmed);
        continue;
      }

      const headingMatch = trimmed.match(/^#{1,4}\s+(.+)$/);
      if (headingMatch) {
        flushParagraph();
        flushList();
        htmlParts.push(`<h4>${formatInline(headingMatch[1])}</h4>`);
        continue;
      }

      const bulletMatch = trimmed.match(/^(?:[-*•])\s+(.+)$/);
      if (bulletMatch) {
        flushParagraph();
        if (listType !== "ul") {
          flushList();
          listType = "ul";
          htmlParts.push("<ul>");
        }
        htmlParts.push(`<li>${formatInline(bulletMatch[1])}</li>`);
        continue;
      }

      const orderedMatch = trimmed.match(/^\d+\.\s+(.+)$/);
      if (orderedMatch) {
        flushParagraph();
        if (listType !== "ol") {
          flushList();
          listType = "ol";
          htmlParts.push("<ol>");
        }
        htmlParts.push(`<li>${formatInline(orderedMatch[1])}</li>`);
        continue;
      }

      flushList();
      paragraphLines.push(trimmed);
    }

    flushParagraph();
    flushList();

    let rendered = htmlParts.join("");
    rendered = rendered.replace(/\u0000IC_(\d+)\u0000/g, (_, idx) => inlineCodes[Number(idx)] || "");
    rendered = rendered.replace(/\u0000CB_(\d+)\u0000/g, (_, idx) => codeBlocks[Number(idx)] || "");

    return rendered || "<p></p>";
  }

  function showToast(message, variant = "ok") {
    const container = $("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = `toast ${variant === "err" ? "toast-err" : "toast-ok"}`;
    toast.textContent = message;
    container.appendChild(toast);
    window.setTimeout(() => {
      toast.remove();
    }, 4200);
  }

  async function apiRequest(url, options = {}) {
    const response = await fetch(url, options);
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      payload = await response.json();
    } else {
      payload = await response.text();
    }
    if (!response.ok) {
      const detail =
        payload && typeof payload === "object" && payload.detail
          ? payload.detail
          : `HTTP ${response.status}`;
      throw new Error(String(detail));
    }
    return payload;
  }

  function updateHeaderStatus() {
    const modePill = $("mode-pill");
    const aiPill = $("ai-pill");
    const activeMode = state.dashboard?.mode || state.health?.active_mode || "demo";

    if (modePill) {
      const normMode = String(activeMode).toLowerCase();
      let pillCls = "pill-demo";
      let pillLabel = "Demo Mode";
      if (normMode.includes("mono")) {
        pillCls = "pill-live";
        pillLabel = "Monobank Live";
      } else if (normMode === "csv") {
        pillCls = "pill-csv";
        pillLabel = "CSV Import";
      }
      modePill.className = `status-pill ${pillCls}`;
      const label = modePill.querySelector(".pill-label");
      if (label) {
        label.textContent = pillLabel;
      }
    }

    if (aiPill) {
      const hasSessionGemini = Boolean(getSessionKey(SESSION_KEYS.GEMINI));
      const serverGemini = Boolean(state.health?.gemini_configured);
      const isAiLive = serverGemini || hasSessionGemini;
      aiPill.className = `status-pill ${isAiLive ? "pill-live" : "pill-fallback"}`;
      const label = aiPill.querySelector(".pill-label");
      if (label) {
        label.textContent = isAiLive ? "AI Live" : "Local Tools";
      }
    }
  }

  function renderCurrencyTicker(rates) {
    const ticker = $("currency-ticker");
    if (!ticker) return;
    const list = Array.isArray(rates) ? rates : [];
    const findRate = (code) =>
      list.find(
        (r) =>
          (r.currency_a === code && r.currency_b === "UAH") ||
          String(r.pair || "").toUpperCase().startsWith(`${code}/UAH`)
      );

    const formatPair = (code, entry) => {
      if (!entry) return `<span class="ticker-item"><span class="ticker-ccy">${code}</span> <span class="mono-num">— / —</span></span>`;
      if (entry.rate_buy && entry.rate_sell) {
        return `<span class="ticker-item"><span class="ticker-ccy">${escapeHtml(code)}</span> <span class="mono-num">${formatNumber(entry.rate_buy, 2)} / ${formatNumber(entry.rate_sell, 2)}</span></span>`;
      }
      if (entry.rate_cross) {
        return `<span class="ticker-item"><span class="ticker-ccy">${escapeHtml(code)}</span> <span class="mono-num">${formatNumber(entry.rate_cross, 2)}</span></span>`;
      }
      return `<span class="ticker-item"><span class="ticker-ccy">${escapeHtml(code)}</span> <span class="mono-num">— / —</span></span>`;
    };

    const usd = findRate("USD");
    const eur = findRate("EUR");
    ticker.innerHTML = `${formatPair("USD", usd)}<span class="ticker-sep"></span>${formatPair("EUR", eur)}`;
  }

  function renderKpis(data) {
    $("kpi-expense-val").textContent = formatUAH(data.total_expense_uah);
    $("kpi-expense-sub").textContent = `${data.total_transactions ?? 0} транзакцій за ${data.period_days ?? state.days} дн.`;

    $("kpi-income-val").textContent = formatUAH(data.total_income_uah, true);
    $("kpi-income-sub").textContent = `Кешбек: +${formatUAH(data.total_cashback_uah)}`;

    const netVal = Number(data.net_cashflow_uah || 0);
    const netEl = $("kpi-net-val");
    netEl.textContent = formatUAH(netVal, true);
    netEl.className = `kpi-value mono-num ${netVal >= 0 ? "kpi-positive" : "kpi-negative"}`;
    $("kpi-net-sub").textContent = `Баланс картки: ${formatUAH(data.current_balance_uah)}`;

    const forecast = data.forecast || {};
    $("kpi-forecast-val").textContent = formatUAH(forecast.projected_month_total_uah);
    const statusMeta = FORECAST_LABELS[forecast.status] || FORECAST_LABELS.on_track;
    const badge = $("kpi-forecast-badge");
    badge.textContent = statusMeta.text;
    badge.className = `forecast-badge ${statusMeta.cls}`;
    $("kpi-forecast-sub").textContent =
      `Ліміт ${formatNumber(forecast.total_monthly_budget_uah, 0)} ₴ · ${formatNumber(forecast.safe_daily_allowance_uah, 0)} ₴/день`;

    const modeLabel =
      data.mode === "monobank"
        ? "Monobank Live"
        : data.mode === "csv"
          ? "CSV Import"
          : "Demo";
    $("dashboard-meta").textContent =
      `Режим: ${modeLabel} · ${data.total_transactions} операцій за останні ${data.period_days} днів`;
  }

  function renderCategoriesChart(categories) {
    const canvas = $("categories-chart");
    const fallback = $("categories-empty");
    const chip = $("categories-total-chip");
    const list = Array.isArray(categories) ? categories : [];

    if (chip) {
      chip.textContent = `${list.length} категорій`;
    }

    if (state.categoriesChart) {
      state.categoriesChart.destroy();
      state.categoriesChart = null;
    }

    if (list.length === 0 || typeof window.Chart === "undefined") {
      if (canvas) canvas.hidden = true;
      if (fallback) {
        fallback.hidden = false;
        fallback.innerHTML =
          list.length === 0
            ? `<p class="empty-state">Немає витрат за обраний період.</p>`
            : list
                .map(
                  (c) =>
                    `<div class="insight-row" style="padding:4px 0;"><span>${escapeHtml(c.category)}</span><span class="mono-num">${formatUAH(c.total_uah)} (${formatNumber(c.share_pct, 1)}%)</span></div>`
                )
                .join("");
      }
      return;
    }

    if (canvas) canvas.hidden = false;
    if (fallback) fallback.hidden = true;

    const topItems = list.slice(0, 8);
    const ctx = canvas.getContext("2d");
    state.categoriesChart = new window.Chart(ctx, {
      type: "doughnut",
      data: {
        labels: topItems.map((c) => c.category),
        datasets: [
          {
            data: topItems.map((c) => Number(c.total_uah || 0)),
            backgroundColor: topItems.map((_, idx) => CHART_PALETTE[idx % CHART_PALETTE.length]),
            borderColor: "#141821",
            borderWidth: 2,
            hoverOffset: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "64%",
        plugins: {
          legend: {
            position: "right",
            labels: {
              color: "#cbd5e1",
              boxWidth: 11,
              padding: 10,
              font: { size: 11, family: "Inter, sans-serif" },
            },
          },
          tooltip: {
            backgroundColor: "#0b0d12",
            borderColor: "rgba(255,255,255,0.12)",
            borderWidth: 1,
            callbacks: {
              label: (item) => {
                const cat = topItems[item.dataIndex];
                return ` ${cat.category}: ${formatUAH(cat.total_uah)} (${formatNumber(cat.share_pct, 1)}%)`;
              },
            },
          },
        },
      },
    });
  }

  function renderDailyChart(dailySeries) {
    const canvas = $("daily-chart");
    const fallback = $("daily-empty");
    const list = Array.isArray(dailySeries) ? dailySeries : [];

    if (state.dailyChart) {
      state.dailyChart.destroy();
      state.dailyChart = null;
    }

    if (list.length === 0 || typeof window.Chart === "undefined") {
      if (canvas) canvas.hidden = true;
      if (fallback) {
        fallback.hidden = false;
        fallback.innerHTML =
          list.length === 0
            ? `<p class="empty-state">Немає даних по днях за обраний період.</p>`
            : list
                .slice(-10)
                .map(
                  (d) =>
                    `<div class="insight-row" style="padding:4px 0;"><span class="mono-num">${escapeHtml(d.date)}</span><span class="mono-num">-${formatNumber(d.expense_uah, 0)} / +${formatNumber(d.income_uah, 0)} ₴</span></div>`
                )
                .join("");
      }
      return;
    }

    if (canvas) canvas.hidden = false;
    if (fallback) fallback.hidden = true;

    const ctx = canvas.getContext("2d");
    state.dailyChart = new window.Chart(ctx, {
      type: "bar",
      data: {
        labels: list.map((d) => String(d.date).slice(5)),
        datasets: [
          {
            label: "Витрати",
            data: list.map((d) => Number(d.expense_uah || 0)),
            backgroundColor: "rgba(250, 82, 85, 0.85)",
            borderRadius: 4,
            maxBarThickness: 18,
          },
          {
            label: "Доходи",
            data: list.map((d) => Number(d.income_uah || 0)),
            backgroundColor: "rgba(34, 197, 94, 0.8)",
            borderRadius: 4,
            maxBarThickness: 18,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: "index",
          intersect: false,
        },
        scales: {
          x: {
            ticks: { color: "#9aa4b2", font: { size: 10, family: "JetBrains Mono, monospace" } },
            grid: { display: false },
          },
          y: {
            beginAtZero: true,
            ticks: {
              color: "#9aa4b2",
              font: { size: 10, family: "JetBrains Mono, monospace" },
              callback: (val) => `${val} ₴`,
            },
            grid: { color: "rgba(255,255,255,0.06)" },
          },
        },
        plugins: {
          legend: {
            labels: {
              color: "#cbd5e1",
              boxWidth: 11,
              font: { size: 11, family: "Inter, sans-serif" },
            },
          },
          tooltip: {
            backgroundColor: "#0b0d12",
            borderColor: "rgba(255,255,255,0.12)",
            borderWidth: 1,
            callbacks: {
              label: (ctxItem) => ` ${ctxItem.dataset.label}: ${formatUAH(ctxItem.raw)}`,
            },
          },
        },
      },
    });
  }

  function renderSubscriptions(subscriptions) {
    const container = $("subscriptions-list");
    const countEl = $("subscriptions-count");
    const list = Array.isArray(subscriptions) ? subscriptions : [];
    if (countEl) countEl.textContent = String(list.length);
    if (!container) return;

    if (list.length === 0) {
      container.innerHTML = `<p class="empty-state">Регулярних підписок не виявлено.</p>`;
      return;
    }

    container.innerHTML = list
      .map(
        (sub) => `
          <div class="insight-item">
            <div class="insight-row">
              <span class="insight-title">${escapeHtml(sub.merchant)}</span>
              <span class="insight-amount mono-num">${formatUAH(sub.avg_amount_uah)}/міс</span>
            </div>
            <div class="insight-meta">
              <span>${escapeHtml(sub.category)} · ${sub.occurrences} списань</span>
              <span class="mono-num">Рік: ${formatUAH(sub.annual_cost_uah)} · Наст: ${escapeHtml(sub.next_expected_date)}</span>
            </div>
          </div>
        `
      )
      .join("");
  }

  function renderAnomalies(anomalies) {
    const container = $("anomalies-list");
    const countEl = $("anomalies-count");
    const list = Array.isArray(anomalies) ? anomalies : [];
    if (countEl) countEl.textContent = String(list.length);
    if (!container) return;

    if (list.length === 0) {
      container.innerHTML = `<p class="empty-state">Аномально великих витрат не виявлено.</p>`;
      return;
    }

    container.innerHTML = list
      .map(
        (item) => `
          <div class="insight-item">
            <div class="insight-row">
              <span class="insight-title">${escapeHtml(item.description)}</span>
              <span class="insight-amount kpi-negative mono-num">${formatUAH(item.amount_uah)}</span>
            </div>
            <div class="insight-meta">
              <span>${escapeHtml(item.tx_date)} · Сер. по категорії: ${formatUAH(item.category_avg_uah)}</span>
              <span class="ratio-pill mono-num">${formatNumber(item.ratio_to_avg, 1)}× від середнього</span>
            </div>
            <div class="insight-meta">
              <span>${escapeHtml(item.reason)}</span>
            </div>
          </div>
        `
      )
      .join("");
  }

  function renderTransactions(transactions) {
    const tbody = $("transactions-tbody");
    const countEl = $("transactions-count");
    const list = Array.isArray(transactions) ? transactions : [];
    if (countEl) countEl.textContent = String(list.length);
    if (!tbody) return;

    if (list.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">Транзакцій поки немає.</td></tr>`;
      return;
    }

    tbody.innerHTML = list
      .map((tx) => {
        const isExpense = Boolean(tx.is_expense);
        const amountClass = isExpense ? "kpi-negative" : "kpi-positive";
        const signedAmount = isExpense
          ? `-${formatUAH(Math.abs(Number(tx.amount_uah || 0)))}`
          : formatUAH(Math.abs(Number(tx.amount_uah || 0)), true);
        const cashbackHtml =
          Number(tx.cashback_uah || 0) > 0
            ? `<span class="tx-cashback mono-num">+${formatUAH(tx.cashback_uah)} кешбек</span>`
            : "";

        return `
          <tr>
            <td class="mono-num">${escapeHtml(tx.tx_date)}</td>
            <td>
              <div>${escapeHtml(tx.description)}</div>
              ${tx.comment ? `<div class="kpi-sub">${escapeHtml(tx.comment)}</div>` : ""}
            </td>
            <td><span class="category-pill">${escapeHtml(tx.category)}</span></td>
            <td class="align-right">
              <span class="mono-num ${amountClass}">${signedAmount}</span>
              ${cashbackHtml}
            </td>
          </tr>
        `;
      })
      .join("");
  }

  async function loadHealth() {
    try {
      state.health = await apiRequest("/api/health");
      updateHeaderStatus();
    } catch (err) {
      showToast(`Помилка health-check: ${err.message}`, "err");
    }
  }

  async function loadDashboard() {
    try {
      const data = await apiRequest(`/api/dashboard?days=${encodeURIComponent(state.days)}`);
      state.dashboard = data;
      updateHeaderStatus();
      renderCurrencyTicker(data.currency_rates);
      renderKpis(data);
      renderCategoriesChart(data.categories);
      renderDailyChart(data.daily_series);
      renderSubscriptions(data.subscriptions);
      renderAnomalies(data.anomalies);
      renderBudgets();
    } catch (err) {
      showToast(`Не вдалося оновити дашборд: ${err.message}`, "err");
    }
  }

  function renderBudgets() {
    const container = $("budgets-grid");
    const totalChip = $("budgets-total-chip");
    if (!container) return;

    const budgetMap = new Map(
      (Array.isArray(state.budgets) ? state.budgets : []).map((b) => [
        b.category,
        Number(b.monthly_limit_uah || 0),
      ])
    );
    const spendMap = new Map(
      (Array.isArray(state.dashboard?.categories) ? state.dashboard.categories : []).map((c) => [
        c.category,
        Number(c.total_uah || 0),
      ])
    );

    const allCats = Array.from(new Set([...spendMap.keys(), ...budgetMap.keys()]));
    allCats.sort((a, b) => (spendMap.get(b) || 0) - (spendMap.get(a) || 0));

    const totalBudget = Array.from(budgetMap.values()).reduce((acc, v) => acc + v, 0);
    if (totalChip) {
      totalChip.textContent = `Разом: ${formatNumber(totalBudget, 0)} ₴/міс`;
    }

    if (allCats.length === 0) {
      container.innerHTML = `<p class="empty-state">Немає налаштованих бюджетів.</p>`;
      return;
    }

    container.innerHTML = allCats
      .slice(0, 8)
      .map((cat) => {
        const spent = spendMap.get(cat) || 0;
        const limit = budgetMap.has(cat) ? budgetMap.get(cat) : 0;
        const pct = limit > 0 ? (spent / limit) * 100 : 0;
        const widthPct = Math.min(100, Math.max(0, pct));
        const fillCls =
          pct > 100 ? "budget-fill-over" : pct >= 85 ? "budget-fill-warn" : "budget-fill-ok";
        const pctText = limit > 0 ? `${formatNumber(pct, 0)}%` : "без ліміту";

        return `
          <div class="budget-item" data-category="${escapeHtml(cat)}">
            <div class="budget-top-row">
              <span class="budget-cat-name">${escapeHtml(cat)}</span>
              <span class="budget-pct mono-num">${pctText}</span>
            </div>
            <div class="budget-bar-track">
              <div class="budget-bar-fill ${fillCls}" style="width: ${widthPct.toFixed(1)}%"></div>
            </div>
            <div class="budget-bottom-row">
              <span class="mono-num">Витрачено: ${formatNumber(spent, 0)} ₴</span>
              <div class="budget-edit-wrap">
                <input
                  type="number"
                  min="0"
                  step="100"
                  class="budget-limit-input"
                  value="${Number.isFinite(limit) ? Math.round(limit) : 0}"
                  aria-label="Місячний ліміт для ${escapeHtml(cat)}"
                />
                <button type="button" class="btn btn-secondary btn-sm budget-save-btn">OK</button>
              </div>
            </div>
          </div>
        `;
      })
      .join("");
  }

  async function loadBudgets() {
    try {
      state.budgets = await apiRequest("/api/budgets");
      renderBudgets();
    } catch (err) {
      showToast(`Не вдалося завантажити бюджети: ${err.message}`, "err");
    }
  }

  async function saveCategoryBudget(category, limitValue) {
    const numeric = Number(limitValue);
    if (!Number.isFinite(numeric) || numeric < 0) {
      showToast("Вкажіть коректний невід'ємний ліміт бюджету.", "err");
      return;
    }
    try {
      state.budgets = await apiRequest("/api/budgets", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          category,
          monthly_limit_uah: numeric,
        }),
      });
      await loadDashboard();
      renderBudgets();
      showToast(`Ліміт «${category}» оновлено: ${formatNumber(numeric, 0)} ₴/міс`, "ok");
    } catch (err) {
      showToast(`Помилка оновлення бюджету: ${err.message}`, "err");
    }
  }

  async function loadTransactions() {
    try {
      const rows = await apiRequest("/api/transactions?limit=50&offset=0");
      renderTransactions(rows);
    } catch (err) {
      showToast(`Не вдалося завантажити транзакції: ${err.message}`, "err");
    }
  }

  async function refreshAll() {
    await Promise.all([loadHealth(), loadDashboard(), loadBudgets(), loadTransactions()]);
    renderBudgets();
  }

  function renderToolTracesHtml(data) {
    const traces = Array.isArray(data?.tool_traces) ? data.tool_traces : [];
    const latency = Math.round(Number(data?.total_latency_ms || 0));
    const modelName = escapeHtml(data?.model || "MonoPilot");
    const fallbackBadge = data?.used_fallback
      ? `<span class="trace-badge trace-badge-warn">Fallback режим</span>`
      : `<span class="trace-badge trace-badge-ok">Gemini Tool Loop</span>`;

    const traceCards =
      traces.length === 0
        ? `<p class="empty-state">Інструменти не викликались для цієї відповіді.</p>`
        : traces
            .map((trace) => {
              const statusCls = trace.status === "ok" ? "trace-badge-ok" : "trace-badge-err";
              const sqlSection = trace.sql_query
                ? `
                  <div>
                    <div class="trace-section-label">SQL-запит (Read-Only):</div>
                    <pre class="trace-sql"><code>${escapeHtml(trace.sql_query)}</code></pre>
                  </div>
                `
                : "";

              const argsJson = escapeHtml(JSON.stringify(trace.arguments || {}, null, 2));
              const resultJson = escapeHtml(formatPreview(trace.result_preview));

              return `
                <div class="trace-card">
                  <div class="trace-card-header">
                    <span class="trace-tool-name">${escapeHtml(trace.tool_name)}</span>
                    <div class="tool-inspector-meta">
                      <span class="trace-badge">${Number(trace.duration_ms || 0).toFixed(1)} ms</span>
                      <span class="trace-badge ${statusCls}">${escapeHtml(trace.status || "ok")}</span>
                    </div>
                  </div>
                  ${sqlSection}
                  <div>
                    <div class="trace-section-label">Аргументи:</div>
                    <pre class="trace-json"><code>${argsJson}</code></pre>
                  </div>
                  <div>
                    <div class="trace-section-label">Результат:</div>
                    <pre class="trace-json"><code>${resultJson}</code></pre>
                  </div>
                </div>
              `;
            })
            .join("");

    return `
      <details class="tool-inspector">
        <summary>
          <span class="tool-inspector-title">🔍 Під капотом (Tool Calls &amp; SQL)</span>
          <div class="tool-inspector-meta">
            <span class="trace-badge">${traces.length} tools</span>
            <span class="trace-badge">${latency} ms</span>
            <span class="trace-badge">${modelName}</span>
            ${fallbackBadge}
          </div>
        </summary>
        <div class="tool-inspector-body">
          ${traceCards}
        </div>
      </details>
    `;
  }

  function appendChatMessage(role, content, chatResponseData = null) {
    const container = $("chat-messages");
    if (!container) return;

    const row = document.createElement("div");
    row.className = `msg-row ${role === "user" ? "msg-user" : "msg-assistant"}`;

    const roleLabel = document.createElement("div");
    roleLabel.className = "msg-role";
    if (role === "assistant" && chatResponseData?.model) {
      const rowHeader = document.createElement("div");
      rowHeader.className = "msg-role-row";
      const nameSpan = document.createElement("span");
      nameSpan.textContent = "MonoPilot AI";
      const modelPill = document.createElement("span");
      modelPill.className = `msg-model-pill mono-num${chatResponseData.used_fallback ? " is-fallback" : ""}`;
      modelPill.textContent = chatResponseData.used_fallback
        ? `Fallback · ${chatResponseData.model}`
        : chatResponseData.model;
      rowHeader.appendChild(nameSpan);
      rowHeader.appendChild(modelPill);
      roleLabel.appendChild(rowHeader);
    } else {
      roleLabel.textContent = role === "user" ? "Ви" : "MonoPilot AI";
    }

    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    if (role === "user") {
      bubble.innerHTML = `<p>${escapeHtml(content).replaceAll("\n", "<br>")}</p>`;
    } else {
      bubble.innerHTML = renderMarkdownSafe(content);
    }

    row.appendChild(roleLabel);
    row.appendChild(bubble);

    if (role === "assistant" && chatResponseData) {
      const traceWrapper = document.createElement("div");
      traceWrapper.innerHTML = renderToolTracesHtml(chatResponseData);
      if (traceWrapper.firstElementChild) {
        row.appendChild(traceWrapper.firstElementChild);
      }
    }

    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
  }

  function seedWelcomeMessage() {
    const container = $("chat-messages");
    if (!container) return;
    container.innerHTML = "";
    appendChatMessage(
      "assistant",
      "Привіт! Я **MonoPilot AI** — персональний фінансовий копілот.\n\nЗапитайте мене про ваші витрати, підписки, аномалії чи прогноз бюджету, або оберіть готовий сценарій вище. Під кожною відповіддю відкривається інспектор **«Під капотом»** із реальними SQL-запитами та викликами інструментів."
    );
  }

  function setSendingState(sending) {
    state.isSending = sending;
    const btn = $("chat-send-btn");
    const input = $("chat-input");
    if (btn) {
      btn.disabled = sending;
      const label = btn.querySelector(".send-label");
      const spinner = btn.querySelector(".spinner");
      if (label) label.textContent = sending ? "Аналіз..." : "Надіслати";
      if (spinner) spinner.hidden = !sending;
    }
    if (input) {
      input.disabled = sending;
    }
  }

  async function sendChatMessage(rawPrompt) {
    const message = (rawPrompt || "").trim();
    if (!message || state.isSending) return;

    appendChatMessage("user", message);
    const input = $("chat-input");
    if (input) input.value = "";

    const historySnapshot = state.history.slice(-10);
    setSendingState(true);

    try {
      const headers = { "Content-Type": "application/json" };
      const geminiKey = getSessionKey(SESSION_KEYS.GEMINI);
      if (geminiKey) {
        headers["X-Gemini-Key"] = geminiKey;
      }

      const data = await apiRequest("/api/chat", {
        method: "POST",
        headers,
        body: JSON.stringify({
          message,
          history: historySnapshot,
          days_context: state.days,
        }),
      });

      appendChatMessage("assistant", data.answer || "Отримано порожню відповідь.", data);
      state.history.push({ role: "user", content: message });
      state.history.push({ role: "assistant", content: data.answer || "" });

      if (data.used_fallback === false) {
        const aiPill = $("ai-pill");
        if (aiPill) {
          aiPill.className = "status-pill pill-live";
          const label = aiPill.querySelector(".pill-label");
          if (label) label.textContent = "AI Live";
        }
      }
    } catch (err) {
      appendChatMessage(
        "assistant",
        `Не вдалося отримати відповідь від агента: \`${err.message}\``
      );
      showToast(`Помилка чату: ${err.message}`, "err");
    } finally {
      setSendingState(false);
      if (input) input.focus();
    }
  }

  function setModalFeedback(message, isError = false) {
    const box = $("modal-feedback");
    if (!box) return;
    if (!message) {
      box.hidden = true;
      box.textContent = "";
      box.className = "modal-feedback";
      return;
    }
    box.hidden = false;
    box.textContent = message;
    box.className = `modal-feedback ${isError ? "err" : "ok"}`;
  }

  function openKeysModal() {
    const modal = $("keys-modal");
    if (!modal) return;
    $("gemini-key-input").value = getSessionKey(SESSION_KEYS.GEMINI);
    $("mono-token-input").value = getSessionKey(SESSION_KEYS.MONO);
    setModalFeedback("");
    modal.hidden = false;
  }

  function closeKeysModal() {
    const modal = $("keys-modal");
    if (modal) modal.hidden = true;
  }

  function bindEvents() {
    // Period pills
    document.querySelectorAll(".period-pill").forEach((pill) => {
      pill.addEventListener("click", () => {
        const days = Number(pill.getAttribute("data-days")) || 30;
        if (days === state.days) return;
        state.days = days;
        document.querySelectorAll(".period-pill").forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");
        const indicator = $("chat-context-indicator");
        if (indicator) indicator.textContent = `Контекст: ${days}д`;
        loadDashboard();
      });
    });

    // Starter prompt chips
    document.querySelectorAll(".prompt-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const prompt = chip.getAttribute("data-prompt") || chip.textContent;
        sendChatMessage(prompt);
      });
    });

    // Chat form submit & Enter handling
    const chatForm = $("chat-form");
    const chatInput = $("chat-input");
    if (chatForm) {
      chatForm.addEventListener("submit", (e) => {
        e.preventDefault();
        sendChatMessage(chatInput?.value);
      });
    }
    if (chatInput) {
      chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          sendChatMessage(chatInput.value);
        }
      });
    }

    // Clear chat
    $("clear-chat-btn")?.addEventListener("click", () => {
      state.history = [];
      seedWelcomeMessage();
    });

    // Reset Demo
    $("reset-demo-btn")?.addEventListener("click", async () => {
      const btn = $("reset-demo-btn");
      if (btn) btn.disabled = true;
      try {
        const res = await apiRequest("/api/data/demo", { method: "POST" });
        showToast(res.message || `Завантажено ${res.total_in_db} демо-транзакцій`, "ok");
        await refreshAll();
      } catch (err) {
        showToast(`Помилка скидання demo: ${err.message}`, "err");
      } finally {
        if (btn) btn.disabled = false;
      }
    });

    // Modal controls
    $("open-keys-btn")?.addEventListener("click", openKeysModal);
    $("close-modal-btn")?.addEventListener("click", closeKeysModal);
    $("close-modal-x")?.addEventListener("click", closeKeysModal);

    $("keys-modal")?.addEventListener("click", (e) => {
      if (e.target === $("keys-modal")) {
        closeKeysModal();
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("keys-modal")?.hidden) {
        closeKeysModal();
      }
    });

    document.querySelectorAll(".toggle-vis-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const targetId = btn.getAttribute("data-target");
        const input = targetId ? $(targetId) : null;
        if (!input) return;
        const isPass = input.type === "password";
        input.type = isPass ? "text" : "password";
        btn.textContent = isPass ? "Сховати" : "Показати";
      });
    });

    $("save-keys-btn")?.addEventListener("click", () => {
      setSessionKey(SESSION_KEYS.GEMINI, $("gemini-key-input")?.value);
      setSessionKey(SESSION_KEYS.MONO, $("mono-token-input")?.value);
      updateHeaderStatus();
      setModalFeedback("Ключі збережено в sessionStorage поточної вкладки.");
      showToast("Налаштування ключів оновлено", "ok");
    });

    $("clear-keys-btn")?.addEventListener("click", () => {
      clearSessionKeys();
      $("gemini-key-input").value = "";
      $("mono-token-input").value = "";
      updateHeaderStatus();
      setModalFeedback("Ключі видалено з sessionStorage.");
    });

    $("sync-mono-btn")?.addEventListener("click", async () => {
      const monoToken = ($("mono-token-input")?.value || "").trim();
      setSessionKey(SESSION_KEYS.GEMINI, $("gemini-key-input")?.value);
      setSessionKey(SESSION_KEYS.MONO, monoToken);
      updateHeaderStatus();

      const daysRaw = Number($("mono-days-input")?.value || 31);
      const syncDays = Math.min(31, Math.max(1, Number.isFinite(daysRaw) ? Math.round(daysRaw) : 31));

      const syncBtn = $("sync-mono-btn");
      if (syncBtn) syncBtn.disabled = true;
      setModalFeedback("Синхронізація виписки monobank...");

      try {
        const headers = { "Content-Type": "application/json" };
        if (monoToken) {
          headers["X-Mono-Token"] = monoToken;
        }
        const res = await apiRequest("/api/data/sync-monobank", {
          method: "POST",
          headers,
          body: JSON.stringify({ days: syncDays, account_id: "0" }),
        });
        setModalFeedback(res.message || `Синхронізовано ${res.synced_count} транзакцій.`);
        showToast(res.message || "Monobank виписку синхронізовано!", "ok");
        await refreshAll();
      } catch (err) {
        setModalFeedback(`Помилка синхронізації: ${err.message}`, true);
        showToast(`Monobank sync помилка: ${err.message}`, "err");
      } finally {
        if (syncBtn) syncBtn.disabled = false;
      }
    });

    // CSV Statement Upload
    $("upload-csv-btn")?.addEventListener("click", async () => {
      const fileInput = $("csv-file-input");
      const file = fileInput?.files?.[0];
      if (!file) {
        setModalFeedback("Оберіть CSV-файл виписки для завантаження.", true);
        return;
      }

      const replaceExisting = Boolean($("csv-replace-checkbox")?.checked);
      const uploadBtn = $("upload-csv-btn");
      if (uploadBtn) uploadBtn.disabled = true;
      setModalFeedback("Імпорт CSV-виписки...");

      try {
        const formData = new FormData();
        formData.append("file", file);
        const res = await apiRequest(
          `/api/data/upload-csv?replace_existing=${replaceExisting ? "true" : "false"}`,
          {
            method: "POST",
            body: formData,
          }
        );
        setModalFeedback(res.message || `Імпортовано ${res.synced_count} транзакцій.`);
        showToast(res.message || "CSV-виписку імпортовано!", "ok");
        if (fileInput) fileInput.value = "";
        await refreshAll();
      } catch (err) {
        setModalFeedback(`Помилка імпорту CSV: ${err.message}`, true);
        showToast(`CSV імпорт помилка: ${err.message}`, "err");
      } finally {
        if (uploadBtn) uploadBtn.disabled = false;
      }
    });

    // Interactive category budgets delegation
    const budgetsGrid = $("budgets-grid");
    budgetsGrid?.addEventListener("click", (e) => {
      const btn = e.target.closest(".budget-save-btn");
      if (!btn) return;
      const card = btn.closest(".budget-item");
      const cat = card?.getAttribute("data-category");
      const input = card?.querySelector(".budget-limit-input");
      if (cat && input) {
        btn.disabled = true;
        saveCategoryBudget(cat, input.value).finally(() => {
          btn.disabled = false;
        });
      }
    });

    budgetsGrid?.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const input = e.target.closest(".budget-limit-input");
      if (!input) return;
      e.preventDefault();
      const card = input.closest(".budget-item");
      const cat = card?.getAttribute("data-category");
      if (cat) {
        saveCategoryBudget(cat, input.value);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    seedWelcomeMessage();
    bindEvents();
    refreshAll();
  });
})();
