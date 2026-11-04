import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const widget = require("../dashboard/widget.js");
const schedule = require("../scripts/widget_schedule.cjs");
const html = readFileSync(new URL("../dashboard/widget.html", import.meta.url), "utf8");
const source = readFileSync(new URL("../dashboard/widget.js", import.meta.url), "utf8");
const mainSource = readFileSync(new URL("../scripts/desktop_widget.cjs", import.meta.url), "utf8");
const nativeSource = readFileSync(new URL("../scripts/desktop_window_level.mm", import.meta.url), "utf8");
const css = readFileSync(new URL("../dashboard/widget.css", import.meta.url), "utf8");

test("标准化当前接口并保留账户时效、零值和逐股序列", () => {
  const payload = widget.normalizePayload({
    ok: true,
    available: true,
    as_of: "2026-10-30",
    generated_at: "2026-10-30T12:21:32+08:00",
    market_phase: "lunch",
    account_snapshot: { as_of: "2026-09-04", current_verified: false },
    total_series: [
      { time: "2026-10-30T10:00:00+08:00", pnl: 100 },
      { time: "2026-10-30T09:30:00+08:00", pnl: 0 },
      { time: "2026-10-30T10:00:00+08:00", pnl: 120 },
      { time: "bad", pnl: 500 },
      { time: "2026-10-30T10:01:00+08:00", pnl: null },
    ],
    positions: [{ symbol: "600519.SH", name: "贵州茅台", series: [["09:30:00", -20]] }],
  });
  assert.equal(payload.asOf, "2026-10-30");
  assert.equal(payload.marketPhase, "lunch");
  assert.equal(payload.account.current, false);
  assert.equal(payload.totalSeries.length, 2);
  assert.equal(payload.totalSeries[0].pnl, 0);
  assert.equal(payload.totalSeries[1].pnl, 120);
  assert.equal(widget.selectedSeries(payload, "total").label, "全部持仓合计");
  assert.equal(widget.selectedSeries(payload, "600519.SH").label, "贵州茅台");
  assert.equal(widget.selectedSeries(payload, "600519.SH").series[0].pnl, -20);
});

