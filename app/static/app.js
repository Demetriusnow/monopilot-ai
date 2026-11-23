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

  const VALID_TABS = new Set(["home", "analytics", "budgets", "chat"]);

  const state = {
    days: 30,
    mobileTab: "home",
    health: null,
    dashboard: null,
    budgets: [],
    jars: [],
    selectedJarId: "",
    history: [],
    isSending: false,
    categoriesChart: null,
    dailyChart: null,
    deferredInstallPrompt: null,
    whatIfTimer: null,
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
      const trimmed = lines[i].trim();

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

  function buildOptionalGeminiHeaders(baseHeaders = {}) {
    const headers = { ...baseHeaders };
    const geminiKey = getSessionKey(SESSION_KEYS.GEMINI);
    if (geminiKey) {
      headers["X-Gemini-Key"] = geminiKey;
    }
    return headers;
  }

  function setMobileTab(tabName) {
    const nextTab = VALID_TABS.has(tabName) ? tabName : "home";
    state.mobileTab = nextTab;
    document.body.dataset.mobileTab = nextTab;

    document.querySelectorAll(".mobile-nav-item").forEach((item) => {
      const isActive = item.getAttribute("data-tab") === nextTab;
      item.classList.toggle("active", isActive);
    });

    if (nextTab === "analytics") {
      window.requestAnimationFrame(() => {
        if (state.dashboard) {
          renderCategoriesChart(state.dashboard.categories);
          renderDailyChart(state.dashboard.daily_series);
        }
      });
    }
    window.scrollTo({ top: 0, behavior: "instant" });
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

    // Virtual monobank Black Card Hero bindings
    const heroBalance = $("hero-balance-val");
    if (heroBalance) heroBalance.textContent = formatUAH(data.current_balance_uah);
    const heroNet = $("hero-net-pill");
    if (heroNet) heroNet.textContent = `Нетто: ${formatUAH(netVal, true)}`;
    const heroCashback = $("hero-cashback-pill");
    if (heroCashback) heroCashback.textContent = `Кешбек: +${formatUAH(data.total_cashback_uah)}`;
    const heroMode = $("hero-card-mode");
    if (heroMode) {
      heroMode.textContent =
        data.mode === "monobank" ? "LIVE" : data.mode === "csv" ? "CSV" : "DEMO";
    }

    const budgetSummaryStrip = $("budget-summary-strip");
    if (budgetSummaryStrip) {
      budgetSummaryStrip.innerHTML = `
        <span class="mono-num">Місяць: <strong>${escapeHtml(forecast.current_month || "—")}</strong> (день ${forecast.days_elapsed ?? 0} з ${forecast.days_in_month ?? 30})</span>
        <span class="mono-num">Витрачено: <strong>${formatUAH(forecast.spent_so_far_uah)}</strong> · Темп: <strong>${formatNumber(forecast.daily_burn_rate_uah, 0)} ₴/день</strong></span>
        <span class="mono-num">Прогноз: <strong>${formatUAH(forecast.projected_month_total_uah)}</strong></span>
      `;
    }

    const modeLabel =
      data.mode === "monobank"
        ? "Monobank Live"
        : data.mode === "csv"
          ? "CSV Import"
          : "Demo";
    $("dashboard-meta").textContent =
      `Режим: ${modeLabel} · ${data.total_transactions} операцій за останні ${data.period_days} дн.`;
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
            borderColor: "#11151e",
            borderWidth: 2,
            hoverOffset: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "65%",
        plugins: {
          legend: {
            position: window.innerWidth <= 540 ? "bottom" : "right",
            labels: {
              color: "#cbd5e1",
              boxWidth: 10,
              padding: 9,
              font: { size: 11, family: "Inter, sans-serif" },
            },
          },
          tooltip: {
            backgroundColor: "#07090d",
            borderColor: "rgba(255,255,255,0.14)",
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
            backgroundColor: "rgba(34, 197, 94, 0.82)",
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
            ticks: { color: "#98a3b3", font: { size: 10, family: "JetBrains Mono, monospace" } },
            grid: { display: false },
          },
          y: {
            beginAtZero: true,
            ticks: {
              color: "#98a3b3",
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
              boxWidth: 10,
              font: { size: 11, family: "Inter, sans-serif" },
            },
          },
          tooltip: {
            backgroundColor: "#07090d",
            borderColor: "rgba(255,255,255,0.14)",
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

  /* =====================================================================
     Mono Jars + What-If Simulator
     ===================================================================== */

  function syncJarSelectOptions() {
    const select = $("whatif-jar-select");
    if (!select) return;
    const list = Array.isArray(state.jars) ? state.jars : [];

    if (list.length === 0) {
      state.selectedJarId = "";
      select.innerHTML = `<option value="">Загальні накопичення</option>`;
      return;
    }

    const exists = list.some((j) => j.id === state.selectedJarId);
    if (!exists) {
      state.selectedJarId = list[0].id;
    }

    select.innerHTML = list
      .map(
        (jar) =>
          `<option value="${escapeHtml(jar.id)}"${jar.id === state.selectedJarId ? " selected" : ""}>${escapeHtml(jar.icon || "🏺")} ${escapeHtml(jar.name)} (${formatNumber(jar.remaining_uah, 0)} ₴ до цілі)</option>`
      )
      .join("");
  }

  function renderJars() {
    const grid = $("jars-grid");
    if (!grid) return;
    const list = Array.isArray(state.jars) ? state.jars : [];
    syncJarSelectOptions();

    if (list.length === 0) {
      grid.innerHTML = `<p class="empty-state">У вас ще немає Банок. Натисніть «+ Нова Банка», щоб задати фінансову ціль.</p>`;
      return;
    }

    grid.innerHTML = list
      .map((jar) => {
        const pct = Math.min(100, Math.max(0, Number(jar.progress_pct || 0)));
        const isSelected = jar.id === state.selectedJarId;
        let etaText = "Вкажіть внесок";
        if (Number(jar.remaining_uah || 0) <= 0) {
          etaText = "✅ Зібрано!";
        } else if (jar.months_to_goal !== null && jar.months_to_goal !== undefined) {
          etaText = `~${formatNumber(jar.months_to_goal, 1)} міс.`;
        }

        return `
          <div class="jar-card${isSelected ? " is-selected" : ""}" data-jar-id="${escapeHtml(jar.id)}">
            <div class="jar-card-top">
              <div class="jar-title-wrap">
                <span class="jar-emoji">${escapeHtml(jar.icon || "🏺")}</span>
                <span class="jar-name" title="${escapeHtml(jar.name)}">${escapeHtml(jar.name)}</span>
              </div>
              <button
                type="button"
                class="icon-btn delete-jar-btn"
                data-jar-id="${escapeHtml(jar.id)}"
                title="Видалити банку"
                aria-label="Видалити банку ${escapeHtml(jar.name)}"
              >✕</button>
            </div>
            <div class="jar-amounts-row mono-num">
              <span class="jar-current">${formatNumber(jar.current_uah, 0)} ₴</span>
              <span class="jar-target">із ${formatNumber(jar.target_uah, 0)} ₴ (${formatNumber(pct, 0)}%)</span>
            </div>
            <div class="jar-progress-track">
              <div class="jar-progress-fill" style="width: ${pct.toFixed(1)}%"></div>
            </div>
            <div class="jar-card-foot">
              <span class="mono-num">+${formatNumber(jar.monthly_contribution_uah, 0)} ₴/міс</span>
              <span class="jar-badge-eta mono-num">${etaText}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function renderWhatIfResult(sim) {
    const box = $("whatif-result");
    if (!box || !sim) return;

    const baselineMonthly = Number(sim.baseline_monthly_savings_uah || 0);
    const totalNew = Number(sim.total_new_monthly_savings_uah || 0);
    const monthlyBoost = Math.max(0, totalNew - baselineMonthly);
    const annualBoost = Number(sim.annual_savings_boost_uah || 0);
    const catSav = Number(sim.monthly_category_savings_uah || 0);
    const subSav = Number(sim.monthly_subscription_savings_uah || 0);

    let jarLine = `<span>Оберіть або створіть Банку, щоб побачити прискорення досягнення цілі.</span>`;
    if (sim.jar_name) {
      const baseM = sim.baseline_months !== null && sim.baseline_months !== undefined
        ? `${formatNumber(sim.baseline_months, 1)} міс`
        : "∞";
      const simM = sim.simulated_months !== null && sim.simulated_months !== undefined
        ? `${formatNumber(sim.simulated_months, 1)} міс`
        : "∞";
      const savedM = Number(sim.months_saved || 0);
      const savedPill =
        savedM > 0
          ? `<strong> · Швидше на ${formatNumber(savedM, 1)} міс.!</strong>`
          : "";
      jarLine = `<span>🏺 <strong>${escapeHtml(sim.jar_name)}</strong>: <span class="mono-num">${baseM} → ${simM}</span>${savedPill}</span>`;
    }

    box.innerHTML = `
      <div class="whatif-result-top">
        <span class="whatif-boost-main mono-num">+${formatUAH(monthlyBoost)} / міс додатково (разом ${formatUAH(totalNew)}/міс)</span>
        <span class="whatif-annual-pill mono-num">+${formatNumber(annualBoost, 0)} ₴ / рік</span>
      </div>
      <div class="whatif-result-meta">
        ${jarLine}
        <span class="mono-num">Категорії: +${formatNumber(catSav, 0)} ₴ · Підписки: +${formatNumber(subSav, 0)} ₴</span>
      </div>
    `;
  }

  async function simulateWhatIf() {
    const box = $("whatif-result");
    const jarId = $("whatif-jar-select")?.value || null;
    const cafeCut = Number($("cut-cafe-slider")?.value || 0);
    const hobbyCut = Number($("cut-hobby-slider")?.value || 0);
    const extraRaw = Number($("whatif-extra-input")?.value || 0);
    const extraMonthly = Number.isFinite(extraRaw) && extraRaw > 0 ? extraRaw : 0;

    const cancelSubs = Array.from(document.querySelectorAll(".whatif-sub-toggle:checked"))
      .map((cb) => cb.value)
      .filter(Boolean);

    try {
      const sim = await apiRequest("/api/jars/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jar_id: jarId || null,
          category_cuts_pct: {
            "Кафе та ресторани": cafeCut,
            "Покупки та хобі": hobbyCut,
          },
          cancel_subscriptions: cancelSubs,
          extra_monthly_uah: extraMonthly,
        }),
      });
      renderWhatIfResult(sim);
    } catch (err) {
      if (box) {
        box.innerHTML = `<span class="empty-state">Симулятор очікує підключення API (${escapeHtml(err.message)})</span>`;
      }
    }
  }

  function scheduleWhatIfSimulation() {
    if (state.whatIfTimer) {
      window.clearTimeout(state.whatIfTimer);
    }
    state.whatIfTimer = window.setTimeout(() => {
      simulateWhatIf();
    }, 110);
  }

  async function loadJars() {
    try {
      const jars = await apiRequest("/api/jars");
      state.jars = Array.isArray(jars) ? jars : [];
      renderJars();
      await simulateWhatIf();
    } catch (err) {
      const grid = $("jars-grid");
      if (grid) {
        grid.innerHTML = `<p class="empty-state">Не вдалося завантажити Банки: ${escapeHtml(err.message)}</p>`;
      }
    }
  }

  async function createJarFromForm() {
    const nameInput = $("jar-name-input");
    const targetInput = $("jar-target-input");
    const currentInput = $("jar-current-input");
    const monthlyInput = $("jar-monthly-input");
    const iconInput = $("jar-icon-input");
    const saveBtn = $("jar-save-btn");

    const name = (nameInput?.value || "").trim();
    const target = Number(targetInput?.value || 0);
    const current = Math.max(0, Number(currentInput?.value || 0));
    const monthly = Math.max(0, Number(monthlyInput?.value || 0));
    const icon = (iconInput?.value || "🏺").trim() || "🏺";

    if (!name) {
      showToast("Вкажіть назву для нової Банки.", "err");
      return;
    }
    if (!Number.isFinite(target) || target <= 0) {
      showToast("Цільова сума Банки має бути більшою за 0.", "err");
      return;
    }

    if (saveBtn) saveBtn.disabled = true;
    try {
      const updated = await apiRequest("/api/jars", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          icon,
          target_uah: target,
          current_uah: Number.isFinite(current) ? current : 0,
          monthly_contribution_uah: Number.isFinite(monthly) ? monthly : 3000,
        }),
      });
      state.jars = Array.isArray(updated) ? updated : [];
      if (state.jars.length > 0) {
        const newest = state.jars[state.jars.length - 1];
        if (newest?.id) state.selectedJarId = newest.id;
      }
      if (nameInput) nameInput.value = "";
      if (targetInput) targetInput.value = "";
      if (currentInput) currentInput.value = "0";
      const form = $("new-jar-form");
      if (form) form.hidden = true;
      renderJars();
      await simulateWhatIf();
      showToast(`Банку «${name}» створено!`, "ok");
    } catch (err) {
      showToast(`Помилка створення Банки: ${err.message}`, "err");
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function deleteJarById(jarId) {
    if (!jarId) return;
    try {
      const res = await apiRequest(`/api/jars/${encodeURIComponent(jarId)}`, {
        method: "DELETE",
      });
      if (Array.isArray(res)) {
        state.jars = res;
      } else {
        await loadJars();
        return;
      }
      if (state.selectedJarId === jarId) {
        state.selectedJarId = state.jars[0]?.id || "";
      }
      renderJars();
      await simulateWhatIf();
      showToast("Банку видалено", "ok");
    } catch (err) {
      showToast(`Помилка видалення Банки: ${err.message}`, "err");
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

  async function loadBudgets() {
    try {
      state.budgets = await apiRequest("/api/budgets");
      renderBudgets();
    } catch (err) {
      showToast(`Не вдалося завантажити бюджети: ${err.message}`, "err");
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
    await Promise.all([loadHealth(), loadDashboard(), loadBudgets(), loadTransactions(), loadJars()]);
    renderBudgets();
  }

  /* =====================================================================
     Multi-Agent Chat + Live SSE Streaming
     ===================================================================== */

  function renderToolTracesHtml(data) {
    const traces = Array.isArray(data?.tool_traces) ? data.tool_traces : [];
    const pipeline = Array.isArray(data?.agent_pipeline) ? data.agent_pipeline : [];
    const latency = Math.round(Number(data?.total_latency_ms || 0));
    const modelName = escapeHtml(data?.model || "MonoPilot");
    const orchMode = escapeHtml(data?.orchestration_mode || "multi-agent");
    const fallbackBadge = data?.used_fallback
      ? `<span class="trace-badge trace-badge-warn">Fallback режим</span>`
      : `<span class="trace-badge trace-badge-ok">Gemini Tool Loop</span>`;

    const pipelineSection =
      pipeline.length > 0
        ? `
          <div class="agent-pipeline-list">
            <div class="trace-section-label">Multi-Agent Pipeline (${orchMode}):</div>
            ${pipeline
              .map((stage) => {
                const stCls = stage.status === "ok" || stage.status === "done"
                  ? "trace-badge-ok"
                  : stage.status === "blocked" || stage.status === "error"
                    ? "trace-badge-err"
                    : "trace-badge-warn";
                return `
                  <div class="stage-trace-card">
                    <div class="stage-trace-top">
                      <span class="stage-trace-title">${escapeHtml(stage.label || stage.agent_role)}</span>
                      <div class="tool-inspector-meta">
                        <span class="trace-badge">${escapeHtml(stage.agent_role)}</span>
                        <span class="trace-badge">${Number(stage.duration_ms || 0).toFixed(1)} ms</span>
                        <span class="trace-badge ${stCls}">${escapeHtml(stage.status || "ok")}</span>
                      </div>
                    </div>
                    <div class="stage-trace-summary">${escapeHtml(stage.summary || "")}</div>
                  </div>
                `;
              })
              .join("")}
          </div>
        `
        : "";

    const traceCards =
      traces.length === 0
        ? `<p class="empty-state">Інструменти не викликались для цієї відповіді.</p>`
        : traces
            .map((trace) => {
              const statusCls = trace.status === "ok" ? "trace-badge-ok" : "trace-badge-err";
              const sqlSection = trace.sql_query
                ? `
                  <div>
                    <div class="trace-sql-header">
                      <span class="trace-section-label">SQL-запит (Read-Only):</span>
                      <button type="button" class="btn btn-ghost btn-sm copy-sql-btn" data-sql="${escapeHtml(trace.sql_query)}">
                        Копіювати SQL
                      </button>
                    </div>
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

    const agentsBadge = pipeline.length > 0 ? `<span class="trace-badge">${pipeline.length} agents</span>` : "";

    return `
      <details class="tool-inspector">
        <summary>
          <span class="tool-inspector-title">🔍 Під капотом (Multi-Agent Pipeline + Tool Calls &amp; SQL)</span>
          <div class="tool-inspector-meta">
            ${agentsBadge}
            <span class="trace-badge">${traces.length} tools</span>
            <span class="trace-badge">${latency} ms</span>
            <span class="trace-badge">${modelName}</span>
            ${fallbackBadge}
          </div>
        </summary>
        <div class="tool-inspector-body">
          ${pipelineSection}
          ${traceCards}
        </div>
      </details>
    `;
  }

  function buildAssistantHeaderElement(chatResponseData = null) {
    const roleLabel = document.createElement("div");
    roleLabel.className = "msg-role";
    if (chatResponseData?.model) {
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
      roleLabel.textContent = "MonoPilot AI";
    }
    return roleLabel;
  }

  function appendChatMessage(role, content, chatResponseData = null) {
    const container = $("chat-messages");
    if (!container) return;

    const row = document.createElement("div");
    row.className = `msg-row ${role === "user" ? "msg-user" : "msg-assistant"}`;

    if (role === "user") {
      const roleLabel = document.createElement("div");
      roleLabel.className = "msg-role";
      roleLabel.textContent = "Ви";
      row.appendChild(roleLabel);
    } else {
      row.appendChild(buildAssistantHeaderElement(chatResponseData));
    }

    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    if (role === "user") {
      bubble.innerHTML = `<p>${escapeHtml(content).replaceAll("\n", "<br>")}</p>`;
    } else {
      bubble.innerHTML = renderMarkdownSafe(content);
    }

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

  function createLiveStreamingRow() {
    const container = $("chat-messages");
    if (!container) return null;

    const row = document.createElement("div");
    row.className = "msg-row msg-assistant msg-streaming";
    row.innerHTML = `
      <div class="msg-role">
        <div class="msg-role-row">
          <span>MonoPilot AI</span>
          <span class="msg-model-pill mono-num">Multi-Agent Stream</span>
        </div>
      </div>
      <div class="live-pipeline-card">
        <div class="live-stepper" aria-label="Multi-Agent Pipeline">
          <span class="pipeline-step is-active" data-stage="planner">🧠 Планування</span>
          <span class="pipeline-arrow">→</span>
          <span class="pipeline-step" data-stage="tools">🗄️ SQL / Tools</span>
          <span class="pipeline-arrow">→</span>
          <span class="pipeline-step" data-stage="synthesizer">✍️ Синтез</span>
        </div>
        <div class="live-tools-feed"></div>
      </div>
      <div class="msg-bubble live-answer-box">
        <p class="streaming-placeholder">Запуск Multi-Agent оркестратора...</p>
      </div>
    `;

    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
    return row;
  }

  function updateStepperForStage(liveRow, stagePayload) {
    if (!liveRow || !stagePayload) return;
    const role = String(stagePayload.agent_role || stagePayload.stage || "").toLowerCase();
    const status = String(stagePayload.status || "ok").toLowerCase();
    const label = stagePayload.label ? String(stagePayload.label) : "";

    const plannerEl = liveRow.querySelector('[data-stage="planner"]');
    const toolsEl = liveRow.querySelector('[data-stage="tools"]');
    const synthEl = liveRow.querySelector('[data-stage="synthesizer"]');

    if (role === "planner") {
      if (plannerEl) {
        plannerEl.classList.remove("is-active");
        plannerEl.classList.add("is-done");
        if (label) plannerEl.textContent = label;
      }
      if (toolsEl && status !== "blocked") {
        toolsEl.classList.add("is-active");
      }
    } else if (role === "sql_analyst" || role === "risk_auditor" || role === "savings_advisor" || role === "tools") {
      if (plannerEl) {
        plannerEl.classList.remove("is-active");
        plannerEl.classList.add("is-done");
      }
      if (toolsEl) {
        if (status === "running") {
          toolsEl.classList.remove("is-done");
          toolsEl.classList.add("is-active");
        } else {
          toolsEl.classList.remove("is-active");
          toolsEl.classList.add("is-done");
          if (synthEl && status !== "blocked") {
            synthEl.classList.add("is-active");
          }
        }
        if (label) toolsEl.textContent = label;
      }
    } else if (role === "synthesizer") {
      if (plannerEl) {
        plannerEl.classList.remove("is-active");
        plannerEl.classList.add("is-done");
      }
      if (toolsEl) {
        toolsEl.classList.remove("is-active");
        toolsEl.classList.add("is-done");
      }
      if (synthEl) {
        synthEl.classList.remove("is-active");
        synthEl.classList.add("is-done");
        if (label) synthEl.textContent = label;
      }
    }
  }

  function appendLiveToolBadge(liveRow, toolPayload) {
    if (!liveRow || !toolPayload) return;
    const feed = liveRow.querySelector(".live-tools-feed");
    const toolsEl = liveRow.querySelector('[data-stage="tools"]');
    if (toolsEl) {
      toolsEl.classList.add("is-active");
    }
    if (!feed) return;

    const badge = document.createElement("span");
    const ok = String(toolPayload.status || "ok").toLowerCase() === "ok";
    badge.className = `live-tool-badge ${ok ? "is-ok" : "is-err"}`;
    const duration = Number(toolPayload.duration_ms || 0).toFixed(1);
    badge.textContent = `${toolPayload.tool_name || "tool"} • ${duration}ms ${toolPayload.status || "ok"}`;
    feed.appendChild(badge);
  }

  function finalizeLiveStreamingRow(liveRow, chatResponseData) {
    if (!liveRow) return;
    const container = $("chat-messages");
    liveRow.classList.remove("msg-streaming");
    liveRow.innerHTML = "";
    liveRow.appendChild(buildAssistantHeaderElement(chatResponseData));

    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.innerHTML = renderMarkdownSafe(chatResponseData?.answer || "Отримано порожню відповідь.");
    liveRow.appendChild(bubble);

    if (chatResponseData) {
      const traceWrapper = document.createElement("div");
      traceWrapper.innerHTML = renderToolTracesHtml(chatResponseData);
      if (traceWrapper.firstElementChild) {
        liveRow.appendChild(traceWrapper.firstElementChild);
      }
    }

    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }

  function parseSseFrame(rawFrame) {
    let eventName = "message";
    const dataLines = [];
    for (const line of rawFrame.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (dataLines.length === 0) return null;
    const rawData = dataLines.join("\n");
    let parsed = rawData;
    try {
      parsed = JSON.parse(rawData);
    } catch {
      // Keep plain text payload
    }
    return { event: eventName, data: parsed };
  }

  function seedWelcomeMessage() {
    const container = $("chat-messages");
    if (!container) return;
    container.innerHTML = "";
    appendChatMessage(
      "assistant",
      "Привіт! Я **MonoPilot AI v2.0** — Multi-Agent фінансовий копілот.\n\nЗапитайте мене про витрати, підписки, аномалії, прогноз бюджету чи курс валют. Під час аналізу ви побачите етапи агентів у реальному часі (SSE), а під кожною відповіддю відкривається інспектор **«Під капотом»** із Multi-Agent Pipeline та SQL-запитами."
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

    const liveRow = createLiveStreamingRow();
    const requestPayload = {
      message,
      history: historySnapshot,
      days_context: state.days,
    };
    const headers = buildOptionalGeminiHeaders({ "Content-Type": "application/json" });

    let donePayload = null;
    let streamedText = "";
    const streamedStages = [];
    const streamedTools = [];

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers,
        body: JSON.stringify(requestPayload),
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        donePayload = await response.json();
      } else if (response.body && typeof response.body.getReader === "function") {
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        const handleFrame = (frameText) => {
          const parsedFrame = parseSseFrame(frameText);
          if (!parsedFrame) return;
          const { event, data } = parsedFrame;

          if (event === "stage") {
            if (data && typeof data === "object") {
              streamedStages.push(data);
              updateStepperForStage(liveRow, data);
            }
          } else if (event === "tool") {
            if (data && typeof data === "object") {
              streamedTools.push(data);
              appendLiveToolBadge(liveRow, data);
            }
          } else if (event === "delta") {
            const chunk =
              typeof data === "string"
                ? data
                : data?.text ?? data?.delta ?? data?.content ?? "";
            if (chunk) {
              streamedText += String(chunk);
              const answerBox = liveRow?.querySelector(".live-answer-box");
              if (answerBox) {
                answerBox.innerHTML = renderMarkdownSafe(streamedText);
              }
              const chatBox = $("chat-messages");
              if (chatBox) chatBox.scrollTop = chatBox.scrollHeight;
            }
          } else if (event === "done") {
            if (data && typeof data === "object") {
              donePayload = data;
            }
          } else if (event === "error") {
            const detail = data?.detail || data?.message || "SSE stream error";
            throw new Error(String(detail));
          }
        };

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";
          for (const frame of parts) {
            if (frame.trim()) handleFrame(frame);
          }
        }
        buffer += decoder.decode().replaceAll("\r\n", "\n");
        if (buffer.trim()) {
          for (const frame of buffer.split("\n\n")) {
            if (frame.trim()) handleFrame(frame);
          }
        }
      }

      if (!donePayload && streamedText) {
        donePayload = {
          answer: streamedText,
          tool_traces: streamedTools,
          agent_pipeline: streamedStages,
          orchestration_mode: "multi-agent",
          model: state.health?.model || "MonoPilot",
          total_latency_ms: 0,
          mode: state.dashboard?.mode || "demo",
          used_fallback: true,
        };
      }

      if (!donePayload) {
        donePayload = await apiRequest("/api/chat", {
          method: "POST",
          headers,
          body: JSON.stringify(requestPayload),
        });
      }

      finalizeLiveStreamingRow(liveRow, donePayload);
      const finalAnswer = donePayload.answer || "";
      state.history.push({ role: "user", content: message });
      state.history.push({ role: "assistant", content: finalAnswer });

      if (donePayload.used_fallback === false) {
        const aiPill = $("ai-pill");
        if (aiPill) {
          aiPill.className = "status-pill pill-live";
          const label = aiPill.querySelector(".pill-label");
          if (label) label.textContent = "AI Live";
        }
      }
    } catch (streamErr) {
      try {
        const fallbackData = await apiRequest("/api/chat", {
          method: "POST",
          headers,
          body: JSON.stringify(requestPayload),
        });
        finalizeLiveStreamingRow(liveRow, fallbackData);
        state.history.push({ role: "user", content: message });
        state.history.push({ role: "assistant", content: fallbackData.answer || "" });
      } catch (err) {
        if (liveRow) {
          liveRow.remove();
        }
        const errMessage = err?.message || streamErr?.message || "Unknown error";
        appendChatMessage(
          "assistant",
          `Не вдалося отримати відповідь від агента: \`${errMessage}\``
        );
        showToast(`Помилка чату: ${errMessage}`, "err");
      }
    } finally {
      setSendingState(false);
      if (input) input.focus();
    }
  }

  /* =====================================================================
     Keys, Install, Receipt Vision & AI Evals Modals
     ===================================================================== */

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

  function openKeysModal(focusTargetId = null) {
    const modal = $("keys-modal");
    if (!modal) return;
    $("gemini-key-input").value = getSessionKey(SESSION_KEYS.GEMINI);
    $("mono-token-input").value = getSessionKey(SESSION_KEYS.MONO);
    setModalFeedback("");
    modal.hidden = false;
    if (focusTargetId) {
      const targetEl = $(focusTargetId);
      if (targetEl) {
        targetEl.scrollIntoView({ behavior: "smooth", block: "center" });
        targetEl.focus();
      }
    }
  }

  function closeKeysModal() {
    const modal = $("keys-modal");
    if (modal) modal.hidden = true;
  }

  function openInstallModal() {
    const modal = $("install-modal");
    if (!modal) return;
    const triggerBtn = $("trigger-pwa-install-btn");
    const statusBox = $("pwa-install-status");
    const isStandalone =
      window.matchMedia("(display-mode: standalone)").matches ||
      window.navigator.standalone === true;

    if (isStandalone) {
      if (triggerBtn) triggerBtn.hidden = true;
      if (statusBox) {
        statusBox.textContent = "✅ Додаток MonoPilot AI вже відкрито у встановленому standalone-режимі.";
      }
    } else if (state.deferredInstallPrompt) {
      if (triggerBtn) triggerBtn.hidden = false;
      if (statusBox) {
        statusBox.textContent = "🚀 Ваш браузер готовий встановити MonoPilot AI як повноцінний додаток у 1 клік:";
      }
    } else {
      if (triggerBtn) triggerBtn.hidden = true;
      if (statusBox) {
        statusBox.textContent = "Відкрийте сторінку в Chrome (Android) або Safari (iPhone) та дотримуйтесь короткої інструкції нижче:";
      }
    }
    modal.hidden = false;
  }

  function closeInstallModal() {
    const modal = $("install-modal");
    if (modal) modal.hidden = true;
  }

  function setReceiptFeedback(message, isError = false) {
    const box = $("receipt-feedback");
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

  function openReceiptModal() {
    const modal = $("receipt-modal");
    if (!modal) return;
    setReceiptFeedback("");
    modal.hidden = false;
  }

  function closeReceiptModal() {
    const modal = $("receipt-modal");
    if (modal) modal.hidden = true;
  }

  function renderReceiptResult(res) {
    const box = $("receipt-result-box");
    if (!box || !res) return;
    box.hidden = false;

    const items = Array.isArray(res.items) ? res.items : [];
    let matchBlock = `<div class="receipt-match-badge is-unmatched">ℹ️ Прямого збігу з транзакцією у SQLite не знайдено.</div>`;
    if (res.matched_transaction) {
      const m = res.matched_transaction;
      const confPct = Math.round(Number(m.confidence || 0) * 100);
      matchBlock = `
        <div class="receipt-match-badge is-matched">
          ✅ <strong>Звірено з SQLite (${confPct}%):</strong> ${escapeHtml(m.description)} ·
          <span class="mono-num">${escapeHtml(m.tx_date)} · ${formatUAH(Math.abs(Number(m.amount_uah || 0)))}</span>
          <div style="margin-top:3px;">${escapeHtml(m.reason || "")}</div>
        </div>
      `;
    } else if (res.added_transaction_id) {
      matchBlock = `
        <div class="receipt-match-badge is-added">
          ➕ <strong>Додано транзакцію у SQLite:</strong> ID <code>${escapeHtml(res.added_transaction_id)}</code>
        </div>
      `;
    }

    const itemsRows =
      items.length === 0
        ? `<tr><td colspan="3" class="empty-cell">Позиції чека не розпізнано.</td></tr>`
        : items
            .map(
              (item) => `
                <tr>
                  <td>${escapeHtml(item.name)}</td>
                  <td class="mono-num">${formatNumber(item.quantity ?? 1, 2)}</td>
                  <td class="mono-num align-right">${formatUAH(item.price_uah)}</td>
                </tr>
              `
            )
            .join("");

    box.innerHTML = `
      <div class="receipt-header-row">
        <span class="receipt-merchant">${escapeHtml(res.merchant || "Чек")}</span>
        <span class="receipt-total mono-num">${formatUAH(res.total_uah)}</span>
      </div>
      <div class="receipt-meta-row">
        <span class="category-pill">${escapeHtml(res.category || "Інше")}</span>
        <span class="mono-num">📅 ${escapeHtml(res.receipt_date || "—")}</span>
        <span class="trace-badge">${escapeHtml(res.model || "vision")}</span>
        <span class="trace-badge">${Math.round(Number(res.latency_ms || 0))} ms</span>
        ${res.used_fallback ? `<span class="trace-badge trace-badge-warn">Demo/Fallback</span>` : `<span class="trace-badge trace-badge-ok">Gemini Vision</span>`}
      </div>
      ${matchBlock}
      <div class="receipt-items-wrap">
        <table class="receipt-items-table">
          <thead>
            <tr>
              <th>Позиція</th>
              <th>К-сть</th>
              <th class="align-right">Сума</th>
            </tr>
          </thead>
          <tbody>${itemsRows}</tbody>
        </table>
      </div>
    `;
  }

  async function runReceiptScan(isDemo) {
    const demoBtn = $("scan-demo-receipt-btn");
    const photoBtn = $("scan-receipt-btn");
    const autoAdd = Boolean($("receipt-auto-add")?.checked);
    const fileInput = $("receipt-file-input");

    if (!isDemo && !fileInput?.files?.[0]) {
      setReceiptFeedback("Оберіть фото чека або натисніть «✨ Демо-чек Сільпо».", true);
      return;
    }

    if (demoBtn) demoBtn.disabled = true;
    if (photoBtn) photoBtn.disabled = true;
    setReceiptFeedback(isDemo ? "Сканування демо-чека Сільпо..." : "Аналіз фото чека через Vision AI...");

    try {
      const headers = buildOptionalGeminiHeaders();
      let res;
      if (isDemo) {
        res = await apiRequest(`/api/receipts/scan-demo?auto_add=${autoAdd ? "true" : "false"}`, {
          method: "POST",
          headers,
        });
      } else {
        const formData = new FormData();
        formData.append("file", fileInput.files[0]);
        res = await apiRequest(`/api/receipts/scan?auto_add=${autoAdd ? "true" : "false"}`, {
          method: "POST",
          headers,
          body: formData,
        });
      }

      renderReceiptResult(res);
      setReceiptFeedback(`Чек «${res.merchant}» успішно розпізнано (${formatUAH(res.total_uah)}).`);
      if (res.added_transaction_id) {
        showToast(`Транзакцію з чека додано в SQLite (${formatUAH(res.total_uah)})`, "ok");
        await refreshAll();
      }
    } catch (err) {
      setReceiptFeedback(`Помилка сканування чека: ${err.message}`, true);
      showToast(`Чек AI помилка: ${err.message}`, "err");
    } finally {
      if (demoBtn) demoBtn.disabled = false;
      if (photoBtn) photoBtn.disabled = false;
    }
  }

  function renderEvalReport(report) {
    const numEl = $("eval-numeric-pct");
    const toolEl = $("eval-tool-pct");
    const secEl = $("eval-security-pct");
    const msEl = $("eval-avg-ms");
    const metaEl = $("eval-run-meta");
    const tbody = $("evals-tbody");

    if (!report) {
      if (numEl) numEl.textContent = "—";
      if (toolEl) toolEl.textContent = "—";
      if (secEl) secEl.textContent = "—";
      if (msEl) msEl.textContent = "—";
      if (metaEl) metaEl.textContent = "Ще немає збережених прогонів. Натисніть «▶ Запустити 20 тестів».";
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-cell">Немає збережених результатів. Натисніть запуск для перевірки 20 кейсів.</td></tr>`;
      }
      return;
    }

    if (numEl) numEl.textContent = `${formatNumber(report.numeric_accuracy_pct, 1)}%`;
    if (toolEl) toolEl.textContent = `${formatNumber(report.tool_accuracy_pct, 1)}%`;
    if (secEl) secEl.textContent = `${formatNumber(report.security_block_rate_pct, 1)}%`;
    if (msEl) msEl.textContent = `${formatNumber(report.avg_latency_ms, 1)} ms`;
    if (metaEl) {
      metaEl.textContent = `Run ${report.run_id} · Mode: ${report.mode} · Passed: ${report.passed_cases}/${report.total_cases}`;
    }

    if (!tbody) return;
    const cases = Array.isArray(report.cases) ? report.cases : [];
    if (cases.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty-cell">Список тест-кейсів порожній.</td></tr>`;
      return;
    }

    tbody.innerHTML = cases
      .map((c) => {
        const suiteCls =
          c.suite === "numeric"
            ? "suite-numeric"
            : c.suite === "tool"
              ? "suite-tool"
              : "suite-redteam";
        const statusCls = c.passed ? "eval-status-pass" : "eval-status-fail";
        const statusText = c.passed ? "PASS" : "FAIL";
        const toolsText =
          Array.isArray(c.tools_called) && c.tools_called.length > 0
            ? c.tools_called.join(", ")
            : "—";

        return `
          <tr>
            <td class="mono-num">${escapeHtml(c.case_id)}</td>
            <td><span class="suite-badge ${suiteCls}">${escapeHtml(c.suite)}</span></td>
            <td>${escapeHtml(c.prompt)}</td>
            <td><span class="eval-status-pill ${statusCls}">${statusText}</span></td>
            <td>
              <div class="mono-num">${Number(c.latency_ms || 0).toFixed(1)} ms · tools: ${escapeHtml(toolsText)}</div>
              <div class="kpi-sub">${escapeHtml(c.reason || "")}</div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  async function loadLatestEvals() {
    const metaEl = $("eval-run-meta");
    if (metaEl) metaEl.textContent = "Завантаження останнього звіту...";
    try {
      const report = await apiRequest("/api/evals/latest");
      renderEvalReport(report);
    } catch (err) {
      if (metaEl) metaEl.textContent = `Помилка завантаження: ${err.message}`;
    }
  }

  async function runEvalsBenchmark(mode = "deterministic") {
    const detBtn = $("run-evals-btn");
    const liveBtn = $("run-evals-live-btn");
    const metaEl = $("eval-run-meta");
    if (detBtn) detBtn.disabled = true;
    if (liveBtn) liveBtn.disabled = true;
    if (metaEl) metaEl.textContent = `Виконання 20 тест-кейсів (${mode})...`;

    try {
      const headers = buildOptionalGeminiHeaders();
      const report = await apiRequest(`/api/evals/run?mode=${encodeURIComponent(mode)}`, {
        method: "POST",
        headers,
      });
      renderEvalReport(report);
      showToast(
        `Benchmark завершено: ${report.passed_cases}/${report.total_cases} тестів пройдено`,
        report.passed_cases === report.total_cases ? "ok" : "err"
      );
    } catch (err) {
      if (metaEl) metaEl.textContent = `Помилка запуску: ${err.message}`;
      showToast(`Eval Lab помилка: ${err.message}`, "err");
    } finally {
      if (detBtn) detBtn.disabled = false;
      if (liveBtn) liveBtn.disabled = false;
    }
  }

  function openEvalsModal() {
    const modal = $("evals-modal");
    if (!modal) return;
    modal.hidden = false;
    loadLatestEvals();
  }

  function closeEvalsModal() {
    const modal = $("evals-modal");
    if (modal) modal.hidden = true;
  }

  async function resetDemoData() {
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
  }

  function registerServiceWorker() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        // Ignore SW registration failure in restricted environments
      });
    }
  }

  function bindEvents() {
    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      state.deferredInstallPrompt = event;
      const installBtn = $("install-app-btn");
      if (installBtn) {
        installBtn.classList.add("btn-primary");
      }
    });

    window.addEventListener("appinstalled", () => {
      state.deferredInstallPrompt = null;
      closeInstallModal();
      showToast("MonoPilot AI встановлено на пристрій!", "ok");
    });

    // Mobile 4-tab navigation
    document.querySelectorAll(".mobile-nav-item").forEach((navBtn) => {
      navBtn.addEventListener("click", () => {
        setMobileTab(navBtn.getAttribute("data-tab") || "home");
      });
    });

    // Quick Action Pills
    $("qa-receipt-btn")?.addEventListener("click", openReceiptModal);
    $("qa-sync-btn")?.addEventListener("click", () => openKeysModal("mono-token-input"));
    $("qa-csv-btn")?.addEventListener("click", () => openKeysModal("csv-file-input"));
    $("qa-ask-ai-btn")?.addEventListener("click", () => {
      if (window.innerWidth <= 860) {
        setMobileTab("chat");
      }
      $("chat-input")?.focus();
    });

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

    // Copy SQL delegation in chat inspector
    $("chat-messages")?.addEventListener("click", async (e) => {
      const copyBtn = e.target.closest(".copy-sql-btn");
      if (!copyBtn) return;
      e.preventDefault();
      e.stopPropagation();
      const sqlText = copyBtn.getAttribute("data-sql") || "";
      if (!sqlText) return;
      try {
        await navigator.clipboard.writeText(sqlText);
        showToast("SQL-запит скопійовано в буфер обміну", "ok");
      } catch {
        showToast("Не вдалося скопіювати SQL у цьому браузері", "err");
      }
    });

    // Clear chat
    $("clear-chat-btn")?.addEventListener("click", () => {
      state.history = [];
      seedWelcomeMessage();
    });

    // Reset Demo top button
    $("reset-demo-btn")?.addEventListener("click", resetDemoData);

    // Keys modal controls
    $("open-keys-btn")?.addEventListener("click", () => openKeysModal());
    $("close-modal-btn")?.addEventListener("click", closeKeysModal);
    $("close-modal-x")?.addEventListener("click", closeKeysModal);

    $("keys-modal")?.addEventListener("click", (e) => {
      if (e.target === $("keys-modal")) {
        closeKeysModal();
      }
    });

    // Install modal controls
    $("install-app-btn")?.addEventListener("click", openInstallModal);
    $("close-install-modal-btn")?.addEventListener("click", closeInstallModal);
    $("close-install-modal-x")?.addEventListener("click", closeInstallModal);

    $("install-modal")?.addEventListener("click", (e) => {
      if (e.target === $("install-modal")) {
        closeInstallModal();
      }
    });

    $("trigger-pwa-install-btn")?.addEventListener("click", async () => {
      if (!state.deferredInstallPrompt) return;
      state.deferredInstallPrompt.prompt();
      const choice = await state.deferredInstallPrompt.userChoice;
      state.deferredInstallPrompt = null;
      if (choice?.outcome === "accepted") {
        closeInstallModal();
      }
    });

    // Receipt modal controls
    $("close-receipt-modal-x")?.addEventListener("click", closeReceiptModal);
    $("close-receipt-modal-btn")?.addEventListener("click", closeReceiptModal);
    $("receipt-modal")?.addEventListener("click", (e) => {
      if (e.target === $("receipt-modal")) {
        closeReceiptModal();
      }
    });
    $("scan-demo-receipt-btn")?.addEventListener("click", () => runReceiptScan(true));
    $("scan-receipt-btn")?.addEventListener("click", () => runReceiptScan(false));

    // Evals modal controls
    $("open-evals-btn")?.addEventListener("click", openEvalsModal);
    $("close-evals-modal-x")?.addEventListener("click", closeEvalsModal);
    $("close-evals-modal-btn")?.addEventListener("click", closeEvalsModal);
    $("evals-modal")?.addEventListener("click", (e) => {
      if (e.target === $("evals-modal")) {
        closeEvalsModal();
      }
    });
    $("run-evals-btn")?.addEventListener("click", () => runEvalsBenchmark("deterministic"));
    $("run-evals-live-btn")?.addEventListener("click", () => runEvalsBenchmark("live"));

    // Escape closes any open modal
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (!$("keys-modal")?.hidden) closeKeysModal();
        if (!$("install-modal")?.hidden) closeInstallModal();
        if (!$("receipt-modal")?.hidden) closeReceiptModal();
        if (!$("evals-modal")?.hidden) closeEvalsModal();
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

    // Mono Jars & What-If Simulator bindings
    $("toggle-new-jar-btn")?.addEventListener("click", () => {
      const form = $("new-jar-form");
      if (!form) return;
      form.hidden = !form.hidden;
      if (!form.hidden) {
        $("jar-name-input")?.focus();
      }
    });

    $("new-jar-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      createJarFromForm();
    });

    $("jars-grid")?.addEventListener("click", (e) => {
      const delBtn = e.target.closest(".delete-jar-btn");
      if (delBtn) {
        e.stopPropagation();
        const jarId = delBtn.getAttribute("data-jar-id");
        if (jarId) deleteJarById(jarId);
        return;
      }
      const jarCard = e.target.closest(".jar-card");
      const jarId = jarCard?.getAttribute("data-jar-id");
      if (jarId) {
        state.selectedJarId = jarId;
        renderJars();
        simulateWhatIf();
      }
    });

    $("whatif-jar-select")?.addEventListener("change", (e) => {
      state.selectedJarId = e.target.value || "";
      renderJars();
      simulateWhatIf();
    });

    $("cut-cafe-slider")?.addEventListener("input", (e) => {
      const badge = $("cut-cafe-val");
      if (badge) badge.textContent = `-${e.target.value}%`;
      scheduleWhatIfSimulation();
    });

    $("cut-hobby-slider")?.addEventListener("input", (e) => {
      const badge = $("cut-hobby-val");
      if (badge) badge.textContent = `-${e.target.value}%`;
      scheduleWhatIfSimulation();
    });

    $("whatif-extra-input")?.addEventListener("input", scheduleWhatIfSimulation);
    document.querySelectorAll(".whatif-sub-toggle").forEach((toggle) => {
      toggle.addEventListener("change", simulateWhatIf);
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
    const params = new URLSearchParams(window.location.search);
    const initialTab = params.get("tab") || "home";
    setMobileTab(initialTab);
    registerServiceWorker();
    seedWelcomeMessage();
    bindEvents();
    refreshAll();
  });
})();
