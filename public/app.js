const state = {
  user: null,
  page: "overview",
  cache: {},
  search: "",
};

const pageNames = {
  overview: "Executive overview",
  inventory: "Inventory",
  planning: "Demand & planning",
  procurement: "Procurement",
  suppliers: "Suppliers",
  reports: "Reports & audit",
};
const roleRank = { viewer: 0, operator: 1, manager: 2, admin: 3 };

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const pageRoot = $("#page-content");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function money(value) {
  return new Intl.NumberFormat(undefined, {
    style: "currency", currency: "USD", maximumFractionDigits: 0,
  }).format(Number(value || 0));
}

function quantity(value, digits = 0) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(Number(value || 0));
}

function canWrite(minimumRole) {
  return (roleRank[state.user?.role] ?? -1) >= roleRank[minimumRole];
}

function barHeightClass(value, maximum) {
  const percentage = maximum > 0 ? Math.max(0, Math.min(100, value / maximum * 100)) : 0;
  const step = percentage === 0 ? 0 : Math.min(100, Math.ceil(percentage / 10) * 10);
  return `h-${step}`;
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (!["GET", "HEAD"].includes(method)) {
    headers["X-CSRF-Token"] = state.user?.csrf_token || "";
    if (options.idempotency) headers["Idempotency-Key"] = options.idempotency;
  }
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (response.status === 401 && !path.endsWith("/auth/login")) {
    state.user = null;
    showLogin();
    throw new Error("Your session has ended. Please sign in again.");
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("text/csv")) return response.blob();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error?.message || "The request could not be completed.");
  return data;
}

function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => el.classList.remove("show"), 3200);
}

function showLogin() {
  $("#app-shell").hidden = true;
  $("#login-screen").hidden = false;
  $("#login-error").textContent = "";
}

function showApplication(user) {
  state.user = user;
  $("#login-screen").hidden = true;
  $("#app-shell").hidden = false;
  $("#organization-name").textContent = user.organization_name;
  $("#user-name").textContent = user.display_name;
  $("#user-role").textContent = user.role;
  $("#user-avatar").textContent = user.display_name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
  $("#today-label").textContent = new Intl.DateTimeFormat(undefined, {
    weekday: "short", month: "short", day: "numeric",
  }).format(new Date());
  renderPage();
}

function pageHeading(eyebrow, title, description, actions = "") {
  return `<div class="page-heading">
    <div><div class="eyebrow">${escapeHtml(eyebrow)}</div><h1>${escapeHtml(title)}</h1>
    <p class="page-subtitle">${escapeHtml(description)}</p></div>
    ${actions ? `<div class="heading-actions">${actions}</div>` : ""}
  </div>`;
}

function button(label, action, primary = false, icon = "") {
  return `<button type="button" class="button ${primary ? "button-primary" : ""}" data-action="${escapeHtml(action)}">${icon ? `<span>${icon}</span>` : ""}${escapeHtml(label)}</button>`;
}

function emptyState(title, detail, action = "") {
  return `<div class="empty-state"><div class="empty-mark">◈</div><strong>${escapeHtml(title)}</strong>
    <span>${escapeHtml(detail)}</span>${action ? `<div class="empty-action">${action}</div>` : ""}</div>`;
}