test("桌面 Widget 默认请求 1020 组合并显示组合名称", () => {
  assert.equal(widget.DEFAULT_PORTFOLIO_ID, "pf-1020abcdef");
  assert.equal(widget.intradayPnlUrl(), "/api/intraday-pnl/history?portfolio=pf-1020abcdef");
  assert.equal(widget.intradayPnlUrl("main"), "/api/intraday-pnl/history?portfolio=main");
  assert.equal(widget.widgetTitle(), "1020");
  const payload = widget.normalizePayload({ portfolio_label: "1020" });
  assert.equal(widget.widgetTitle(payload.portfolioLabel), "1020");
  assert.match(html, /<h1 id="widget-title">1020<\/h1>/);
  assert.match(source, /fetch\(intradayPnlUrl\(\)/);
  assert.match(source, /fetch\("\/api\/intraday-pnl\/backfill"/);
  assert.match(source, /backfillGapKey\(state\.payload\)/);
  assert.equal(widget.BACKFILL_REQUEST_TIMEOUT_MS, 120_000);
});

test("固定交易轴压缩午休并剔除交易时段外点位", () => {
  const hour = 3_600_000;
  assert.equal(widget.axisPosition(widget.clockMilliseconds("09:30:00")).plotTimeValue, 0);
  assert.equal(widget.axisPosition(widget.clockMilliseconds("11:30:00")).plotTimeValue, 2 * hour);
  assert.equal(widget.axisPosition(widget.clockMilliseconds("13:00:00")).plotTimeValue, 2 * hour);
  assert.equal(widget.axisPosition(widget.clockMilliseconds("15:00:00")).plotTimeValue, 4 * hour);
  assert.equal(widget.axisPosition(widget.clockMilliseconds("12:00:00")), null);
  assert.equal(widget.axisPosition(widget.clockMilliseconds("09:29:59")), null);
  const projected = widget.projectSeries(widget.normalizeSeries([
    ["09:30:00", -10],
    ["12:00:00", 999],
    ["13:00:00", 20],
  ]));
  assert.deepEqual(projected.map((point) => point.pnl), [-10, 20]);
});

test("时间缺口拆线且同时间戳保留最后值", () => {
  const series = widget.projectSeries(widget.normalizeSeries([
    ["09:30:00", 1],
    ["09:31:00", 2],
    ["09:31:00", 3],
    ["10:40:00", 4],
  ]));
  assert.equal(series.length, 3);
  assert.equal(series[1].pnl, 3);
  assert.deepEqual(widget.splitSeriesAtGaps(series).map((segment) => segment.length), [2, 1]);
});

test("当天内部断点需要回补，历史日期与非交易日不触发", () => {
  const payload = widget.normalizePayload({
    as_of: "2026-10-30",
    market_phase: "closed",
    total_series: [
      { time: "2026-10-30T09:30:00+08:00", pnl: 0 },
      { time: "2026-10-30T09:31:00+08:00", pnl: 10 },
      { time: "2026-10-30T10:40:00+08:00", pnl: 20 },
    ],
  });
  const gapKey = widget.backfillGapKey(payload, "2026-10-30");
  assert.match(gapKey, /09:31:00\+08:00>2026-10-30T10:40:00/);
  assert.equal(widget.backfillGapKey(payload, "2026-10-31"), "");
  assert.equal(widget.backfillGapKey({ ...payload, marketPhase: "non_trading" }, "2026-10-30"), "");
  assert.equal(widget.backfillGapKey({ ...payload, marketPhase: "pre_open" }, "2026-10-30"), "");

  const empty = widget.normalizePayload({ as_of: "2026-10-30", market_phase: "open", total_series: [] });
  assert.equal(widget.backfillGapKey(empty, "2026-10-30"), "empty");
  const leading = widget.normalizePayload({
    as_of: "2026-10-30",
    market_phase: "open",
    total_series: [{ time: "2026-10-30T10:00:00+08:00", pnl: 1 }],
  });
  assert.match(widget.backfillGapKey(leading, "2026-10-30"), /session-start/);
  const trailing = widget.normalizePayload({
    as_of: "2026-10-30",
    market_phase: "closed",
    total_series: [{ time: "2026-10-30T09:30:00+08:00", pnl: 1 }],
  });
  assert.match(widget.backfillGapKey(trailing, "2026-10-30"), /session-end/);
  assert.equal(widget.backfillGapKey({ ...trailing, marketPhase: "open" }, "2026-10-30"), "");
  assert.equal(widget.shanghaiDateKey(new Date("2026-10-30T16:30:00Z")), "2026-10-31");
});

test("回补点保留来源且实时与回补采样交界处不再拆线", () => {
  const series = widget.normalizeSeries([
    { time: "2026-10-30T09:30:00+08:00", pnl: 0, source: "recorded" },
    { time: "2026-10-30T09:31:00+08:00", pnl: 10, source: "recorded" },
    { time: "2026-10-30T09:36:00+08:00", pnl: 20, source: "backfill" },
    { time: "2026-10-30T09:41:00+08:00", pnl: 30, source: "backfill" },
    { time: "2026-10-30T09:43:00+08:00", pnl: 40, source: "corrected", pnl_recorded: 99 },
    { time: "2026-10-30T09:50:00+08:00", pnl: 50, source: "backfill" },
  ]);
  assert.equal(series[2].source, "backfill");
  assert.equal(series[4].source, "corrected");
  assert.equal(series[4].pnlRecorded, 99);
  const projected = widget.projectSeries(series);
  assert.equal(widget.splitSeriesAtGaps(projected).length, 1);
  const payload = widget.normalizePayload({
    as_of: "2026-10-30",
    market_phase: "open",
    total_series: series,
  });
  assert.equal(widget.backfillGapKey(payload, "2026-10-30"), "");
});

test("金额、时间与阶段显示遵守红涨绿跌所需数值方向", () => {
  assert.equal(widget.formatAmount(12345.6), "+12,345.60");
  assert.equal(widget.formatAmount(-12345.6), "−12,345.60");
  assert.equal(widget.formatAmount(0), "0.00");
  assert.equal(widget.formatAmount(null), "--");
  assert.equal(widget.formatTime("2026-10-30T09:30:04+08:00"), "09:30:04");
  assert.equal(widget.phaseLabel("open"), "连续竞价");
  assert.equal(widget.phaseLabel("lunch"), "午间休市");
});

test("精简桌面 Widget 保留置顶与金额显隐按钮且可自由缩放", () => {
  assert.match(html, /id="amount-toggle"/);
  assert.match(html, /id="pin-toggle"/);
  assert.match(html, /class="eye eye-open"/);
  assert.match(html, /class="eye eye-closed"/);
  assert.equal((html.match(/class="resize-handle"/g) || []).length, 8);
  assert.doesNotMatch(html, /opacity-panel|desktop-button/);
  assert.match(mainSource, /DEFAULT_BOUNDS = \{ width: 180, height: 130 \}/);
  assert.match(mainSource, /DEFAULT_OPACITY = 0\.6/);
  assert.match(mainSource, /resizable:\s*true/);
  assert.match(mainSource, /minWidth: MIN_BOUNDS\.width/);
  assert.match(mainSource, /maxWidth: MAX_BOUNDS\.width/);
});

test("置顶按钮切换悬浮层级并记住选择", () => {
  assert.match(html, /id="pin-toggle"/);
  assert.match(html, /class="pin-icon"/);
  assert.match(source, /togglePin/);
  assert.match(source, /getState\?\./);
  assert.match(source, /setPinned\?\./);
  assert.match(source, /is-active/);
  assert.match(css, /\.icon-button\.is-active \.pin-icon/);
  assert.match(css, /rotate\(-40deg\)/);
  assert.match(mainSource, /widget:set-pinned/);
  assert.match(mainSource, /pinnedOnTop/);
  assert.match(mainSource, /readSavedPinned/);
  assert.match(mainSource, /pinned: pinnedOnTop/);
});

test("缩放按窗口尺寸同步 CSS 比例并限制最小最大尺寸", () => {
  assert.match(source, /syncWidgetScale/);
  assert.match(source, /--widget-scale/);
  assert.match(source, /setProperty\("--widget-scale"/);
  assert.match(source, /svg\.clientWidth/);
  assert.match(html, /data-direction="se"/);
  assert.match(source, /startResize/);
  assert.match(source, /resizeTo/);
  assert.match(mainSource, /widget:resize-start/);
  assert.match(mainSource, /widget:resize-move/);
  assert.match(mainSource, /widget:resize-end/);
  assert.match(mainSource, /widgetWindow\.setBounds/);
  assert.match(mainSource, /MIN_BOUNDS = \{ width: 140, height: 100 \}/);
  assert.match(mainSource, /MAX_BOUNDS = \{ width: 900, height: 700 \}/);
});

test("窗口放大时字体上限为五号", () => {
  assert.equal(widget.BASE_FONT_PX, 9);
  assert.equal(widget.FONT_CAP_PX, 14);
  assert.equal(widget.MAX_SCALE, 14 / 9);
  assert.ok(widget.MAX_SCALE < 1.6);
});

test("眼睛按钮切换金额显隐并记住选择", () => {
  assert.match(source, /AMOUNT_HIDDEN_KEY/);
  assert.match(source, /MASKED_AMOUNT/);
  assert.match(source, /localStorage/);
  assert.match(source, /toggleAmount/);
  assert.match(source, /amount-hidden/);
  assert.match(source, /aria-pressed/);
  assert.match(html, /aria-label="隐藏金额"/);
  assert.doesNotMatch(html, /<h1 id="widget-title">[^<]*盘中当日盈亏/);
});

test("活跃期十五秒刷新，非活跃期六十秒，错误时十五秒重试", () => {
  assert.equal(widget.refreshDelay("open"), 15_000);
  assert.equal(widget.refreshDelay("auction"), 15_000);
  assert.equal(widget.refreshDelay("lunch"), 60_000);
  assert.equal(widget.refreshDelay("closed"), 60_000);
  assert.equal(widget.refreshDelay("closed", true), 15_000);
});

test("错误、缓存和过期账户快照都进入显式状态说明", () => {
  const note = widget.buildNote({
    account: { asOf: "2026-09-04", current: false },
    stale: true,
  }, "看板服务未连接");
  assert.match(note, /更新失败：看板服务未连接/);
  assert.match(note, /2026-09-04/);
  assert.match(note, /未核验/);
  assert.match(note, /缓存轨迹/);
});

test("组件 DOM 引用完整且不会主动触发上游强制刷新", () => {
  const ids = [...source.matchAll(/getElementById\("([^"]+)"\)/g)].map((match) => match[1]);
  for (const id of new Set(ids)) assert.match(html, new RegExp(`id="${id}"`), `缺少 #${id}`);
  assert.doesNotMatch(source, /refresh=1/);
  assert.doesNotMatch(source, /nodeIntegration\s*:\s*true/);
});

test("交易时段浮动置顶显示，收盘后自动隐藏，并保留安全隔离", () => {
  assert.match(mainSource, /setAlwaysOnTop\(enabled, "floating"\)/);
  assert.match(mainSource, /setVisibleOnAllWorkspaces\(true, \{ visibleOnFullScreen: true \}\)/);
  assert.match(mainSource, /showInactive\(\)/);
  assert.match(mainSource, /widget:set-market-phase/);
  assert.match(mainSource, /schedule\.shouldDisplay/);
  assert.match(mainSource, /contextIsolation:\s*true/);
  assert.match(mainSource, /nodeIntegration:\s*false/);
  assert.match(mainSource, /sandbox:\s*true/);
  assert.match(mainSource, /backgroundThrottling:\s*false/);
  assert.match(mainSource, /widgetWindow\.setOpacity\(normalized\)/);
  assert.match(mainSource, /Math\.max\(0\.35, opacity\)/);
  assert.match(source, /setMarketPhase\?\./);
  assert.match(nativeSource, /SetFloatingMode/);
  assert.match(nativeSource, /kCGFloatingWindowLevelKey/);
  assert.match(nativeSource, /NSWindowCollectionBehaviorCanJoinAllSpaces/);
  assert.match(nativeSource, /NSWindowCollectionBehaviorStationary/);
  assert.match(nativeSource, /NSWindowCollectionBehaviorIgnoresCycle/);
  assert.match(nativeSource, /NSWindowCollectionBehaviorFullScreenAuxiliary/);
  assert.match(mainSource, /acceptFirstMouse:\s*true/);
  assert.match(mainSource, /widget:drag-start/);
  assert.match(mainSource, /widget:drag-move/);
  assert.match(mainSource, /widget:drag-end/);
  assert.match(mainSource, /widgetWindow\.setPosition/);
  assert.match(source, /setPointerCapture/);
  assert.match(source, /screenX/);
  assert.match(source, /dragTo\?\./);
  assert.match(source, /addEventListener\("blur"/);
  assert.doesNotMatch(nativeSource, /addGlobalMonitorForEventsMatchingMask/);
});

test("上海时区的交易窗口决定显示时段", () => {
  const at = (iso) => new Date(iso);
  assert.equal(schedule.shouldDisplay(at("2026-11-02T01:14:00Z"), "auction"), false); // 09:14
  assert.equal(schedule.shouldDisplay(at("2026-11-02T01:15:00Z"), "auction"), true); // 09:15
  assert.equal(schedule.shouldDisplay(at("2026-11-02T04:00:00Z"), "lunch"), true); // 12:00
  assert.equal(schedule.shouldDisplay(at("2026-11-02T07:04:00Z"), "post_close"), true); // 15:04
  assert.equal(schedule.shouldDisplay(at("2026-11-02T07:05:00Z"), "post_close"), false); // 15:05
  assert.equal(schedule.shouldDisplay(at("2026-11-02T02:00:00Z"), "holiday"), false);
  assert.equal(schedule.shouldDisplay(at("2026-11-02T02:00:00Z"), "weekend"), false);
  assert.equal(schedule.shouldDisplay(at("2026-10-31T02:00:00Z"), "open"), false); // 周六 10:00
});
