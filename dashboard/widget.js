(function initPnlWidget(global) {
  "use strict";

  const ACTIVE_REFRESH_MS = 15_000;
  const IDLE_REFRESH_MS = 60_000;
  const REQUEST_TIMEOUT_MS = 10_000;
  const BACKFILL_REQUEST_TIMEOUT_MS = 120_000;
  const DEFAULT_PORTFOLIO_ID = "pf-1020abcdef";
  const DEFAULT_PORTFOLIO_LABEL = "1020";
  const AMOUNT_HIDDEN_KEY = "pnl-widget-amount-hidden";
  const MASKED_AMOUNT = "••••••";
  const BASE_WIDTH = 180;
  const BASE_HEIGHT = 130;
  const BASE_FONT_PX = 9;
  const FONT_CAP_PX = 14; // 五号字 10.5pt ≈ 14px
  const MIN_SCALE = 0.55;
  const MAX_SCALE = FONT_CAP_PX / BASE_FONT_PX;
  const NO_BACKFILL_PHASES = Object.freeze(["pre_open", "preopen", "non_trading", "holiday"]);
  const SESSIONS = Object.freeze([
    Object.freeze({ startMs: 9.5 * 3_600_000, endMs: 11.5 * 3_600_000 }),
    Object.freeze({ startMs: 13 * 3_600_000, endMs: 15 * 3_600_000 }),
  ]);
  const AXIS_DURATION_MS = 4 * 3_600_000;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const PHASE_LABELS = Object.freeze({
    open: "连续竞价",
    auction: "集合竞价",
    lunch: "午间休市",
    closed: "已收盘",
    pre_open: "盘前等待",
    preopen: "盘前等待",
    after_hours: "盘后",
    non_trading: "非交易日",
    holiday: "非交易日",
  });

  const state = {
    started: false,
    inFlight: false,
    payload: null,
    selected: "total",
    selectorKey: "",
    error: "",
    backfillInFlight: false,
    backfillAttempts: new Set(),
    backfillError: "",
    amountHidden: false,
    pinned: true,
    timer: null,
    chartModel: null,
  };

  function asObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function firstValue(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }

  function clockMilliseconds(value) {
    const text = String(value ?? "").trim();
    const direct = text.match(/^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?/);
    if (direct) {
      const hour = Number(direct[1]);
      const minute = Number(direct[2]);
      const second = Number(direct[3] || 0);
      if (hour > 23 || minute > 59 || second > 59) return null;
      return ((hour * 60 + minute) * 60 + second) * 1000
        + Number(String(direct[4] || "0").padEnd(3, "0"));
    }
    const parsed = new Date(text);
    if (!text || !Number.isFinite(parsed.getTime())) return null;
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(parsed).map((part) => [part.type, part.value]));
    return ((Number(parts.hour) * 60 + Number(parts.minute)) * 60 + Number(parts.second)) * 1000
      + parsed.getUTCMilliseconds();
  }

  function timeSortValue(value) {
    const parsed = new Date(String(value ?? ""));
    if (Number.isFinite(parsed.getTime())) return parsed.getTime();
    return clockMilliseconds(value);
  }

  function normalizeSeries(raw) {
    if (!Array.isArray(raw)) return [];
    const points = [];
    for (const value of raw) {
      const item = Array.isArray(value) ? {} : asObject(value);
      const time = Array.isArray(value)
        ? value[0]
        : firstValue(item.time, item.timestamp, item.observed_at, item.observedAt, item.datetime);
      const pnl = finiteNumber(Array.isArray(value)
        ? value[1]
        : firstValue(item.pnl, item.daily_pnl_cny, item.dailyPnlCny, item.day_pnl, item.value));
      const sortValue = timeSortValue(time);
      const clockMs = clockMilliseconds(time);
      if (pnl === null || sortValue === null || clockMs === null) continue;
      points.push({
        time: String(time),
        pnl,
        sortValue,
        clockMs,
        source: String(item.source || ""),
        pnlRecorded: finiteNumber(firstValue(item.pnl_recorded, item.pnlRecorded)),
      });
    }
    points.sort((left, right) => left.sortValue - right.sortValue);
    const deduplicated = [];
    for (const point of points) {
      if (deduplicated.length && deduplicated[deduplicated.length - 1].sortValue === point.sortValue) {
        deduplicated[deduplicated.length - 1] = point;
      } else {
        deduplicated.push(point);
      }
    }
    return deduplicated;
  }

  function normalizePayload(payload) {
    const outer = asObject(payload);
    const root = { ...outer, ...asObject(outer.data) };
    const account = asObject(firstValue(root.account_snapshot, root.accountSnapshot, root.account));
    const positionsRaw = firstValue(root.positions, root.position_series, root.positionSeries);
    const entries = Array.isArray(positionsRaw)
      ? positionsRaw.map((value, index) => [String(index), value])
      : Object.entries(asObject(positionsRaw));
    const positions = [];
    for (const [key, raw] of entries) {
      const item = Array.isArray(raw) ? { series: raw } : asObject(raw);
      const symbol = String(firstValue(item.symbol, item.thscode, item.code, key) || "").trim();
      if (!symbol) continue;
      positions.push({
        symbol,
        name: String(firstValue(item.name, item.security_name, item.stock_name, symbol)),
        series: normalizeSeries(firstValue(item.series, item.points, item.pnl_series, item.data)),
      });
    }
    return {
      ok: root.ok !== false,
      available: root.available !== false,
      asOf: String(firstValue(root.as_of, root.asOf, root.trade_date, root.date) || ""),
      generatedAt: firstValue(root.generated_at, root.generatedAt, root.updated_at),
      marketPhase: String(firstValue(root.market_phase, root.marketPhase, root.phase) || ""),
      portfolioLabel: String(firstValue(root.portfolio_label, root.portfolioLabel) || ""),
      stale: Boolean(firstValue(root.stale, root.is_stale, root.isStale)),
      refreshing: Boolean(root.refreshing),
      message: String(firstValue(root.message, root.error) || ""),
      account: {
        asOf: String(firstValue(account.as_of, account.asOf, account.date) || ""),
        current: typeof firstValue(account.current_verified, account.currentVerified, account.verified) === "boolean"
          ? firstValue(account.current_verified, account.currentVerified, account.verified)
          : null,
      },
      totalSeries: normalizeSeries(firstValue(root.total_series, root.totalSeries, root.portfolio_series)),
      positions,
    };
  }

  function intradayPnlUrl(portfolioId = DEFAULT_PORTFOLIO_ID) {
    return `/api/intraday-pnl/history?portfolio=${encodeURIComponent(portfolioId)}`;
  }

  function widgetTitle(portfolioLabel) {
    return portfolioLabel || DEFAULT_PORTFOLIO_LABEL;
  }

  function selectedSeries(payload, selected) {
    if (payload && selected !== "total") {
      const position = payload.positions.find((item) => item.symbol === selected);
      if (position) return { key: selected, label: position.name || position.symbol, series: position.series };
    }
    return { key: "total", label: "全部持仓合计", series: payload?.totalSeries || [] };
  }

  function axisPosition(clockMs) {
    if (!Number.isFinite(clockMs)) return null;
    let elapsedMs = 0;
    for (let index = 0; index < SESSIONS.length; index += 1) {
      const session = SESSIONS[index];
      if (clockMs >= session.startMs && clockMs <= session.endMs) {
        return { plotTimeValue: elapsedMs + clockMs - session.startMs, sessionIndex: index };
      }
      elapsedMs += session.endMs - session.startMs;
    }
    return null;
  }

  function projectSeries(series) {
    const projected = [];
    for (const point of series || []) {
      const position = axisPosition(point.clockMs);
      if (position) projected.push({ ...point, ...position });
    }
    return projected;
  }

  function splitSeriesAtGaps(series) {
    if (series.length < 2) return series.length ? [series] : [];
    const gaps = series.slice(1)
      .map((point, index) => point.plotTimeValue - series[index].plotTimeValue)
      .filter((value) => value > 0)
      .sort((left, right) => left - right);
    const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 60_000;
    const hasBackfill = series.some((point) => point.source === "backfill");
    const threshold = hasBackfill
      ? 30 * 60_000
      : Math.min(30 * 60_000, Math.max(5 * 60_000, median * 6));
    const segments = [[series[0]]];
    for (let index = 1; index < series.length; index += 1) {
      if (series[index].plotTimeValue - series[index - 1].plotTimeValue > threshold) segments.push([]);
      segments[segments.length - 1].push(series[index]);
    }
    return segments;
  }

  function shanghaiDateKey(value = new Date()) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(value).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  function backfillGapKey(payload, today = shanghaiDateKey()) {
    if (!payload || payload.asOf !== today || NO_BACKFILL_PHASES.includes(payload.marketPhase)) return "";
    const allSeries = [
      payload.totalSeries || [],
      ...(payload.positions || []).map((position) => position.series || []),
    ];
    const gaps = new Set();
    const dayClosed = payload.marketPhase === "closed" || payload.marketPhase === "after_hours";
    let pointCount = 0;
    for (const series of allSeries) {
      pointCount += series.length;
      const projected = projectSeries(series);
      if (projected.length) {
        const first = projected[0];
        const last = projected[projected.length - 1];
        if (first.clockMs - SESSIONS[0].startMs > 5 * 60_000) {
          gaps.add(`session-start>${first.time}`);
        }
        if (dayClosed && SESSIONS[1].endMs - last.clockMs > 5 * 60_000) {
          gaps.add(`${last.time}>session-end`);
        }
      }
      const segments = splitSeriesAtGaps(projected);
      for (let index = 1; index < segments.length; index += 1) {
        const previous = segments[index - 1];
        const current = segments[index];
        gaps.add(`${previous[previous.length - 1].time}>${current[0].time}`);
      }
    }
    if (!pointCount) return "empty";
    return [...gaps].sort().join("|");
  }

  function directionClass(value) {
    return value > 0 ? "up" : value < 0 ? "down" : "flat";
  }

  function formatAmount(value, signed = true) {
    const number = finiteNumber(value);
    if (number === null) return "--";
    const absolute = Math.abs(number).toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    const prefix = number < 0 ? "−" : signed && number > 0 ? "+" : "";
    return `${prefix}${absolute}`;
  }

  function formatTime(value) {
    const text = String(value ?? "");
    const direct = text.match(/^(?:\d{4}-\d{2}-\d{2}[T ])?(\d{1,2}):(\d{2})(?::(\d{2}))?/);
    if (direct) return `${direct[1].padStart(2, "0")}:${direct[2]}:${direct[3] || "00"}`;
    const parsed = new Date(text);
    if (!Number.isFinite(parsed.getTime())) return "--";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).format(parsed);
  }

  function phaseLabel(phase) {
    return PHASE_LABELS[phase] || (phase ? String(phase) : "等待数据");
  }

  function isActivePhase(phase) {
    return phase === "open" || phase === "auction";
  }

  function refreshDelay(phase, hasError = false) {
    return isActivePhase(phase) || hasError ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS;
  }

  function buildNote(payload, error) {
    const notes = [];
    if (error) notes.push(`更新失败：${error}`);
    if (payload?.account.current === false) {
      notes.push(`账户快照${payload.account.asOf ? ` ${payload.account.asOf} ` : ""}未核验，金额为估算`);
    } else if (payload?.account.current == null) {
      notes.push("账户快照时效待核验，金额为估算");
    }
    if (payload?.stale) notes.push("当前展示缓存轨迹");
    if (!notes.length) notes.push("数量来自账户快照，盈亏为估算值");
    return notes.join(" · ");
  }

  function svgNode(tagName, attributes = {}) {
    const node = document.createElementNS(SVG_NS, tagName);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    return node;
  }

  function hideTooltip() {
    const tooltip = document.getElementById("chart-tooltip");
    if (tooltip) tooltip.hidden = true;
    if (state.chartModel) {
      state.chartModel.hoverLine.setAttribute("visibility", "hidden");
      state.chartModel.hoverDot.setAttribute("visibility", "hidden");
    }
  }

  function drawChart(series, label) {
    const svg = document.getElementById("pnl-chart");
    // 视口坐标跟随布局尺寸，避免窗口放大时 SVG 文字和圆点被二次拉伸。
    const width = Math.max(1, Math.round(svg.clientWidth || 180));
    const height = Math.max(1, Math.round(svg.clientHeight || 64));
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const padding = { top: 5, right: 5, bottom: 12, left: 5 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const values = series.map((point) => point.pnl);
    let valueMin = Math.min(0, ...values);
    let valueMax = Math.max(0, ...values);
    if (valueMin === valueMax) {
      const margin = Math.max(1, Math.abs(valueMin) * 0.08);
      valueMin -= margin;
      valueMax += margin;
    } else {
      const margin = (valueMax - valueMin) * 0.08;
      valueMin -= margin;
      valueMax += margin;
    }
    const x = (value) => padding.left + value / AXIS_DURATION_MS * plotWidth;
    const y = (value) => padding.top + (valueMax - value) / (valueMax - valueMin) * plotHeight;
    const latest = series[series.length - 1];
    svg.replaceChildren();
    const title = svgNode("title");
    title.textContent = `${label}盘中当日盈亏，${series.length}个点，最新${formatAmount(latest.pnl)}元`;
    svg.append(title);

    const gridValues = [...new Set([valueMax, 0, valueMin].map((value) => Number(value.toFixed(8))))];
    for (const value of gridValues) {
      svg.append(svgNode("line", {
        x1: padding.left,
        x2: width - padding.right,
        y1: y(value),
        y2: y(value),
        class: value === 0 ? "zero-line" : "grid-line",
      }));
    }

    const lunchX = x(2 * 3_600_000);
    svg.append(svgNode("line", {
      x1: lunchX,
      x2: lunchX,
      y1: padding.top,
      y2: height - padding.bottom,
      class: "session-join",
    }));

    for (const segment of splitSeriesAtGaps(series)) {
      if (segment.length === 1) {
        svg.append(svgNode("circle", {
          cx: x(segment[0].plotTimeValue), cy: y(segment[0].pnl), r: 2, class: "latest-dot",
        }));
      } else {
        svg.append(svgNode("path", {
          d: segment.map((point, index) => `${index ? "L" : "M"}${x(point.plotTimeValue).toFixed(2)},${y(point.pnl).toFixed(2)}`).join(" "),
          class: "pnl-line",
        }));
      }
    }
    svg.append(svgNode("circle", {
      cx: x(latest.plotTimeValue),
      cy: y(latest.pnl),
      r: 3,
      class: `latest-dot ${directionClass(latest.pnl)}`,
    }));

    const ticks = [
      { value: 0, label: "09:30", anchor: "start" },
      { value: 2 * 3_600_000, label: "11:30/13:00", anchor: "middle" },
      { value: AXIS_DURATION_MS, label: "15:00", anchor: "end" },
    ];
    for (const tick of ticks) {
      const labelNode = svgNode("text", {
        x: x(tick.value),
        y: height - 4,
        class: "axis-label",
        "text-anchor": tick.anchor,
      });
      labelNode.textContent = tick.label;
      svg.append(labelNode);
    }

    const hoverLine = svgNode("line", {
      y1: padding.top, y2: height - padding.bottom, class: "hover-line", visibility: "hidden",
    });
    const hoverDot = svgNode("circle", { r: 3, class: "hover-dot", visibility: "hidden" });
    svg.append(hoverLine, hoverDot);
    state.chartModel = {
      width,
      height,
      padding,
      hoverLine,
      hoverDot,
      points: series.map((point) => ({ ...point, x: x(point.plotTimeValue), y: y(point.pnl) })),
    };
    svg.setAttribute("aria-label", `${label}盘中当日盈亏走势，${series.length}个数据点，最新${formatAmount(latest.pnl)}元`);
  }

  function syncSelector() {
    const select = document.getElementById("symbol-selector");
    const positions = state.payload?.positions || [];
    const selectorKey = positions.map((item) => `${item.symbol}:${item.name}`).join("|");
    if (selectorKey !== state.selectorKey) {
      state.selectorKey = selectorKey;
      const fragment = document.createDocumentFragment();
      const total = document.createElement("option");
      total.value = "total";
      total.textContent = "全部持仓合计";
      fragment.append(total);
      for (const position of positions) {
        const option = document.createElement("option");
        option.value = position.symbol;
        option.textContent = position.name || position.symbol;
        fragment.append(option);
      }
      select.replaceChildren(fragment);
    }
    if (state.selected !== "total" && !positions.some((item) => item.symbol === state.selected)) {
      state.selected = "total";
    }
    select.value = state.selected;
  }

  function readAmountHidden() {
    try {
      return global.localStorage?.getItem(AMOUNT_HIDDEN_KEY) === "1";
    } catch {
      return false;
    }
  }

  function persistAmountHidden(hidden) {
    try {
      global.localStorage?.setItem(AMOUNT_HIDDEN_KEY, hidden ? "1" : "0");
    } catch {
      // 本地存储不可用时只影响跨重启记忆，不影响本次显隐。
    }
  }

  function syncAmountToggle() {
    const shell = document.querySelector(".widget-shell");
    if (shell) shell.classList.toggle("amount-hidden", state.amountHidden);
    const button = document.getElementById("amount-toggle");
    if (!button) return;
    const label = state.amountHidden ? "显示金额" : "隐藏金额";
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-pressed", String(state.amountHidden));
    button.title = label;
  }

  function toggleAmount() {
    state.amountHidden = !state.amountHidden;
    persistAmountHidden(state.amountHidden);
    syncAmountToggle();
    render();
  }

  function syncPinToggle() {
    const button = document.getElementById("pin-toggle");
    if (!button) return;
    const label = state.pinned ? "取消置顶" : "置顶显示";
    button.classList.toggle("is-active", state.pinned);
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-pressed", String(state.pinned));
    button.title = label;
  }

  function togglePin() {
    state.pinned = !state.pinned;
    syncPinToggle();
    global.desktopWidget?.setPinned?.(state.pinned);
  }

  function syncWidgetScale() {
    const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, Math.min(
      global.innerWidth / BASE_WIDTH,
      global.innerHeight / BASE_HEIGHT,
    )));
    document.documentElement.style.setProperty("--widget-scale", String(scale));
  }

  function bindResizeHandles() {
    for (const handle of document.querySelectorAll(".resize-handle")) {
      const direction = handle.dataset.direction;
      if (!direction) continue;
      let pointerId = null;
      const endResize = (event) => {
        if (pointerId !== event.pointerId) return;
        pointerId = null;
        if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
        global.desktopWidget?.endResize?.();
      };
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();
        pointerId = event.pointerId;
        handle.setPointerCapture(event.pointerId);
        global.desktopWidget?.startResize?.(direction, event.screenX, event.screenY);
      });
      handle.addEventListener("pointermove", (event) => {
        if (pointerId !== event.pointerId) return;
        global.desktopWidget?.resizeTo?.(event.screenX, event.screenY);
      });
      handle.addEventListener("pointerup", endResize);
      handle.addEventListener("pointercancel", endResize);
    }
  }

  function renderAmount(latest) {
    const amount = document.getElementById("latest-amount");
    if (state.amountHidden) {
      amount.textContent = latest ? MASKED_AMOUNT : "--";
      amount.className = "amount flat";
      amount.setAttribute("aria-label", latest ? "金额已隐藏" : "等待盈亏数据");
      return;
    }
    amount.textContent = latest ? formatAmount(latest.pnl) : "--";
    amount.className = `amount ${latest ? directionClass(latest.pnl) : "flat"}`;
    amount.setAttribute("aria-label", latest ? `最新盈亏${formatAmount(latest.pnl)}元` : "等待盈亏数据");
  }

  function render() {
    syncSelector();
    const payload = state.payload;
    document.getElementById("widget-title").textContent = widgetTitle(payload?.portfolioLabel);
    const selection = selectedSeries(payload, state.selected);
    const series = selection.series;
    const projected = projectSeries(series);
    const latest = series.length ? series[series.length - 1] : null;
    renderAmount(latest);

    const metaElement = document.getElementById("data-meta");
    metaElement.textContent = state.backfillInFlight
      ? "正在回补缺口…"
      : state.error
        ? "更新失败"
        : latest
          ? `${formatTime(latest.time)} · ${series.length}点`
          : payload?.generatedAt ? `检查 ${formatTime(payload.generatedAt)}` : "等待数据";
    metaElement.title = [buildNote(payload, state.error), state.backfillError].filter(Boolean).join(" · ");
    const delay = refreshDelay(payload?.marketPhase, Boolean(state.error));
    document.getElementById("refresh-meta").textContent = `${delay / 1000}秒`;

    const dot = document.getElementById("status-dot");
    dot.className = `status-dot${state.error ? " is-error" : payload?.stale || !isActivePhase(payload?.marketPhase) ? " is-stale" : " is-live"}`;

    const chart = document.getElementById("pnl-chart");
    const empty = document.getElementById("empty-state");
    state.chartModel = null;
    hideTooltip();
    if (!projected.length) {
      chart.setAttribute("hidden", "");
      empty.hidden = false;
      empty.textContent = state.error && !payload
        ? `盘中盈亏暂不可用：${state.error}`
        : payload?.refreshing
          ? "盘中盈亏轨迹正在生成…"
          : payload?.message || (series.length ? "固定交易时段内暂无点位" : "正在等待首个完整有效的盘中行情点");
      return;
    }
    empty.hidden = true;
    chart.removeAttribute("hidden");
    drawChart(projected, selection.label);
  }

  function friendlyError(error) {
    if (error?.name === "AbortError") return "请求超时";
    if (error instanceof TypeError) return "看板服务未连接";
    return String(error?.message || error || "盘中盈亏暂不可用").slice(0, 120);
  }

  async function autoBackfill(tradeDate, gapKey) {
    const attemptKey = `${tradeDate}:${gapKey}`;
    if (state.backfillInFlight || state.backfillAttempts.has(attemptKey)) return;
    state.backfillAttempts.add(attemptKey);
    state.backfillInFlight = true;
    state.backfillError = "";
    render();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), BACKFILL_REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch("/api/intraday-pnl/backfill", {
        method: "POST",
        cache: "no-store",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ portfolio: DEFAULT_PORTFOLIO_ID, dates: [tradeDate] }),
        signal: controller.signal,
      });
      const raw = await response.json();
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("回补接口返回为空");
      if (!response.ok || raw.ok === false) throw new Error(raw.error || `回补接口返回 ${response.status}`);
      await refresh();
    } catch (error) {
      state.backfillError = `自动回补失败：${friendlyError(error)}`;
    } finally {
      clearTimeout(timeout);
      state.backfillInFlight = false;
      render();
    }
  }

  function scheduleNext() {
    clearTimeout(state.timer);
    const delay = refreshDelay(state.payload?.marketPhase, Boolean(state.error));
    state.timer = setTimeout(() => refresh(), delay);
  }

  async function refresh() {
    if (state.inFlight) return;
    state.inFlight = true;
    clearTimeout(state.timer);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(intradayPnlUrl(), {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`接口返回 ${response.status}`);
      const raw = await response.json();
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("接口返回为空");
      state.payload = normalizePayload(raw);
      state.error = state.payload.ok ? "" : state.payload.message || "盘中盈亏暂不可用";
    } catch (error) {
      state.error = friendlyError(error);
    } finally {
      clearTimeout(timeout);
      state.inFlight = false;
      render();
      global.desktopWidget?.setMarketPhase?.(state.payload?.marketPhase || "");
      const gapKey = state.error ? "" : backfillGapKey(state.payload);
      if (gapKey) void autoBackfill(state.payload.asOf, gapKey);
      scheduleNext();
    }
  }

  function handleChartPointer(event) {
    const model = state.chartModel;
    if (!model?.points.length) return;
    const svg = document.getElementById("pnl-chart");
    const rect = svg.getBoundingClientRect();
    if (!(rect.width > 0 && rect.height > 0)) return;
    const chartX = (event.clientX - rect.left) / rect.width * model.width;
    const chartY = (event.clientY - rect.top) / rect.height * model.height;
    const { left, right, top, bottom } = model.padding;
    if (chartX < left || chartX > model.width - right || chartY < top || chartY > model.height - bottom) {
      hideTooltip();
      return;
    }
    let nearest = 0;
    let distance = Infinity;
    model.points.forEach((point, index) => {
      const current = Math.abs(point.x - chartX);
      if (current < distance) {
        nearest = index;
        distance = current;
      }
    });
    const point = model.points[nearest];
    model.hoverLine.setAttribute("x1", point.x);
    model.hoverLine.setAttribute("x2", point.x);
    model.hoverLine.setAttribute("visibility", "visible");
    model.hoverDot.setAttribute("cx", point.x);
    model.hoverDot.setAttribute("cy", point.y);
    model.hoverDot.setAttribute("visibility", "visible");
    const tooltip = document.getElementById("chart-tooltip");
    document.getElementById("tooltip-time").textContent = formatTime(point.time);
    const value = document.getElementById("tooltip-value");
    const sourceNote = point.source === "backfill"
      ? " · 回补估算"
      : point.source === "corrected" ? " · 已矫正" : "";
    value.textContent = `${formatAmount(point.pnl)} 元${sourceNote}`;
    value.className = directionClass(point.pnl);
    // 提示框位于被缩放的壳体内，left/top 必须使用布局尺寸而不是缩放后的可视尺寸。
    const layoutWidth = svg.clientWidth || rect.width;
    const layoutHeight = svg.clientHeight || rect.height;
    const visualScale = layoutHeight > 0 ? rect.height / layoutHeight : 1;
    tooltip.style.left = `${point.x / model.width * layoutWidth}px`;
    tooltip.style.top = `${Math.max(24 / visualScale, point.y / model.height * layoutHeight)}px`;
    tooltip.hidden = false;
  }

  function start() {
    if (state.started) return;
    state.started = true;
    state.amountHidden = readAmountHidden();
    syncAmountToggle();
    syncPinToggle();
    syncWidgetScale();
    bindResizeHandles();
    document.getElementById("amount-toggle").addEventListener("click", toggleAmount);
    document.getElementById("pin-toggle").addEventListener("click", togglePin);
    let resizeRenderTimer = null;
    global.addEventListener("resize", () => {
      syncWidgetScale();
      clearTimeout(resizeRenderTimer);
      resizeRenderTimer = setTimeout(render, 80);
    });
    void global.desktopWidget?.getState?.().then((info) => {
      if (!info || typeof info.pinned !== "boolean") return;
      state.pinned = info.pinned;
      syncPinToggle();
    }).catch(() => {});
    const shell = document.querySelector(".widget-shell");
    let dragPointerId = null;
    const endPointerDrag = (event) => {
      if (dragPointerId !== event.pointerId) return;
      dragPointerId = null;
      if (shell.hasPointerCapture(event.pointerId)) shell.releasePointerCapture(event.pointerId);
      global.desktopWidget?.endDrag?.();
    };
    shell.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest(".no-drag, .resize-handle, select, input, button")) return;
      if (!global.desktopWidget?.startDrag) return;
      event.preventDefault();
      dragPointerId = event.pointerId;
      shell.setPointerCapture(event.pointerId);
      global.desktopWidget.startDrag(event.screenX, event.screenY);
    });
    shell.addEventListener("pointermove", (event) => {
      if (dragPointerId !== event.pointerId) return;
      global.desktopWidget.dragTo?.(event.screenX, event.screenY);
    });
    shell.addEventListener("pointerup", endPointerDrag);
    shell.addEventListener("pointercancel", endPointerDrag);
    shell.addEventListener("dblclick", (event) => {
      if (event.target.closest(".no-drag, .resize-handle, select, input, button")) return;
      event.preventDefault();
      global.desktopWidget?.openDashboard?.()?.catch?.(() => {});
    });
    global.addEventListener("blur", () => {
      if (dragPointerId === null) return;
      const pointerId = dragPointerId;
      dragPointerId = null;
      if (shell.hasPointerCapture(pointerId)) shell.releasePointerCapture(pointerId);
      global.desktopWidget?.endDrag?.();
    });
    document.getElementById("symbol-selector").addEventListener("change", (event) => {
      state.selected = event.target.value || "total";
      render();
    });
    document.getElementById("pnl-chart").addEventListener("pointermove", handleChartPointer);
    document.getElementById("pnl-chart").addEventListener("pointerleave", hideTooltip);
    global.addEventListener("online", () => refresh());
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
    });
    render();
    void refresh();
  }

  const api = {
    ACTIVE_REFRESH_MS,
    IDLE_REFRESH_MS,
    BACKFILL_REQUEST_TIMEOUT_MS,
    BASE_WIDTH,
    BASE_HEIGHT,
    BASE_FONT_PX,
    FONT_CAP_PX,
    MAX_SCALE,
    SESSIONS,
    AXIS_DURATION_MS,
    DEFAULT_PORTFOLIO_ID,
    intradayPnlUrl,
    widgetTitle,
    normalizePayload,
    normalizeSeries,
    selectedSeries,
    clockMilliseconds,
    axisPosition,
    projectSeries,
    splitSeriesAtGaps,
    shanghaiDateKey,
    backfillGapKey,
    formatAmount,
    formatTime,
    phaseLabel,
    refreshDelay,
    buildNote,
    start,
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  global.PnlWidget = api;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
    else start();
  }
})(typeof window !== "undefined" ? window : globalThis);