function tablePanel(title, subtitle, headers, rows, emptyMessage, options = {}) {
  const body = rows.length
    ? `<div class="table-toolbar"><input class="table-search" type="search" placeholder="Filter records…" aria-label="Filter records" value="${escapeHtml(state.search)}"><span class="table-count">${rows.length} RECORD${rows.length === 1 ? "" : "S"}</span></div>
       <div class="table-wrap"><table><thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : emptyState("No records yet", emptyMessage, options.emptyAction || "");
  return `<section class="panel table-panel"><div class="panel-heading"><div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(subtitle)}</p></div>${options.badge ? `<span class="panel-badge">${escapeHtml(options.badge)}</span>` : ""}</div>${body}</section>`;
}

async function renderPage() {
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === state.page));
  $("#breadcrumb-current").textContent = pageNames[state.page];
  pageRoot.innerHTML = `<div class="page-heading"><div><div class="eyebrow">ABHIVEYRA INTELLIGENCE</div><h1>${escapeHtml(pageNames[state.page])}</h1></div></div><div class="skeleton loading-skeleton"></div>`;
  try {
    const renderers = {
      overview: renderOverview,
      inventory: renderInventory,
      planning: renderPlanning,
      procurement: renderProcurement,
      suppliers: renderSuppliers,
      reports: renderReports,
    };
    await renderers[state.page]();
    attachPageEvents();
  } catch (error) {
    pageRoot.innerHTML = pageHeading("WORKSPACE", pageNames[state.page], "Unable to load current organization data.")
      + emptyState("Data could not be loaded", error.message, button("Try again", "refresh"));
    attachPageEvents();
  }
}

async function renderOverview() {
  const { data: dashboard } = await api("/api/v1/dashboard");
  const { data: inventory } = await api("/api/v1/inventory");
  const kpis = [
    ["Inventory value", money(dashboard.inventory_value), "On-hand units × current recorded unit cost", "◫"],
    ["Available units", quantity(dashboard.on_hand_units - dashboard.reserved_units, 0), `${quantity(dashboard.reserved_units)} units reserved`, "▤"],
    ["Open purchase orders", quantity(dashboard.open_purchase_orders), `${money(dashboard.open_purchase_order_value)} outstanding`, "⇄"],
    ["Replenishment signals", quantity(dashboard.stockout_risks), "Based on recorded 180-day demand history", "⌁"],
  ];
  const maxMovement = Math.max(1, ...dashboard.weekly_movements.map((item) => Math.abs(item.quantity)));
  const movementsByDay = {};
  dashboard.weekly_movements.forEach((item) => {
    const day = movementsByDay[item.day] || (movementsByDay[item.day] = { receipt: 0, issue: 0, adjustment: 0 });
    day[item.movement_type] = Number(item.quantity || 0);
  });
  const days = Array.from({ length: 7 }, (_, index) => {
    const day = new Date();
    day.setDate(day.getDate() - (6 - index));
    const key = day.toISOString().slice(0, 10);
    const data = movementsByDay[key] || {};
    const out = Math.abs(data.issue || 0) + Math.abs(data.adjustment || 0);
    const incoming = Math.abs(data.receipt || 0);
    return `<div class="chart-day" title="${escapeHtml(key)} · received ${quantity(incoming)} · issued ${quantity(out)}">
      <div class="chart-bar ${barHeightClass(incoming, maxMovement)}"></div>
      <div class="chart-bar receipt ${barHeightClass(out, maxMovement)}"></div></div>`;
  }).join("");
  const labels = Array.from({ length: 7 }, (_, index) => {
    const day = new Date();
    day.setDate(day.getDate() - (6 - index));
    return `<span>${day.toLocaleDateString(undefined, { weekday: "short" })}</span>`;
  }).join("");
  const actions = dashboard.actions.map((item) => `
    <div class="action-item"><i class="action-indicator"></i><div><strong>${escapeHtml(item.name)}</strong>
    <small>${escapeHtml(item.sku)} · ${escapeHtml(item.warehouse)} · Suggested ${quantity(item.recommended_order_milli / 1000, 1)} units</small></div>
    <span class="action-meta">${money(item.estimated_cost)}</span></div>`).join("");
  pageRoot.innerHTML = pageHeading(
    "OPERATIONS · LIVE SNAPSHOT", "Executive overview",
    `${escapeHtml(dashboard.organization)} · Metrics reflect posted inventory and procurement records.`,
    `${canWrite("operator") ? button("Record movement", "movement") : ""} ${canWrite("manager") ? button("Add product", "product", true, "+") : ""}`,
  ) + `<div class="kpi-grid">${kpis.map(([label, value, note, icon]) => `<article class="kpi-card">
    <div class="kpi-top"><span>${label}</span><span class="kpi-icon">${icon}</span></div>
    <div class="kpi-value">${value}</div><div class="kpi-foot">${note}</div></article>`).join("")}</div>
    <div class="dashboard-grid">
      <section class="panel"><div class="panel-heading"><div><h2>Inventory movement activity</h2><p>Recorded movement quantities · trailing seven days</p></div><span class="panel-badge">LIVE LEDGER</span></div>
        <div class="chart-area"><div class="bar-chart"><div class="chart-bars">${days}</div><div class="chart-labels">${labels}</div></div></div>
        <div class="chart-legend"><span><i></i>Issues & adjustments</span><span><i class="legend-receipt"></i>Receipts</span></div></section>
      <section class="panel"><div class="panel-heading"><div><h2>Actions to review</h2><p>Explainable replenishment calculations</p></div><span class="panel-badge">${dashboard.actions.length} SIGNALS</span></div>
        <div class="action-list">${actions || emptyState("No action signals", "Signals appear after sufficient demand history and a positive order recommendation.")}</div></section>
    </div>
    ${tablePanel("Inventory positions", `${inventory.length} tracked warehouse positions · values use recorded product cost`,
      ["SKU / PRODUCT", "WAREHOUSE", "ON HAND", "AVAILABLE", "VALUE"],
      inventory.slice(0, 5).map((item) => `<tr><td><span class="sku-cell">${escapeHtml(item.sku)}</span><div class="product-cell"><strong>${escapeHtml(item.name)}</strong></div></td>
        <td>${escapeHtml(item.warehouse_name)}</td><td class="number-cell">${quantity(item.on_hand, 3)} ${escapeHtml(item.unit)}</td>
        <td class="number-cell">${quantity(item.available, 3)}</td><td class="number-cell">${money(item.value)}</td></tr>`),
      "Post a receipt or add products and opening balances to start tracking.")}`;
}

async function renderInventory() {
  const [{ data: products }, { data: inventory }, { data: movements }, { data: warehouses }, { data: suppliers }] = await Promise.all([
    api("/api/v1/products"), api("/api/v1/inventory"), api("/api/v1/inventory/movements"),
    api("/api/v1/warehouses"), api("/api/v1/suppliers"),
  ]);
  state.cache = { ...state.cache, products, warehouses, suppliers };
  const rows = inventory.map((item) => `<tr data-search="${escapeHtml(`${item.sku} ${item.name} ${item.warehouse_name} ${item.category}`.toLowerCase())}">
    <td class="sku-cell">${escapeHtml(item.sku)}</td><td class="product-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.category)}</small></td>
    <td>${escapeHtml(item.warehouse_name)}</td><td class="number-cell">${quantity(item.on_hand, 3)} ${escapeHtml(item.unit)}</td>
    <td class="number-cell">${quantity(item.reserved, 3)}</td><td class="number-cell">${quantity(item.available, 3)}</td>
    <td class="number-cell">${money(item.value)}</td></tr>`);
  const movementRows = movements.slice(0, 8).map((item) => `<tr>
    <td class="number-cell">#${item.id}</td><td>${escapeHtml(item.created_at)}</td>
    <td><span class="status ${item.movement_type === "receipt" ? "status-received" : item.movement_type === "issue" ? "status-partial" : "status-open"}">${escapeHtml(item.movement_type)}</span></td>
    <td class="product-cell"><strong>${escapeHtml(item.product_name)}</strong><small>${escapeHtml(item.sku)}</small></td>
    <td>${escapeHtml(item.warehouse_name)}</td><td class="number-cell">${item.signed_quantity > 0 ? "+" : ""}${quantity(item.signed_quantity, 3)}</td>
    <td>${escapeHtml(item.actor)}</td></tr>`);
  pageRoot.innerHTML = pageHeading(
    "INVENTORY OPERATIONS", "Inventory",
    `${products.length} active products · ${inventory.length} warehouse balances · quantities stored to 0.001 units`,
    `${canWrite("operator") ? `${button("Record demand", "demand")} ${button("Record movement", "movement", true, "+")}` : ""} ${canWrite("manager") ? button("New product", "product") : ""}`,
  ) + `<div class="callout">Available quantity = eligible on-hand quantity minus active reservations. Incoming purchase orders are shown separately in planning and are never counted as physically available.</div>`
    + tablePanel("Stock by location", "Stock balances and current product cost valuation", ["SKU", "PRODUCT", "WAREHOUSE", "ON HAND", "RESERVED", "AVAILABLE", "VALUE"], rows, "Add a product and post a receipt to establish stock.")
    + tablePanel("Recent stock ledger", "Posted transactions are traceable and corrected with compensating entries.", ["ID", "POSTED AT", "TYPE", "PRODUCT", "WAREHOUSE", "CHANGE", "ACTOR"], movementRows, "No stock transactions have been posted.");
  attachPageEvents();
}

async function renderPlanning() {
  const [{ data: forecasts }, { data: recommendations }, { data: products }] = await Promise.all([
    api("/api/v1/forecasts"), api("/api/v1/recommendations"), api("/api/v1/products"),
  ]);
  const { data: warehouses } = await api("/api/v1/warehouses");
  state.cache = { ...state.cache, products, warehouses };
  const recRows = recommendations.map((item) => `<tr>
    <td class="sku-cell">${escapeHtml(item.sku)}</td><td class="product-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.warehouse)}</small></td>
    <td class="number-cell">${quantity(item.available_quantity, 1)}</td>
    <td class="number-cell">${item.available ? quantity(item.reorder_point, 1) : "—"}</td>
    <td class="number-cell">${item.available ? quantity(item.recommended_order_milli / 1000, 1) : "—"}</td>
    <td class="number-cell">${item.available ? money(item.estimated_cost) : "—"}</td>
    <td>${item.available ? (item.recommended_order_milli > 0 ? `<span class="status status-open">REVIEW</span>` : `<span class="status status-received">ON PLAN</span>`) : `<span class="status status-inactive">NEEDS DATA</span>`}</td></tr>`);
  const forecastRows = forecasts.map((item) => `<tr>
    <td class="sku-cell">${escapeHtml(item.sku)}</td><td class="product-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.warehouse)}</small></td>
    <td>${item.available ? escapeHtml(item.model) : "Insufficient history"}</td>
    <td class="number-cell">${item.available ? quantity(item.daily_forecast, 2) : "—"}</td>
    <td class="number-cell">${item.available ? quantity(item.mae, 2) : "—"}</td>
    <td class="number-cell">${item.available && item.wape !== null ? `${quantity(item.wape * 100, 1)}%` : "—"}</td>
    <td class="number-cell">${item.observations || 0}</td></tr>`);
  const recommendationsWithData = recommendations.filter((item) => item.available).length;
  pageRoot.innerHTML = pageHeading(
    "PLANNING LAB", "Demand & planning",
    "Simple, explainable baselines evaluated on a chronological holdout. Forecasts are estimates, not guarantees.",
    canWrite("operator") ? button("Add demand observation", "demand", true, "+") : "",
  ) + `<div class="planning-grid">
    <section class="panel"><div class="panel-heading"><div><h2>Forecast methodology</h2><p>Rolling chronological holdout · no random time-series split</p></div><span class="panel-badge">BASELINE MODEL</span></div>
      <div class="metric-list"><div class="metric-row"><span>Candidate models</span><strong>28-day mean · naive</strong></div>
      <div class="metric-row"><span>Evaluation metrics</span><strong>MAE · WAPE</strong></div>
      <div class="metric-row"><span>Observed calendar window</span><strong>180 days</strong></div>
      <div class="metric-row"><span>SKUs with sufficient observations</span><strong>${forecasts.filter((item) => item.available).length} / ${forecasts.length}</strong></div></div></section>
    <section class="panel"><div class="panel-heading"><div><h2>Reorder policy</h2><p>Demand and lead-time units are daily and calendar days</p></div><span class="panel-badge">95% TARGET</span></div>
      <div class="metric-list"><div class="metric-row"><span>Reorder point</span><strong>μ × lead time + safety stock</strong></div>
      <div class="metric-row"><span>Safety stock</span><strong>1.645 × σ × √lead time</strong></div>
      <div class="metric-row"><span>Target stock</span><strong>lead-time + 14-day cycle demand</strong></div>
      <div class="metric-row"><span>Recommendations with evidence</span><strong>${recommendationsWithData} / ${recommendations.length}</strong></div></div></section></div>`
    + tablePanel("Replenishment recommendations", "Existing open PO quantity is included as incoming; recommendations are not purchase commitments.", ["SKU", "PRODUCT / LOCATION", "NET AVAILABLE", "REORDER POINT", "SUGGESTED QTY", "EST. COST", "SIGNAL"], recRows, "No products are available for analysis.")
    + tablePanel("Forecast backtests", "Trailing mean and last-observation naive baselines compared on a chronological holdout.", ["SKU", "PRODUCT / LOCATION", "SELECTED MODEL", "DAILY FORECAST", "MAE", "WAPE", "DAYS"], forecastRows, "Add demand observations to evaluate forecasting models.")
    + `<p class="data-note">Data limitations: zero-filled missing calendar dates are treated as zero demand. Stockout-censored demand, promotions, seasonality model selection, confidence intervals, and variable lead-time uncertainty are not yet modeled. WAPE is undefined when holdout demand is zero.</p>`;
  attachPageEvents();
}

async function renderProcurement() {
  const [{ data: orders }, { data: suppliers }, { data: warehouses }, { data: products }] = await Promise.all([
    api("/api/v1/purchase-orders"), api("/api/v1/suppliers"),
    api("/api/v1/warehouses"), api("/api/v1/products"),
  ]);
  state.cache = { ...state.cache, suppliers, warehouses, products };
  const rows = orders.map((order) => `<tr>
    <td class="sku-cell">${escapeHtml(order.order_number)}</td><td>${escapeHtml(order.supplier_name)}</td>
    <td>${escapeHtml(order.warehouse_name)}</td><td class="number-cell">${money(order.total)}</td>
    <td>${escapeHtml(order.expected_date)}</td><td><span class="status status-${escapeHtml(order.status)}">${escapeHtml(order.status)}</span></td>
    <td>${canWrite("operator") && order.status !== "received" && order.status !== "cancelled" ? `<button class="row-action" data-action="receive" data-order-id="${order.id}">Receive</button>` : "—"}</td></tr>
    <tr class="po-detail-row"><td colspan="7"><span class="table-count">${order.lines.map((line) => `${escapeHtml(line.sku)} · ${quantity(line.received, 3)} / ${quantity(line.ordered, 3)} received`).join("  |  ")}</span></td></tr>`);
  pageRoot.innerHTML = pageHeading(
    "PROCUREMENT WORKFLOW", "Procurement",
    `${orders.length} purchase orders · receipts post inventory and ledger changes in one transaction.`,
    canWrite("manager") ? button("Create purchase order", "purchase-order", true, "+") : "",
  ) + `<div class="callout">This release supports purchase order drafting, open/partial/received status, and traceable goods receipts. Approval routing, supplier dispatch integration, invoice reconciliation, PDF generation, and return processing remain out of scope.</div>`
    + tablePanel("Purchase orders", "Open orders are included in replenishment as incoming stock, but not as on-hand inventory.", ["ORDER", "SUPPLIER", "WAREHOUSE", "ORDER VALUE", "EXPECTED", "STATUS", "ACTION"], rows, "Create a purchase order to start tracking procurement.");
  attachPageEvents();
}

async function renderSuppliers() {
  const { data: suppliers } = await api("/api/v1/suppliers");
  const rows = suppliers.map((supplier) => `<tr>
    <td class="product-cell"><strong>${escapeHtml(supplier.name)}</strong><small>${escapeHtml(supplier.email || "No contact email recorded")}</small></td>
    <td class="number-cell">${quantity(supplier.lead_time_days)} days</td>
    <td><span class="status ${supplier.status === "active" ? "status-received" : "status-inactive"}">${escapeHtml(supplier.status)}</span></td>
    <td class="number-cell">${supplier.orders}</td><td class="number-cell">${supplier.completed_orders}</td>
    <td>${supplier.completed_orders ? "Receipts recorded" : "Insufficient history"}</td></tr>`);
  pageRoot.innerHTML = pageHeading(
    "SUPPLIER INTELLIGENCE", "Suppliers",
    "Supplier details and recorded purchase order history. No unverified scores or quotations are fabricated.",
    canWrite("manager") ? button("Add supplier", "supplier", true, "+") : "",
  ) + tablePanel("Supplier directory", "Delivery performance requires completed orders with comparable confirmed and actual delivery dates.", ["SUPPLIER", "LEAD TIME", "STATUS", "ORDERS", "COMPLETED", "PERFORMANCE DATA"], rows, "Add suppliers to make them available for procurement.");
  attachPageEvents();
}

async function renderReports() {
  const [{ data: inventory }, { data: audit }] = await Promise.all([
    api("/api/v1/inventory"), api("/api/v1/audit"),
  ]);
  const total = inventory.reduce((sum, item) => sum + item.value, 0);
  const rows = audit.map((item) => `<tr><td class="number-cell">#${item.id}</td><td>${escapeHtml(item.created_at)}</td>
    <td><span class="status status-partial">${escapeHtml(item.action)}</span></td><td>${escapeHtml(item.entity_type)} · ${escapeHtml(item.entity_id)}</td>
    <td>${escapeHtml(item.actor)}</td><td class="table-count">${escapeHtml(item.details)}</td></tr>`);
  pageRoot.innerHTML = pageHeading(
    "FINANCE & GOVERNANCE", "Reports & audit",
    "Inventory value is on-hand quantity multiplied by the currently recorded product unit cost.",
    `<button class="button button-primary" type="button" data-action="export">↓ Export inventory CSV</button>`,
  ) + `<div class="kpi-grid">
    <article class="kpi-card"><div class="kpi-top"><span>Current inventory value</span><span class="kpi-icon">◫</span></div><div class="kpi-value">${money(total)}</div><div class="kpi-foot">Snapshot · current balances and product cost</div></article>
    <article class="kpi-card"><div class="kpi-top"><span>Tracked positions</span><span class="kpi-icon">▤</span></div><div class="kpi-value">${inventory.length}</div><div class="kpi-foot">Product × warehouse combinations</div></article>
    <article class="kpi-card"><div class="kpi-top"><span>Audit events</span><span class="kpi-icon">◉</span></div><div class="kpi-value">${audit.length}</div><div class="kpi-foot">Most recent 200 organization events</div></article>
    <article class="kpi-card"><div class="kpi-top"><span>Valuation method</span><span class="kpi-icon">∑</span></div><div class="kpi-value kpi-value-small">Recorded unit cost</div><div class="kpi-foot">Not FIFO or weighted-average costing</div></article>
  </div><p class="data-note">Financial limitation: values are indicative inventory-at-cost calculations from manually recorded current product costs. Historical cost layers, landed costs, FX, turnover, DIO, carrying costs, cash released, and profit impact are not implemented. Do not treat this report as accounting valuation.</p>`
    + tablePanel("Audit activity", "Administrative and inventory actions are appended with the actor and recorded context.", ["EVENT", "TIME (UTC)", "ACTION", "ENTITY", "ACTOR", "DETAILS"], rows, "Audit events will appear as users perform actions.");
  attachPageEvents();
}

function attachPageEvents() {
  $$(".table-search", pageRoot).forEach((input) => {
    input.addEventListener("input", () => {
      state.search = input.value.toLowerCase();
      $$(".table-search", pageRoot).forEach((sameInput) => {
        if (sameInput !== input) sameInput.value = input.value;
      });
      $$(".table-search", pageRoot).forEach((sameInput) => {
        const target = sameInput.closest(".panel").querySelector("tbody");
        if (target) [...target.rows].forEach((row) => {
          row.hidden = !row.innerText.toLowerCase().includes(input.value.toLowerCase());
        });
      });
    });
  });
  $$("[data-action]", pageRoot).forEach((element) => {
    element.addEventListener("click", () => handleAction(element.dataset.action, element).catch((error) => toast(error.message, true)));
  });
}

async function handleAction(action, element) {
  if (action === "refresh") return renderPage();
  if (action === "export") {
    try {
      const blob = await api("/api/v1/reports/inventory.csv");
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "abhiveyra-inventory.csv";
      link.click();
      URL.revokeObjectURL(link.href);
      toast("Inventory CSV exported.");
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (action === "receive") return openReceiveDialog(Number(element.dataset.orderId));
  return openActionDialog(action);
}

function selectField(name, label, options, wide = false, required = true) {
  return `<label class="${wide ? "field-wide" : ""}">${escapeHtml(label)}<select name="${escapeHtml(name)}" ${required ? "required" : ""}>
    ${options.map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`).join("")}
  </select></label>`;
}

function inputField(name, label, type = "text", value = "", wide = false, required = true, attrs = "") {
  return `<label class="${wide ? "field-wide" : ""}">${escapeHtml(label)}<input name="${escapeHtml(name)}" type="${type}" value="${escapeHtml(value)}" ${required ? "required" : ""} ${attrs}></label>`;
}

async function openActionDialog(action) {
  if (["product", "movement", "demand", "purchase-order"].includes(action)) {
    const requests = [];
    if (!state.cache.products) requests.push(api("/api/v1/products").then(({ data }) => { state.cache.products = data; }));
    if (!state.cache.warehouses) requests.push(api("/api/v1/warehouses").then(({ data }) => { state.cache.warehouses = data; }));
    if (!state.cache.suppliers) requests.push(api("/api/v1/suppliers").then(({ data }) => { state.cache.suppliers = data; }));
    await Promise.all(requests);
  }
  const dialog = $("#action-dialog");
  const titleMap = {
    product: ["PRODUCT CATALOG", "Add product", "Save product"],
    supplier: ["SUPPLIER DIRECTORY", "Add supplier", "Save supplier"],
    movement: ["INVENTORY LEDGER", "Record stock movement", "Post movement"],
    demand: ["DEMAND HISTORY", "Record demand observation", "Save observation"],
    "purchase-order": ["PROCUREMENT", "Create purchase order", "Create order"],
  };
  const config = titleMap[action];
  if (!config) return;
  $("#dialog-eyebrow").textContent = config[0];
  $("#dialog-title").textContent = config[1];
  $("#dialog-submit").textContent = config[2];
  const products = state.cache.products || [];
  const suppliers = state.cache.suppliers || [];
  const warehouses = state.cache.warehouses || [];
  const productOptions = products.filter((p) => p.active).map((p) => ({ value: p.id, label: `${p.sku} · ${p.name}` }));
  const supplierOptions = suppliers.filter((s) => s.status === "active").map((s) => ({ value: s.id, label: s.name }));
  const warehouseOptions = warehouses.map((w) => ({ value: w.id, label: w.name }));
  let fields = "";
  if (action === "product") {
    fields = inputField("sku", "SKU") + inputField("name", "Product name", "text", "", true)
      + inputField("category", "Category", "text", "Uncategorized")
      + inputField("unit", "Unit of measure", "text", "ea")
      + inputField("unit_cost", "Unit cost (USD)", "number", "0", false, true, 'min="0" step="0.01"')
      + inputField("lead_time_days", "Lead time (days)", "number", "14", false, true, 'min="0" step="1"')
      + inputField("minimum_order", "Minimum order", "number", "0", false, true, 'min="0" step="0.001"')
      + inputField("order_multiple", "Order multiple", "number", "1", false, true, 'min="0.001" step="0.001"')
      + selectField("supplier_id", "Preferred supplier", [{ value: "", label: "Not assigned" }, ...supplierOptions], true, false);
  } else if (action === "supplier") {
    fields = inputField("name", "Supplier name", "text", "", true)
      + inputField("email", "Contact email", "email", "", true, false)
      + inputField("lead_time_days", "Quoted lead time (days)", "number", "14", false, true, 'min="0" step="1"');
  } else if (action === "movement") {
    fields = selectField("movement_type", "Movement type", [
      { value: "receipt", label: "Receipt" }, { value: "issue", label: "Issue" }, { value: "adjustment", label: "Adjustment" },
    ]) + selectField("product_id", "Product", productOptions)
      + selectField("warehouse_id", "Warehouse", warehouseOptions)
      + inputField("quantity", "Quantity", "number", "", false, true, 'min="0.001" step="0.001"')
      + selectField("direction", "Adjustment direction", [{ value: "increase", label: "Increase" }, { value: "decrease", label: "Decrease" }])
      + inputField("reason", "Reason", "text", "", true)
      + inputField("reference", "Reference (optional)", "text", "", true, false);
  } else if (action === "demand") {
    fields = selectField("product_id", "Product", productOptions, true)
      + selectField("warehouse_id", "Warehouse (optional)", [{ value: "", label: "All warehouses" }, ...warehouseOptions], true, false)
      + inputField("date", "Demand date", "date", new Date().toISOString().slice(0, 10))
      + inputField("quantity", "Observed demand", "number", "", false, true, 'min="0" step="0.001"');
  } else if (action === "purchase-order") {
    fields = selectField("supplier_id", "Supplier", supplierOptions)
      + selectField("warehouse_id", "Receiving warehouse", warehouseOptions)
      + inputField("expected_date", "Expected delivery", "date", new Date(Date.now() + 14 * 86400000).toISOString().slice(0, 10))
      + selectField("product_id", "Product", productOptions)
      + inputField("quantity", "Order quantity", "number", "", false, true, 'min="0.001" step="0.001"')
      + inputField("unit_cost", "Unit cost (USD)", "number", "0", false, true, 'min="0" step="0.01"');
  }
  $("#dialog-fields").innerHTML = fields || emptyState("Setup required", "Create a warehouse and supplier before placing a purchase order.");
  dialog.dataset.action = action;
  $("#dialog-form").dataset.action = action;
  dialog.showModal();
}

async function openReceiveDialog(orderId) {
  try {
    const { data: orders } = await api("/api/v1/purchase-orders");
    const order = orders.find((item) => item.id === orderId);
    if (!order) throw new Error("Purchase order was not found.");
    const pending = order.lines.filter((line) => line.ordered > line.received);
    if (!pending.length) throw new Error("This purchase order has no outstanding quantity.");
    const line = pending[0];
    $("#dialog-eyebrow").textContent = `GOODS RECEIPT · ${order.order_number}`;
    $("#dialog-title").textContent = `Receive ${line.sku}`;
    $("#dialog-submit").textContent = "Post receipt";
    $("#dialog-fields").innerHTML = `<p class="field-wide page-subtitle">Outstanding quantity: <strong>${quantity(line.ordered - line.received, 3)}</strong> ${escapeHtml(line.product_name)}</p>
      <input type="hidden" name="line_id" value="${line.id}">
      ${inputField("quantity", "Quantity received", "number", line.ordered - line.received, true, true, `min="0.001" max="${line.ordered - line.received}" step="0.001"`)}`;
    $("#dialog-form").dataset.action = "receive";
    $("#dialog-form").dataset.orderId = String(orderId);
    $("#action-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

$("#dialog-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const data = Object.fromEntries(new FormData(form).entries());
  const action = form.dataset.action;
  const submit = $("#dialog-submit");
  submit.disabled = true;
  try {
    if (action === "product") {
      data.lead_time_days = Number(data.lead_time_days);
      data.supplier_id = data.supplier_id ? Number(data.supplier_id) : null;
      await api("/api/v1/products", { method: "POST", body: data });
    } else if (action === "supplier") {
      data.lead_time_days = Number(data.lead_time_days);
      await api("/api/v1/suppliers", { method: "POST", body: data });
    } else if (action === "movement") {
      data.product_id = Number(data.product_id);
      data.warehouse_id = Number(data.warehouse_id);
      await api("/api/v1/inventory/movements", {
        method: "POST", body: data, idempotency: crypto.randomUUID(),
      });
    } else if (action === "demand") {
      data.product_id = Number(data.product_id);
      data.warehouse_id = data.warehouse_id ? Number(data.warehouse_id) : null;
      await api("/api/v1/demand", { method: "POST", body: data });
    } else if (action === "purchase-order") {
      await api("/api/v1/purchase-orders", {
        method: "POST",
        body: {
          supplier_id: Number(data.supplier_id), warehouse_id: Number(data.warehouse_id),
          expected_date: data.expected_date,
          lines: [{ product_id: Number(data.product_id), quantity: data.quantity, unit_cost: data.unit_cost }],
        },
      });
    } else if (action === "receive") {
      await api(`/api/v1/purchase-orders/${form.dataset.orderId}/receive`, {
        method: "POST",
        body: { lines: [{ line_id: Number(data.line_id), quantity: data.quantity }] },
        idempotency: crypto.randomUUID(),
      });
    }
    $("#action-dialog").close();
    state.cache = {};
    toast("Changes saved successfully.");
    await renderPage();
  } catch (error) {
    toast(error.message, true);
  } finally {
    submit.disabled = false;
  }
});

$$(".nav-item").forEach((item) => item.addEventListener("click", () => {
  state.page = item.dataset.page;
  state.search = "";
  renderPage();
}));

$("#refresh-button").addEventListener("click", () => renderPage());
$("#logout-button").addEventListener("click", async () => {
  try { await api("/api/v1/auth/logout", { method: "POST", body: {} }); }
  finally { state.user = null; showLogin(); }
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("button[type=submit]", event.currentTarget);
  button.disabled = true;
  $("#login-error").textContent = "";
  try {
    const credentials = Object.fromEntries(new FormData(event.currentTarget).entries());
    const { user } = await api("/api/v1/auth/login", { method: "POST", body: credentials });
    const { user: profile } = await api("/api/v1/auth/me");
    showApplication({ ...profile, csrf_token: user.csrf_token });
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

(async function initialize() {
  try {
    const { user } = await api("/api/v1/auth/me");
    showApplication(user);
  } catch (_) {
    showLogin();
  }
})();
