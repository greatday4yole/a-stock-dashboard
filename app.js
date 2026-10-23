"use strict";

const QUOTE_INTERVAL_MS = 5_000;
const QUOTE_INACTIVE_INTERVAL_MS = 60_000;
const INTRADAY_PNL_INTERVAL_MS = 15_000;
const INTRADAY_PNL_INACTIVE_CHECK_MS = 60_000;
const DAILY_PNL_HISTORY_INTERVAL_MS = 60_000;
const DAILY_PNL_HISTORY_START_DATE = "2026-07-01";
const PERSONAL_INDEX_INTERVAL_MS = 5 * 60_000;
const PERSONAL_INDEX_REQUEST_TIMEOUT_MS = 15_000;
const PERSONAL_INDEX_DATA_URL = "/api/personal-index";
const PERSONAL_INDEX_PERIOD_LABELS = Object.freeze({
  day: { singular: "日", kline: "日K线", detail: "每日", pnlLabel: "当日盈亏" },
  week: { singular: "周", kline: "周K线", detail: "每周", pnlLabel: "本周盈亏" },
  month: { singular: "月", kline: "月K线", detail: "每月", pnlLabel: "本月盈亏" },
});
const RISK_INTERVAL_MS = 30_000;
const NEWS_INTERVAL_MS = 5 * 60_000;
const SCHOOL_SIGNAL_INTERVAL_MS = 5 * 60_000;
const HEALTH_INTERVAL_MS = 60_000;
const REQUEST_TIMEOUT_MS = 9_000;
const AUTO_PAPER_REQUEST_TIMEOUT_MS = 30_000;
const INTRADAY_PNL_REQUEST_TIMEOUT_MS = 30_000;
const DAILY_PNL_HISTORY_REQUEST_TIMEOUT_MS = 30_000;
const RISK_REQUEST_TIMEOUT_MS = 25_000;
const DECISION_REQUEST_TIMEOUT_MS = 45_000;
const STOCK_CHART_CACHE_TTL_MS = 15_000;
const STOCK_CHART_HOVER_CLOSE_MS = 180;
const STOCK_CHART_MAX_KLINE_POINTS = 260;
const STOCK_CHART_MAX_KLINE_FETCH_POINTS = 500;
const STOCK_CHART_MAX_INTRADAY_POINTS = 120;
const STOCK_CHART_PERIOD_LABELS = Object.freeze({
  day: { singular: "日", kline: "日K线" },
  week: { singular: "周", kline: "周K线" },
  month: { singular: "月", kline: "月K线" },
});
const INTRADAY_PNL_SESSIONS = Object.freeze([
  Object.freeze({ startMs: (9 * 60 + 30) * 60_000, endMs: (11 * 60 + 30) * 60_000 }),
  Object.freeze({ startMs: 13 * 60 * 60_000, endMs: 15 * 60 * 60_000 }),
]);
const INTRADAY_PNL_AXIS_DURATION_MS = 4 * 60 * 60_000;
// 历史分时回看：连续 n 个交易日的拼接展示参数。
const INTRADAY_HISTORY_DAY_OPTIONS = Object.freeze([2, 3, 5, 10, 15, 20, 30]);
const INTRADAY_HISTORY_DEFAULT_DAYS = 5;
const INTRADAY_HISTORY_DAYS_STORAGE_KEY = "stock-dashboard-intraday-history-days";
const INTRADAY_HISTORY_CACHE_LIMIT = 120;
// 已完结交易日的盘中盈亏持久化到 IndexedDB，页面重开/切换天数不再重复拉取。
// （localStorage 键仅保留用于清理早期版本的遗留缓存）
const INTRADAY_HISTORY_CACHE_STORAGE_KEY = "stock-dashboard-intraday-history-cache-v1";
const INTRADAY_HISTORY_IDB_NAME = "stock-dashboard-intraday-history";
const INTRADAY_HISTORY_IDB_STORE = "dayCache";
const INTRADAY_HISTORY_BACKFILL_TIMEOUT_MS = 120_000;
// 历史日请求在服务重启后需现场解码 7MB/天的账本，冷缓存首载可能明显变慢。
const INTRADAY_HISTORY_REQUEST_TIMEOUT_MS = 60_000;

const INDEX_ORDER = ["上证综指", "深证成指", "创业板指", "科创50", "沪深300", "中证500", "中证1000"];
// 支持K线/分时弹层的指数白名单（与后端 INDEX_CHART_INSTRUMENTS 对应）：
// A股指数用腾讯 sh/sz 指数代码；全球参考全部 13 项均已配置图表数据源
// （海外指数/期货/外汇走东财或新浪，保留新浪原始符号大小写）。
const INDEX_CHART_SYMBOLS = new Set([
  "sh000001", "sz399001", "sz399006", "sh000688", "sh000300", "sh000905", "sh000852",
]);
// 主要市场卡片内嵌当日分时：与 INDEX_ORDER 同序；首屏并发加载，之后每只
// 120 秒一轮、相位均匀错开（每只间隔 120/7 ≈ 17 秒），避免同时打接口。
const INDEX_INTRADAY_SYMBOLS = Object.freeze([
  "sh000001", "sz399001", "sz399006", "sh000688", "sh000300", "sh000905", "sh000852",
]);
const INDEX_INTRADAY_REFRESH_MS = 120_000;
// 先行指标（A50 + 期权 IV/PCR + 两融 + 北向 + 涨跌停 + 股指基差 + 货币市场 + 盘口）：
// 每 8 秒拉一次；每个可得因子前端累积出分时曲线显示在卡片上。
// 期权 IV 冷计算较重，后端自带缓存，5-10 秒轮询可命中缓存。
const LEADING_INDICATORS_REFRESH_MS = 8_000;
// 衍生品仪表盘：后端已缓存（5 分钟 TTL），这里 5 分钟同步一次即可
const DERIVATIVES_REFRESH_MS = 300_000;
const LEADING_SPARK_MAX_POINTS = 180;
// ASAP 加载期：只要还有任何分时图点位不足 2 个（无法连线），就每 2 秒再拉一次，
// 直到所有可得因子的分时图都能画线；之后回归 8 秒轮询。最多连续 8 次（16 秒），
// 避免某个因子持续不可用时无限早刷新。
const LEADING_EARLY_REFRESH_MS = 2_000;
const LEADING_EARLY_REFRESH_MAX = 8;

// 每个先行指标的分时图：key 唯一标识卡片，extract 从轮询 payload 取该指标的可数值化值。
// 取不到值（不可用）的因子不累积点位，分时图保持隐藏，不阻塞早刷新逻辑。
const LEADING_SPARK_KEYS = Object.freeze([
  { key: "a50", extract: (p) => (p?.a50?.available ? p.a50.price : null) },
  { key: "iv_0", extract: (p) => p?.options?.items?.[0]?.iv_near?.iv ?? null },
  { key: "iv_1", extract: (p) => p?.options?.items?.[1]?.iv_near?.iv ?? null },
  { key: "margin_rzye", extract: (p) => (p?.margin?.available ? p.margin.rzye_yi : null) },
  { key: "northbound", extract: (p) => (p?.northbound?.available ? p.northbound.net_yi : null) },
  { key: "limit_up", extract: (p) => (p?.limit_pool?.available ? p.limit_pool.limit_up : null) },
  { key: "basis_IF", extract: (p) => p?.basis?.rows?.[0]?.futures_price ?? null },
  { key: "basis_IH", extract: (p) => p?.basis?.rows?.[1]?.futures_price ?? null },
  { key: "basis_IC", extract: (p) => p?.basis?.rows?.[2]?.futures_price ?? null },
  { key: "basis_IM", extract: (p) => p?.basis?.rows?.[3]?.futures_price ?? null },
  { key: "dr007", extract: (p) => p?.money_rates?.rates?.dr007?.value ?? null },
  { key: "imbalance", extract: (p) => (p?.imbalance?.available ? p.imbalance.imbalance : null) },
]);
const GLOBAL_CHART_SYMBOLS = new Set([
  "b_KOSPI", "b_NKY", "hkHSI", "usINX", "usIXIC", "usDJI",
  "hf_ES", "hf_NQ", "DINIW", "fx_susdcnh", "US10Y",
  // 全球参考新增：海外半导体/科技个股、半导体 ETF 与更多指数。
  "usNDX", "hkHSTECH", "usSOXX", "usSMH", "usIWM", "usVIXY",
  "usNVDA", "usMU", "usTSM", "usASML", "usAMD", "usAVGO", "usQCOM",
  "usINTC", "usAMAT", "usLRCX", "usKLAC", "usARM", "usMRVL",
  "usWDC", "usSTX", "usSNDK",
  "b_SAMSUNG", "b_TWII",
  // 隔夜全球标的（与后端 EM_OVERNIGHT_INSTRUMENTS / INDEX_CHART_INSTRUMENTS 对应）。
  "b_FTSE", "b_GDAXI", "b_FCHI", "b_CL", "b_GC", "b_SI", "b_HG",
  "b_MSFT", "b_META", "b_NVDA", "b_GOOGL", "b_AAPL", "b_AMZN", "b_TSLA",
  "b_SNDK", "b_STX", "b_WDC", "b_MU", "b_SKHYNIX",
  "b_RKLB", "b_LUNR", "b_ASTS",
  "b_COP", "b_EOG", "b_CVX", "b_XOM",
  "b_LI", "b_NIO", "b_PDD", "b_XPEV", "b_JD", "b_BABA", "b_TCOM", "b_TCEHY",
  "b_NTES", "b_BIDU", "b_BILI",
]);

// 隔夜全球标的的分组展示顺序（与后端 EM_OVERNIGHT_INSTRUMENTS 的 group 字段对应）。
const OVERNIGHT_GROUP_ORDER = ["欧股指数", "商品", "美股科技", "存储链", "太空概念", "油气", "中概股"];

// 隔夜标的与持仓（信用+普通账户市值加权）的相关度快照：2026-10-14 基于近 180
// 个交易日日收益率本地计算。|同日相关|≥0.15 的卡片加 ★ 高亮；负相关（如雪佛龙）
// 实为对冲项，悬浮提示中注明。完整矩阵见会话记录，此处只保留高亮阈值内的标的。
const OVERNIGHT_PORTFOLIO_CORRELATION = {
  b_SKHYNIX: { same: 0.35, next: -0.07 },
  b_WDC: { same: 0.19, next: 0.18 },
  b_SNDK: { same: 0.18, next: 0.15 },
  b_STX: { same: 0.17, next: 0.15 },
  b_NVDA: { same: 0.18, next: 0.09 },
  b_BIDU: { same: 0.17, next: 0.13 },
  b_MU: { same: 0.16, next: 0.11 },
  b_TSLA: { same: 0.15, next: 0.08 },
};
const INDEX_ALIASES = {
  "000001.SH": "上证综指", "sh000001": "上证综指", "1A0001": "上证综指", 上证指数: "上证综指",
  "399001.SZ": "深证成指", "sz399001": "深证成指",
  "399006.SZ": "创业板指", "sz399006": "创业板指",
  "000688.SH": "科创50", "sh000688": "科创50",
  "000300.SH": "沪深300", "sh000300": "沪深300",
  "000905.SH": "中证500", "sh000905": "中证500",
  "000852.SH": "中证1000", "sh000852": "中证1000",
};

const SOURCE_LABELS = {
  cls: "财联社",
  eastmoney: "东方财富",
  google_news: "Google 新闻",
  cninfo: "巨潮资讯",
  sina: "新浪财经",
  wallstreetcn: "华尔街见闻",
  upstream: "实时上游",
  memory: "本地缓存",
};

const MARKET_NEWS_TAG_LABELS = {
  regulatory: "监管",
  earnings_downside: "业绩风险",
  shareholder_pressure: "股东压力",
  credit_stress: "信用风险",
  geopolitical: "地缘风险",
  market_stress: "市场压力",
  tightening: "流动性收紧",
  policy_support: "政策支持",
  earnings_upside: "业绩改善",
  orders_and_contracts: "订单合同",
  shareholder_support: "回购增持",
  innovation: "产业进展",
  capital_inflow: "资金流入",
};

// 持仓行业 → 新闻关键词。行业名本身（按“与/、/”切分）也会参与匹配；
// 命中任意关键词的快讯视为持仓关联板块新闻。
const SECTOR_NEWS_KEYWORDS = Object.freeze({
  "服务器与算力": ["算力", "服务器", "液冷", "光模块", "CPO", "英伟达"],
  "数据中心": ["数据中心", "IDC", "算力", "液冷"],
  "半导体材料": ["半导体", "电子特气", "电子化学品", "晶圆", "光刻"],
  "半导体": ["半导体", "存储芯片", "DRAM", "晶圆", "内存"],
  "存储与数字资产": ["存储", "NAND", "DRAM", "内存", "数字资产"],
  "电子元件": ["电感", "滤波器", "被动元件", "射频"],
  "AI主题ETF": ["人工智能", "AI", "大模型", "算力"],
  "电力ETF": ["电力", "电网", "储能", "特高压", "绿电"],
  "营销传媒": ["营销", "广告", "传媒"],
  "计算机设备": ["信创", "国产算力", "计算机"],
  "环保与能源工程": ["环保", "节能", "光热", "电站", "能源工程"],
  "机器人与智能装备": ["机器人", "智能装备", "自动化"],
  "机场与商业地产": ["机场", "航空", "商业地产", "免税"],
  "电网设备": ["电网设备", "变压器", "低压电器", "电气设备"],
  "数据服务": ["数据要素", "数据服务", "大数据"],
  "专业视听设备": ["视听", "音视频", "显控"],
});

const POSITION_SORT_KEYS = new Set(["price", "changePct", "quantity", "marketValue", "dayPnl", "weight", "turnover", "pe", "pb"]);

// 持仓行情信号列沿用「持仓雷达」本地规则引擎的即时规则与默认阈值；
// 连续下跌 / MA20 / 回撤 / 创新高等依赖每日收盘积累的规则未启用。
const SIGNAL_THRESHOLDS = {
  dropDay: -5, riseDay: 5, peHigh: 100, pbHigh: 15, peLow: 15, pbLow: 2,
  weightWarn: 15, weightDanger: 20,
  volUpTurn: 8, volUpChg: 3, volDnTurn: 10, volDnChg: -3, nearStop: 3,
};
const SIGNAL_LEVEL_ORDER = { danger: 0, warn: 1, op: 2 };
const PRICE_LINES_STORAGE_KEY = "stock-dashboard-price-lines";

const MAIN_PORTFOLIO_ID = "main";
const COMPARE_VIEW_ID = "compare";
const COMPARE_INTERVAL_MS = 15_000;

const state = {
  quotePayload: null,
  riskPayload: null,
  newsPayload: null,
  marketNewsPayload: null,
  healthPayload: null,
  decisionPayload: null,
  marketStatePayload: null,
  eventsPayload: null,
  opportunitiesPayload: null,
  rebalancePayload: null,
  intradayPnlPayload: null,
  intradayPnl: null,
  dailyPnlHistoryPayload: null,
  dailyPnlHistory: null,
  positions: [],
  markets: [],
  breadth: [],
  globals: [],
  overnight: [],
  news: [],
  marketNews: [],
  selectedSymbol: "all",
  selectedPnlSymbol: "total",
  intradayPnlFilterKey: "",
  positionSortKey: "dayPnl",
  positionSortDirection: "asc",
  positionAdviceBySymbol: {},
  priceLines: readStoredPriceLines(),
  linesDialogSymbol: "",
  newsFilterKey: null,
  maskAmounts: readStoredMask(),
  quoteReceivedAt: 0,
  riskReceivedAt: 0,
  newsReceivedAt: 0,
  marketNewsReceivedAt: 0,
  intradayPnlReceivedAt: 0,
  dailyPnlHistoryReceivedAt: 0,
  quoteServerAt: null,
  newsServerAt: null,
  quoteBaseAgeSeconds: 0,
  riskBaseAgeSeconds: 0,
  newsBaseAgeSeconds: 0,
  marketNewsBaseAgeSeconds: 0,
  intradayPnlBaseAgeSeconds: 0,
  dailyPnlHistoryBaseAgeSeconds: 0,
  quoteError: "",
  riskError: "",
  newsError: "",
  marketNewsError: "",
  intradayPnlError: "",
  dailyPnlHistoryError: "",
  healthError: "",
  decisionError: "",
  quoteLoading: false,
  riskLoading: false,
  newsLoading: false,
  healthLoading: false,
  decisionLoading: false,
  intradayPnlLoading: false,
  intradayPnlChartModel: null,
  intradayHistory: {
    days: readStoredIntradayHistoryDays(),
    endDate: "",
    dates: [],
    retentionDays: 0,
    datesLoaded: false,
    datesError: "",
    loading: false,
    token: 0,
    pendingRecheck: false,
    dayErrors: {},
    cache: new Map(),
    cacheRestored: false,
    backfillBusy: false,
    backfillResult: null,
  },
  intradayHistoryChartModel: null,
  intradayHistoryRenderKey: "",
  paperT: {
    today: null,
    history: null,
    loading: false,
    busy: false,
    historyLoading: false,
    error: "",
    historyError: "",
  },
  dailyPnlHistoryLoading: false,
  dailyPnlHistoryChartModel: null,
  personalIndexPayload: null,
  personalIndexLoading: false,
  personalIndexError: "",
  personalIndexReceivedAt: 0,
  personalIndexChart: null,
  personalIndexPeriod: "day",
  decisionReceivedAt: 0,
  manualRefreshing: false,
  newsRetryTimer: null,
  renderedMarketNewsFingerprint: "",
  stockChartCache: new Map(),
  stockChartPending: new Map(),
  positionIntradayCache: new Map(),
  positionIntradayPending: new Map(),
  // 主要市场指数分时：首屏并发加载，之后每只 120s 均匀错峰刷新
  indexIntraday: { cache: new Map(), pending: new Map(), started: false },
  // 先行指标（A50期货 + 期权PCR/IV），后端已带缓存与容错
  leadingIndicators: null,
  leadingIndicatorsLoading: false,
  // 各先行指标前端累积分时点：key 与 LEADING_SPARK_KEYS 对应，
  // 每次轮询拉到有效值就追加 {t, value}，最多保留 LEADING_SPARK_MAX_POINTS 个。
  leadingHistory: {},
  leadingEarlyRefreshCount: 0,
  // 衍生品风险仪表盘：因子科学性审查 + 观点逐条核查（后端已做口径升级与取数核对）
  derivativesRegime: null,
  derivativesRegimeLoading: false,
  // 做T信号明细区：默认折叠；触发/了结时自动展开；用户手动点过后不再自动干预。
  aggTDetailExpanded: false,
  aggTDetailManual: false,
  stockChartSymbol: "",
  stockChartPeriod: "day",
  stockChartAnchor: null,
  stockChartPinned: false,
  stockChartRequestToken: 0,
  stockChartCloseTimer: null,
  stockChartSuppressFocusOnce: false,
  tomorrowPlans: null,
  tomorrowBacktest: null,
  marketPlan: null,
  tomorrowModelVersion: "",
  tomorrowPlansFetchedAt: 0,
  tPlanSignature: "",
  technicalSignalsBySymbol: new Map(),
  technicalSignalsFetchedAt: 0,
  technicalSignalsPending: null,
  portfolios: [],
  activePortfolioId: readStoredActivePortfolio(),
  portfoliosLoaded: false,
  portfoliosError: "",
  comparePayload: null,
  compareReceivedAt: 0,
  compareError: "",
  compareLoading: false,
  editorMode: "create",
  editorPortfolioId: "",
  editorDeleteArmed: false,
};

const els = {};

document.addEventListener("DOMContentLoaded", init);

function init() {
  cacheElements();
  syncNewsFilter();
  bindEvents();
  els.maskAmounts.checked = state.maskAmounts;
  renderPortfolioTabs();
  renderCompare();
  applyViewVisibility();
  renderPositions();
  renderIntradayPnl();
  renderIntradayHistory();
  renderDailyPnlHistory();
  renderPersonalIndex();
  renderRisk();
  renderMarketNews();
  renderDecisionModules();
  updateFreshness();
  void refreshPortfolios().then(() => {
    void refreshAll();
  });
  void refreshIntradayHistoryDates();
  void refreshPaperTHistory();
  window.setInterval(updateFreshness, 1_000);
  scheduleQuotes();
  scheduleIntradayPnl();
  scheduleDailyPnlHistory();
  schedulePersonalIndex();
  scheduleRisk();
  scheduleNews();
  scheduleHealth();
  scheduleCompare();
  void refreshSchoolSignals();
  scheduleSchoolSignals();
  void refreshLeadingIndicators();
  window.setInterval(() => void refreshLeadingIndicators(), LEADING_INDICATORS_REFRESH_MS);
  void refreshDerivativesRegime();
  window.setInterval(() => void refreshDerivativesRegime(), DERIVATIVES_REFRESH_MS);
}

function cacheElements() {
  const ids = [
    "connection-status", "market-phase", "freshness-text", "mask-amounts", "refresh-button",
    "notice-bar", "quote-source", "market-grid", "breadth-grid", "global-grid", "overnight-grid", "summary-grid",
    "leading-grid", "leading-meta",
    "portfolio-coverage", "risk-state", "risk-generated", "risk-context", "risk-metrics",
    "risk-breaches", "risk-actions", "positions-count", "positions-account-state", "positions-body", "news-symbol-filter",
    "news-source", "news-count", "news-list", "health-text", "market-regime",
    "market-news-state", "market-news-meta", "market-news-list",
    "market-state-score", "market-state-style", "market-state-message", "market-state-meta",
    "events-state", "events-meta", "events-list", "opportunities-state",
    "opportunities-meta", "opportunities-list", "rebalance-state", "rebalance-meta",
    "rebalance-list", "rebalance-note",
    "intraday-pnl-latest", "intraday-pnl-meta", "intraday-pnl-symbol",
    "intraday-pnl-chart-wrap", "intraday-pnl-chart", "intraday-pnl-empty", "intraday-pnl-note",
    "intraday-pnl-tooltip", "intraday-pnl-tooltip-time", "intraday-pnl-tooltip-value",
    "agg-t-signal", "agg-t-signal-badge", "agg-t-signal-text",
    "agg-t-orders-toggle", "agg-t-orders", "agg-t-orders-body", "agg-t-orders-note",
    "agg-t-detail", "agg-t-detail-toggle",
    "agg-t-empty-note",
    "t-center-panel", "t-center-model-summary", "t-plan-body", "t-plan-summary",
    "paper-t-lines", "paper-t-note",
    "paper-t-compare-head", "paper-t-history-stats", "paper-t-history-meta", "paper-t-history-body",
    "paper-t-history-empty", "paper-t-history-note",
    "intraday-history-range", "intraday-history-days", "intraday-history-end", "intraday-history-backfill",
    "intraday-history-chart-wrap", "intraday-history-chart", "intraday-history-empty", "intraday-history-note",
    "intraday-history-tooltip", "intraday-history-tooltip-date", "intraday-history-tooltip-value",
    "intraday-history-tooltip-cumulative",
    "daily-pnl-history-total", "daily-pnl-history-meta", "daily-pnl-history-chart-wrap",
    "daily-pnl-history-chart", "daily-pnl-history-empty", "daily-pnl-history-note",
    "daily-pnl-history-tooltip", "daily-pnl-history-tooltip-date", "daily-pnl-history-tooltip-value",
    "daily-pnl-history-tooltip-cumulative", "daily-pnl-history-body",
    "personal-index-latest", "personal-index-change", "personal-index-meta",
    "personal-index-metrics", "personal-index-base-date", "personal-index-base-point",
    "personal-index-base-mv", "personal-index-last", "personal-index-vs-base", "personal-index-today-pnl",
    "personal-index-chart", "personal-index-empty", "personal-index-table-meta",
    "personal-index-body", "personal-index-note",
    "personal-index-period-toggle", "personal-index-composition-note", "personal-index-detail-title",
    "positions-table",
    "school-signal-state", "school-signal-meta",
    "school-signal-accuracy", "school-signal-events",
    "derivatives-state", "derivatives-meta", "derivatives-factors",
    "derivatives-readings", "derivatives-checks", "derivatives-check-summary",
    "stock-chart-popover", "stock-chart-title", "stock-chart-meta", "stock-chart-close",
    "stock-chart-kline", "stock-chart-kline-empty", "stock-chart-intraday", "stock-chart-intraday-empty",
    "stock-chart-note", "stock-chart-kline-caption", "stock-chart-period-toggle",
    "stock-chart-indicators",
    "stock-lines-dialog", "stock-lines-title", "stock-lines-info",
    "stock-lines-stop", "stock-lines-buy", "stock-lines-sell", "stock-lines-note",
    "stock-lines-delete", "stock-lines-cancel", "stock-lines-save",
    "portfolio-tab-list", "portfolio-add-button", "portfolio-compare-button", "portfolio-tab-note",
    "portfolio-view", "compare-panel", "compare-meta", "compare-body",
    "compare-daily-chart", "compare-daily-empty", "compare-record-chart", "compare-record-empty",
    "compare-trend-chart", "compare-trend-legend", "compare-trend-empty",
    "compare-shared-count", "compare-shared-list",
    "portfolio-editor", "portfolio-editor-title", "portfolio-editor-close",
    "editor-label", "editor-base-field", "editor-base", "editor-note",
    "editor-cash", "editor-debt", "editor-add-row", "editor-positions-body",
    "editor-error", "editor-delete", "editor-cancel", "editor-save",
  ];
  for (const id of ids) els[toCamel(id)] = document.getElementById(id);
}

function bindEvents() {
  els.maskAmounts.addEventListener("change", () => {
    state.maskAmounts = els.maskAmounts.checked;
    try { window.localStorage.setItem("stock-dashboard-mask-amounts", state.maskAmounts ? "1" : "0"); } catch (_) { /* storage may be disabled */ }
    renderSummary();
    renderRisk();
    renderPositions();
    renderIntradayPnl();
    renderIntradayHistory();
    renderDailyPnlHistory();
    renderPersonalIndex();
    renderDecisionModules();
    renderCompare();
  });

  els.refreshButton.addEventListener("click", () => void refreshAll(true));

  els.portfolioAddButton.addEventListener("click", () => openPortfolioEditor("create"));
  els.portfolioCompareButton.addEventListener("click", () => switchPortfolio(COMPARE_VIEW_ID));
  els.portfolioEditorClose.addEventListener("click", closePortfolioEditor);
  els.editorCancel.addEventListener("click", closePortfolioEditor);
  els.editorSave.addEventListener("click", () => void submitPortfolioEditor());
  els.editorDelete.addEventListener("click", () => void deleteActivePortfolio());
  els.editorAddRow.addEventListener("click", () => editorAddRow());
  els.stockLinesSave.addEventListener("click", saveStockLines);
  els.stockLinesDelete.addEventListener("click", deleteStockLines);
  els.stockLinesCancel.addEventListener("click", () => els.stockLinesDialog.close());
  els.stockLinesDialog.addEventListener("close", () => { state.linesDialogSymbol = ""; });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.portfolioEditor.hidden) {
      event.preventDefault();
      closePortfolioEditor();
    }
  });
  els.portfolioEditor.addEventListener("pointerdown", (event) => {
    if (event.target === els.portfolioEditor) closePortfolioEditor();
  });

  els.newsSymbolFilter.addEventListener("change", () => {
    state.selectedSymbol = els.newsSymbolFilter.value;
    renderNews();
    renderPositions();
  });

  if (els.personalIndexPeriodToggle) {
    els.personalIndexPeriodToggle.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-period]") : null;
      if (!target) return;
      selectPersonalIndexPeriod(target.dataset.period);
    });
  }

  els.intradayPnlSymbol.addEventListener("change", () => {
    state.selectedPnlSymbol = els.intradayPnlSymbol.value;
    renderIntradayPnl();
    renderIntradayHistory();
  });

  els.aggTOrdersToggle.addEventListener("click", () => {
    // 委托清单位于折叠明细区内，点击时若明细折叠则一并展开
    if (!state.aggTDetailExpanded) {
      state.aggTDetailExpanded = true;
      state.aggTDetailManual = true;
      applyAggTDetailExpanded();
    }
    const willShow = els.aggTOrders.hidden;
    els.aggTOrders.hidden = !willShow;
    els.aggTOrdersToggle.textContent = willShow ? "委托清单 ▴" : "委托清单 ▾";
  });

  els.aggTDetailToggle.addEventListener("click", () => {
    state.aggTDetailExpanded = !state.aggTDetailExpanded;
    state.aggTDetailManual = true;
    applyAggTDetailExpanded();
  });


  els.intradayHistoryDays.addEventListener("change", () => {
    const value = Number(els.intradayHistoryDays.value);
    state.intradayHistory.days = INTRADAY_HISTORY_DAY_OPTIONS.includes(value)
      ? value
      : INTRADAY_HISTORY_DEFAULT_DAYS;
    try { window.localStorage.setItem(INTRADAY_HISTORY_DAYS_STORAGE_KEY, String(state.intradayHistory.days)); } catch (_) { /* storage may be disabled */ }
    void refreshIntradayHistory();
  });

  els.intradayHistoryEnd.addEventListener("change", () => {
    state.intradayHistory.endDate = els.intradayHistoryEnd.value;
    void refreshIntradayHistory();
  });

  els.intradayHistoryBackfill.addEventListener("click", () => void submitIntradayHistoryBackfill());

  els.intradayHistoryChart.addEventListener("pointermove", handleIntradayHistoryPointer);
  els.intradayHistoryChart.addEventListener("pointerdown", handleIntradayHistoryPointer);
  els.intradayHistoryChart.addEventListener("pointerleave", hideIntradayHistoryTooltip);
  els.intradayHistoryChart.addEventListener("pointercancel", hideIntradayHistoryTooltip);
  els.intradayHistoryChart.addEventListener("blur", hideIntradayHistoryTooltip);
  els.intradayHistoryChart.addEventListener("keydown", handleIntradayHistoryKeyboard);

  els.intradayHistoryDays.value = String(state.intradayHistory.days);
  els.intradayHistoryEnd.value = state.intradayHistory.endDate;

  els.intradayPnlChart.addEventListener("pointermove", handleIntradayPnlPointer);
  els.intradayPnlChart.addEventListener("pointerdown", handleIntradayPnlPointer);
  els.intradayPnlChart.addEventListener("pointerleave", hideIntradayPnlTooltip);
  els.intradayPnlChart.addEventListener("pointercancel", hideIntradayPnlTooltip);
  els.intradayPnlChart.addEventListener("blur", hideIntradayPnlTooltip);
  els.intradayPnlChart.addEventListener("keydown", handleIntradayPnlKeyboard);

  els.dailyPnlHistoryChart.addEventListener("pointermove", handleDailyPnlHistoryPointer);
  els.dailyPnlHistoryChart.addEventListener("pointerdown", handleDailyPnlHistoryPointer);
  els.dailyPnlHistoryChart.addEventListener("pointerleave", hideDailyPnlHistoryTooltip);
  els.dailyPnlHistoryChart.addEventListener("pointercancel", hideDailyPnlHistoryTooltip);
  els.dailyPnlHistoryChart.addEventListener("blur", hideDailyPnlHistoryTooltip);
  els.dailyPnlHistoryChart.addEventListener("keydown", handleDailyPnlHistoryKeyboard);

  els.positionsTable.addEventListener("click", (event) => {
    const chartTrigger = event.target.closest(".stock-chart-trigger");
    if (chartTrigger) {
      event.stopPropagation();
      const symbol = normalizeSymbol(chartTrigger.dataset.symbol);
      if (!symbol) return;
      if (!els.stockChartPopover.hidden && state.stockChartSymbol === symbol && state.stockChartPinned) {
        closeStockChart();
      } else {
        showStockChart(chartTrigger, true);
      }
      selectNewsSymbol(symbol);
      return;
    }

    const button = event.target.closest(".table-sort-button");
    if (button) {
      const key = button.dataset.sortKey;
      if (!POSITION_SORT_KEYS.has(key)) return;
      if (state.positionSortKey === key) {
        state.positionSortDirection = state.positionSortDirection === "desc" ? "asc" : "desc";
      } else {
        state.positionSortKey = key;
        // 当日涨跌金额默认从低到高（升序），便于优先关注最大亏损标的；其余列默认降序。
        state.positionSortDirection = key === "dayPnl" ? "asc" : "desc";
      }
      renderPositions();
      return;
    }

    const gear = event.target.closest(".lines-gear");
    if (gear?.dataset.symbol) {
      openStockLines(gear.dataset.symbol);
      return;
    }

    const row = event.target.closest(".position-row");
    if (row?.dataset.symbol) selectNewsSymbol(row.dataset.symbol);
  });

  els.positionsTable.addEventListener("pointerover", (event) => {
    if (event.pointerType === "touch") return;
    const trigger = event.target.closest(".stock-chart-trigger");
    if (!trigger || trigger.contains(event.relatedTarget)) return;
    cancelStockChartClose();
    showStockChart(trigger, false);
  });

  els.positionsTable.addEventListener("pointerout", (event) => {
    const trigger = event.target.closest(".stock-chart-trigger");
    if (!trigger || trigger.contains(event.relatedTarget) || els.stockChartPopover.contains(event.relatedTarget)) return;
    scheduleStockChartClose();
  });

  els.positionsTable.addEventListener("focusin", (event) => {
    const trigger = event.target.closest(".stock-chart-trigger");
    if (!trigger) return;
    if (state.stockChartSuppressFocusOnce) {
      state.stockChartSuppressFocusOnce = false;
      return;
    }
    showStockChart(trigger, false);
  });

  els.positionsTable.addEventListener("focusout", (event) => {
    const trigger = event.target.closest(".stock-chart-trigger");
    if (!trigger || els.stockChartPopover.contains(event.relatedTarget)) return;
    scheduleStockChartClose();
  });

  // 主要市场 / 全球参考 / 隔夜全球标的卡片复用持仓行情的图表弹层交互
  // （悬停预览、点击固定）。与持仓表唯一的差异：点击指数卡片不筛选新闻。
  for (const container of [els.marketGrid, els.globalGrid, els.overnightGrid]) {
    if (!container) continue;
    container.addEventListener("click", (event) => {
      const chartTrigger = event.target.closest(".stock-chart-trigger");
      if (!chartTrigger) return;
      const symbol = normalizeChartSymbol(chartTrigger.dataset.symbol);
      if (!symbol) return;
      if (!els.stockChartPopover.hidden && state.stockChartSymbol === symbol && state.stockChartPinned) {
        closeStockChart();
      } else {
        showStockChart(chartTrigger, true);
      }
    });
    container.addEventListener("pointerover", (event) => {
      if (event.pointerType === "touch") return;
      const trigger = event.target.closest(".stock-chart-trigger");
      if (!trigger || trigger.contains(event.relatedTarget)) return;
      cancelStockChartClose();
      showStockChart(trigger, false);
    });
    container.addEventListener("pointerout", (event) => {
      const trigger = event.target.closest(".stock-chart-trigger");
      if (!trigger || trigger.contains(event.relatedTarget) || els.stockChartPopover.contains(event.relatedTarget)) return;
      scheduleStockChartClose();
    });
    container.addEventListener("focusin", (event) => {
      const trigger = event.target.closest(".stock-chart-trigger");
      if (!trigger) return;
      if (state.stockChartSuppressFocusOnce) {
        state.stockChartSuppressFocusOnce = false;
        return;
      }
      showStockChart(trigger, false);
    });
    container.addEventListener("focusout", (event) => {
      const trigger = event.target.closest(".stock-chart-trigger");
      if (!trigger || els.stockChartPopover.contains(event.relatedTarget)) return;
      scheduleStockChartClose();
    });
  }

  els.stockChartPopover.addEventListener("pointerenter", cancelStockChartClose);
  els.stockChartPopover.addEventListener("pointerleave", scheduleStockChartClose);
  els.stockChartClose.addEventListener("click", () => closeStockChart(true));

  if (els.stockChartPeriodToggle) {
    els.stockChartPeriodToggle.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-period]") : null;
      if (!target) return;
      const period = selectStockChartPeriod(target.dataset.period);
      const cached = state.stockChartCache.get(state.stockChartSymbol);
      if (cached && cached.chart) renderStockChart(cached.chart);
      void period;
    });
  }
  document.addEventListener("pointerdown", (event) => {
    if (els.stockChartPopover.hidden) return;
    if (els.stockChartPopover.contains(event.target) || event.target.closest(".stock-chart-trigger")) return;
    closeStockChart();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.stockChartPopover.hidden) {
      event.preventDefault();
      closeStockChart(true);
    }
  });
  window.addEventListener("resize", positionStockChartPopover);
  window.addEventListener("resize", () => {
    if (state.personalIndexChart) state.personalIndexChart.resize();
  });
  window.addEventListener("scroll", positionStockChartPopover, true);

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      void refreshQuotes();
      void refreshIntradayPnl();
      void refreshDecisionContext().then((loaded) => {
        if (!loaded) void refreshRisk();
      });
    }
  });
}

async function refreshAll(manual = false) {
  if (state.manualRefreshing) return;
  state.manualRefreshing = true;
  els.refreshButton.disabled = true;
  els.refreshButton.textContent = "刷新中";
  try {
    if (compareActive()) {
      // 比较页：行情接口仍取主组合快照以驱动共享的市场大盘模块。
      await Promise.allSettled([refreshQuotes(), refreshCompare(manual), refreshHealth()]);
      return;
    }
    const intradayPnlRequest = refreshIntradayPnl(manual);
    const intradayHistoryRequest = refreshIntradayHistoryDates();
    const dailyPnlHistoryRequest = refreshDailyPnlHistory(manual);
    const personalIndexRequest = refreshPersonalIndex();
    const schoolSignalRequest = refreshSchoolSignals(manual);
    const unified = await refreshDecisionContext(manual);
    if (unified) {
      await Promise.allSettled([refreshHealth(), intradayPnlRequest, intradayHistoryRequest, dailyPnlHistoryRequest, personalIndexRequest, schoolSignalRequest]);
    } else {
      await Promise.allSettled([
        refreshQuotes(),
        refreshRisk(manual),
        refreshNews(manual),
        refreshHealth(),
        intradayPnlRequest,
        intradayHistoryRequest,
        dailyPnlHistoryRequest,
        personalIndexRequest,
        schoolSignalRequest,
      ]);
      renderDecisionModules();
    }
  } finally {
    state.manualRefreshing = false;
    els.refreshButton.disabled = false;
    els.refreshButton.textContent = manual ? "已刷新" : "刷新";
    if (manual) window.setTimeout(() => { els.refreshButton.textContent = "刷新"; }, 1_200);
  }
}

async function refreshDecisionContext(force = false) {
  if (state.activePortfolioId !== MAIN_PORTFOLIO_ID) {
    // 统一决策快照当前只绑定主组合；自定义组合走单接口通道。
    return false;
  }
  if (state.decisionLoading) return Boolean(state.decisionPayload);
  state.decisionLoading = true;
  try {
    const payload = await fetchJson(decisionContextRequestUrl(force), DECISION_REQUEST_TIMEOUT_MS);
    if (!payload || typeof payload !== "object") throw new Error("统一决策接口返回为空");
    applyDecisionContext(payload);
    state.decisionError = "";
    return true;
  } catch (error) {
    state.decisionError = friendlyError(error, "统一决策快照暂时不可用");
    renderDecisionModules();
    renderNotice();
    return false;
  } finally {
    state.decisionLoading = false;
  }
}

function decisionContextRequestUrl(force = false) {
  return force === true ? "/api/decision-context?refresh=1" : "/api/decision-context";
}

function applyDecisionContext(payload) {
  const root = rootObject(payload);
  state.decisionPayload = payload;
  state.marketStatePayload = asObject(root.market_state || root.marketState);
  state.eventsPayload = asObject(root.events || root.event_monitor || root.eventMonitor);
  state.opportunitiesPayload = asObject(root.opportunities || root.opportunity_radar || root.opportunityRadar);
  state.rebalancePayload = asObject(root.rebalance || root.rebalance_plan || root.rebalancePlan);
  state.decisionReceivedAt = Date.now();

  const adviceRaw = firstDefined(
    root,
    ["position_advice", "positionAdvice", "advice_by_symbol", "adviceBySymbol"],
  );
  if (adviceRaw !== undefined) state.positionAdviceBySymbol = normalizeAdviceMapping(adviceRaw);

  const quotes = root.quotes;
  if (quotes && typeof quotes === "object") {
    state.quotePayload = quotes;
    const quoteAdviceRaw = firstDefined(rootObject(quotes), ["position_advice", "positionAdvice"]);
    if (quoteAdviceRaw !== undefined) state.positionAdviceBySymbol = normalizeAdviceMapping(quoteAdviceRaw);
    state.positions = normalizePositions(quotes, state.positionAdviceBySymbol);
    syncNewsFilter();
    state.markets = normalizeMarkets(quotes);
    state.breadth = normalizeBreadth(quotes);
    state.globals = normalizeGlobals(quotes);
    state.overnight = normalizeOvernight(quotes);
    state.quoteReceivedAt = Date.now();
    state.quoteServerAt = extractTimestamp(quotes) || pick(asObject(root.as_of || root.asOf), ["quote"]);
    state.quoteBaseAgeSeconds = firstNumber(pickNumber(asObject(rootObject(quotes).cache), ["age_seconds", "ageSeconds"]), 0);
    state.quoteError = "";
  }

  const risk = root.risk;
  if (risk && typeof risk === "object") {
    state.riskPayload = risk;
    state.riskReceivedAt = Date.now();
    state.riskBaseAgeSeconds = firstNumber(pickNumber(asObject(rootObject(risk).cache), ["age_seconds", "ageSeconds"]), 0);
    state.riskError = "";
  }

  const news = root.news;
  if (news && typeof news === "object") {
    state.newsPayload = news;
    state.news = normalizeNews(news);
    state.newsReceivedAt = Date.now();
    state.newsServerAt = extractTimestamp(news) || pick(asObject(root.as_of || root.asOf), ["news"]);
    state.newsBaseAgeSeconds = firstNumber(pickNumber(rootObject(news), ["cache_age_seconds", "cacheAgeSeconds"]), 0);
    state.newsError = "";
  }

  const marketNews = root.market_news || root.marketNews;
  if (marketNews && typeof marketNews === "object") {
    state.marketNewsPayload = marketNews;
    state.marketNews = normalizeMarketNews(marketNews);
    state.marketNewsReceivedAt = Date.now();
    state.marketNewsBaseAgeSeconds = firstNumber(
      pickNumber(rootObject(marketNews), ["cache_age_seconds", "cacheAgeSeconds"]),
      0,
    );
    const errors = pick(rootObject(marketNews), ["errors"]);
    state.marketNewsError = Array.isArray(errors) && errors.length ? String(errors[0]) : "";
  }

  const intradayPnl = root.intraday_pnl || root.intradayPnl;
  if (intradayPnl && typeof intradayPnl === "object") applyIntradayPnl(intradayPnl);

  renderQuoteSections();
  renderRisk();
  renderNews();
  renderMarketNews();
  renderDecisionModules();
  renderSourceLabels();
  renderNotice();
}

async function refreshQuotes() {
  if (state.quoteLoading) return;
  state.quoteLoading = true;
  try {
    const payload = await fetchJson(portfolioQuery("/api/quotes"));
    if (payload && payload.ok === false) throw new Error(payload.error || payload.message || "行情接口返回失败");
    state.quotePayload = payload;
    const adviceRaw = firstDefined(rootObject(payload), ["position_advice", "positionAdvice"]);
    if (adviceRaw !== undefined) state.positionAdviceBySymbol = normalizeAdviceMapping(adviceRaw);
    state.positions = normalizePositions(payload, state.positionAdviceBySymbol);
    syncNewsFilter();
    state.markets = normalizeMarkets(payload);
    state.breadth = normalizeBreadth(payload);
    state.globals = normalizeGlobals(payload);
    state.overnight = normalizeOvernight(payload);
    state.quoteReceivedAt = Date.now();
    state.quoteServerAt = extractTimestamp(payload);
    state.quoteBaseAgeSeconds = firstNumber(pickNumber(asObject(rootObject(payload).cache), ["age_seconds", "ageSeconds"]), 0);
    state.quoteError = "";
    renderQuoteSections();
    // 组合 Tab 上的当日盈亏来自 /api/portfolios（读服务端行情缓存）。
    // 行情每次轮询都会更新缓存，因此这里同步刷新 Tab 摘要，
    // 避免金额停留在页面首次加载时的旧值。
    void refreshPortfolios();
  } catch (error) {
    state.quoteError = friendlyError(error, "行情暂时不可用");
    renderConnectionState();
    renderNotice();
  } finally {
    state.quoteLoading = false;
  }
}

async function refreshIntradayPnl(force = false) {
  if (compareActive()) return;
  if (state.intradayPnlLoading) return;
  state.intradayPnlLoading = true;
  try {
    const payload = await fetchJson(
      portfolioQuery(force ? "/api/intraday-pnl?refresh=1" : "/api/intraday-pnl"),
      INTRADAY_PNL_REQUEST_TIMEOUT_MS,
    );
    if (!payload || typeof payload !== "object") throw new Error("盘中盈亏接口返回为空");
    state.intradayPnlError = payload.ok === false
      ? String(payload.error || payload.message || "盘中盈亏暂不可用")
      : "";
    applyIntradayPnl(payload);
  } catch (error) {
    state.intradayPnlError = friendlyError(error, "盘中盈亏暂不可用");
  } finally {
    state.intradayPnlLoading = false;
    // Render after clearing the loading flag so a failed first request cannot
    // leave the empty state permanently saying “正在获取…”.  When an older
    // snapshot exists, renderIntradayPnl keeps that curve and adds the error
    // to its note instead of blanking the chart.
    renderIntradayPnl();
  }
}

function applyIntradayPnl(payload) {
  state.intradayPnlPayload = payload;
  state.intradayPnl = normalizeIntradayPnl(payload);
  state.intradayPnlReceivedAt = Date.now();
  state.intradayPnlBaseAgeSeconds = firstNumber(
    pickNumber(asObject(rootObject(payload).cache), ["age_seconds", "ageSeconds"]),
    pickNumber(rootObject(payload), ["cache_age_seconds", "cacheAgeSeconds"]),
    0,
  );
  syncIntradayPnlSelector();
  renderIntradayPnl();
  // 历史分时回看的"今日"一段复用实时轮询数据；范围不含今日时 renderKey 去重会跳过重绘。
  renderIntradayHistory();
  // 模拟盘状态随同一轮询节奏刷新（信号口径一致，接口本身很轻）。
  void refreshPaperT();
}

function dailyPnlHistoryRequestUrl(force = false) {
  return portfolioQuery(`/api/daily-pnl-history?start=${DAILY_PNL_HISTORY_START_DATE}${force ? "&refresh=1" : ""}`);
}

async function refreshDailyPnlHistory(force = false) {
  if (compareActive()) return;
  if (state.dailyPnlHistoryLoading) return;
  state.dailyPnlHistoryLoading = true;
  try {
    const payload = await fetchJson(
      dailyPnlHistoryRequestUrl(force),
      DAILY_PNL_HISTORY_REQUEST_TIMEOUT_MS,
    );
    if (!payload || typeof payload !== "object") throw new Error("每日盈亏历史接口返回为空");
    if (payload.ok === false) throw new Error(payload.error || payload.message || "每日盈亏历史暂不可用");
    applyDailyPnlHistory(payload);
    state.dailyPnlHistoryError = "";
  } catch (error) {
    state.dailyPnlHistoryError = friendlyError(error, "每日盈亏历史暂不可用");
  } finally {
    state.dailyPnlHistoryLoading = false;
    renderDailyPnlHistory();
  }
}

function applyDailyPnlHistory(payload) {
  state.dailyPnlHistoryPayload = payload;
  state.dailyPnlHistory = normalizeDailyPnlHistory(payload);
  state.dailyPnlHistoryReceivedAt = Date.now();
  state.dailyPnlHistoryBaseAgeSeconds = firstNumber(
    pickNumber(asObject(rootObject(payload).cache), ["age_seconds", "ageSeconds"]),
    pickNumber(rootObject(payload), ["cache_age_seconds", "cacheAgeSeconds"]),
    0,
  );
  renderDailyPnlHistory();
}

async function refreshRisk(force = false) {
  if (compareActive()) return;
  if (state.riskLoading) return;
  state.riskLoading = true;
  try {
    const payload = await fetchJson(portfolioQuery(force ? "/api/risk?refresh=1" : "/api/risk"), RISK_REQUEST_TIMEOUT_MS);
    state.riskPayload = payload;
    state.riskReceivedAt = Date.now();
    state.riskBaseAgeSeconds = firstNumber(pickNumber(asObject(rootObject(payload).cache), ["age_seconds", "ageSeconds"]), 0);
    state.riskError = "";
  } catch (error) {
    state.riskError = friendlyError(error, "风险快照暂时不可用");
  } finally {
    state.riskLoading = false;
    renderRisk();
    renderNotice();
  }
}

async function refreshNews(force = false) {
  if (compareActive()) return;
  if (state.newsLoading) return;
  state.newsLoading = true;
  try {
    const payload = await fetchJson(portfolioQuery(force ? "/api/news?refresh=1" : "/api/news"));
    if (payload && payload.ok === false) throw new Error(payload.error || payload.message || "新闻接口返回失败");
    state.newsPayload = payload;
    state.news = normalizeNews(payload);
    state.newsReceivedAt = Date.now();
    state.newsServerAt = extractTimestamp(payload);
    state.newsBaseAgeSeconds = firstNumber(pickNumber(rootObject(payload), ["cache_age_seconds", "cacheAgeSeconds"]), 0);
    state.newsError = "";
    renderNews();
    renderSourceLabels();
    renderNotice();
    if (state.newsRetryTimer) window.clearTimeout(state.newsRetryTimer);
    state.newsRetryTimer = payload.refreshing ? window.setTimeout(() => {
      state.newsRetryTimer = null;
      if (!document.hidden) void refreshNews();
    }, 2_500) : null;
  } catch (error) {
    state.newsError = friendlyError(error, "新闻暂时不可用");
    renderNews();
    renderSourceLabels();
    renderNotice();
  } finally {
    state.newsLoading = false;
  }
}

async function refreshHealth() {
  if (state.healthLoading) return;
  state.healthLoading = true;
  try {
    const payload = await fetchJson("/api/health");
    state.healthPayload = payload;
    state.healthError = payload && payload.ok === false ? (payload.error || "服务降级") : "";
  } catch (error) {
    state.healthError = friendlyError(error, "服务状态未知");
  } finally {
    state.healthLoading = false;
    renderHealth();
    renderNotice();
  }
}

function scheduleQuotes() {
  window.setTimeout(async () => {
    if (!document.hidden) await refreshQuotes();
    scheduleQuotes();
  }, quotePollIntervalMs(state.quotePayload));
}

function scheduleIntradayPnl() {
  window.setTimeout(async () => {
    if (!document.hidden && intradayPnlRefreshEnabled(state.intradayPnl, state.quotePayload)) {
      await refreshIntradayPnl();
    }
    scheduleIntradayPnl();
  }, intradayPnlPollIntervalMs(state.intradayPnl, state.quotePayload));
}

function scheduleDailyPnlHistory() {
  window.setTimeout(async () => {
    if (!document.hidden && dailyPnlHistoryRefreshEnabled(state.quotePayload)) {
      await refreshDailyPnlHistory();
    }
    scheduleDailyPnlHistory();
  }, dailyPnlHistoryPollIntervalMs());
}

/* ---------- 个人指数日K线（自 snapshots/index_kline.html 整体迁移） ---------- */

function personalIndexRecords(payload) {
  const rows = Array.isArray(asObject(payload).kline) ? payload.kline : [];
  const records = [];
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const record = {
      date: normalizeDailyPnlDate(row.date),
      open: firstNumber(row.open),
      close: firstNumber(row.close),
      high: firstNumber(row.high),
      low: firstNumber(row.low),
      mv: firstNumber(row.mv),
      pctChange: firstNumber(row.pct_change),
      pnlToday: firstNumber(row.pnl_today),
      pnlCum: firstNumber(row.pnl_cum),
      ma5: firstNumber(row.ma5),
      ma10: firstNumber(row.ma10),
      ma20: firstNumber(row.ma20),
      composition: asObject(row.composition),
    };
    if (!record.date || !isFiniteNumber(record.close)) continue;
    records.push(record);
  }
  return records;
}

function personalIndexIsoWeekKey(date) {
  // ISO 周从周一开始；年份取该周周四所在年份
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!matched) return date;
  const year = Number(matched[1]);
  const month = Number(matched[2]);
  const day = Number(matched[3]);
  const utc = new Date(Date.UTC(year, month - 1, day));
  const target = new Date(utc.getTime());
  const dayNr = (utc.getUTCDay() + 6) % 7; // Monday=0..Sunday=6
  target.setUTCDate(target.getUTCDate() - dayNr + 3); // 周四
  const firstThursday = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  const firstThursdayDay = (firstThursday.getUTCDay() + 6) % 7;
  const firstThursdayWeekStart = new Date(firstThursday);
  firstThursdayWeekStart.setUTCDate(firstThursdayWeekStart.getUTCDate() - firstThursdayDay);
  const weekNum = Math.round((target - firstThursdayWeekStart) / (7 * 24 * 3600 * 1000)) + 1;
  return `${target.getUTCFullYear()}-W${String(weekNum).padStart(2, "0")}`;
}

function personalIndexMonthKey(date) {
  return /^(\d{4})-(\d{2})/.test(date) ? date.slice(0, 7) : date;
}

function aggregatePersonalIndexRecords(records, period) {
  if (!records.length) return records;
  if (period !== "week" && period !== "month") return records.slice();
  const keyFn = period === "week" ? personalIndexIsoWeekKey : personalIndexMonthKey;
  const groups = [];
  const groupIndex = new Map();
  for (const record of records) {
    const key = keyFn(record.date);
    if (!groupIndex.has(key)) {
      groupIndex.set(key, groups.length);
      groups.push([]);
    }
    groups[groupIndex.get(key)].push(record);
  }
  const aggregated = [];
  for (const group of groups) {
    const first = group[0];
    const last = group[group.length - 1];
    let pnlToday = 0;
    let hasPnlToday = false;
    let volume = 0;
    let hasVolume = false;
    for (const row of group) {
      if (isFiniteNumber(row.pnlToday)) { pnlToday += row.pnlToday; hasPnlToday = true; }
      if (isFiniteNumber(row.volume)) { volume += row.volume; hasVolume = true; }
    }
    const composition = group.find((row) => row.composition && (row.composition.missing_codes || []).length)
      || first.composition
      || last.composition;
    aggregated.push({
      date: last.date,
      open: first.open,
      close: last.close,
      high: group.reduce((acc, row) => Math.max(acc, row.high), -Infinity),
      low: group.reduce((acc, row) => Math.min(acc, row.low), Infinity),
      mv: last.mv,
      pctChange: isFiniteNumber(first.open) && isFiniteNumber(last.close) && first.open > 0
        ? Math.round((last.close / first.open - 1) * 10000) / 100
        : null,
      pnlToday: hasPnlToday ? Math.round(pnlToday * 100) / 100 : null,
      pnlCum: last.pnlCum,
      volume: hasVolume ? volume : null,
      ma5: null, ma10: null, ma20: null,
      composition,
    });
  }
  const closes = aggregated.map((row) => row.close);
  for (const window of [5, 10, 20]) {
    for (let i = 0; i < aggregated.length; i += 1) {
      if (i + 1 < window) continue;
      const slice = closes.slice(i - window + 1, i + 1);
      if (slice.some((value) => !isFiniteNumber(value))) continue;
      const sum = slice.reduce((acc, value) => acc + value, 0);
      aggregated[i][`ma${window}`] = Math.round(sum / window * 100) / 100;
    }
  }
  return aggregated;
}

function personalIndexPeriodMeta(records, period) {
  return PERSONAL_INDEX_PERIOD_LABELS[period] || PERSONAL_INDEX_PERIOD_LABELS.day;
}

function personalIndexCompositionDates(records) {
  const partial = [];
  for (const record of records) {
    const composition = record.composition;
    if (!composition || !Array.isArray(composition.missing_codes) || composition.missing_codes.length === 0) {
      continue;
    }
    if (!partial.length || partial[partial.length - 1].date !== record.date) {
      partial.push({ date: record.date, missing_codes: composition.missing_codes.slice() });
    }
  }
  return partial;
}

function selectPersonalIndexPeriod(period) {
  if (!Object.prototype.hasOwnProperty.call(PERSONAL_INDEX_PERIOD_LABELS, period)) {
    period = "day";
  }
  if (state.personalIndexPeriod === period) return;
  state.personalIndexPeriod = period;
  for (const button of document.querySelectorAll("#personal-index-period-toggle [data-period]")) {
    const isActive = button.dataset.period === period;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
  }
  renderPersonalIndex();
}

async function refreshPersonalIndex() {
  if (compareActive()) return;
  if (state.personalIndexLoading) return;
  state.personalIndexLoading = true;
  try {
    const payload = await fetchJson(PERSONAL_INDEX_DATA_URL, PERSONAL_INDEX_REQUEST_TIMEOUT_MS);
    if (!payload || typeof payload !== "object") throw new Error("个人指数数据返回为空");
    if (!personalIndexRecords(payload).length) throw new Error("个人指数数据缺少有效K线序列");
    state.personalIndexPayload = payload;
    state.personalIndexError = "";
    state.personalIndexReceivedAt = Date.now();
  } catch (error) {
    state.personalIndexError = friendlyError(error, "个人指数数据暂不可用");
  } finally {
    state.personalIndexLoading = false;
    renderPersonalIndex();
  }
}

function schedulePersonalIndex() {
  window.setTimeout(async () => {
    if (!document.hidden) await refreshPersonalIndex();
    schedulePersonalIndex();
  }, PERSONAL_INDEX_INTERVAL_MS);
}

function personalIndexEmptyMessage() {
  if (state.personalIndexLoading && !state.personalIndexPayload) return "正在获取个人指数K线…";
  if (state.personalIndexError) return `个人指数暂不可用：${state.personalIndexError}`;
  return "个人指数数据尚未加载";
}

function renderPersonalIndex() {
  const payload = state.personalIndexPayload;
  const dailyRecords = personalIndexRecords(payload);
  const period = state.personalIndexPeriod || "day";
  const periodMeta = personalIndexPeriodMeta(dailyRecords, period);
  const records = period === "day"
    ? dailyRecords
    : aggregatePersonalIndexRecords(dailyRecords, period);
  const last = records.length ? records[records.length - 1] : null;
  const latestDaily = dailyRecords.length ? dailyRecords[dailyRecords.length - 1] : null;

  if (!last) {
    els.personalIndexChart.setAttribute("hidden", "");
    els.personalIndexEmpty.hidden = false;
    els.personalIndexEmpty.textContent = personalIndexEmptyMessage();
    els.personalIndexMeta.textContent = "等待个人指数数据";
    els.personalIndexCompositionNote.textContent = "";
    if (els.personalIndexDetailTitle) {
      els.personalIndexDetailTitle.textContent = `${periodMeta.detail}涨跌明细（按日期倒序）`;
    }
    renderPersonalIndexTable(records);
    return;
  }

  els.personalIndexEmpty.hidden = true;
  els.personalIndexChart.removeAttribute("hidden");

  const basePoint = firstNumber(payload.base_point);
  const vsBasePct = basePoint ? (last.close / basePoint - 1) * 100 : null;

  els.personalIndexLatest.textContent = last.close.toFixed(2);
  els.personalIndexLatest.className = `personal-index-latest ${directionClass(vsBasePct)}`;
  els.personalIndexChange.textContent = `较基准 ${formatPct(vsBasePct)}`;
  els.personalIndexChange.className = `personal-index-change ${directionClass(vsBasePct)}`;

  els.personalIndexBaseDate.textContent = normalizeDailyPnlDate(payload.base_date) || String(payload.base_date || "--");
  els.personalIndexBasePoint.textContent = basePoint != null ? String(basePoint) : "--";
  els.personalIndexBaseMv.textContent = formatAmount(payload.base_mv);
  els.personalIndexLast.textContent = `${last.close.toFixed(2)}（${last.date}）`;
  els.personalIndexVsBase.textContent = formatPct(vsBasePct);
  els.personalIndexVsBase.className = directionClass(vsBasePct);
  // “当日/本周/本月盈亏”：沿用周期口径
  const pnlMetric = (period === "day" ? latestDaily : last) || last;
  els.personalIndexTodayPnl.textContent = formatAmount(pnlMetric.pnlToday, true);
  els.personalIndexTodayPnl.className = directionClass(pnlMetric.pnlToday);

  const meta = [];
  const updateDate = normalizeDailyPnlDate(payload.update_date) || String(payload.update_date || "");
  if (updateDate) meta.push(`数据 ${updateDate}`);
  meta.push(`${records.length} 根${periodMeta.singular}K线`);
  if (state.personalIndexReceivedAt) meta.push(ageLabel(state.personalIndexReceivedAt, 0));
  els.personalIndexMeta.textContent = meta.filter(Boolean).join(" · ");

  if (els.personalIndexDetailTitle) {
    els.personalIndexDetailTitle.textContent = `${periodMeta.detail}涨跌明细（按日期倒序）`;
  }

  // 组合切换/部分成分股提示
  const partialDates = personalIndexCompositionDates(dailyRecords);
  if (partialDates.length) {
    const firstPartial = partialDates[0].date;
    const missing = partialDates[0].missing_codes.join(", ");
    const fullBasisStart = dailyRecords.find((row) => {
      const composition = row.composition;
      return composition && Array.isArray(composition.missing_codes) && composition.missing_codes.length === 0;
    });
    const range = fullBasisStart
      ? `${firstPartial} 至 ${formatIndexPointForDate(fullBasisStart.date)}（首日按 15-股, 其后按 16-股）`
      : `${firstPartial} 起`;
    els.personalIndexCompositionNote.textContent =
      `成分回填：${range}按 15 只成分股计算（${missing} 7月27日上市），与 16-股期口径不可直接比较`;
  } else if (state.activePortfolioId !== MAIN_PORTFOLIO_ID) {
    els.personalIndexCompositionNote.textContent =
      "当前查看其他组合；个人指数固定跟踪主组合16只成分股";
  } else {
    els.personalIndexCompositionNote.textContent = "";
  }

  const notes = [els.personalIndexCompositionNote.textContent];
  if (state.personalIndexError) notes.push(`数据更新失败：${state.personalIndexError}，展示最近一次快照`);
  notes.push(
    "个人指数 = 1000 × (组合当日总市值 ÷ 基准日总市值)，市值加权，16只成分股数量固定；全历史按快照篮子×前复权价统一重建，正确处理分红除权，累计盈亏自K线首日起算",
    `收盘后今日 bar 自动按收盘价修正；当前周期：${periodMeta.kline}`,
  );
  els.personalIndexNote.textContent = notes.filter(Boolean).join(" · ");

  renderPersonalIndexTable(records);
  if (!drawPersonalIndexChart(records)) {
    // 图表库缺失（如 vendor 文件未加载）时保留指标与明细表并明确降级。
    els.personalIndexChart.setAttribute("hidden", "");
    els.personalIndexEmpty.hidden = false;
    els.personalIndexEmpty.textContent = "K线图组件未加载，已展示指标与每日明细";
  }
}

function formatIndexPointForDate(value) {
  if (typeof value !== "string") return "--";
  return value.slice(5);
}

function renderPersonalIndexTable(records) {
  els.personalIndexBody.replaceChildren();
  if (!records.length) {
    const row = document.createElement("tr");
    const cell = td(personalIndexEmptyMessage(), "empty");
    cell.colSpan = 6;
    row.append(cell);
    els.personalIndexBody.append(row);
    els.personalIndexTableMeta.textContent = "";
    return;
  }
  els.personalIndexTableMeta.textContent = `${records.length} 个交易日`;
  for (const record of [...records].reverse()) {
    const row = document.createElement("tr");
    row.append(
      td(record.date, ""),
      td(record.close.toFixed(2), "numeric"),
      td(formatPct(record.pctChange), `numeric ${directionClass(record.pctChange)}`),
      td(formatAmount(record.pnlToday, true), `numeric sensitive ${directionClass(record.pnlToday)}`),
      td(formatAmount(record.pnlCum, true), `numeric sensitive ${directionClass(record.pnlCum)}`),
      td(formatAmount(record.mv), "numeric sensitive"),
    );
    els.personalIndexBody.append(row);
  }
}

function personalIndexEcharts() {
  const library = globalThis.echarts;
  return library && typeof library.init === "function" ? library : null;
}

function personalIndexCssVariable(name, fallback) {
  if (typeof getComputedStyle !== "function") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function personalIndexPalette() {
  return {
    up: personalIndexCssVariable("--up", "#9c5158"),
    down: personalIndexCssVariable("--down", "#487b68"),
    muted: personalIndexCssVariable("--muted", "#747c83"),
    line: personalIndexCssVariable("--line-soft", "#eaeced"),
    ma: [
      personalIndexCssVariable("--accent", "#4e687d"),
      personalIndexCssVariable("--warning", "#8a7040"),
      "#7a5c8e",
    ],
  };
}

function drawPersonalIndexChart(records) {
  const library = personalIndexEcharts();
  if (!library) return false;
  try {
    if (!state.personalIndexChart) state.personalIndexChart = library.init(els.personalIndexChart);
    state.personalIndexChart.setOption(personalIndexChartOption(records), { notMerge: true });
    state.personalIndexChart.resize();
    return true;
  } catch (_) {
    // 图表初始化失败（如容器尺寸异常）不影响指标与明细表。
    state.personalIndexChart = null;
    return false;
  }
}

function formatIndexPoint(value) {
  return value == null ? "-" : value.toFixed(2);
}

function personalIndexTooltipHtml(record) {
  // CSP 禁止内联 style 属性，红绿着色复用样式表中的 up/down 类。
  const todayClass = (record.pnlToday ?? 0) >= 0 ? "up" : "down";
  let html = `<b>${record.date}</b><br/>`;
  html += `指数: 开 ${formatIndexPoint(record.open)} / 收 ${formatIndexPoint(record.close)} / 高 ${formatIndexPoint(record.high)} / 低 ${formatIndexPoint(record.low)}<br/>`;
  if (record.pctChange != null) {
    html += `<span class="${todayClass}">涨跌: ${formatPct(record.pctChange)}</span>　`;
    html += `<span class="${todayClass}">当日盈亏: ${formatAmount(record.pnlToday, true)}</span><br/>`;
  }
  html += `组合市值: ${formatAmount(record.mv)}<br/>`;
  if (record.pnlCum != null) {
    const cumClass = record.pnlCum >= 0 ? "up" : "down";
    html += `<span class="${cumClass}">累计盈亏: ${formatAmount(record.pnlCum, true)}</span><br/>`;
  }
  html += `MA5: ${formatIndexPoint(record.ma5)} MA10: ${formatIndexPoint(record.ma10)} MA20: ${formatIndexPoint(record.ma20)}`;
  return html;
}

function personalIndexChartOption(records) {
  const palette = personalIndexPalette();
  const dates = records.map((record) => record.date);
  const ohlc = records.map((record) => [record.open, record.close, record.low, record.high]);
  const maSeries = [5, 10, 20].map((period, index) => ({
    name: `MA${period}`,
    type: "line",
    data: records.map((record) => record[`ma${period}`]),
    smooth: true,
    showSymbol: false,
    lineStyle: { width: 1.2, color: palette.ma[index] },
  }));
  // 下方柱状图 = 当日盈亏额（红涨绿跌，单位万元）
  const pnlBars = records.map((record) => ({
    value: record.pnlToday == null ? null : +(record.pnlToday / 10_000).toFixed(3),
    itemStyle: { color: (record.pnlToday ?? 0) >= 0 ? palette.up : palette.down },
  }));
  const zoomStart = Math.max(0, 100 - 40);

  return {
    animation: false,
    textStyle: { color: palette.muted, fontSize: 10 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      backgroundColor: "#fff",
      borderColor: personalIndexCssVariable("--line", "#dfe2e4"),
      textStyle: { color: personalIndexCssVariable("--text", "#30353a"), fontSize: 10 },
      formatter(params) {
        const list = Array.isArray(params) ? params : [];
        const anchor = list.find((item) => item && item.seriesName === "个人指数") || list[0];
        if (!anchor) return "";
        const record = records[anchor.dataIndex];
        return record ? personalIndexTooltipHtml(record) : "";
      },
    },
    legend: { data: ["个人指数", "MA5", "MA10", "MA20"], top: 0, textStyle: { color: palette.muted, fontSize: 10 } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 56, right: 20, top: 34, height: "62%" },
      { left: 56, right: 20, top: "76%", height: "14%" },
    ],
    xAxis: [
      {
        type: "category",
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: palette.muted } },
        axisLabel: { show: false },
      },
      {
        type: "category",
        gridIndex: 1,
        data: dates,
        boundaryGap: true,
        axisLabel: {
          interval: Math.max(0, Math.ceil(dates.length / 8)),
          fontSize: 10,
          color: palette.muted,
        },
      },
    ],
    yAxis: [
      {
        scale: true,
        splitLine: { lineStyle: { color: palette.line } },
        axisLabel: { formatter: (value) => value.toFixed(0), color: palette.muted, fontSize: 10 },
      },
      {
        gridIndex: 1,
        splitNumber: 2,
        axisLabel: {
          formatter: (value) => (state.maskAmounts ? "" : `${value.toFixed(1)}万`),
          color: palette.muted,
          fontSize: 10,
        },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: zoomStart, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], bottom: 0, height: 16, start: zoomStart, end: 100 },
    ],
    series: [
      {
        name: "个人指数",
        type: "candlestick",
        data: ohlc,
        itemStyle: { color: palette.up, color0: palette.down, borderColor: palette.up, borderColor0: palette.down },
      },
      ...maSeries,
      {
        name: "当日盈亏",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: pnlBars,
        barWidth: "60%",
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// 三大流派信号面板（趋势跟踪 / 道氏理论 / 量价分析Wyckoff）
// ---------------------------------------------------------------------------

const SCHOOL_ORDER = ["trend", "dow", "wyckoff"];
const SCHOOL_LABELS = { trend: "趋势跟踪", dow: "道氏理论", wyckoff: "量价分析" };
const SCHOOL_SHORT_LABELS = { trend: "趋势", dow: "道氏", wyckoff: "量价" };
const SCHOOL_LEVEL_LABELS = { trigger: "今日触发", near: "接近触发", hold: "持仓中", avoid: "空仓等待" };
const SCHOOL_EVENT_LABELS = { buy_trigger: "买入触发", sell_trigger: "卖出触发", near_trigger: "接近触发" };

async function refreshSchoolSignals(force = false) {
  if (state.schoolSignalLoading) return;
  state.schoolSignalLoading = true;
  try {
    const payload = await fetchJson(force ? "/api/school-signals?refresh=1" : "/api/school-signals", 25_000);
    if (payload && payload.ok === false) throw new Error(payload.error || payload.message || "信号接口返回失败");
    state.schoolSignalPayload = payload;
    state.schoolSignalError = "";
  } catch (error) {
    state.schoolSignalError = friendlyError(error, "三大流派信号暂时不可用");
  } finally {
    state.schoolSignalLoading = false;
    renderSchoolSignals();
  }
}

function scheduleSchoolSignals() {
  window.setTimeout(async () => {
    if (!document.hidden) await refreshSchoolSignals();
    scheduleSchoolSignals();
  }, SCHOOL_SIGNAL_INTERVAL_MS);
}

function _el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function renderSchoolSignals() {
  const payload = state.schoolSignalPayload;
  const errorText = state.schoolSignalError;

  // 顶部状态徽标与元信息
  if (els.schoolSignalState) {
    els.schoolSignalState.classList.remove("is-pending", "is-ok", "is-blocked");
    if (errorText && !payload) {
      els.schoolSignalState.textContent = "不可用";
      els.schoolSignalState.classList.add("is-blocked");
    } else if (payload) {
      const triggered = (payload.stocks || []).filter((s) => s.overall === "trigger").length;
      els.schoolSignalState.textContent = triggered > 0 ? `今日 ${triggered} 只触发` : "运行中";
      els.schoolSignalState.classList.add(triggered > 0 ? "is-blocked" : "is-ok");
    } else {
      els.schoolSignalState.textContent = "待计算";
      els.schoolSignalState.classList.add("is-pending");
    }
  }
  if (els.schoolSignalMeta) {
    const parts = [];
    if (payload && payload.generated_at) parts.push(`更新 ${String(payload.generated_at).replace("T", " ").slice(5, 19)}`);
    if (payload && payload.events_logged !== undefined) parts.push(`累计提示 ${payload.events_logged} 条`);
    if (payload && payload.events_appended) parts.push(`本次新增 ${payload.events_appended} 条`);
    if (errorText) parts.push(errorText);
    els.schoolSignalMeta.textContent = parts.join(" · ") || "每 5 分钟自动刷新";
  }

  // 重仓股卡片已并入「持仓行情」表的流派信号列，这里只刷新徽标与回溯统计。
  renderSchoolSignalAccuracy(payload);
  renderSchoolSignalEvents(payload);
  renderPositions();
}

function renderSchoolSignalAccuracy(payload) {
  const box = els.schoolSignalAccuracy;
  if (!box) return;
  box.textContent = "";
  const stats = (payload && payload.accuracy) || {};
  const keys = SCHOOL_ORDER.filter((k) => stats[k]);
  if (keys.length === 0) {
    box.appendChild(_el("span", "muted", "暂无已记录的提示；出现触发/接近触发后会自动记录并统计命中率"));
    return;
  }
  for (const key of keys) {
    const st = stats[key];
    const rate = st.hit_rate != null ? `${Math.round(st.hit_rate * 100)}%` : "--";
    const line = _el("div", "school-accuracy-line");
    line.appendChild(_el("span", "school-accuracy-name", SCHOOL_LABELS[key] || key));
    line.appendChild(_el("strong", null, `命中率 ${rate}`));
    line.appendChild(_el("span", "muted", `已验证 ${st.total} 笔（命中 ${st.hit}）· 待验证 ${st.pending} 笔`));
    box.appendChild(line);
  }
}

function renderSchoolSignalEvents(payload) {
  const box = els.schoolSignalEvents;
  if (!box) return;
  box.textContent = "";
  const events = (payload && payload.recent_events) || [];
  if (events.length === 0) {
    box.appendChild(_el("span", "muted", "暂无提示记录"));
    return;
  }
  for (const ev of events.slice(0, 10)) {
    const line = _el("div", `school-event-line event-${ev.event}`);
    const head = _el("div", "school-event-head");
    head.appendChild(_el("span", "school-event-date", String(ev.date || "").slice(5)));
    head.appendChild(_el("strong", null, ev.name || ev.symbol));
    head.appendChild(_el("span", "muted", SCHOOL_LABELS[ev.school] || ev.school));
    head.appendChild(_el("span", `school-event-tag event-${ev.event}`, SCHOOL_EVENT_LABELS[ev.event] || ev.event));
    head.appendChild(_el("span", "school-event-price", ev.price != null ? `@${Number(ev.price).toFixed(2)}` : ""));
    line.appendChild(head);
    const outcome = (ev.outcomes || {}).ret_5d || {};
    let tail;
    if (outcome.hit === null || outcome.hit === undefined) {
      tail = _el("span", "muted", "未满 5 个交易日，待验证");
    } else {
      const pct5 = `${outcome.ret > 0 ? "+" : ""}${(outcome.ret * 100).toFixed(1)}%`;
      tail = _el("span", outcome.hit ? "school-event-hit" : "school-event-miss",
        `5日 ${pct5} ${outcome.hit ? "✓ 命中" : "✗ 未中"}`);
    }
    line.appendChild(tail);
    box.appendChild(line);
  }
}

function scheduleNews() {
  window.setTimeout(async () => {
    if (!document.hidden) await refreshNews();
    scheduleNews();
  }, NEWS_INTERVAL_MS);
}

function scheduleRisk() {
  window.setTimeout(async () => {
    if (!document.hidden) {
      const unified = await refreshDecisionContext();
      if (!unified) await refreshRisk();
    }
    scheduleRisk();
  }, RISK_INTERVAL_MS);
}

function scheduleHealth() {
  window.setTimeout(async () => {
    if (!document.hidden) await refreshHealth();
    scheduleHealth();
  }, HEALTH_INTERVAL_MS);
}

async function fetchJson(url, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" }, signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("json")) throw new Error("接口未返回 JSON");
    return await response.json();
  } catch (error) {
    if (error && error.name === "AbortError") throw new Error("请求超时");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function postJson(url, body, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal: controller.signal,
    });
    if (!response.ok) {
      let detail = "";
      try {
        const errorBody = await response.json();
        if (errorBody && errorBody.error) detail = String(errorBody.error);
      } catch (parseError) {
        // 非 JSON 错误响应体：退回 HTTP 状态码文案。
      }
      const error = new Error(detail || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("json")) throw new Error("接口未返回 JSON");
    return await response.json();
  } catch (error) {
    if (error && error.name === "AbortError") throw new Error("请求超时");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

/* ---------- 多组合：Tab、作用域请求、比较、编辑 ---------- */

function compareActive() {
  return state.activePortfolioId === COMPARE_VIEW_ID;
}

function activePortfolioEntry() {
  return state.portfolios.find((item) => item.portfolioId === state.activePortfolioId) || null;
}

function portfolioQuery(url) {
  if (compareActive() || state.activePortfolioId === MAIN_PORTFOLIO_ID) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}portfolio=${encodeURIComponent(state.activePortfolioId)}`;
}

function normalizePortfolioDescriptor(raw) {
  const item = asObject(raw);
  return {
    portfolioId: String(pick(item, ["portfolio_id", "portfolioId", "id"]) || ""),
    label: String(pick(item, ["label", "name"]) || ""),
    editable: Boolean(pick(item, ["editable"])),
    positionCount: firstNumber(pickNumber(item, ["position_count", "positionCount"])),
    marketValue: firstNumber(pickNumber(item, ["market_value", "marketValue"])),
    dailyPnl: firstNumber(pickNumber(item, ["daily_pnl", "dailyPnl"])),
    netEquity: firstNumber(pickNumber(item, ["net_equity", "netEquity"])),
    note: String(pick(item, ["note"]) || ""),
    asOf: pick(item, ["as_of", "asOf"]) || "",
  };
}

function readStoredActivePortfolio() {
  try { return window.localStorage.getItem("stock-dashboard-active-portfolio") || MAIN_PORTFOLIO_ID; } catch (_) { return MAIN_PORTFOLIO_ID; }
}

function persistActivePortfolio() {
  try { window.localStorage.setItem("stock-dashboard-active-portfolio", state.activePortfolioId); } catch (_) { /* storage may be disabled */ }
}

async function refreshPortfolios() {
  try {
    const payload = await fetchJson("/api/portfolios");
    const list = Array.isArray(payload?.portfolios) ? payload.portfolios : [];
    state.portfolios = list.map(normalizePortfolioDescriptor).filter((item) => item.portfolioId);
    state.portfoliosLoaded = true;
    state.portfoliosError = "";
    if (!compareActive() && !state.portfolios.some((item) => item.portfolioId === state.activePortfolioId)) {
      state.activePortfolioId = MAIN_PORTFOLIO_ID;
      persistActivePortfolio();
    }
  } catch (error) {
    state.portfoliosError = friendlyError(error, "组合列表暂不可用");
  }
  renderPortfolioTabs();
  applyViewVisibility();
}

/* 组合 Tab 的渲染去重：行情每 5 秒轮询会连带刷新 Tab 摘要，
   结构未变时只就地更新金额文本，避免反复重建按钮打断点击。 */
let portfolioTabStructureKey = "";
const portfolioTabPnlSpans = new Map();

function applyPortfolioTabPnl(item) {
  const pnlSpan = portfolioTabPnlSpans.get(item.portfolioId);
  if (!pnlSpan) return;
  pnlSpan.textContent = isFiniteNumber(item.dailyPnl) ? formatAmount(item.dailyPnl, true) : "";
  pnlSpan.className = `portfolio-tab-pnl ${directionClass(item.dailyPnl)}`;
}

function renderPortfolioTabs() {
  if (!els.portfolioTabList) return;
  const entries = state.portfolios.length
    ? state.portfolios
    : [{ portfolioId: MAIN_PORTFOLIO_ID, label: "主组合", editable: false }];
  const structureKey = JSON.stringify([
    entries.map((item) => [item.portfolioId, item.label, Boolean(item.editable)]),
    state.activePortfolioId,
    state.portfoliosError,
  ]);
  if (structureKey !== portfolioTabStructureKey) {
    portfolioTabStructureKey = structureKey;
    portfolioTabPnlSpans.clear();
    els.portfolioTabList.replaceChildren();
    for (const item of entries) {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = `portfolio-tab${state.activePortfolioId === item.portfolioId ? " is-active" : ""}`;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", state.activePortfolioId === item.portfolioId ? "true" : "false");
      const nameSpan = span(item.label || item.portfolioId, "portfolio-tab-label");
      tab.append(nameSpan);
      const pnlSpan = span("", "portfolio-tab-pnl");
      portfolioTabPnlSpans.set(item.portfolioId, pnlSpan);
      tab.append(pnlSpan);
      if (item.editable && state.activePortfolioId === item.portfolioId) {
        const editButton = document.createElement("button");
        editButton.type = "button";
        editButton.className = "portfolio-tab-edit";
        editButton.textContent = "编辑";
        editButton.title = "修改该组合的股票与持仓量";
        editButton.addEventListener("click", (event) => {
          event.stopPropagation();
          openPortfolioEditor("edit", item.portfolioId);
        });
        tab.append(editButton);
      }
      tab.addEventListener("click", () => switchPortfolio(item.portfolioId));
      els.portfolioTabList.append(tab);
      applyPortfolioTabPnl(item);
    }
  } else {
    for (const item of entries) applyPortfolioTabPnl(item);
  }
  // 比较视图通过右上角「组合比较」按钮进入，不占用组合 Tab 位。
  els.portfolioCompareButton.classList.toggle("is-active-view", compareActive());
  els.portfolioCompareButton.setAttribute("aria-pressed", compareActive() ? "true" : "false");

  const note = els.portfolioTabNote;
  if (note) {
    if (state.portfoliosError) {
      note.textContent = state.portfoliosError;
      note.hidden = false;
    } else if (!compareActive() && state.activePortfolioId !== MAIN_PORTFOLIO_ID) {
      note.textContent = "当前为用户自定义组合：行情、新闻与主组合按股票共用；决策模块（重要事件/机会雷达/再平衡草案）目前仅覆盖主组合。";
      note.hidden = false;
    } else if (compareActive()) {
      note.textContent = "组合比较使用同一次共享行情快照；市场大盘模块在所有组合间保持同一份。";
      note.hidden = false;
    } else {
      note.textContent = "";
      note.hidden = true;
    }
  }
}

function applyViewVisibility() {
  const compare = compareActive();
  if (els.portfolioView) els.portfolioView.hidden = compare;
  if (els.comparePanel) els.comparePanel.hidden = !compare;
  // 做T 决策中心是组合口径内容，比较视图下不适用。
  if (els.tCenterPanel) els.tCenterPanel.hidden = compare;
}

function clearPortfolioViewState() {
  state.quotePayload = null;
  state.riskPayload = null;
  state.newsPayload = null;
  state.intradayPnlPayload = null;
  state.intradayPnl = null;
  state.intradayHistory.dates = [];
  state.intradayHistory.datesLoaded = false;
  state.intradayHistory.datesError = "";
  state.intradayHistory.endDate = "";
  state.intradayHistory.loading = false;
  state.intradayHistory.token += 1;
  state.intradayHistory.pendingRecheck = false;
  state.intradayHistory.dayErrors = {};
  state.intradayHistory.cache = new Map();
  state.intradayHistory.cacheRestored = false;
  state.intradayHistory.backfillBusy = false;
  state.intradayHistory.backfillResult = null;
  state.intradayHistoryRenderKey = "";
  state.intradayHistoryChartModel = null;
  state.dailyPnlHistoryPayload = null;
  state.dailyPnlHistory = null;
  state.decisionPayload = null;
  state.positions = [];
  // 做T计划走 /api/tomorrow-plans（主组合口径，不随组合切换失效），
  // 这里只重置签名，让底部做T参考表按新组合的持仓重新铺行。
  state.tPlanSignature = "";
  state.selectedSymbol = "all";
  state.selectedPnlSymbol = "total";
  state.quoteError = "";
  state.riskError = "";
  state.newsError = "";
  state.intradayPnlError = "";
  state.dailyPnlHistoryError = "";
  if (!els.stockChartPopover.hidden) closeStockChart();
}

function switchPortfolio(portfolioId) {
  if (portfolioId === state.activePortfolioId) return;
  state.activePortfolioId = portfolioId;
  persistActivePortfolio();
  clearPortfolioViewState();
  renderPortfolioTabs();
  applyViewVisibility();
  renderPositions();
  renderSummary();
  renderIntradayPnl();
  renderDailyPnlHistory();
  renderPersonalIndex();
  renderRisk();
  renderNews();
  renderDecisionModules();
  renderCompare();
  void refreshAll(true);
}

/* ---------- 组合比较 ---------- */

function buildCompareRows(payload) {
  const root = asObject(payload);
  const list = Array.isArray(root.portfolios) ? root.portfolios : [];
  return list.map((raw) => {
    const item = asObject(raw);
    const ranks = asObject(item.ranks);
    return {
      portfolioId: String(item.portfolio_id || ""),
      label: String(item.label || item.portfolio_id || ""),
      editable: Boolean(item.editable),
      positionCount: firstNumber(pickNumber(item, ["position_count"])),
      marketValue: firstNumber(pickNumber(item, ["market_value"])),
      dailyPnl: firstNumber(pickNumber(item, ["daily_pnl"])),
      dailyPnlPct: firstNumber(pickNumber(item, ["daily_pnl_pct"])),
      netEquity: firstNumber(pickNumber(item, ["net_equity"])),
      exposure: firstNumber(pickNumber(item, ["gross_exposure_pct"])),
      cumulativePnl: firstNumber(pickNumber(item, ["cumulative_pnl"])),
      cumulativeSeries: (Array.isArray(item.cumulative_series) ? item.cumulative_series : [])
        .map((point) => {
          const entry = asObject(point);
          return { date: String(entry.date || ""), value: firstNumber(pickNumber(entry, ["cumulative_pnl"])) };
        })
        .filter((point) => point.date && isFiniteNumber(point.value)),
      cumulativePartial: Boolean(item.cumulative_partial),
      recordReturnPct: firstNumber(pickNumber(item, ["record_return_pct"])),
      recordDays: firstNumber(pickNumber(item, ["record_days"]), 0),
      topName: pick(asObject(item.top_position), ["name"]) || "",
      topWeight: firstNumber(pickNumber(asObject(item.top_position), ["weight_pct_securities"])),
      rankDaily: firstNumber(pickNumber(ranks, ["daily_pnl_pct"])),
      rankCumulative: firstNumber(pickNumber(ranks, ["cumulative_pnl"])),
      rankRecord: firstNumber(pickNumber(ranks, ["record_return_pct"])),
    };
  }).filter((row) => row.portfolioId);
}

function rankBadge(rank, total) {
  if (!isFiniteNumber(rank) || total < 2) return null;
  const badge = span(`第${rank}名`, `rank-badge${rank === 1 ? " rank-first" : ""}`);
  return badge;
}

async function refreshCompare(force = false) {
  if (state.compareLoading) return;
  state.compareLoading = true;
  try {
    const payload = await fetchJson(
      force ? "/api/portfolio-compare?refresh=1" : "/api/portfolio-compare",
      RISK_REQUEST_TIMEOUT_MS,
    );
    if (!payload || payload.ok === false) throw new Error(payload?.error || "组合比较暂不可用");
    state.comparePayload = payload;
    state.compareReceivedAt = Date.now();
    state.compareError = "";
  } catch (error) {
    state.compareError = friendlyError(error, "组合比较暂不可用");
  } finally {
    state.compareLoading = false;
    renderCompare();
  }
}

function scheduleCompare() {
  window.setTimeout(async () => {
    if (!document.hidden && compareActive()) await refreshCompare();
    scheduleCompare();
  }, COMPARE_INTERVAL_MS);
}

function renderCompare() {
  if (!els.comparePanel) return;
  const rows = buildCompareRows(state.comparePayload);
  const total = rows.length;
  els.compareBody.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = td(state.compareError || "正在获取组合比较数据…", "empty");
    cell.colSpan = 9;
    row.append(cell);
    els.compareBody.append(row);
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.className = "compare-row";
    const nameCell = document.createElement("td");
    nameCell.className = "compare-name-cell";
    nameCell.append(span(row.label, "compare-name"), span(`${row.positionCount ?? "--"} 只持仓`, "muted compare-sub"));
    const dailyPctCell = document.createElement("td");
    dailyPctCell.className = `numeric ${directionClass(row.dailyPnlPct)}`;
    dailyPctCell.append(document.createTextNode(formatPct(row.dailyPnlPct)));
    const dailyRank = rankBadge(row.rankDaily, total);
    if (dailyRank) dailyPctCell.append(dailyRank);
    const cumulativeCell = document.createElement("td");
    cumulativeCell.className = `numeric ${directionClass(row.cumulativePnl)}`;
    cumulativeCell.append(document.createTextNode(state.maskAmounts ? "•••" : formatAmount(row.cumulativePnl, true)));
    const cumulativeRank = rankBadge(row.rankCumulative, total);
    if (cumulativeRank) cumulativeCell.append(cumulativeRank);
    const recordCell = document.createElement("td");
    recordCell.className = `numeric ${directionClass(row.recordReturnPct)}`;
    recordCell.append(document.createTextNode(formatPct(row.recordReturnPct)));
    const recordRank = rankBadge(row.rankRecord, total);
    if (recordRank) recordCell.append(recordRank);
    if (isFiniteNumber(row.recordDays) && row.recordDays > 0) {
      recordCell.append(span(`${row.recordDays} 天`, "muted compare-sub"));
    }
    tr.append(
      nameCell,
      td(state.maskAmounts ? "•••" : formatAmount(row.marketValue), "numeric sensitive"),
      td(state.maskAmounts ? "•••" : formatAmount(row.dailyPnl, true), `numeric sensitive ${directionClass(row.dailyPnl)}`),
      dailyPctCell,
      td(state.maskAmounts ? "•••" : formatAmount(row.netEquity), "numeric sensitive"),
      td(formatPct(row.exposure), "numeric"),
      cumulativeCell,
      recordCell,
      td(row.topName ? `${row.topName} ${formatPct(row.topWeight)}` : "--", ""),
    );
    els.compareBody.append(tr);
  }
  const meta = state.comparePayload
    ? `行情时点 ${formatDateTime(state.comparePayload.quote_time || state.comparePayload.generated_at)} · ${total} 个组合`
    : state.compareError || "等待比较数据";
  els.compareMeta.textContent = meta;
  drawCompareChart(els.compareDailyChart, els.compareDailyEmpty, rows.map((row) => ({ label: row.label, value: row.dailyPnlPct })), "当日收益率");
  drawCompareChart(els.compareRecordChart, els.compareRecordEmpty, rows.map((row) => ({ label: row.label, value: row.recordReturnPct })), "记录以来收益");
  drawCompareTrend(rows);
  renderCompareShared();
}

function drawCompareChart(svg, emptyNode, entries, label) {
  if (!svg || !emptyNode) return;
  const usable = entries.filter((entry) => isFiniteNumber(entry.value));
  svg.replaceChildren();
  if (!usable.length) {
    svg.hidden = true;
    emptyNode.hidden = false;
    emptyNode.textContent = state.compareError || "暂无可比较的数据";
    return;
  }
  svg.hidden = false;
  emptyNode.hidden = true;
  const width = 960;
  const rowHeight = Math.min(34, 150 / usable.length);
  const topPad = 10;
  const maxAbs = Math.max(...usable.map((entry) => Math.abs(entry.value)), 0.5);
  const zeroX = 240 + (width - 280) * (maxAbs / (2 * maxAbs));
  const scale = (width - 280) / (2 * maxAbs);
  const axis = document.createElementNS("http://www.w3.org/2000/svg", "line");
  axis.setAttribute("x1", String(zeroX));
  axis.setAttribute("x2", String(zeroX));
  axis.setAttribute("y1", "0");
  axis.setAttribute("y2", "160");
  axis.setAttribute("class", "compare-chart-axis");
  svg.append(axis);
  usable.forEach((entry, index) => {
    const y = topPad + index * rowHeight;
    const barLength = Math.max(2, Math.abs(entry.value) * scale);
    const x = entry.value >= 0 ? zeroX : zeroX - barLength;
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", "8");
    text.setAttribute("y", String(y + rowHeight / 2 + 4));
    text.setAttribute("class", "compare-chart-label");
    text.textContent = entry.label;
    const bar = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    bar.setAttribute("x", String(x));
    bar.setAttribute("y", String(y + 4));
    bar.setAttribute("width", String(barLength));
    bar.setAttribute("height", String(rowHeight - 10));
    bar.setAttribute("rx", "3");
    bar.setAttribute("class", entry.value >= 0 ? "compare-bar-up" : "compare-bar-down");
    const valueText = document.createElementNS("http://www.w3.org/2000/svg", "text");
    valueText.setAttribute("x", String(entry.value >= 0 ? x + barLength + 6 : x - 6));
    valueText.setAttribute("y", String(y + rowHeight / 2 + 4));
    valueText.setAttribute("class", "compare-chart-value");
    valueText.setAttribute("text-anchor", entry.value >= 0 ? "start" : "end");
    valueText.textContent = formatPct(entry.value);
    svg.append(text, bar, valueText);
  });
}

const COMPARE_TREND_COLORS = [
  "#3a6ea5", "#c05a2e", "#2f8f6e", "#8a5fb0", "#b03a5b", "#707a2f",
  "#4a7a8c", "#a06a2f", "#5b5fb0", "#8c4a6e", "#476b3a", "#7456a8",
];

function buildCompareTrendSeries(rows) {
  return (Array.isArray(rows) ? rows : [])
    .map((row, index) => ({
      label: String(row?.label || row?.portfolioId || ""),
      color: COMPARE_TREND_COLORS[index % COMPARE_TREND_COLORS.length],
      points: (Array.isArray(row?.cumulativeSeries) ? row.cumulativeSeries : [])
        .filter((point) => point && point.date && isFiniteNumber(point.value)),
    }))
    .filter((series) => series.label && series.points.length);
}

function drawCompareTrend(rows) {
  const svg = els.compareTrendChart;
  const legend = els.compareTrendLegend;
  const emptyNode = els.compareTrendEmpty;
  if (!svg || !legend || !emptyNode) return;
  const seriesList = buildCompareTrendSeries(rows);
  svg.replaceChildren();
  legend.replaceChildren();
  if (!seriesList.length) {
    svg.hidden = true;
    legend.hidden = true;
    emptyNode.hidden = false;
    emptyNode.textContent = state.compareError || "暂无可比较的数据";
    return;
  }
  svg.hidden = false;
  legend.hidden = false;
  emptyNode.hidden = true;

  const dates = [...new Set(seriesList.flatMap((series) => series.points.map((point) => point.date)))].sort();
  const indexByDate = new Map(dates.map((date, index) => [date, index]));
  const values = seriesList.flatMap((series) => series.points.map((point) => point.value));
  const dataMin = Math.min(0, ...values);
  const dataMax = Math.max(0, ...values);
  const valueRange = Math.max(dataMax - dataMin, 1);
  const minValue = dataMin - valueRange * 0.08;
  const maxValue = dataMax + valueRange * 0.08;

  const width = 960;
  const height = 240;
  const leftPad = 12;
  const rightPad = 12;
  const topPad = 16;
  const bottomPad = 26;
  const plotW = width - leftPad - rightPad;
  const plotH = height - topPad - bottomPad;
  const xFor = (index) => leftPad + (dates.length <= 1 ? plotW / 2 : (index / (dates.length - 1)) * plotW);
  const yFor = (value) => topPad + ((maxValue - value) / (maxValue - minValue)) * plotH;

  if (dataMin < 0 && dataMax > 0) {
    svg.append(svgNode("line", { x1: leftPad, x2: width - rightPad, y1: yFor(0), y2: yFor(0), class: "compare-trend-zero" }));
  }
  const maxLabel = svgNode("text", { x: leftPad, y: topPad - 5, class: "compare-trend-axis-label" });
  maxLabel.textContent = formatAmount(dataMax);
  const minLabel = svgNode("text", { x: leftPad, y: topPad + plotH + 4, class: "compare-trend-axis-label" });
  minLabel.textContent = formatAmount(dataMin);
  svg.append(maxLabel, minLabel);
  const tickIndexes = [...new Set([0, Math.floor((dates.length - 1) / 2), dates.length - 1])];
  for (const index of tickIndexes) {
    const label = svgNode("text", {
      x: xFor(index),
      y: height - 8,
      class: "compare-trend-axis-label",
      "text-anchor": index === 0 ? "start" : index === dates.length - 1 ? "end" : "middle",
    });
    label.textContent = dates[index].slice(5).replace("-", "/");
    svg.append(label);
  }

  for (const series of seriesList) {
    const coords = series.points.map((point) => [xFor(indexByDate.get(point.date)), yFor(point.value), point]);
    if (coords.length === 1) {
      const dot = svgNode("circle", { cx: coords[0][0], cy: coords[0][1], r: 3.5, class: "compare-trend-dot" });
      dot.setAttribute("fill", series.color);
      const title = svgNode("title");
      title.textContent = `${series.label} ${coords[0][2].date} ${formatAmount(coords[0][2].value, true)}`;
      dot.append(title);
      svg.append(dot);
    } else {
      const line = svgNode("polyline", {
        points: coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "),
        class: "compare-trend-line",
      });
      line.setAttribute("stroke", series.color);
      svg.append(line);
      const end = svgNode("circle", { cx: coords[coords.length - 1][0], cy: coords[coords.length - 1][1], r: 3, class: "compare-trend-dot" });
      end.setAttribute("fill", series.color);
      const endTitle = svgNode("title");
      endTitle.textContent = `${series.label} ${coords[coords.length - 1][2].date} ${formatAmount(coords[coords.length - 1][2].value, true)}`;
      end.append(endTitle);
      svg.append(end);
    }
  }

  for (const series of seriesList) {
    const chip = span("", "compare-trend-chip");
    const swatch = span("", "compare-trend-swatch");
    swatch.style.background = series.color;
    const first = series.points[0];
    const last = series.points[series.points.length - 1];
    chip.append(swatch, document.createTextNode(`${series.label} ${formatAmount(last.value, true)}`));
    chip.title = `${series.label}：${first.date} 至 ${last.date}，共 ${series.points.length} 个交易日`;
    legend.append(chip);
  }
}

function renderCompareShared() {
  if (!els.compareSharedList) return;
  const shared = Array.isArray(state.comparePayload?.shared_positions) ? state.comparePayload.shared_positions : [];
  els.compareSharedList.replaceChildren();
  els.compareSharedCount.textContent = shared.length ? `${shared.length} 只股票被多个组合持有，行情与新闻只获取一次` : "没有跨组合的重合持仓";
  if (!shared.length) return;
  for (const item of shared.slice(0, 24)) {
    const chip = span("", "compare-shared-chip");
    const holders = Array.isArray(item.holders) ? item.holders : [];
    chip.textContent = `${item.name || item.thscode} · ${holders.map((h) => h.label).join(" / ")}`;
    chip.title = `${item.thscode}：${holders.map((h) => `${h.label} ${h.quantity ?? "--"}股`).join("；")}`;
    els.compareSharedList.append(chip);
  }
}

/* ---------- 组合编辑器 ---------- */

function editorAddRow(data = {}) {
  const row = document.createElement("tr");
  const codeCell = document.createElement("td");
  const codeInput = document.createElement("input");
  codeInput.type = "text";
  codeInput.inputMode = "numeric";
  codeInput.maxLength = 6;
  codeInput.placeholder = "600519";
  codeInput.value = String(data.code || "");
  codeInput.className = "editor-code-input";
  codeCell.append(codeInput);
  const exchangeCell = document.createElement("td");
  const exchangeSelect = document.createElement("select");
  for (const value of ["SH", "SZ", "BJ"]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    exchangeSelect.append(option);
  }
  exchangeSelect.value = data.exchange || "SH";
  exchangeCell.append(exchangeSelect);
  const quantityCell = document.createElement("td");
  const quantityInput = document.createElement("input");
  quantityInput.type = "number";
  quantityInput.min = "1";
  quantityInput.step = "1";
  quantityInput.placeholder = "100";
  quantityInput.value = data.quantity ? String(data.quantity) : "";
  quantityInput.className = "editor-quantity-input";
  quantityCell.append(quantityInput);
  quantityCell.className = "numeric";
  const removeCell = document.createElement("td");
  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.className = "plain-button editor-row-remove";
  removeButton.textContent = "移除";
  removeButton.addEventListener("click", () => row.remove());
  removeCell.append(removeButton);
  row.append(codeCell, exchangeCell, quantityCell, removeCell);
  els.editorPositionsBody.append(row);
}

function editorRowsPayload() {
  const rows = [];
  for (const tr of els.editorPositionsBody.querySelectorAll("tr")) {
    const inputs = tr.querySelectorAll("input");
    const select = tr.querySelector("select");
    const code = (inputs[0]?.value || "").trim();
    const quantity = (inputs[1]?.value || "").trim();
    if (!code && !quantity) continue;
    rows.push({ thscode: `${code}.${select?.value || "SH"}`, quantity });
  }
  return rows;
}

function openPortfolioEditor(mode, portfolioId = "") {
  state.editorMode = mode;
  state.editorPortfolioId = portfolioId;
  state.editorDeleteArmed = false;
  els.editorError.hidden = true;
  els.editorError.textContent = "";
  els.editorDelete.hidden = mode !== "edit";
  els.editorDelete.textContent = "删除组合";
  els.editorBaseField.hidden = mode !== "create";
  els.portfolioEditorTitle.textContent = mode === "edit" ? "编辑组合" : "新建组合";
  els.editorBase.replaceChildren();
  for (const item of state.portfolios) {
    const option = document.createElement("option");
    option.value = item.portfolioId;
    option.textContent = item.label;
    els.editorBase.append(option);
  }
  els.editorPositionsBody.replaceChildren();
  if (mode === "edit") {
    els.editorSave.textContent = "保存修改";
    els.editorLabel.value = "";
    els.editorNote.value = "";
    els.editorCash.value = "0";
    els.editorDebt.value = "0";
    void loadPortfolioForEdit(portfolioId);
  } else {
    els.editorSave.textContent = "保存组合";
    els.editorLabel.value = "";
    els.editorNote.value = "";
    els.editorCash.value = "0";
    els.editorDebt.value = "0";
    editorAddRow();
  }
  els.portfolioEditor.hidden = false;
}

async function loadPortfolioForEdit(portfolioId) {
  try {
    const payload = await fetchJson(`/api/quotes?portfolio=${encodeURIComponent(portfolioId)}`);
    const entry = state.portfolios.find((item) => item.portfolioId === portfolioId);
    if (entry) {
      els.editorLabel.value = entry.label;
      els.editorNote.value = entry.note || "";
    }
    const summary = asObject(rootObject(payload).portfolio);
    els.editorCash.value = String(firstNumber(pickNumber(summary, ["cash"]), 0));
    els.editorDebt.value = String(firstNumber(pickNumber(summary, ["debt"]), 0));
    els.editorPositionsBody.replaceChildren();
    const positions = normalizePositions(payload, {});
    for (const position of positions) {
      const symbol = String(position.symbol || "");
      const [code, exchange] = symbol.split(".");
      editorAddRow({ code, exchange, quantity: position.quantity });
    }
    if (!positions.length) editorAddRow();
  } catch (error) {
    showEditorError(friendlyError(error, "组合持仓加载失败"));
  }
}

function closePortfolioEditor() {
  els.portfolioEditor.hidden = true;
}

function showEditorError(message) {
  els.editorError.textContent = message;
  els.editorError.hidden = !message;
}

async function submitPortfolioEditor() {
  const label = els.editorLabel.value.trim();
  if (!label) {
    showEditorError("请填写组合名称");
    return;
  }
  const rows = editorRowsPayload();
  if (!rows.length) {
    showEditorError("请至少保留一行有效持仓（代码 + 数量）");
    return;
  }
  const body = {
    label,
    note: els.editorNote.value.trim(),
    cash: els.editorCash.value,
    debt: els.editorDebt.value,
    positions: rows,
  };
  els.editorSave.disabled = true;
  try {
    let payload;
    if (state.editorMode === "edit") {
      payload = await sendPortfolioMutation("PUT", `/api/portfolios/${encodeURIComponent(state.editorPortfolioId)}`, body);
    } else {
      body.base_portfolio_id = els.editorBase.value || MAIN_PORTFOLIO_ID;
      payload = await sendPortfolioMutation("POST", "/api/portfolios", body);
    }
    if (!payload || payload.ok === false) throw new Error(payload?.error || "保存失败");
    await refreshPortfolios();
    const target = payload.created || payload.updated || state.editorPortfolioId;
    closePortfolioEditor();
    if (target) switchPortfolio(target);
    else await refreshAll(true);
  } catch (error) {
    showEditorError(friendlyError(error, "保存组合失败"));
  } finally {
    els.editorSave.disabled = false;
  }
}

async function deleteActivePortfolio() {
  if (state.editorMode !== "edit" || !state.editorPortfolioId) return;
  if (!state.editorDeleteArmed) {
    state.editorDeleteArmed = true;
    els.editorDelete.textContent = "再次点击确认删除";
    window.setTimeout(() => {
      state.editorDeleteArmed = false;
      if (!els.editorDelete.hidden) els.editorDelete.textContent = "删除组合";
    }, 3_000);
    return;
  }
  els.editorDelete.disabled = true;
  try {
    const payload = await sendPortfolioMutation("DELETE", `/api/portfolios/${encodeURIComponent(state.editorPortfolioId)}`);
    if (!payload || payload.ok === false) throw new Error(payload?.error || "删除失败");
    const deletedId = state.editorPortfolioId;
    closePortfolioEditor();
    await refreshPortfolios();
    if (state.activePortfolioId === deletedId) switchPortfolio(MAIN_PORTFOLIO_ID);
    else await refreshAll(true);
  } catch (error) {
    showEditorError(friendlyError(error, "删除组合失败"));
  } finally {
    els.editorDelete.disabled = false;
  }
}

async function sendPortfolioMutation(method, url, body) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 20_000);
  try {
    const response = await fetch(url, {
      method,
      cache: "no-store",
      headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
    return payload;
  } finally {
    window.clearTimeout(timeout);
  }
}


function renderQuoteSections() {
  renderMarkets();
  renderBreadth();
  renderGlobals();
  renderOvernight();
  renderSummary();
  renderPositions();
  renderConnectionState();
  renderSourceLabels();
  renderNotice();
}

function renderMarkets() {
  const focusedChartSymbol = document.activeElement?.matches?.(".stock-chart-trigger")
    ? normalizeChartSymbol(document.activeElement.dataset.symbol)
    : "";
  let replacementFocus = null;
  els.marketGrid.replaceChildren();
  if (!state.markets.length) {
    els.marketGrid.append(emptyNode("暂无市场指数数据", "compact"));
    return;
  }

  const ordered = [...state.markets].sort((a, b) => {
    const ai = INDEX_ORDER.indexOf(a.name);
    const bi = INDEX_ORDER.indexOf(b.name);
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
  });

  for (const market of ordered.slice(0, 7)) {
    const symbol = normalizeChartSymbol(market.symbol);
    const quoteTitle = market.quoteTime ? `报价 ${formatDateTime(market.quoteTime)}${market.isStale ? " · 延迟" : ""}` : "";
    // 与持仓行情一致的交互：整张卡片是图表触发器，悬停预览 / 点击固定。
    const item = symbol ? document.createElement("button") : div("market-item");
    if (symbol) {
      item.type = "button";
      item.className = "market-item stock-chart-trigger";
      item.dataset.symbol = symbol;
      item.setAttribute("aria-controls", "stock-chart-popover");
      item.setAttribute("aria-haspopup", "dialog");
      item.setAttribute("aria-expanded", state.stockChartSymbol === symbol && !els.stockChartPopover.hidden ? "true" : "false");
      item.setAttribute("aria-label", `查看${market.name || symbol}的日K线和当日分时图`);
      item.title = `查看${market.name || symbol}K线与分时图${quoteTitle ? ` · ${quoteTitle}` : ""}`;
      if (focusedChartSymbol === symbol) replacementFocus = item;
      if (state.stockChartSymbol === symbol && !els.stockChartPopover.hidden) {
        state.stockChartAnchor = item;
      }
    } else {
      if (market.quoteTime) item.title = quoteTitle;
    }
    const nameLine = div("market-name");
    nameLine.append(span(market.name || market.symbol || "指数"), span(formatCompactTurnover(market.turnover)));
    const numberLine = div("market-number-line");
    const price = span(formatPrice(market.price), "market-price");
    const change = span(formatPct(market.changePct), directionClass(market.changePct));
    numberLine.append(price, change);
    item.append(nameLine, numberLine);
    // 国内指数卡片内嵌当日分时小图：有缓存立即绘制，否则由调度器拉取后回填。
    if (symbol && INDEX_CHART_SYMBOLS.has(symbol)) {
      const spark = svgNode("svg", { class: "market-spark" });
      spark.dataset.symbol = symbol;
      spark.setAttribute("hidden", "");
      const cachedSpark = state.indexIntraday.cache.get(symbol);
      if (cachedSpark) renderPositionIntradaySparkline(spark, cachedSpark.items, cachedSpark.prevClose);
      item.append(spark);
    }
    els.marketGrid.append(item);
  }

  if (replacementFocus) replacementFocus.focus({ preventScroll: true });
  startIndexIntradayScheduler();
}

// ---- 主要市场指数：卡片内嵌当日分时 -------------------------------

function fetchIndexIntraday(symbol) {
  let pending = state.indexIntraday.pending.get(symbol);
  if (!pending) {
    pending = (async () => {
      try {
        const payload = await fetchJson(`/api/stock-chart?symbol=${encodeURIComponent(symbol)}`);
        if (payload?.ok === false) throw new Error(payload?.message || "fetch failed");
        const chart = normalizeStockChart(payload);
        const result = {
          receivedAt: Date.now(),
          items: chart.intraday.items,
          prevClose: chart.intraday.prevClose,
        };
        state.indexIntraday.cache.set(symbol, result);
        paintIndexSparklines(symbol);
        return result;
      } catch {
        return null;
      } finally {
        if (state.indexIntraday.pending.get(symbol) === pending) {
          state.indexIntraday.pending.delete(symbol);
        }
      }
    })();
    state.indexIntraday.pending.set(symbol, pending);
  }
  return pending;
}

function paintIndexSparklines(symbol) {
  // 市场卡片随行情刷新整排重建，数据到达后绘制当前 DOM 中的对应小图。
  const data = state.indexIntraday.cache.get(symbol);
  document
    .querySelectorAll(`#market-grid .market-spark[data-symbol="${symbol}"]`)
    .forEach((svg) => {
      if (!data || !renderPositionIntradaySparkline(svg, data.items, data.prevClose)) {
        svg.setAttribute("hidden", "");
      }
    });
}

function startIndexIntradayScheduler() {
  if (state.indexIntraday.started) return;
  state.indexIntraday.started = true;
  // 首屏：全部指数并发拉取，到达即绘制。
  INDEX_INTRADAY_SYMBOLS.forEach((symbol) => void fetchIndexIntraday(symbol));
  // 之后每只指数独立 120 秒一轮，相位均匀错开（约 17 秒一只），避免同时打接口。
  const stagger = INDEX_INTRADAY_REFRESH_MS / INDEX_INTRADAY_SYMBOLS.length;
  INDEX_INTRADAY_SYMBOLS.forEach((symbol, index) => {
    const tick = () => {
      void fetchIndexIntraday(symbol);
      window.setTimeout(tick, INDEX_INTRADAY_REFRESH_MS);
    };
    window.setTimeout(tick, INDEX_INTRADAY_REFRESH_MS + index * stagger);
  });
}

// ---- 先行指标：A50 期货 + 期权 IV/PCR -------------------------------

async function refreshLeadingIndicators() {
  if (state.leadingIndicatorsLoading) return;
  state.leadingIndicatorsLoading = true;
  try {
    // 期权 IV 需现场拉取期权链并反解，冷计算可能耗时数十秒，超时放宽到 90 秒。
    const payload = await fetchJson("/api/leading-indicators", 90_000);
    if (payload?.ok === false) throw new Error(payload?.message || "fetch failed");
    state.leadingIndicators = payload;
    // 每个先行指标拿到有效值时追加一个分时点，供卡片内分时曲线展示。
    const now = Date.now();
    for (const spec of LEADING_SPARK_KEYS) {
      const value = spec.extract(payload);
      if (value == null || !isFiniteNumber(value)) continue;
      const hist = state.leadingHistory[spec.key] || (state.leadingHistory[spec.key] = []);
      const last = hist[hist.length - 1];
      // 同一秒内重复值不重复追加，避免点位堆积成竖线。
      if (!last || now - last.t >= 1000) {
        hist.push({ t: now, value });
        if (hist.length > LEADING_SPARK_MAX_POINTS) {
          hist.splice(0, hist.length - LEADING_SPARK_MAX_POINTS);
        }
      } else {
        // 同秒内只更新最新点的值，反映最新报价而不新增点位。
        last.value = value;
      }
    }
  } catch {
    // 网络失败时保留上一份数据；首次失败保持 null，由渲染层显示等待态。
  } finally {
    state.leadingIndicatorsLoading = false;
    renderLeadingIndicators();
    scheduleLeadingEarlyRefresh();
  }
}

// ASAP 加载：只要还有任何"已可得但点位不足 2 个（无法连线）"的先行指标分时图，
// 就 2 秒后再拉一次，让分时图尽快画出来；所有可得因子的分时图都能画线后，
// 回归 8 秒轮询。连续早刷新上限 LEADING_EARLY_REFRESH_MAX 次，避免某个因子
// 持续不可用时无限早刷新。
let _leadingEarlyTimer = null;
function scheduleLeadingEarlyRefresh() {
  const needsEarly = LEADING_SPARK_KEYS.some((spec) => {
    const hist = state.leadingHistory[spec.key] || [];
    return hist.length === 1;
  });
  if (needsEarly && (state.leadingEarlyRefreshCount || 0) < LEADING_EARLY_REFRESH_MAX && !_leadingEarlyTimer) {
    state.leadingEarlyRefreshCount = (state.leadingEarlyRefreshCount || 0) + 1;
    _leadingEarlyTimer = setTimeout(() => {
      _leadingEarlyTimer = null;
      void refreshLeadingIndicators();
    }, LEADING_EARLY_REFRESH_MS);
  } else if (!needsEarly) {
    // 所有可得因子分时图都已能画线，重置早刷新计数。
    state.leadingEarlyRefreshCount = 0;
  }
}

// ---------------------------------------------------------------- 衍生品风险仪表盘
//
// 这一块把「10-15 市场研判」里可量化的说法拆成因子，逐个标注口径是否科学，
// 再用实盘数据自动核对。后端 derivatives_regime 负责取数与判定，前端只做呈现。

const DERIVATIVES_STATUS_CLASS = {
  confirmed: "is-ok",
  partial: "is-warning",
  refuted: "is-blocked",
  pending: "is-pending",
};

const DERIVATIVES_VERDICT_CLASS = {
  ok: "is-ok",
  recalibrate: "is-warning",
  wrong: "is-blocked",
  principle: "",
  backtest: "is-pending",
};

function derivativesNum(value, suffix = "", digits = 2) {
  if (!isFiniteNumber(value)) return "--";
  return `${value.toFixed(digits)}${suffix}`;
}

function renderDerivativesFactorReview(reviewList) {
  if (!els.derivativesFactors) return;
  els.derivativesFactors.replaceChildren();
  if (!Array.isArray(reviewList) || !reviewList.length) {
    els.derivativesFactors.append(span("暂无因子审查数据", "muted"));
    return;
  }
  for (const raw of reviewList) {
    const item = asObject(raw);
    const row = div("factor-row");
    const head = div("factor-head");
    const badge = span(String(item.rating || "--"), `decision-badge ${DERIVATIVES_VERDICT_CLASS[item.verdict] || ""}`.trim());
    head.append(badge, span(String(item.factor || "--"), "factor-name"));
    row.append(head);
    const claim = div("factor-claim");
    claim.textContent = `「${item.claim || "--"}」`;
    row.append(claim);
    const ours = div("factor-ours");
    ours.append(span("本面板口径", "muted"), span(String(item.ours || "--"), "factor-ours-text"));
    row.append(ours);
    const why = div("factor-why");
    why.textContent = String(item.why || "");
    row.append(why);
    els.derivativesFactors.append(row);
  }
}

function renderDerivativesReadings(payload) {
  if (!els.derivativesReadings) return;
  els.derivativesReadings.replaceChildren();
  const readings = Array.isArray(payload.readings) ? payload.readings : [];
  const vix = asObject(payload.vix);
  const history = asObject(payload.history);

  for (const raw of readings) {
    const item = asObject(raw);
    const card = div("derivative-card");
    const head = div("derivative-card-head");
    head.append(span(String(item.label || "--"), "derivative-card-title"));
    if (isFiniteNumber(item.underlying_change_pct)) {
      head.append(span(
        `${formatPrice(item.underlying_price)} ${formatPct(item.underlying_change_pct)}`,
        `market-price ${directionClass(item.underlying_change_pct)}`,
      ));
    }
    card.append(head);

    const ivLine = div("derivative-card-main");
    ivLine.append(
      span(isFiniteNumber(item.iv_near) ? `${item.iv_near.toFixed(1)}%` : "--", "derivative-big"),
      span(isFiniteNumber(item.iv_near_days) ? `近月平值 IV · ${Math.round(item.iv_near_days)}天` : "近月平值 IV", "muted"),
    );
    card.append(ivLine);

    const rows = div("derivative-rows");
    const pushRow = (label, value, title) => {
      const row = div("derivative-row");
      row.append(span(label, "muted"), span(value, "derivative-row-value"));
      if (title) row.title = title;
      rows.append(row);
    };
    pushRow("次月 IV", isFiniteNumber(item.iv_next)
      ? `${item.iv_next.toFixed(1)}%${isFiniteNumber(item.iv_next_days) ? `（${Math.round(item.iv_next_days)}天）` : ""}`
      : "--");
    const shapeText = { contango: "contango（常态）", backwardation: "backwardation（近端避险）", flat: "flat（持平）" };
    pushRow(
      "期限结构",
      shapeText[item.term_shape] || "--",
      "近月 IV 与次月 IV 的差：近月高于次月（backwardation）表示避险需求集中在近端，是期权口径里最可靠的压力信号。",
    );
    pushRow(
      "25Δ 风险逆转 RR",
      derivativesNum(item.rr_25d, "", 2),
      "25Δ 认沽 IV 减 25Δ 认购 IV（恒定期限≈30天）。正值表示看跌保护更贵，市场更愿意为下行买保险。",
    );
    pushRow("25Δ 蝶式 BF", derivativesNum(item.bf_25d, "", 2), "0.5×(25Δ 认购 + 25Δ 认沽) 减 平值 IV。正值代表微笑曲线上凸。");
    pushRow("持仓 PCR", derivativesNum(item.oi_pcr, "", 2), "认沽持仓量 / 认购持仓量。水平值跨市场不可比，只宜作自身历史参照。");
    pushRow("成交 PCR", derivativesNum(item.vol_pcr, "", 2), "认沽成交量 / 认购成交量，反映当日交易层面的情绪。");
    pushRow("持仓变化比", derivativesNum(item.oi_change_ratio, "", 2), "认沽净增仓 / 认购净增仓。比水平值更早反映资金流向的切换。");
    pushRow("max pain", item.max_pain == null ? "--" : String(item.max_pain), "使期权卖方总亏损最小的到期价位。");
    card.append(rows);

    if (isFiniteNumber(history.iv_history_days) && history.iv_history_days > 0) {
      const foot = div("derivative-card-foot muted");
      foot.textContent = `A股 IV 历史百分位样本：${history.iv_history_days} 个交易日（逐日积累中）`;
      card.append(foot);
    }
    els.derivativesReadings.append(card);
  }

  // VIX 卡片：把绝对水平换成历史百分位，才能回答「贵不贵」
  if (isFiniteNumber(vix.last)) {
    const card = div("derivative-card");
    const head = div("derivative-card-head");
    head.append(span("VIX 恐慌指数（美股）", "derivative-card-title"));
    if (isFiniteNumber(vix.change_pct)) {
      head.append(span(formatPct(vix.change_pct), `market-price ${directionClass(vix.change_pct)}`));
    }
    card.append(head);

    const ivLine = div("derivative-card-main");
    ivLine.append(
      span(vix.last.toFixed(2), "derivative-big"),
      span("Cboe 波动率指数", "muted"),
    );
    card.append(ivLine);

    const rows = div("derivative-rows");
    const pushRow = (label, value, title) => {
      const row = div("derivative-row");
      row.append(span(label, "muted"), span(value, "derivative-row-value"));
      if (title) row.title = title;
      rows.append(row);
    };
    pushRow("昨收", derivativesNum(vix.prev_close, "", 2));
    pushRow(
      "历史百分位",
      isFiniteNumber(vix.percentile) ? `第 ${vix.percentile} 百分位` : "--",
      `样本 ${vix.sample_days || 0} 个交易日（自 ${vix.sample_start || "--"}），中位数 ${vix.median ?? "--"}，区间 ${vix.min ?? "--"}~${vix.max ?? "--"}。`,
    );
    pushRow("样本区间", vix.sample_start ? `${vix.sample_start} 起 ${vix.sample_days} 日` : "--");
    pushRow("样本中位数", derivativesNum(vix.median, "", 2));
    card.append(rows);
    els.derivativesReadings.append(card);
  }

  if (!els.derivativesReadings.childElementCount) {
    els.derivativesReadings.append(span("衍生品数据暂不可用（数据源限流中，下轮自动重试）", "muted"));
  }
}

function renderDerivativesChecks(payload) {
  if (!els.derivativesChecks) return;
  els.derivativesChecks.replaceChildren();
  const checks = Array.isArray(payload.checks) ? payload.checks : [];
  if (!checks.length) {
    els.derivativesChecks.append(span("暂无核查结果", "muted"));
    if (els.derivativesCheckSummary) els.derivativesCheckSummary.textContent = "";
    return;
  }
  const counts = { confirmed: 0, partial: 0, refuted: 0, pending: 0 };
  for (const raw of checks) {
    const item = asObject(raw);
    const status = String(item.status || "pending");
    counts[status] = (counts[status] || 0) + 1;
    const row = div("check-row");
    const head = div("check-head");
    head.append(
      span(String(item.status_text || "待验证"), `decision-badge ${DERIVATIVES_STATUS_CLASS[status] || ""}`.trim()),
      span(String(item.title || "--"), "check-title"),
      span(String(item.factor || ""), "muted check-factor"),
    );
    row.append(head);
    const text = div("check-text");
    text.textContent = String(item.text || "");
    row.append(text);
    els.derivativesChecks.append(row);
  }
  if (els.derivativesCheckSummary) {
    els.derivativesCheckSummary.replaceChildren(
      span(`已验证 ${counts.confirmed}`, "muted"),
      span(`部分成立 ${counts.partial}`, "muted"),
      span(`不成立 ${counts.refuted}`, "muted"),
      span(`待验证 ${counts.pending}`, "muted"),
    );
  }
}

function renderDerivativesRegime() {
  if (!els.derivativesFactors) return;
  const payload = state.derivativesRegime;
  if (!payload) {
    els.derivativesMeta.textContent = "正在获取…";
    els.derivativesState.textContent = "加载中";
    els.derivativesState.className = "decision-badge is-pending";
    return;
  }
  const errors = Array.isArray(payload.errors) ? payload.errors : [];
  const stale = Boolean(payload.stale);
  const metaParts = [];
  if (payload.generated_at) metaParts.push(`更新 ${formatDateTime(payload.generated_at)}`);
  const optionsMeta = asObject(payload.options_meta);
  if (optionsMeta.available && optionsMeta.computed_at) {
    metaParts.push(`期权 ${formatDateTime(optionsMeta.computed_at)}`);
  } else if (optionsMeta.error) {
    metaParts.push("期权数据源限流中");
  }
  if (stale) metaParts.push("展示上次缓存");
  els.derivativesMeta.textContent = metaParts.join(" · ") || "--";

  const confirmed = (Array.isArray(payload.checks) ? payload.checks : [])
    .filter((c) => asObject(c).status === "confirmed").length;
  const refuted = (Array.isArray(payload.checks) ? payload.checks : [])
    .filter((c) => asObject(c).status === "refuted").length;
  if (payload.ok === false) {
    els.derivativesState.textContent = "不可用";
    els.derivativesState.className = "decision-badge is-blocked";
  } else {
    els.derivativesState.textContent = `已验证 ${confirmed} · 不成立 ${refuted}`;
    els.derivativesState.className = "decision-badge is-ok";
  }

  renderDerivativesFactorReview(payload.factor_review);
  renderDerivativesReadings(payload);
  renderDerivativesChecks(payload);

  if (errors.length) {
    const warn = div("derivatives-warn");
    warn.textContent = `部分数据源降级：${errors.join("；")}`;
    els.derivativesFactors.append(warn);
  }
}

async function refreshDerivativesRegime() {
  if (state.derivativesRegimeLoading) return;
  state.derivativesRegimeLoading = true;
  try {
    const payload = await fetchJson("/api/derivatives-regime", 60_000);
    if (payload?.ok === false && !payload?.factor_review) throw new Error(payload?.error || "fetch failed");
    state.derivativesRegime = payload;
  } catch {
    // 失败时保留上一份数据，由渲染层显示等待态
  } finally {
    state.derivativesRegimeLoading = false;
    renderDerivativesRegime();
  }
}

function renderLeadingIndicators() {
  if (!els.leadingGrid) return;
  els.leadingGrid.replaceChildren();
  const payload = state.leadingIndicators;
  if (!payload) {
    els.leadingMeta.textContent = "正在获取…";
    els.leadingGrid.append(span("正在获取先行指标…", "muted"));
    return;
  }
  const a50 = asObject(payload.a50);
  const options = asObject(payload.options);
  const margin = asObject(payload.margin);
  const northbound = asObject(payload.northbound);
  const limitPool = asObject(payload.limit_pool);
  const basis = asObject(payload.basis);
  const moneyRates = asObject(payload.money_rates);
  const imbalance = asObject(payload.imbalance);

  const metaParts = [];
  if (a50.quote_time) metaParts.push(`A50 ${formatDateTime(a50.quote_time)}`);
  if (options.computed_at) metaParts.push(`期权 ${formatDateTime(options.computed_at)}`);
  if (margin.date) metaParts.push(`两融 ${margin.date}`);
  if (northbound.date) metaParts.push(`北向 ${northbound.date}`);
  els.leadingMeta.textContent = metaParts.join(" · ") || "--";

  // ---- A50 期货（含前端累积分时小图） ----
  const a50Card = div("market-item leading-item");
  const a50NameLine = div("market-name");
  a50NameLine.append(
    span(String(a50.name || "富时A50期货")),
    span(a50.stale ? "延迟" : "", "muted"),
  );
  const a50NumberLine = div("market-number-line");
  if (a50.available) {
    a50NumberLine.append(
      span(formatPrice(a50.price), "market-price"),
      span(formatPct(a50.change_pct), directionClass(a50.change_pct)),
    );
    a50Card.title = `盘前/夜盘先行参考 · 报价 ${a50.quote_time || "--"} · 高 ${formatPrice(a50.high)} 低 ${formatPrice(a50.low)}`;
  } else {
    a50NumberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
    a50Card.title = `A50 期货暂不可用：${a50.error || "未知原因"}`;
  }
  a50Card.append(a50NameLine, a50NumberLine);
  appendLeadingSparkline(a50Card, "a50");
  els.leadingGrid.append(a50Card);

  // ---- 期权情绪 IV/PCR + skew/max_pain/期限结构 ----
  const optionItems = Array.isArray(options.items) ? options.items : [];
  if (options.available && optionItems.length) {
    for (let optIdx = 0; optIdx < optionItems.length; optIdx++) {
      const item = asObject(optionItems[optIdx]);
      const near = asObject(item.iv_near);
      const next = asObject(item.iv_next);
      const card = div("market-item leading-item");
      const nameLine = div("market-name");
      nameLine.append(
        span(String(item.label || item.key || "期权")),
        span(isFiniteNumber(item.oi_pcr) ? `PCR ${item.oi_pcr.toFixed(2)}` : "", "muted"),
      );
      const numberLine = div("market-number-line");
      numberLine.append(
        span(isFiniteNumber(near.iv) ? `${near.iv.toFixed(1)}%` : "--", "market-price"),
        span(isFiniteNumber(near.days) ? `近月${Math.round(near.days)}天` : "", "muted"),
      );
      const subParts = [];
      if (isFiniteNumber(next.iv)) {
        subParts.push(`次月 ${next.iv.toFixed(1)}%${isFiniteNumber(next.days) ? `（${Math.round(next.days)}天）` : ""}`);
      }
      if (isFiniteNumber(item.vol_pcr)) subParts.push(`成交PCR ${item.vol_pcr.toFixed(2)}`);
      if (isFiniteNumber(item.iv_skew)) subParts.push(`skew ${item.iv_skew >= 0 ? "+" : ""}${item.iv_skew.toFixed(1)}%`);
      if (isFiniteNumber(item.max_pain)) subParts.push(`max_pain ${item.max_pain}`);
      const subLine = div("leading-sub muted");
      subLine.textContent = subParts.join(" · ") || "—";
      card.append(nameLine, numberLine, subLine);
      card.title = "平值隐含波动率（IV）：越高代表市场越恐慌；PCR=看跌/看涨持仓比，>1 偏避险；skew>0 偏空（put IV 更高）；max_pain=使期权卖方总亏损最小的标的价位";
      appendLeadingSparkline(card, `iv_${optIdx}`);
      els.leadingGrid.append(card);
    }
  } else {
    const card = div("market-item leading-item");
    const nameLine = div("market-name");
    nameLine.append(span("期权情绪 IV/PCR"), span("", "muted"));
    const numberLine = div("market-number-line");
    numberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
    const subLine = div("leading-sub muted");
    subLine.textContent = options.stale ? "展示上一次缓存" : "数据源限流中，下轮自动重试";
    card.append(nameLine, numberLine, subLine);
    card.title = `期权情绪暂不可用：${options.error || "未知原因"}`;
    els.leadingGrid.append(card);
  }

  // ---- iVIX 近似：50ETF ATM IV（中国波指 2018 停发） ----
  {
    const iv50 = asObject((optionItems[0] && asObject(optionItems[0]).iv_near) || {});
    const card = div("market-item leading-item");
    const nameLine = div("market-name");
    nameLine.append(span("iVIX 近似"), span("50ETF ATM", "muted"));
    const numberLine = div("market-number-line");
    numberLine.append(
      span(isFiniteNumber(iv50.iv) ? `${iv50.iv.toFixed(1)}%` : "--", "market-price"),
      span("≈ 波指", "muted"),
    );
    const subLine = div("leading-sub muted");
    subLine.textContent = "中国波指 iVIX 上交所 2018 停发，用 50ETF 平值 IV 近似";
    card.append(nameLine, numberLine, subLine);
    card.title = "iVIX（中国波指）2018 年上交所停发，此处用 50ETF 平值隐含波动率近似";
    appendLeadingSparkline(card, "iv_0");
    els.leadingGrid.append(card);
  }

  // ---- 两融：融资余额/净买入 + 融券余额/卖出量 ----
  {
    const card = div("market-item leading-item");
    const nameLine = div("market-name");
    nameLine.append(span("两融"), span(margin.date || "", "muted"));
    const numberLine = div("market-number-line");
    const subParts = [];
    if (margin.available) {
      numberLine.append(
        span(isFiniteNumber(margin.rzye_yi) ? `${margin.rzye_yi}亿` : "--", "market-price"),
        span(isFiniteNumber(margin.rzjme_yi) ? `净买入${margin.rzjme_yi}亿` : "", "muted"),
      );
      if (isFiniteNumber(margin.rqye_yi)) subParts.push(`融券余额 ${margin.rqye_yi}亿`);
      if (isFiniteNumber(margin.rqmre_yi)) subParts.push(`融券卖出 ${margin.rqmre_yi}亿`);
      if (isFiniteNumber(margin.rzmre_yi)) subParts.push(`融资买入 ${margin.rzmre_yi}亿`);
      card.title = `融资余额 ${margin.rzye_yi}亿 · 净买入 ${margin.rzjme_yi}亿 · 5日累计 ${margin.rzjme_5d_yi}亿 · 融券余额 ${margin.rqye_yi}亿 · 融券卖出 ${margin.rqmre_yi}亿`;
    } else {
      numberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
      card.title = `两融暂不可用：${margin.error || "未知原因"}`;
    }
    const subLine = div("leading-sub muted");
    subLine.textContent = subParts.join(" · ") || (margin.available ? "—" : "暂不可用");
    card.append(nameLine, numberLine, subLine);
    appendLeadingSparkline(card, "margin_rzye");
    els.leadingGrid.append(card);
  }

  // ---- 北向资金净流入 ----
  {
    const card = div("market-item leading-item");
    const nameLine = div("market-name");
    nameLine.append(span("北向资金"), span(northbound.date || "", "muted"));
    const numberLine = div("market-number-line");
    if (northbound.available && isFiniteNumber(northbound.net_yi)) {
      numberLine.append(
        span(`${northbound.net_yi >= 0 ? "+" : ""}${northbound.net_yi}亿`, directionClass(northbound.net_yi)),
        span("净流入", "muted"),
      );
      card.title = `沪深港通北向当日净流入 ${northbound.net_yi}亿（${northbound.source || "东财"}）`;
    } else {
      numberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
      card.title = `北向资金暂不可用：${northbound.error || "未知原因"}`;
    }
    const subLine = div("leading-sub muted");
    subLine.textContent = northbound.available ? "盘后/盘前参考" : "暂不可用";
    card.append(nameLine, numberLine, subLine);
    appendLeadingSparkline(card, "northbound");
    els.leadingGrid.append(card);
  }

  // ---- 涨跌停数量 + 封板率 ----
  {
    const card = div("market-item leading-item");
    const nameLine = div("market-name");
    nameLine.append(span("涨跌停"), span("封板率", "muted"));
    const numberLine = div("market-number-line");
    if (limitPool.available) {
      numberLine.append(
        span(`${limitPool.limit_up}↑ / ${limitPool.limit_down}↓`, "market-price"),
        span(isFiniteNumber(limitPool.seal_rate) ? `${limitPool.seal_rate}%` : "--", "muted"),
      );
      card.title = `涨停 ${limitPool.limit_up} · 跌停 ${limitPool.limit_down} · 封板率 ${limitPool.seal_rate ?? "--"}%（涨停/(涨停+跌停)）`;
    } else {
      numberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
      card.title = `涨跌停暂不可用：${limitPool.error || "未知原因"}`;
    }
    const subLine = div("leading-sub muted");
    subLine.textContent = limitPool.available ? "封板率高=赚钱效应强" : "暂不可用";
    card.append(nameLine, numberLine, subLine);
    appendLeadingSparkline(card, "limit_up");
    els.leadingGrid.append(card);
  }

  // ---- CFFEX 股指期货基差 IF/IH/IC/IM ----
  const BASIS_SPARK_KEYS = ["basis_IF", "basis_IH", "basis_IC", "basis_IM"];
  const basisStale = Boolean(basis.stale);
  if (basis.available && Array.isArray(basis.rows)) {
    for (let bi = 0; bi < basis.rows.length; bi++) {
      const row = asObject(basis.rows[bi]);
      const card = div("market-item leading-item");
      const nameLine = div("market-name");
      nameLine.append(
        span(String(row.name || "基差")),
        span(row.contango ? "升水" : (isFiniteNumber(row.basis) ? "贴水" : ""), "muted"),
      );
      if (basisStale) nameLine.append(span("延迟", "muted"));
      const numberLine = div("market-number-line");
      if (isFiniteNumber(row.basis)) {
        numberLine.append(
          span(`${row.basis >= 0 ? "+" : ""}${row.basis}`, directionClass(row.basis)),
          span(isFiniteNumber(row.basis_pct) ? `${row.basis_pct >= 0 ? "+" : ""}${row.basis_pct}%` : "--", "muted"),
        );
        card.title = `${row.name}：期货 ${row.futures_price} · 现货 ${row.spot_price} · 基差 ${row.basis}（${row.basis_pct}%）${row.contango ? "升水" : "贴水"}`;
      } else {
        numberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
        card.title = `${row.name} 基差暂不可用：${row.error || "未知原因"}`;
      }
      const subLine = div("leading-sub muted");
      if (!isFiniteNumber(row.basis)) {
        subLine.textContent = "暂不可用";
      } else if (basisStale) {
        subLine.textContent = "上游暂不可达，展示上次成功值";
      } else {
        subLine.textContent = row.contango ? "升水=看多情绪" : "贴水=看空情绪";
      }
      card.append(nameLine, numberLine, subLine);
      appendLeadingSparkline(card, BASIS_SPARK_KEYS[bi] || `basis_${bi}`);
      els.leadingGrid.append(card);
    }
  } else {
    const card = div("market-item leading-item");
    const nameLine = div("market-name");
    nameLine.append(span("股指基差"), span("IF/IH/IC/IM", "muted"));
    const numberLine = div("market-number-line");
    numberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
    const subLine = div("leading-sub muted");
    subLine.textContent = `暂不可用：${basis.error || "未知原因"}`;
    card.append(nameLine, numberLine, subLine);
    els.leadingGrid.append(card);
  }

  // ---- 货币市场利率 SHIBOR + 回购定盘 ----
  {
    const card = div("market-item leading-item");
    const nameLine = div("market-name");
    nameLine.append(span("货币市场"), span("SHIBOR/回购定盘", "muted"));
    if (moneyRates.stale) nameLine.append(span("延迟", "muted"));
    const numberLine = div("market-number-line");
    const subParts = [];
    if (moneyRates.available && moneyRates.rates) {
      const dr = moneyRates.rates.dr007;
      const on = moneyRates.rates.shibor_on;
      // 标签取后端给出的名称（FR007 回购定盘 / SHIBOR 隔夜），避免展示
      // 与实际数据源不符的简称。
      if (dr && isFiniteNumber(dr.value)) {
        numberLine.append(span(`${dr.value.toFixed(2)}%`, "market-price"), span(String(dr.name || "回购定盘"), "muted"));
      } else if (on && isFiniteNumber(on.value)) {
        numberLine.append(span(`${on.value.toFixed(2)}%`, "market-price"), span("隔夜", "muted"));
      } else {
        numberLine.append(span("--", "market-price"));
      }
      for (const key of ["shibor_on", "shibor_1w", "shibor_1y", "dr007"]) {
        const r = moneyRates.rates[key];
        if (r && isFiniteNumber(r.value)) subParts.push(`${r.name} ${r.value.toFixed(2)}%`);
      }
      card.title = subParts.join(" · ");
    } else {
      numberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
      card.title = `货币市场利率暂不可用：${moneyRates.error || "未知原因"}`;
    }
    const subLine = div("leading-sub muted");
    subLine.textContent = subParts.join(" · ") || (moneyRates.available ? "—" : "暂不可用");
    card.append(nameLine, numberLine, subLine);
    appendLeadingSparkline(card, "dr007");
    els.leadingGrid.append(card);
  }

  // ---- 5 档盘口近似多空挂单不平衡（300ETF） ----
  {
    const card = div("market-item leading-item");
    const nameLine = div("market-name");
    nameLine.append(span("盘口不平衡"), span("300ETF 近似", "muted"));
    if (imbalance.stale) nameLine.append(span("延迟", "muted"));
    const numberLine = div("market-number-line");
    if (imbalance.available && isFiniteNumber(imbalance.imbalance)) {
      numberLine.append(
        span(`${imbalance.imbalance >= 0 ? "+" : ""}${(imbalance.imbalance * 100).toFixed(1)}%`, directionClass(imbalance.imbalance)),
        span("IAI", "muted"),
      );
      card.title = `300ETF 5档盘口：买盘 ${imbalance.buy_total_wan}万 vs 卖盘 ${imbalance.sell_total_wan}万 · 不平衡度 ${imbalance.imbalance}（${imbalance.source}）`;
    } else {
      numberLine.append(span("--", "market-price"), span("暂不可用", "muted"));
      card.title = `盘口不平衡暂不可用：${imbalance.error || "未知原因"}`;
    }
    const subLine = div("leading-sub muted");
    if (!imbalance.available || !isFiniteNumber(imbalance.imbalance)) {
      subLine.textContent = "暂不可用（需付费 L2 才能全市场）";
    } else if (imbalance.stale) {
      subLine.textContent = `买 ${imbalance.buy_total_wan}万 / 卖 ${imbalance.sell_total_wan}万（上次成功值）`;
    } else {
      subLine.textContent = `买 ${imbalance.buy_total_wan}万 / 卖 ${imbalance.sell_total_wan}万`;
    }
    card.append(nameLine, numberLine, subLine);
    appendLeadingSparkline(card, "imbalance");
    els.leadingGrid.append(card);
  }
}

// 先行指标前端累积分时小图：横轴为时间（按最早/最晚点跨度线性映射），
// 纵轴为该指标值；点数不足 2 个时隐藏，2 个起连成折线。
function renderLeadingSparkline(svg, history) {
  svg.replaceChildren();
  const points = Array.isArray(history) ? history.filter((p) => isFiniteNumber(p.t) && isFiniteNumber(p.value)) : [];
  if (points.length < 2) {
    svg.setAttribute("hidden", "");
    return;
  }
  const w = 100;
  const h = 30;
  const pad = 2;
  const plotW = w - pad * 2;
  const plotH = h - pad * 2;
  const tMin = points[0].t;
  const tMax = points[points.length - 1].t;
  const tRange = Math.max(tMax - tMin, 1);
  const values = points.map((p) => p.value);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (!isFiniteNumber(lo) || !isFiniteNumber(hi)) {
    svg.setAttribute("hidden", "");
    return;
  }
  const range = hi - lo || Math.max(Math.abs(hi) * 0.001, 0.0001);
  const padded = { min: lo - range * 0.1, max: hi + range * 0.1 };
  const x = (t) => pad + (t - tMin) / tRange * plotW;
  const y = (val) => pad + (padded.max - val) / (padded.max - padded.min) * plotH;
  const latest = points[points.length - 1];
  const first = points[0];
  const direction = directionClass(latest.value - first.value);

  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.removeAttribute("hidden");

  // 2 个点起即画折线（首点画小圆，连接线 + 末点大圆），让分时曲线尽早可见。
  svg.append(svgNode("circle", {
    cx: x(first.t).toFixed(2), cy: y(first.value).toFixed(2), r: 1.2,
    class: `stock-intraday-dot ${direction}`,
  }));
  svg.append(svgNode("path", {
    d: points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(2)},${y(p.value).toFixed(2)}`).join(" "),
    class: `stock-intraday-line ${direction}`,
    "stroke-width": "1", fill: "none",
    "vector-effect": "non-scaling-stroke",
  }));
  svg.append(svgNode("circle", {
    cx: x(latest.t).toFixed(2), cy: y(latest.value).toFixed(2), r: 1.5,
    class: `stock-intraday-dot ${direction}`,
  }));
  const title = svgNode("title");
  const totalMinutes = Math.max(0, Math.round((tMax - tMin) / 60_000));
  const spanText = totalMinutes >= 60 ? `${Math.floor(totalMinutes / 60)}小时${totalMinutes % 60}分` : `${totalMinutes}分`;
  const fmt = (v) => Math.abs(v) >= 1000 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(3);
  title.textContent = `近${points.length}点（${spanText}）· 最低 ${fmt(lo)} 最高 ${fmt(hi)} · 最新 ${fmt(latest.value)}`;
  svg.append(title);
}

// 在先行指标卡片末尾追加一个分时小图 SVG，绑定到 state.leadingHistory[key]。
// history 点位不足时 SVG 隐藏，等点位累积到 2 个起画线。
function appendLeadingSparkline(card, key) {
  const spark = svgNode("svg", { class: "market-spark leading-spark" });
  spark.dataset.key = key;
  spark.setAttribute("hidden", "");
  renderLeadingSparkline(spark, state.leadingHistory[key] || []);
  card.append(spark);
}

function renderBreadth() {
  els.breadthGrid.replaceChildren();
  if (!state.breadth.length) {
    els.breadthGrid.append(span("暂无市场广度数据", "muted"));
    return;
  }
  for (const row of state.breadth.slice(0, 3)) {
    const item = div("breadth-item");
    const stats = span("", "breadth-stats");
    appendTextParts(stats, [
      [numberOrDash(row.up), "up"],
      [" / ", "muted"],
      [numberOrDash(row.flat), "flat"],
      [" / ", "muted"],
      [numberOrDash(row.down), "down"],
    ]);
    const label = row.turnover != null ? `${row.name} ${formatYi(row.turnover)}` : row.name;
    item.append(span(label), stats);
    els.breadthGrid.append(item);
  }
}

function renderGlobals() {
  const focusedChartSymbol = document.activeElement?.matches?.(".stock-chart-trigger")
    ? normalizeChartSymbol(document.activeElement.dataset.symbol)
    : "";
  let replacementFocus = null;
  els.globalGrid.replaceChildren();
  if (!state.globals.length) {
    els.globalGrid.append(span("暂无全球参考数据", "muted"));
    return;
  }
  // 纽约原油/黄金（hf_CL/hf_GC）已并入隔夜全球标的的商品组，全球参考不再重复展示；
  // 旧行情缓存仍会带出这两行，渲染时过滤。
  for (const market of state.globals.filter((row) => row.symbol !== "hf_CL" && row.symbol !== "hf_GC").slice(0, 60)) {
    const symbol = normalizeChartSymbol(market.symbol);
    const chartable = symbol && GLOBAL_CHART_SYMBOLS.has(symbol);
    const quoteTitle = market.quoteTime ? `报价 ${formatDateTime(market.quoteTime)}${market.isStale ? " · 延迟" : ""}` : "";
    const value = span(`${formatPrice(market.price)}  ${formatPct(market.changePct)}`, directionClass(market.changePct));
    // 全球参考中的海外指数/个股均已配置图表数据源（美股/港股走腾讯，韩股走 Naver，欧股/台湾走东财，ES/NQ 期货走新浪）；
    // 无分时源的品种弹层里分时面板显示空态。
    const item = chartable ? document.createElement("button") : div("global-item");
    if (chartable) {
      item.type = "button";
      item.className = "global-item stock-chart-trigger";
      item.dataset.symbol = symbol;
      item.setAttribute("aria-controls", "stock-chart-popover");
      item.setAttribute("aria-haspopup", "dialog");
      item.setAttribute("aria-expanded", state.stockChartSymbol === symbol && !els.stockChartPopover.hidden ? "true" : "false");
      item.setAttribute("aria-label", `查看${market.name || symbol}的日K线和当日分时图`);
      item.title = `查看${market.name || symbol}K线与分时图${quoteTitle ? ` · ${quoteTitle}` : ""}`;
      if (focusedChartSymbol === symbol) replacementFocus = item;
      if (state.stockChartSymbol === symbol && !els.stockChartPopover.hidden) {
        state.stockChartAnchor = item;
      }
    } else if (quoteTitle) {
      item.title = quoteTitle;
    }
    item.append(span(market.name || market.symbol || "--"), value);
    els.globalGrid.append(item);
  }
  if (replacementFocus) replacementFocus.focus({ preventScroll: true });
}

function renderOvernight() {
  const focusedChartSymbol = document.activeElement?.matches?.(".stock-chart-trigger")
    ? normalizeChartSymbol(document.activeElement.dataset.symbol)
    : "";
  let replacementFocus = null;
  els.overnightGrid.replaceChildren();
  if (!state.overnight.length) {
    els.overnightGrid.append(span("暂无隔夜全球标的数据", "muted"));
    return;
  }

  // 只保留与持仓相关度高（|同日相关|≥0.15）的隔夜标的；
  // 相关度快照见 OVERNIGHT_PORTFOLIO_CORRELATION，其余隔夜品种不再展示，
  // 以聚焦对持仓有解释力/对冲意义的标的。
  const correlatedOvernight = state.overnight.filter((market) => {
    const symbol = normalizeChartSymbol(market.symbol);
    const correlation = OVERNIGHT_PORTFOLIO_CORRELATION[symbol];
    return correlation && Math.abs(correlation.same) >= 0.15;
  });
  if (!correlatedOvernight.length) {
    els.overnightGrid.append(span("暂无与持仓高相关的隔夜标的数据", "muted"));
    return;
  }

  // 所有保留的标的横向排成一行，节约纵向高度。
  const grid = div("global-grid overnight-grid overnight-grid-row");
  for (const market of correlatedOvernight) {
    const symbol = normalizeChartSymbol(market.symbol);
    const chartable = symbol && GLOBAL_CHART_SYMBOLS.has(symbol);
    const quoteTitle = market.quoteTime ? `报价 ${formatDateTime(market.quoteTime)}` : "";
    const correlation = OVERNIGHT_PORTFOLIO_CORRELATION[symbol];
    const highlighted = correlation && Math.abs(correlation.same) >= 0.15;
    const correlationTitle = correlation
      ? ` · 与持仓相关度 同日${correlation.same >= 0 ? "+" : ""}${correlation.same.toFixed(2)}/隔夜${correlation.next >= 0 ? "+" : ""}${correlation.next.toFixed(2)}${correlation.same < 0 ? "（负相关，对冲项）" : ""}`
      : "";
    const item = chartable ? document.createElement("button") : div("global-item overnight-item");
    if (chartable) {
      item.type = "button";
      item.className = "global-item overnight-item stock-chart-trigger";
      item.dataset.symbol = symbol;
      item.setAttribute("aria-controls", "stock-chart-popover");
      item.setAttribute("aria-haspopup", "dialog");
      item.setAttribute("aria-expanded", state.stockChartSymbol === symbol && !els.stockChartPopover.hidden ? "true" : "false");
      item.setAttribute("aria-label", `查看${market.name || symbol}的日K线和当日分时图`);
      item.title = `${market.name || symbol}${correlationTitle} · 查看K线与分时图${quoteTitle ? ` · ${quoteTitle}` : ""}`;
      if (highlighted) item.classList.add("is-correlated");
      if (focusedChartSymbol === symbol) replacementFocus = item;
      if (state.stockChartSymbol === symbol && !els.stockChartPopover.hidden) {
        state.stockChartAnchor = item;
      }
    } else if (quoteTitle) {
        item.title = quoteTitle;
      }
      const nameLine = div("overnight-name");
      nameLine.append(span((highlighted ? "★ " : "") + (market.name || market.symbol || "--")));
      const valueLine = div("overnight-value");
      valueLine.append(span(formatPrice(market.price)), span(formatPct(market.changePct), directionClass(market.changePct)));
      item.append(nameLine, valueLine);
      grid.append(item);
  }
  els.overnightGrid.append(grid);
  if (replacementFocus) replacementFocus.focus({ preventScroll: true });
}

function renderMarketNews() {
  const root = rootObject(state.marketNewsPayload);
  const items = state.marketNews;
  const status = String(pick(root, ["status"]) || "").toLowerCase();
  const stale = Boolean(root.stale) || status === "stale";
  const unavailable = root.ok === false && !items.length;
  const degraded = Boolean(root.degraded) || status === "degraded";
  const refreshing = Boolean(root.refreshing);
  const sourceStatus = asObject(root.source_status || root.sourceStatus);
  const sources = Object.entries(sourceStatus)
    .filter(([, value]) => value === true || asObject(value).ok === true)
    .map(([key]) => SOURCE_LABELS[key] || key)
    .slice(0, 4);
  const latestEvent = pick(root, ["latest_event_at", "latestEventAt"])
    || (items.length ? items[0].publishedAt : null);

  let stateLabel = "待获取";
  let stateClass = "is-loading";
  if (unavailable) {
    stateLabel = "不可用";
    stateClass = "is-error";
  } else if (stale) {
    stateLabel = "缓存";
    stateClass = "is-stale";
  } else if (degraded) {
    stateLabel = "部分源";
    stateClass = "is-degraded";
  } else if (items.length) {
    stateLabel = sources.length >= 4 ? "四源" : sources.length >= 3 ? "三源" : sources.length > 1 ? "双源" : "可用";
    stateClass = "is-ok";
  }
  if (refreshing && !items.length) stateLabel = "加载中";
  els.marketNewsState.className = `market-news-state ${stateClass}`;
  els.marketNewsState.textContent = stateLabel;

  // 持仓相关：标题/摘要提到持仓名称，或 item.symbols 命中持仓代码，
  // 或标题命中持仓所属板块（关联板块）关键词 → 置顶展示
  const positionNames = state.positions
    .map((position) => String(position.name || "").trim())
    .filter(Boolean);
  const positionSymbols = new Set(state.positions.map((position) => String(position.symbol || "").toUpperCase()).filter(Boolean));
  const sectorKeywordSet = new Set();
  for (const position of state.positions) {
    const industry = String(position.industry || "").trim();
    if (!industry) continue;
    for (const keyword of SECTOR_NEWS_KEYWORDS[industry] || []) sectorKeywordSet.add(keyword);
    for (const segment of industry.split(/[与、/]/)) {
      const token = segment.trim();
      if (token.length >= 2) sectorKeywordSet.add(token);
    }
  }
  const holdingMatch = (item) => {
    if (item.symbols.some((symbol) => positionSymbols.has(symbol))) return true;
    const title = item.title || "";
    if (positionNames.some((name) => name.length >= 2 && title.includes(name))) return true;
    for (const keyword of sectorKeywordSet) {
      if (keyword.length >= 2 && title.includes(keyword)) return true;
    }
    return false;
  };
  const relatedItems = items.filter(holdingMatch);
  const orderedItems = [...relatedItems, ...items.filter((item) => !holdingMatch(item))];

  const metaParts = [sources.join(" / ") || "财联社 / 东方财富 / Google 新闻 / 华尔街见闻", `${items.length} 条`];
  if (relatedItems.length) metaParts.push(`持仓相关 ${relatedItems.length} 条`);
  if (latestEvent) metaParts.push(`最新 ${formatDateTime(latestEvent)}`);
  if (refreshing && items.length) metaParts.push("更新中");
  els.marketNewsMeta.textContent = metaParts.join(" · ");

  const fingerprint = String(
    pick(root, ["content_fingerprint", "contentFingerprint"])
      || items.map((item) => item.id).join("|"),
  );
  if (fingerprint && fingerprint === state.renderedMarketNewsFingerprint && els.marketNewsList.childElementCount) {
    return;
  }
  const previousScroll = els.marketNewsList.scrollTop;
  els.marketNewsList.replaceChildren();
  if (!items.length) {
    const message = unavailable
      ? "市场快讯源暂不可用，等待后台重试"
      : (refreshing || !state.marketNewsReceivedAt ? "正在获取全市场快讯…" : "当前没有可展示的市场快讯");
    els.marketNewsList.append(emptyNode(message, "compact market-news-empty"));
    state.renderedMarketNewsFingerprint = fingerprint;
    return;
  }

  for (const item of orderedItems.slice(0, 24)) {
    const article = document.createElement("article");
    article.className = holdingMatch(item) ? "market-news-item is-holding-related" : "market-news-item";
    const title = safeLink(item.url, item.title || "无标题快讯", "market-news-item-title");
    const meta = div("market-news-item-meta");
    if (holdingMatch(item)) meta.append(span("持仓相关", "market-news-tag is-holding"));
    if (item.publishedAt) meta.append(span(formatDateTime(item.publishedAt), "market-news-time"));
    if (item.source) meta.append(span(item.source, "market-news-source"));
    for (const tag of item.riskTags.slice(0, 1)) {
      meta.append(span(MARKET_NEWS_TAG_LABELS[tag] || tag, "market-news-tag is-risk"));
    }
    for (const tag of item.opportunityTags.slice(0, 1)) {
      meta.append(span(MARKET_NEWS_TAG_LABELS[tag] || tag, "market-news-tag is-opportunity"));
    }
    article.append(title, meta);
    els.marketNewsList.append(article);
  }
  state.renderedMarketNewsFingerprint = fingerprint;
  els.marketNewsList.scrollTop = Math.min(previousScroll, els.marketNewsList.scrollHeight);
}

function accountSnapshotDisplay() {
  const decision = rootObject(state.decisionPayload);
  const decisionQuality = asObject(decision.data_quality || decision.dataQuality);
  const risk = rootObject(state.riskPayload);
  const riskQuality = asObject(risk.data_quality || risk.dataQuality);
  const accountQuality = asObject(riskQuality.account_snapshot || riskQuality.accountSnapshot);
  const quotePortfolio = asObject(rootObject(state.quotePayload).portfolio);
  const asOf = pick(asObject(decision.as_of || decision.asOf), ["account", "portfolio", "account_snapshot"])
    || pick(accountQuality, ["as_of", "date", "snapshot_date", "snapshotDate"])
    || pick(quotePortfolio, ["as_of", "asOf"]);
  const current = firstBoolean(
    pick(decisionQuality, ["account_current", "accountCurrent"]),
    pick(accountQuality, ["current_verified", "currentVerified"]),
    pick(accountQuality, ["verified"]),
  );
  const date = asOf ? formatDateOnly(asOf) : "日期待核验";
  const label = current === true
    ? `账户快照 ${date} · 当日已核验`
    : current === false
      ? `账户快照 ${date} · 未核验，非实时持仓`
      : `账户快照 ${date} · 时效待核验，非券商实时持仓`;
  return { asOf, current, date, label };
}

function renderSummary() {
  const positions = resolvedPositions();
  const live = positions.filter((item) => isFiniteNumber(item.price));
  const calculatedMarketValue = sum(live.map((item) => item.marketValue));
  const summary = extractPortfolioSummary(state.quotePayload);
  const marketValue = firstNumber(summary.market_value, summary.marketValue, summary.position_value, summary.positionValue, summary.securities_value, summary.securitiesValue, calculatedMarketValue);
  const dayPnl = summaryDailyPnl(summary, positions);
  const cash = firstNumber(summary.cash);
  const debt = firstNumber(summary.debt, summary.liability);
  const calculatedNetEquity = [marketValue, cash, debt].every(isFiniteNumber) ? marketValue + cash - debt : null;
  const netEquity = firstNumber(summary.net_equity, summary.netEquity, summary.equity, calculatedNetEquity);
  const exposure = firstNumber(summary.gross_exposure_pct, summary.grossExposurePct, summary.exposurePct, summary.positionRatioPct, isFiniteNumber(marketValue) && netEquity > 0 ? marketValue / netEquity * 100 : null);
  const configuredCount = firstNumber(summary.position_count, summary.positionCount, positions.length);
  const quoteCoverageCount = summaryQuoteCoverageCount(summary, positions);
  const quoteCoverage = `${quoteCoverageCount}/${isFiniteNumber(configuredCount) ? configuredCount : "--"}`;
  const account = accountSnapshotDisplay();
  const snapshotPrefix = account.current === true ? "" : "快照";

  els.summaryGrid.replaceChildren(
    summaryItem(`${snapshotPrefix}持仓市值估算`, formatAmount(marketValue), ""),
    summaryItem(`${snapshotPrefix}当日盈亏估算`, formatAmount(dayPnl, true), directionClass(dayPnl)),
    summaryItem(`${snapshotPrefix}净权益估算`, formatAmount(netEquity), ""),
    summaryItem("股票敞口", formatPct(exposure), exposure != null && exposure > 100 ? "up" : ""),
    summaryItem("报价覆盖", quoteCoverage, isFiniteNumber(configuredCount) && quoteCoverageCount === configuredCount ? "" : "flat"),
  );
  els.portfolioCoverage.textContent = `${account.label} · 实时价格 ${quoteCoverage}`;
}

function summaryDailyPnl(summary, positions) {
  // The server states its conclusion explicitly: a present-but-null daily_pnl
  // means "unknown", and must never be re-derived by summing rows (B03).
  if (hasOwnProperty(summary, "daily_pnl") || hasOwnProperty(summary, "day_pnl")
    || hasOwnProperty(summary, "dayPnl") || hasOwnProperty(summary, "dailyPnl")
    || hasOwnProperty(summary, "today_pnl") || hasOwnProperty(summary, "todayPnl")) {
    return firstNumber(summary.daily_pnl, summary.day_pnl, summary.dayPnl, summary.dailyPnl, summary.today_pnl, summary.todayPnl);
  }
  // Legacy contract without the field: fall back only when every expected row
  // is present, valid and carries its own day P&L.
  const configuredCount = firstNumber(summary.position_count, summary.positionCount, positions.length);
  const computable = positions.filter((item) => isFiniteNumber(item.dayPnl) && isFiniteNumber(item.price) && !item.isStale);
  if (isFiniteNumber(configuredCount) && computable.length === configuredCount && configuredCount > 0) {
    return sum(computable.map((item) => item.dayPnl));
  }
  return null;
}

function summaryQuoteCoverageCount(summary, positions) {
  const declared = firstNumber(
    pickNumber(summary, ["quote_coverage_count", "quoteCoverageCount"]),
  );
  if (isFiniteNumber(declared)) return declared;
  // Legacy contract: derive from rows, counting only live (non-snapshot,
  // non-stale) prices so snapshot fallbacks never inflate coverage (B03).
  return positions.filter((item) => isFiniteNumber(item.price) && !item.isStale).length;
}

function hasOwnProperty(object, key) {
  return Object.prototype.hasOwnProperty.call(asObject(object), key);
}

function renderIntradayPnl() {
  renderAggTSignal();
  const snapshot = state.intradayPnl;
  const selection = selectedIntradayPnlSeries(snapshot);
  const series = selection.series;
  const plottedSeries = projectIntradayPnlSeries(series);
  const phase = snapshot?.marketPhase || "";
  const accountFallback = accountSnapshotDisplay();
  const accountCurrent = snapshot?.account.current ?? accountFallback.current;
  const accountDate = snapshot?.account.asOf || accountFallback.asOf;
  const nonTrading = isNonTradingPhase(phase);
  const latest = series.length ? series[series.length - 1] : null;

  els.intradayPnlLatest.textContent = latest ? formatAmount(latest.pnl, true) : "--";
  els.intradayPnlLatest.className = `intraday-pnl-latest ${latest ? directionClass(latest.pnl) : "flat"}`;

  const meta = [];
  if (snapshot?.asOf) meta.push(formatDateOnly(snapshot.asOf));
  if (phase) meta.push(translatePhase(phase));
  if (series.length) meta.push(`${series.length} 点`);
  if (state.intradayPnlReceivedAt) meta.push(ageLabel(state.intradayPnlReceivedAt, state.intradayPnlBaseAgeSeconds));
  els.intradayPnlMeta.textContent = meta.join(" · ") || "等待盘中数据";

  const notes = [];
  if (accountCurrent === false) {
    notes.push(`账户快照${accountDate ? ` ${formatDateOnly(accountDate)}` : ""}已过期；曲线仅按快照数量估算，不代表当前持仓`);
  } else if (accountCurrent == null) {
    notes.push("账户快照时效待核验；曲线不等同券商结算盈亏");
  }
  if (snapshot?.stale) notes.push("当前展示缓存轨迹");
  if (state.intradayPnlError) notes.push(`轨迹更新失败：${state.intradayPnlError}`);
  notes.push("固定交易轴 09:30–11:30 / 13:00–15:00，午间休市不留空白；悬浮可查看点位");
  els.intradayPnlNote.textContent = notes.join(" · ");

  hideIntradayPnlTooltip();
  state.intradayPnlChartModel = null;
  if (!plottedSeries.length) {
    els.intradayPnlChart.setAttribute("hidden", "");
    els.intradayPnlEmpty.hidden = false;
    els.intradayPnlEmpty.textContent = series.length
      ? "固定交易时段内暂无可展示的盘中盈亏点位"
      : intradayPnlEmptyMessage(snapshot, selection, accountCurrent, nonTrading);
    return;
  }

  els.intradayPnlEmpty.hidden = true;
  els.intradayPnlChart.removeAttribute("hidden");
  drawIntradayPnlChart(plottedSeries, selection.label);
}

const AGG_T_PHASE_META = Object.freeze({
  no_data: { label: "无数据", cls: "is-idle" },
  pre_open: { label: "盘前等待", cls: "is-idle" },
  watching: { label: "监控中", cls: "is-watching" },
  triggered: { label: "⚠ 反T触发", cls: "is-triggered" },
  closed: { label: "已了结", cls: "is-closed" },
  expired: { label: "今日未触发", cls: "is-idle" },
  no_open: { label: "弃做", cls: "is-idle" },
  error: { label: "信号不可用", cls: "is-idle" },
});

function applyAggTDetailExpanded() {
  if (!els.aggTDetail || !els.aggTDetailToggle) return;
  els.aggTDetail.hidden = !state.aggTDetailExpanded;
  els.aggTDetailToggle.setAttribute("aria-expanded", state.aggTDetailExpanded ? "true" : "false");
  els.aggTDetailToggle.textContent = state.aggTDetailExpanded ? "策略明细 ▴" : "策略明细 ▾";
}

function renderAggTSignal() {
  if (!els.aggTSignal) return;
  const signal = state.intradayPnl?.aggTSignal;
  if (!signal || signal.ok === false || !signal.phase) {
    els.aggTSignal.hidden = true;
    if (els.aggTEmptyNote) els.aggTEmptyNote.hidden = false;
    return;
  }
  els.aggTSignal.hidden = false;
  if (els.aggTEmptyNote) els.aggTEmptyNote.hidden = true;
  const meta = AGG_T_PHASE_META[signal.phase] || { label: signal.phase, cls: "is-idle" };
  els.aggTSignalBadge.textContent = meta.label;
  els.aggTSignalBadge.className = `agg-t-signal-badge ${meta.cls}`;

  // 触发/已了结属于"当日有动作"，自动展开明细；其余状态默认折叠。
  // 用户一旦手动点过"策略明细"，之后尊重用户选择不再自动干预。
  if (!state.aggTDetailManual) {
    state.aggTDetailExpanded = signal.phase === "triggered" || signal.phase === "closed";
  }
  applyAggTDetailExpanded();

  const parts = [];
  if (isFiniteNumber(signal.open_bp)) parts.push(`开盘 ${signal.open_bp > 0 ? "+" : ""}${signal.open_bp}bp`);
  if (isFiniteNumber(signal.current_bp)) parts.push(`现价 ${signal.current_bp > 0 ? "+" : ""}${signal.current_bp}bp`);
  if (isFiniteNumber(signal.th_bp)) parts.push(`触发线 +${signal.th_bp}bp`);
  if (signal.phase === "triggered" && signal.entry) {
    parts.push(`卖出参考时点 ${formatIntradayTimeDetailed(signal.entry.time)}（${signal.entry.bp}bp）`);
  }
  if (signal.phase === "closed" && isFiniteNumber(signal.realized_bp)) {
    parts.push(
      `本笔 ${signal.realized_bp > 0 ? "+" : ""}${signal.realized_bp}bp${
        signal.completed ? "（回归了结）" : "（收盘平仓）"
      }${isFiniteNumber(signal.est_cny) ? ` ≈ ${formatAmount(signal.est_cny, true)}` : ""}`,
    );
  }
  if (signal.reason) parts.push(signal.reason);
  els.aggTSignalText.textContent = parts.join(" · ") || "…";

  const orders = signal.orders;
  const hasOrders = orders && Array.isArray(orders.rows) && orders.rows.length > 0;
  els.aggTOrdersToggle.hidden = !hasOrders;
  els.aggTOrdersToggle.textContent = hasOrders
    ? `委托清单（${signal.order_action === "sell" ? "卖出" : "买入"} ${orders.count} 只 ≈ ${formatAmount(orders.total_notional)}）▾`
    : "委托清单 ▾";
  if (hasOrders) {
    const fragment = document.createDocumentFragment();
    for (const row of orders.rows) {
      const tr = document.createElement("tr");
      for (const [key, value] of [
        ["code", row.symbol],
        ["name", row.name],
        ["action", null],
        ["shares", String(row.shares)],
        ["price", formatPrice(row.price)],
        ["notional", formatAmount(row.notional)],
      ]) {
        const td = document.createElement("td");
        if (key === "action") {
          td.textContent = row.action_label;
        } else {
          td.textContent = value ?? "--";
          if (["shares", "price", "notional"].includes(key)) td.className = "numeric";
        if (key === "action") td.className = `agg-t-order-action ${row.action === "sell" ? "down" : "up"}`;
        }
        tr.append(td);
      }
      fragment.append(tr);
    }
    els.aggTOrdersBody.replaceChildren(fragment);
    els.aggTOrdersNote.textContent = `${orders.sizing} · ${signal.note || ""}`;
  }
}

/* ---------- 自动模拟炒股（独立模块，多策略并行，非实盘） ---------- */

async function refreshPaperT() {
  if (compareActive() || state.paperT.loading) return;
  state.paperT.loading = true;
  try {
    const payload = await fetchJson(portfolioQuery("/api/paper-t"), AUTO_PAPER_REQUEST_TIMEOUT_MS);
    if (!payload || typeof payload !== "object") throw new Error("自动模拟盘接口返回为空");
    state.paperT.today = payload;
    state.paperT.error = payload.ok === false ? String(payload.error || "自动模拟盘暂不可用") : "";
  } catch (error) {
    state.paperT.error = friendlyError(error, "自动模拟盘暂不可用");
  } finally {
    state.paperT.loading = false;
  }
  renderPaperT();
}

async function refreshPaperTHistory() {
  if (state.paperT.historyLoading) return;
  state.paperT.historyLoading = true;
  try {
    const payload = await fetchJson(portfolioQuery("/api/paper-t/history"), AUTO_PAPER_REQUEST_TIMEOUT_MS);
    if (!payload || typeof payload !== "object") throw new Error("策略对比接口返回为空");
    if (payload.ok === false) throw new Error(payload.error || "策略对比暂不可用");
    state.paperT.history = payload;
    state.paperT.historyError = "";
  } catch (error) {
    state.paperT.historyError = friendlyError(error, "策略对比暂不可用");
  } finally {
    state.paperT.historyLoading = false;
  }
  renderPaperTHistory();
}

async function voidPaperT(strategyId, strategyName) {
  if (state.paperT.busy) return;
  const reason = window.prompt(`撤销「${strategyName}」当日自动模拟会话——请填写原因（会记入台账）：`, "误操作");
  if (reason == null) return;
  const trimmed = reason.trim();
  if (!trimmed) {
    state.paperT.error = "撤销需填写原因";
    renderPaperT();
    return;
  }
  state.paperT.busy = true;
  renderPaperT();
  try {
    const payload = await postJson(portfolioQuery("/api/paper-t/void"), { strategy: strategyId, reason: trimmed });
    if (payload.ok === false) throw new Error(payload.error || "撤销失败");
    state.paperT.today = payload;
    state.paperT.error = "";
  } catch (error) {
    state.paperT.error = friendlyError(error, "撤销失败");
  } finally {
    state.paperT.busy = false;
    renderPaperT();
  }
  void refreshPaperTHistory();
}

function paperTStatusText(entry) {
  const session = asObject(entry.session);
  const leg = String(session.leg || "idle");
  const signal = asObject(entry.signal);
  if (leg === "error") return String(entry.note || "台账异常");
  if (leg === "voided") return `已撤销${session.void_reason ? `（${session.void_reason}）` : ""}`;
  if (leg === "done") return "已完成本笔模拟T";
  if (leg === "sold") {
    if (String(signal.kind) === "stock") {
      return `已卖出 ${firstNumber(session.sell_count) ?? 0} 只，其中 ${firstNumber(session.matched_count) ?? 0} 只已买回 · 等待各自买回信号`;
    }
    return `已自动卖出 ≈ ${formatAmount(session.sell_notional)}，等待买回信号`;
  }
  const parts = ["自动待命"];
  if (String(signal.kind) === "stock") {
    const triggered = Array.isArray(signal.triggered) ? signal.triggered : [];
    if (triggered.length) parts.push(`触发但未执行（新鲜度门槛）：${triggered.join("、")}`);
  } else {
    const meta = AGG_T_PHASE_META[signal.phase];
    if (meta) parts.push(`信号：${meta.label}`);
    if (signal.reason) parts.push(String(signal.reason));
  }
  return parts.join(" · ");
}

function paperTStrategyRow(entry) {
  const row = document.createElement("div");
  row.className = "paper-t-line";
  const badge = document.createElement("span");
  badge.className = "paper-t-badge";
  badge.textContent = String(entry.name || entry.id || "策略");
  const status = document.createElement("span");
  status.className = "paper-t-status";
  status.textContent = paperTStatusText(entry);
  row.append(badge, status);

  const session = asObject(entry.session);
  const leg = String(session.leg || "idle");
  const pnl = document.createElement("span");
  pnl.className = "paper-t-pnl";
  const realized = firstNumber(session.realized_cny);
  const matched = firstNumber(session.matched_cny);
  if (leg === "done" && isFiniteNumber(realized)) {
    const realizedBp = firstNumber(session.realized_bp);
    pnl.textContent =
      `模拟T盈亏 ${formatAmount(realized, true)}` +
      (isFiniteNumber(realizedBp) ? `（${realizedBp > 0 ? "+" : ""}${realizedBp}bp）` : "");
    pnl.classList.add(directionClass(realized));
  } else if (leg === "sold" && isFiniteNumber(matched)) {
    pnl.textContent = `已实现部分 ${formatAmount(matched, true)}`;
    pnl.classList.add(directionClass(matched));
  }
  row.append(pnl);

  const voidButton = document.createElement("button");
  voidButton.type = "button";
  voidButton.className = "plain-button paper-t-void";
  voidButton.textContent = "撤销模拟";
  voidButton.hidden = !["sold", "idle"].includes(leg) || !session.exists;
  voidButton.disabled = state.paperT.busy;
  voidButton.addEventListener("click", () => void voidPaperT(String(entry.id), String(entry.name || entry.id)));
  row.append(voidButton);
  return row;
}

function renderPaperT() {
  if (!els.paperTLines) return;
  const payload = state.paperT.today;
  els.paperTLines.replaceChildren();
  if (!payload || payload.ok === false) {
    if (els.paperTNote) {
      els.paperTNote.hidden = !state.paperT.error;
      els.paperTNote.textContent = state.paperT.error ? `自动模拟盘不可用：${state.paperT.error}` : "";
    }
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const entry of Array.isArray(payload.strategies) ? payload.strategies : []) {
    fragment.append(paperTStrategyRow(entry));
  }
  els.paperTLines.append(fragment);

  const notes = [];
  if (payload.enabled === false) notes.push("自动模拟执行当前未启用");
  notes.push(String(payload.sizing_note || ""));
  notes.push("自动模拟：无需人工下单，成交价=最新行情价（未含滑点）");
  notes.push(String(payload.fee_note || ""));
  notes.push(String(payload.disclaimer || ""));
  if (state.paperT.error) notes.push(`刷新失败：${state.paperT.error}`);
  if (els.paperTNote) {
    els.paperTNote.hidden = false;
    els.paperTNote.textContent = notes.filter(Boolean).join(" · ");
  }
}

function paperTCellText(cell) {
  if (!cell) return "--";
  if (cell.voided) return "已撤销";
  if (isFiniteNumber(firstNumber(cell.realized_cny))) {
    const bpText = isFiniteNumber(firstNumber(cell.realized_bp))
      ? `（${cell.realized_bp > 0 ? "+" : ""}${cell.realized_bp}bp）`
      : "";
    return `${formatAmount(cell.realized_cny, true)}${bpText}`;
  }
  if (String(cell.status) === "sold") return "待回补";
  if (String(cell.status) === "idle") return "未建仓";
  return String(cell.status || "--");
}

function renderPaperTHistory() {
  if (!els.paperTCompareHead || !els.paperTHistoryBody) return;
  const payload = state.paperT.history;
  const strategies = Array.isArray(payload?.strategies) ? payload.strategies : [];
  const dates = Array.isArray(payload?.dates) ? payload.dates : [];
  const hold = asObject(payload?.hold_baseline);
  const holdByDate = asObject(hold.by_date);

  els.paperTCompareHead.replaceChildren();
  const headRow = els.paperTCompareHead;
  for (const label of ["日期", ...strategies.map((s) => String(s.name || s.id)), "持有不动（基准）"]) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = label;
    headRow.append(th);
  }

  els.paperTHistoryBody.replaceChildren();
  if (!dates.length) {
    els.paperTHistoryEmpty.hidden = false;
    els.paperTHistoryStats.textContent = "0 个模拟交易日";
    els.paperTHistoryStats.className = "paper-t-history-stats flat";
    els.paperTHistoryMeta.textContent = state.paperT.historyError
      ? `读取失败：${state.paperT.historyError}`
      : "等待模拟盘数据";
  } else {
    els.paperTHistoryEmpty.hidden = true;
    for (const dateText of dates) {
      const tr = document.createElement("tr");
      const tdDate = document.createElement("td");
      tdDate.textContent = shortDateLabel(dateText);
      tr.append(tdDate);
      for (const strategy of strategies) {
        const td = document.createElement("td");
        td.className = "numeric";
        const cell = asObject(strategy.by_date)[dateText];
        const cellText = paperTCellText(cell);
        td.textContent = cellText;
        const realized = firstNumber(asObject(cell).realized_cny);
        if (isFiniteNumber(realized)) td.classList.add(directionClass(realized));
        tr.append(td);
      }
      const holdTd = document.createElement("td");
      holdTd.className = "numeric";
      const holdValue = firstNumber(holdByDate[dateText]);
      if (isFiniteNumber(holdValue)) {
        holdTd.textContent = formatAmount(holdValue, true);
        holdTd.classList.add(directionClass(holdValue));
      } else {
        holdTd.textContent = "--";
      }
      tr.append(holdTd);
      els.paperTHistoryBody.append(tr);
    }
    // 合计行：各策略 次数·胜负·累计·平均bp
    const totalsRow = document.createElement("tr");
    totalsRow.className = "paper-t-totals";
    const totalsHead = document.createElement("td");
    totalsHead.textContent = "合计";
    totalsRow.append(totalsHead);
    for (const strategy of strategies) {
      const td = document.createElement("td");
      td.className = "numeric";
      const totals = asObject(strategy.totals);
      const sessions = firstNumber(totals.sessions) ?? 0;
      if (sessions) {
        const avgBp = firstNumber(totals.avg_bp);
        td.textContent =
          `${sessions}次 ${firstNumber(totals.wins) ?? 0}胜${firstNumber(totals.losses) ?? 0}负 ` +
          `${formatAmount(totals.total_cny, true)}` +
          (isFiniteNumber(avgBp) ? ` 均${avgBp > 0 ? "+" : ""}${avgBp}bp` : "");
        if (firstNumber(totals.total_cny) > 0) td.classList.add("up");
        else if (firstNumber(totals.total_cny) < 0) td.classList.add("down");
      } else {
        td.textContent = "—";
      }
      totalsRow.append(td);
    }
    const holdTotalTd = document.createElement("td");
    holdTotalTd.className = "numeric";
    const holdTotal = firstNumber(hold.total);
    if (isFiniteNumber(holdTotal)) {
      holdTotalTd.textContent = formatAmount(holdTotal, true);
      holdTotalTd.classList.add(directionClass(holdTotal));
    } else {
      holdTotalTd.textContent = "--";
    }
    totalsRow.append(holdTotalTd);
    els.paperTHistoryBody.append(totalsRow);
    els.paperTHistoryStats.textContent = `${dates.length} 个模拟交易日`;
    els.paperTHistoryStats.className = "paper-t-history-stats flat";
    els.paperTHistoryMeta.textContent = state.paperT.historyError
      ? `读取失败：${state.paperT.historyError}`
      : "各策略独立模拟账户并行运行，运行越久对比越可信";
  }

  const notes = [String(payload?.fee_note || ""), String(payload?.disclaimer || "")];
  if (state.paperT.historyError) notes.push(`历史读取失败：${state.paperT.historyError}`);
  els.paperTHistoryNote.textContent = notes.filter(Boolean).join(" · ");
}

function selectedIntradayPnlSeries(snapshot) {
  if (!snapshot || state.selectedPnlSymbol === "total") {
    return { key: "total", label: "全部持仓合计", series: snapshot?.totalSeries || [], tPlan: null };
  }
  const position = snapshot.positions.find((item) => item.symbol === state.selectedPnlSymbol);
  return position
    ? { key: position.symbol, label: position.name || position.symbol, series: position.series, tPlan: position.tPlan || null }
    : { key: state.selectedPnlSymbol, label: state.selectedPnlSymbol, series: [], tPlan: null };
}

function intradayPnlEmptyMessage(snapshot, selection, accountCurrent, nonTrading) {
  if (state.intradayPnlLoading && !snapshot) return "正在获取盘中盈亏轨迹…";
  if (!snapshot && !state.intradayPnlError) return "正在获取盘中盈亏轨迹…";
  if (state.intradayPnlError) return `盘中盈亏暂不可用：${state.intradayPnlError}`;
  if (accountCurrent === false) {
    return selection.key === "total"
      ? "账户快照已过期，接口尚未返回可明确标注的合计盘中轨迹"
      : `${selection.label}：账户快照已过期，接口尚未返回该标的盘中轨迹`;
  }
  if (nonTrading) return `当前为${translatePhase(snapshot.marketPhase)}，尚无可展示的今日盘中盈亏轨迹`;
  if (snapshot?.refreshing) return "盘中盈亏轨迹正在生成…";
  if (snapshot?.message) return String(snapshot.message).slice(0, 160);
  return selection.key === "total"
    ? "接口尚未返回全部持仓的盘中盈亏点位"
    : `接口尚未返回${selection.label}的盘中盈亏点位`;
}

function intradayPnlAxisPosition(value) {
  const clockMs = intradayClockMilliseconds(value);
  if (!isFiniteNumber(clockMs)) return null;
  let elapsedMs = 0;
  for (let sessionIndex = 0; sessionIndex < INTRADAY_PNL_SESSIONS.length; sessionIndex += 1) {
    const session = INTRADAY_PNL_SESSIONS[sessionIndex];
    if (clockMs >= session.startMs && clockMs <= session.endMs) {
      return {
        clockMs,
        plotTimeValue: elapsedMs + clockMs - session.startMs,
        sessionIndex,
      };
    }
    elapsedMs += session.endMs - session.startMs;
  }
  return null;
}

function intradayClockMilliseconds(value) {
  const raw = String(value ?? "").trim();
  const matched = raw.match(/^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?/);
  if (matched) {
    const hour = Number(matched[1]);
    const minute = Number(matched[2]);
    const second = Number(matched[3] || 0);
    const millisecond = Number(String(matched[4] || "0").padEnd(3, "0"));
    if (hour > 23 || minute > 59 || second > 59) return null;
    return ((hour * 60 + minute) * 60 + second) * 1000 + millisecond;
  }
  const parsed = parseDate(value);
  if (!parsed) return null;
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

function projectIntradayPnlSeries(series) {
  const projected = [];
  for (const point of series || []) {
    const position = intradayPnlAxisPosition(point.time);
    if (!position) continue;
    projected.push({ ...point, ...position });
  }
  return projected;
}

function drawIntradayPnlChart(series, label) {
  const svg = els.intradayPnlChart;
  const width = 960;
  const height = 132;
  const padding = { top: 11, right: 15, bottom: 21, left: 61 };
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
    const margin = (valueMax - valueMin) * 0.07;
    valueMin -= margin;
    valueMax += margin;
  }
  const x = (value) => padding.left + value / INTRADAY_PNL_AXIS_DURATION_MS * plotWidth;
  const y = (value) => padding.top + (valueMax - value) / (valueMax - valueMin) * plotHeight;

  svg.replaceChildren();
  const title = svgNode("title");
  const latest = series[series.length - 1];
  title.textContent = `${label}盘中当日盈亏，${series.length}个点，最新${formatAmount(latest.pnl, true)}`;
  svg.append(title);

  svg.append(svgNode("rect", {
    x: padding.left,
    y: padding.top,
    width: plotWidth,
    height: plotHeight,
    class: "pnl-hover-capture",
  }));

  const gridValues = [...new Set([valueMax, 0, valueMin].map((value) => Number(value.toFixed(8))))];
  for (const value of gridValues) {
    const line = svgNode("line", {
      x1: padding.left,
      x2: width - padding.right,
      y1: y(value),
      y2: y(value),
      class: value === 0 ? "pnl-zero-line" : "pnl-grid-line",
    });
    const axisLabel = svgNode("text", { x: padding.left - 7, y: y(value) + 3, class: "pnl-axis-label", "text-anchor": "end" });
    axisLabel.textContent = formatChartAmount(value);
    svg.append(line, axisLabel);
  }

  const sessionJoinX = x(INTRADAY_PNL_SESSIONS[0].endMs - INTRADAY_PNL_SESSIONS[0].startMs);
  svg.append(svgNode("line", {
    x1: sessionJoinX,
    x2: sessionJoinX,
    y1: padding.top,
    y2: height - padding.bottom,
    class: "pnl-session-join",
  }));

  const segments = splitPnlSeriesAtGaps(series);
  for (const segment of segments) {
    if (segment.length === 1) {
      svg.append(svgNode("circle", { cx: x(segment[0].plotTimeValue), cy: y(segment[0].pnl), r: 2.2, class: "pnl-line-dot" }));
      continue;
    }
    const path = svgNode("path", {
      d: segment.map((point, index) => `${index ? "L" : "M"}${x(point.plotTimeValue).toFixed(2)},${y(point.pnl).toFixed(2)}`).join(" "),
      class: "pnl-line-path",
    });
    svg.append(path);
  }

  const latestDot = svgNode("circle", { cx: x(latest.plotTimeValue), cy: y(latest.pnl), r: 3, class: `pnl-latest-dot ${directionClass(latest.pnl)}` });
  const latestTitle = svgNode("title");
  latestTitle.textContent = `${formatIntradayTimeDetailed(latest.time)} ${formatAmount(latest.pnl, true)}`;
  latestDot.append(latestTitle);
  svg.append(latestDot);

  const timeTicks = [
    { value: 0, label: "09:30", anchor: "start" },
    { value: 60 * 60_000, label: "10:30", anchor: "middle" },
    { value: 2 * 60 * 60_000, label: "11:30 / 13:00", anchor: "middle" },
    { value: 3 * 60 * 60_000, label: "14:00", anchor: "middle" },
    { value: INTRADAY_PNL_AXIS_DURATION_MS, label: "15:00", anchor: "end" },
  ];
  for (const tick of timeTicks) {
    const tickLabel = svgNode("text", {
      x: x(tick.value),
      y: height - 5,
      class: "pnl-axis-label pnl-time-axis-label",
      "text-anchor": tick.anchor,
      "data-time-label": tick.label,
    });
    tickLabel.textContent = tick.label;
    svg.append(tickLabel);
  }

  const hoverLine = svgNode("line", {
    y1: padding.top,
    y2: height - padding.bottom,
    class: "pnl-hover-line",
    visibility: "hidden",
  });
  const hoverDot = svgNode("circle", {
    r: 3.5,
    class: "pnl-hover-dot",
    visibility: "hidden",
  });
  svg.append(hoverLine, hoverDot);
  state.intradayPnlChartModel = {
    width,
    height,
    padding,
    points: series.map((point) => ({
      ...point,
      x: x(point.plotTimeValue),
      y: y(point.pnl),
    })),
    activeIndex: -1,
    hoverLine,
    hoverDot,
  };
  svg.setAttribute("aria-label", `${label}盘中当日盈亏走势，${series.length}个数据点，最新${formatAmount(latest.pnl, true)}`);
}

function splitPnlSeriesAtGaps(series) {
  if (series.length < 2) return series.length ? [series] : [];
  const coordinate = (point) => isFiniteNumber(point.plotTimeValue) ? point.plotTimeValue : point.timeValue;
  const gaps = series.slice(1).map((point, index) => coordinate(point) - coordinate(series[index])).filter((value) => value > 0).sort((a, b) => a - b);
  const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 60_000;
  const threshold = Math.min(30 * 60_000, Math.max(5 * 60_000, median * 6));
  const segments = [[series[0]]];
  for (let index = 1; index < series.length; index += 1) {
    const previous = series[index - 1];
    const current = series[index];
    if (coordinate(current) - coordinate(previous) > threshold) segments.push([]);
    segments[segments.length - 1].push(series[index]);
  }
  return segments;
}

function handleIntradayPnlPointer(event) {
  const model = state.intradayPnlChartModel;
  if (!model?.points?.length) return;
  const rect = els.intradayPnlChart.getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return;
  const chartX = (event.clientX - rect.left) / rect.width * model.width;
  const chartY = (event.clientY - rect.top) / rect.height * model.height;
  const { left, right, top, bottom } = model.padding;
  if (chartX < left || chartX > model.width - right || chartY < top || chartY > model.height - bottom) {
    hideIntradayPnlTooltip();
    return;
  }
  const index = nearestIntradayPnlPoint(model.points, chartX, chartY);
  if (index >= 0) showIntradayPnlTooltip(index);
}

function handleIntradayPnlKeyboard(event) {
  const model = state.intradayPnlChartModel;
  const points = model?.points || [];
  if (event.key === "Escape") {
    hideIntradayPnlTooltip();
    return;
  }
  if (!points.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  let index = model.activeIndex;
  if (event.key === "Home") index = 0;
  else if (event.key === "End") index = points.length - 1;
  else if (event.key === "ArrowLeft") index = index < 0 ? points.length - 1 : Math.max(0, index - 1);
  else index = index < 0 ? 0 : Math.min(points.length - 1, index + 1);
  showIntradayPnlTooltip(index);
}

function nearestIntradayPnlPoint(points, chartX, chartY) {
  let bestIndex = -1;
  let bestXDistance = Infinity;
  let bestYDistance = Infinity;
  for (let index = 0; index < points.length; index += 1) {
    const point = points[index];
    const xDistance = Math.abs(point.x - chartX);
    const yDistance = Math.abs(point.y - chartY);
    if (xDistance < bestXDistance - 0.01
        || (Math.abs(xDistance - bestXDistance) <= 0.01 && yDistance < bestYDistance)) {
      bestIndex = index;
      bestXDistance = xDistance;
      bestYDistance = yDistance;
    }
  }
  return bestIndex;
}

function showIntradayPnlTooltip(index) {
  const model = state.intradayPnlChartModel;
  const point = model?.points?.[index];
  if (!point || !els.intradayPnlTooltip) return;
  model.activeIndex = index;
  model.hoverLine.setAttribute("x1", point.x);
  model.hoverLine.setAttribute("x2", point.x);
  model.hoverLine.removeAttribute("visibility");
  model.hoverDot.setAttribute("cx", point.x);
  model.hoverDot.setAttribute("cy", point.y);
  model.hoverDot.removeAttribute("visibility");

  els.intradayPnlTooltipTime.textContent = formatIntradayTimeDetailed(point.time);
  els.intradayPnlTooltipValue.textContent = `盈亏 ${formatAmount(point.pnl, true)}`;
  els.intradayPnlTooltipValue.className = `intraday-pnl-tooltip-value ${directionClass(point.pnl)}`;
  els.intradayPnlTooltip.hidden = false;

  const svgRect = els.intradayPnlChart.getBoundingClientRect();
  const wrapRect = els.intradayPnlChartWrap.getBoundingClientRect();
  const rawLeft = svgRect.left - wrapRect.left + point.x / model.width * svgRect.width;
  const rawTop = svgRect.top - wrapRect.top + point.y / model.height * svgRect.height - 7;
  const horizontalMargin = Math.min(72, wrapRect.width / 2);
  const left = Math.min(Math.max(rawLeft, horizontalMargin), Math.max(horizontalMargin, wrapRect.width - horizontalMargin));
  els.intradayPnlTooltip.style.left = `${left}px`;
  els.intradayPnlTooltip.style.top = `${Math.max(27, rawTop)}px`;
}

function hideIntradayPnlTooltip() {
  if (els.intradayPnlTooltip) els.intradayPnlTooltip.hidden = true;
  const model = state.intradayPnlChartModel;
  if (!model) return;
  model.activeIndex = -1;
  model.hoverLine?.setAttribute("visibility", "hidden");
  model.hoverDot?.setAttribute("visibility", "hidden");
}

/* ---------- 历史分时回看：连续 n 个交易日的盘中盈亏拼接图 ---------- */

function readStoredIntradayHistoryDays() {
  try {
    const value = Number(window.localStorage.getItem(INTRADAY_HISTORY_DAYS_STORAGE_KEY));
    return INTRADAY_HISTORY_DAY_OPTIONS.includes(value) ? value : INTRADAY_HISTORY_DEFAULT_DAYS;
  } catch (_) {
    return INTRADAY_HISTORY_DEFAULT_DAYS;
  }
}

function cnISODateNow() {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date());
  } catch (_) {
    return new Date().toISOString().slice(0, 10);
  }
}

function intradayHistoryToday() {
  const asOf = String(state.intradayPnl?.asOf || "");
  if (/^\d{4}-\d{2}-\d{2}/.test(asOf)) return asOf.slice(0, 10);
  return cnISODateNow();
}

function intradayHistoryCacheKey(date) {
  return `${state.activePortfolioId}|${date}`;
}

function intradayHistoryCacheSet(date, entry) {
  const cache = state.intradayHistory.cache;
  cache.set(intradayHistoryCacheKey(date), entry);
  while (cache.size > INTRADAY_HISTORY_CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    cache.delete(oldest);
  }
  // 只持久化已完结交易日（今天的数据盘中会持续变化，不写缓存）
  if (date < intradayHistoryToday()) void intradayHistoryIdbPut(intradayHistoryCacheKey(date), entry);
}

/* 历史分时缓存持久化：单日原始数据近 1MB，localStorage 5MB 容量不够，
 * 改用 IndexedDB（容量百 MB 级）。所有失败静默降级为"下次重新拉取"。 */
let intradayHistoryIdbPromise = null;

function intradayHistoryIdb() {
  if (!("indexedDB" in window)) return Promise.resolve(null);
  if (!intradayHistoryIdbPromise) {
    intradayHistoryIdbPromise = new Promise((resolve) => {
      const req = window.indexedDB.open(INTRADAY_HISTORY_IDB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(INTRADAY_HISTORY_IDB_STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    });
  }
  return intradayHistoryIdbPromise;
}

async function intradayHistoryIdbPut(key, entry) {
  const db = await intradayHistoryIdb();
  if (!db) return;
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(INTRADAY_HISTORY_IDB_STORE, "readwrite");
      tx.objectStore(INTRADAY_HISTORY_IDB_STORE).put(entry, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
    } catch (_) { resolve(); }
  });
}

async function intradayHistoryIdbClear() {
  const db = await intradayHistoryIdb();
  if (!db) return;
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(INTRADAY_HISTORY_IDB_STORE, "readwrite");
      tx.objectStore(INTRADAY_HISTORY_IDB_STORE).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
    } catch (_) { resolve(); }
  });
}

// 页面重开 / 切换组合后，从 IndexedDB 恢复历史日缓存，避免重复拉取。
async function restoreIntradayHistoryCache() {
  if (state.intradayHistory.cacheRestored) return;
  state.intradayHistory.cacheRestored = true;
  // 清理早期版本写入 localStorage 的缓存（容量太小已弃用）
  try { window.localStorage.removeItem(INTRADAY_HISTORY_CACHE_STORAGE_KEY); } catch (_) { /* ignore */ }
  const db = await intradayHistoryIdb();
  if (!db) return;
  const entries = await new Promise((resolve) => {
    try {
      const tx = db.transaction(INTRADAY_HISTORY_IDB_STORE, "readonly");
      const store = tx.objectStore(INTRADAY_HISTORY_IDB_STORE);
      const reqValues = store.getAll();
      const reqKeys = store.getAllKeys();
      let values = null;
      let keys = null;
      const done = () => {
        if (values === null || keys === null) return;
        resolve(keys.map((key, i) => [String(key), values[i]]));
      };
      reqValues.onsuccess = () => { values = reqValues.result || []; done(); };
      reqKeys.onsuccess = () => { keys = reqKeys.result || []; done(); };
      tx.onerror = () => resolve([]);
      tx.onabort = () => resolve([]);
    } catch (_) { resolve([]); }
  });
  const today = intradayHistoryToday();
  for (const [key, entry] of entries) {
    const date = key.split("|")[1];
    // 过期口径：今日及以后的条目一律不恢复
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(date)) || date >= today) continue;
    if (!entry || typeof entry !== "object" || !entry.snapshot) continue;
    if (!state.intradayHistory.cache.has(key)) {
      state.intradayHistory.cache.set(key, entry);
    }
  }
}

function intradayHistoryDateRange() {
  const today = intradayHistoryToday();
  const union = [...new Set([...state.intradayHistory.dates, today])]
    .filter((value) => /^\d{4}-\d{2}-\d{2}$/.test(value))
    .sort();
  if (!union.length) return [];
  const end = state.intradayHistory.endDate && union.includes(state.intradayHistory.endDate)
    ? state.intradayHistory.endDate
    : union[union.length - 1];
  const endIndex = union.indexOf(end);
  const startIndex = Math.max(0, endIndex - state.intradayHistory.days + 1);
  return union.slice(startIndex, endIndex + 1);
}

function syncIntradayHistoryEndOptions() {
  if (!els.intradayHistoryEnd) return;
  const dates = [...new Set([...state.intradayHistory.dates, intradayHistoryToday()])]
    .filter((value) => /^\d{4}-\d{2}-\d{2}$/.test(value))
    .sort()
    .reverse();
  const structureKey = dates.join("|");
  if (structureKey === state.intradayHistory.endOptionsKey) {
    els.intradayHistoryEnd.value = state.intradayHistory.endDate;
    return;
  }
  state.intradayHistory.endOptionsKey = structureKey;
  const fragment = document.createDocumentFragment();
  const latest = document.createElement("option");
  latest.value = "";
  latest.textContent = "最近交易日";
  fragment.append(latest);
  for (const date of dates) {
    const option = document.createElement("option");
    option.value = date;
    option.textContent = `${date} ${weekdayLabelCN(date)}`;
    fragment.append(option);
  }
  els.intradayHistoryEnd.replaceChildren(fragment);
  // 记忆的截止日已超出保留期时回退到"最近交易日"。
  if (state.intradayHistory.endDate && !dates.includes(state.intradayHistory.endDate)) {
    state.intradayHistory.endDate = "";
  }
  els.intradayHistoryEnd.value = state.intradayHistory.endDate;
}

function weekdayLabelCN(value) {
  const matched = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!matched) return "";
  const parsed = new Date(Date.UTC(Number(matched[1]), Number(matched[2]) - 1, Number(matched[3])));
  if (Number.isNaN(parsed.getTime())) return "";
  return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][parsed.getUTCDay()] || "";
}

async function refreshIntradayHistoryDates() {
  if (compareActive()) return;
  try {
    const payload = await fetchJson(portfolioQuery("/api/intraday-pnl/dates"), REQUEST_TIMEOUT_MS);
    if (!payload || payload.ok === false) throw new Error(payload?.error || "历史台账日期暂不可用");
    state.intradayHistory.dates = Array.isArray(payload.dates)
      ? payload.dates.filter((value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value)))
      : [];
    state.intradayHistory.retentionDays = firstNumber(pickNumber(payload, ["retention_days"]), 0);
    state.intradayHistory.datesError = "";
  } catch (error) {
    state.intradayHistory.datesError = friendlyError(error, "历史台账日期暂不可用");
  } finally {
    state.intradayHistory.datesLoaded = true;
    syncIntradayHistoryEndOptions();
    void refreshIntradayHistory();
  }
}

// "今天"一段不单独请求：直接复用 15 秒实时轮询的 state.intradayPnl。
function intradayHistoryEntryFor(date) {
  if (date === intradayHistoryToday()) {
    if (state.intradayPnl && state.intradayPnl.ok !== false) {
      return { snapshot: state.intradayPnl, live: true };
    }
    return null;
  }
  return state.intradayHistory.cache.get(intradayHistoryCacheKey(date)) || null;
}

async function refreshIntradayHistory() {
  if (compareActive()) return;
  // 防重复：已有一轮拉取在进行时只登记待复查，结束后自动补一轮。
  if (state.intradayHistory.loading) {
    state.intradayHistory.pendingRecheck = true;
    return;
  }
  await restoreIntradayHistoryCache();
  const dates = intradayHistoryDateRange();
  const pending = dates.filter((date) => {
    if (date === intradayHistoryToday()) return false;
    return !state.intradayHistory.cache.has(intradayHistoryCacheKey(date));
  });
  state.intradayHistory.dayErrors = {};
  for (const date of dates) delete state.intradayHistory.dayErrors[date];
  if (!pending.length) {
    renderIntradayHistory();
    return;
  }
  const token = ++state.intradayHistory.token;
  state.intradayHistory.loading = true;
  renderIntradayHistory();
  await Promise.allSettled(pending.map(async (date) => {
    try {
      const payload = await fetchJson(
        portfolioQuery(`/api/intraday-pnl/history?date=${encodeURIComponent(date)}`),
        INTRADAY_HISTORY_REQUEST_TIMEOUT_MS,
      );
      if (token !== state.intradayHistory.token) return;
      if (!payload || payload.ok === false) {
        throw new Error(String(payload?.error || payload?.message || "该日盘中轨迹不可用"));
      }
      intradayHistoryCacheSet(date, {
        snapshot: normalizeIntradayPnl(payload),
        backfill: payload.backfill && typeof payload.backfill === "object" ? payload.backfill : null,
        holdingsSource: payload.holdings_source || "",
      });
    } catch (error) {
      // 失败不进缓存：只在本轮记为"不可用"，下次刷新自动重试
      //（服务重启后冷缓存的首轮请求可能超时，不应永久 poisoning）。
      if (token !== state.intradayHistory.token) return;
      state.intradayHistory.dayErrors[date] = friendlyError(error, "该日盘中轨迹不可用");
    }
  }));
  if (token !== state.intradayHistory.token) return;
  state.intradayHistory.loading = false;
  syncIntradayPnlSelector();
  renderIntradayHistory();
  if (state.intradayHistory.pendingRecheck) {
    state.intradayHistory.pendingRecheck = false;
    void refreshIntradayHistory();
  }
}

function syncIntradayHistoryBackfillButton() {
  if (!els.intradayHistoryBackfill) return;
  els.intradayHistoryBackfill.disabled = state.intradayHistory.backfillBusy || compareActive();
  els.intradayHistoryBackfill.textContent = state.intradayHistory.backfillBusy ? "回补中…" : "补全缺失时段";
  els.intradayHistoryBackfill.title = "用腾讯5分钟K线按当日持仓静态回算重建缺失时段，并对已记录数据做参考性校验（原始台账不变）";
}

async function submitIntradayHistoryBackfill() {
  if (compareActive() || state.intradayHistory.backfillBusy) return;
  const today = intradayHistoryToday();
  const dates = intradayHistoryDateRange().filter((date) => date < today);
  if (!dates.length) return;
  state.intradayHistory.backfillBusy = true;
  syncIntradayHistoryBackfillButton();
  try {
    const payload = await postJson(
      "/api/intraday-pnl/backfill",
      {
        portfolio: state.activePortfolioId === MAIN_PORTFOLIO_ID ? "" : state.activePortfolioId,
        dates,
      },
      INTRADAY_HISTORY_BACKFILL_TIMEOUT_MS,
    );
    if (!payload || payload.ok === false) {
      throw new Error(String(payload?.error || "回补失败"));
    }
    const results = Array.isArray(payload.results) ? payload.results : [];
    state.intradayHistory.backfillResult = {
      at: Date.now(),
      built: results.filter((item) => item.ok).length,
      total: results.length,
      window: payload.window && payload.window.start ? payload.window : null,
      firstMessage: results.map((item) => item.message).find(Boolean) || "",
    };
    // 窗口更新会影响所有已缓存日期的合并与校验结果，全部失效重拉（含 IndexedDB 持久化缓存）。
    state.intradayHistory.cache.clear();
    void intradayHistoryIdbClear();
    try { window.localStorage.removeItem(INTRADAY_HISTORY_CACHE_STORAGE_KEY); } catch (_) { /* ignore */ }
  } catch (error) {
    state.intradayHistory.backfillResult = { at: Date.now(), error: friendlyError(error, "回补失败") };
  } finally {
    state.intradayHistory.backfillBusy = false;
    syncIntradayHistoryBackfillButton();
    await refreshIntradayHistory();
  }
}

function shortDateLabel(value) {
  const matched = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return matched ? `${matched[2]}-${matched[3]}` : String(value || "--");
}

function renderIntradayHistory() {
  if (!els.intradayHistoryChart) return;
  const range = intradayHistoryDateRange();
  const selection = selectedIntradayPnlSeries(state.intradayPnl);
  const selectionLabel = selection.label;

  const segments = [];
  let waitingCount = 0;
  let errorCount = 0;
  let backfillPoints = 0;
  let correctedPoints = 0;
  let backfillWindow = null;
  for (const date of range) {
    const entry = intradayHistoryEntryFor(date);
    const dayError = state.intradayHistory.dayErrors[date] || entry?.error;
    if (!entry && !dayError) {
      waitingCount += 1;
      segments.push({ date, series: [], status: "waiting" });
      continue;
    }
    if (dayError) {
      errorCount += 1;
      segments.push({ date, series: [], status: "error", message: dayError });
      continue;
    }
    if (entry.backfill) {
      backfillPoints += Number(entry.backfill.points_added || 0);
      const check = entry.backfill.check || {};
      correctedPoints += Number(check.corrected_count || 0);
      const window = entry.backfill.window || {};
      if (window.start && window.end) {
        backfillWindow = backfillWindow
          ? {
              start: backfillWindow.start < window.start ? backfillWindow.start : window.start,
              end: backfillWindow.end > window.end ? backfillWindow.end : window.end,
            }
          : { start: window.start, end: window.end };
      }
    }
    const daySelection = selectedIntradayPnlSeries(entry.snapshot);
    const daySeries = projectIntradayPnlSeries(daySelection.series);
    let status = daySeries.length ? "ok" : (entry.live ? "empty-today" : "empty");
    let message = daySeries.length ? "" : (daySelection.key === "total" ? "该日无已记录的盘中点位" : "该日台账中无此标的的盘中点位");
    if (!daySeries.length && !entry.live) {
      const reason = entry.backfill?.reason || "";
      if (entry.backfill && entry.backfill.available === false && reason === "no_window") {
        status = "backfillable";
        message = "该日无台账记录；本地尚无回补数据，可点击「补全缺失时段」重建。";
      } else if (reason === "no_bars_for_date") {
        status = "unbackfillable";
        message = "该日在回补窗口之外或无5分钟数据，无法重建。";
      }
    }
    segments.push({
      date,
      series: daySeries,
      status,
      message,
      live: Boolean(entry.live),
      backfill: entry.backfill || null,
      tPlan: daySelection.tPlan || null,
    });
  }

  els.intradayHistoryRange.textContent = intradayHistoryRangeText(range, waitingCount, errorCount);

  const notes = [];
  if (state.intradayHistory.datesError) notes.push(`台账日期获取失败：${state.intradayHistory.datesError}`);
  notes.push("上栏当日盈亏（每日归零）· 下栏期间累计（昨日收盘衔接今日开盘）");
  notes.push(`曲线：${selectionLabel}（与上方「盘中当日盈亏」联动）`);
  if (state.selectedPnlSymbol !== "total") {
    const planWithProfile = segments.map((segment) => segment.tPlan).find((plan) => plan && plan.direction);
    if (planWithProfile) {
      if (planWithProfile.suitable) {
        const sim = planWithProfile.direction === "long" ? planWithProfile.sim?.long : planWithProfile.sim?.short;
        const markedDays = segments.filter((segment) => segment.tPlan?.trades?.length).length;
        notes.push(
          `做T标记：${planWithProfile.direction_label} · 阈值 ${planWithProfile.th_pct}% · 窗口内 ${sim?.tries ?? 0} 次/胜率 ${
            sim?.win_rate != null ? `${Math.round(sim.win_rate * 100)}%` : "--"
          }/净均 ${sim?.avg_net_bps ?? "--"}bp · 本范围标记 ${markedDays} 天 · ${planWithProfile.sizing}`,
        );
      } else {
        notes.push(`做T标记：该股不适合规则化做T（${planWithProfile.reason || "期望为负"}），未标买卖点`);
      }
    } else {
      notes.push("做T标记：该股无回补窗口数据，暂无法评估");
    }
  } else {
    notes.push("做T标记仅在上方选择个股曲线时显示");
  }
  if (backfillWindow) {
    notes.push(`回补窗口 ${shortDateLabel(backfillWindow.start)}~${shortDateLabel(backfillWindow.end)}（腾讯5分钟K线，估算）`);
  }
  if (backfillPoints) notes.push(`含回补点 ${backfillPoints} 个`);
  if (correctedPoints) notes.push(`已矫正可疑点 ${correctedPoints} 个`);
  const backfillResult = state.intradayHistory.backfillResult;
  if (backfillResult?.error) notes.push(`最近回补失败：${backfillResult.error}`);
  else if (backfillResult) {
    notes.push(
      `最近回补：${backfillResult.built}/${backfillResult.total} 日成功${
        backfillResult.window?.start
          ? `（窗口 ${shortDateLabel(backfillResult.window.start)}~${shortDateLabel(backfillResult.window.end)}）`
          : ""
      }${backfillResult.firstMessage ? ` · ${backfillResult.firstMessage}` : ""}`,
    );
  }
  if (state.intradayHistory.retentionDays) {
    notes.push(`台账保留最近 ${state.intradayHistory.retentionDays} 个自然日（config/data-retention-policy.json）`);
  }
  els.intradayHistoryNote.textContent = notes.join(" · ");
  syncIntradayHistoryBackfillButton();

  hideIntradayHistoryTooltip();

  const renderKey = JSON.stringify({
    range,
    symbol: state.selectedPnlSymbol,
    loading: state.intradayHistory.loading,
    datesError: state.intradayHistory.datesError,
    masked: state.maskAmounts,
    backfillSummary: { backfillPoints, correctedPoints, window: backfillWindow },
    segments: segments.map((segment) => [
      segment.status,
      segment.series.length,
      segment.series.length ? segment.series[segment.series.length - 1].time : "",
      segment.series.filter((point) => point.source === "backfill").length,
      segment.series.filter((point) => point.source === "corrected").length,
      segment.tPlan ? (segment.tPlan.trades?.length || 0) : -1,
    ]),
  });
  if (renderKey === state.intradayHistoryRenderKey) return;
  state.intradayHistoryRenderKey = renderKey;
  state.intradayHistoryChartModel = null;

  const plottedCount = segments.reduce((total, segment) => total + segment.series.length, 0);
  if (!plottedCount) {
    els.intradayHistoryChart.setAttribute("hidden", "");
    els.intradayHistoryEmpty.hidden = false;
    els.intradayHistoryEmpty.textContent = intradayHistoryEmptyMessage(segments);
    return;
  }

  els.intradayHistoryEmpty.hidden = true;
  els.intradayHistoryChart.removeAttribute("hidden");
  drawIntradayHistoryChart(segments);
}

function intradayHistoryRangeText(range, waitingCount, errorCount) {
  if (state.intradayHistory.loading) {
    return `${shortDateLabel(range[0])} ~ ${shortDateLabel(range[range.length - 1])} · 正在获取 ${range.length} 个交易日`;
  }
  const parts = [];
  if (range.length) {
    parts.push(`${shortDateLabel(range[0])} ~ ${shortDateLabel(range[range.length - 1])} · ${range.length} 个交易日`);
  } else {
    parts.push("暂无台账日期");
  }
  if (waitingCount) parts.push(`${waitingCount} 日等待数据`);
  if (errorCount) parts.push(`${errorCount} 日不可用`);
  return parts.join(" · ");
}

function intradayHistoryEmptyMessage(segments) {
  if (state.intradayHistory.loading) return "正在获取历史分时数据…";
  if (!segments.length) {
    if (state.intradayHistory.datesError) return `历史台账日期暂不可用：${state.intradayHistory.datesError}`;
    if (!state.intradayHistory.datesLoaded) return "正在获取历史台账日期…";
    return "本地尚无任何盘中盈亏台账；交易时段内服务端会自动记录，之后可在此回看。";
  }
  const errors = segments.filter((segment) => segment.status === "error");
  if (errors.length && errors.length === segments.length) {
    return String(errors[0].message || "历史盘中轨迹暂不可用").slice(0, 160);
  }
  if (segments.some((segment) => segment.status === "waiting")) {
    return "今日盘中盈亏轨迹尚未生成，其余交易日也暂无可展示点位。";
  }
  return "所选范围内暂无可展示的盘中盈亏点位";
}

function drawIntradayHistoryChart(segments) {
  const svg = els.intradayHistoryChart;
  const width = 960;
  const height = 300;
  const padding = { top: 24, right: 15, bottom: 24, left: 61 };
  const plotWidth = width - padding.left - padding.right;
  // 上栏：当日盈亏分时（每日归零，看单日形态）；下栏：期间累计盈亏
  // （昨日收盘自然衔接今日开盘，跨日不归零）。两栏共享同一拼接横轴与十字线。
  const dailyPane = { top: padding.top, height: 148 };
  const cumPane = { top: padding.top + 148 + 30, height: 74 };
  dailyPane.bottom = dailyPane.top + dailyPane.height;
  cumPane.bottom = cumPane.top + cumPane.height;

  const dayMs = INTRADAY_PNL_AXIS_DURATION_MS;
  const totalMs = Math.max(1, segments.length * dayMs);
  const x = (value) => padding.left + value / totalMs * plotWidth;

  // 期间累计链：carried = 此前各交易日收盘盈亏之和；当日累计 = carried + 当日点位。
  let carried = 0;
  const cumulativeSegments = segments.map((segment) => {
    const series = segment.series.map((point) => ({ ...point, pnl: carried + point.pnl }));
    if (segment.series.length) carried += segment.series[segment.series.length - 1].pnl;
    return { ...segment, series };
  });
  const finalCumulative = carried;

  const scaleOf = (values) => {
    let valueMin = Math.min(0, ...values);
    let valueMax = Math.max(0, ...values);
    if (valueMin === valueMax) {
      const margin = Math.max(1, Math.abs(valueMin) * 0.08);
      valueMin -= margin;
      valueMax += margin;
    } else {
      const margin = (valueMax - valueMin) * 0.07;
      valueMin -= margin;
      valueMax += margin;
    }
    return { valueMin, valueMax };
  };
  const dailyScale = scaleOf(segments.flatMap((segment) => segment.series.map((point) => point.pnl)));
  const cumScale = scaleOf(cumulativeSegments.flatMap((segment) => segment.series.map((point) => point.pnl)));
  const yDaily = (value) => dailyPane.top + (dailyScale.valueMax - value) / (dailyScale.valueMax - dailyScale.valueMin) * dailyPane.height;
  const yCum = (value) => cumPane.top + (cumScale.valueMax - value) / (cumScale.valueMax - cumScale.valueMin) * cumPane.height;

  svg.replaceChildren();
  const title = svgNode("title");
  title.textContent = `历史分时回看：${segments.length}个交易日，期间累计${formatAmount(finalCumulative, true)}`;
  svg.append(title);

  svg.append(svgNode("rect", {
    x: padding.left,
    y: dailyPane.top,
    width: plotWidth,
    height: cumPane.bottom - dailyPane.top,
    class: "pnl-hover-capture",
  }));

  const gridOf = (scale, y) => {
    const gridValues = [...new Set([scale.valueMax, 0, scale.valueMin].map((value) => Number(value.toFixed(8))))];
    for (const value of gridValues) {
      const line = svgNode("line", {
        x1: padding.left,
        x2: width - padding.right,
        y1: y(value),
        y2: y(value),
        class: value === 0 ? "pnl-zero-line" : "pnl-grid-line",
      });
      const axisLabel = svgNode("text", { x: padding.left - 7, y: y(value) + 3, class: "pnl-axis-label", "text-anchor": "end" });
      axisLabel.textContent = formatChartAmount(value);
      svg.append(line, axisLabel);
    }
  };
  gridOf(dailyScale, yDaily);
  gridOf(cumScale, yCum);

  const showCloseLabels = segments.length <= 10;
  const modelPoints = [];
  const middayOffset = INTRADAY_PNL_SESSIONS[0].endMs - INTRADAY_PNL_SESSIONS[0].startMs;

  segments.forEach((segment, segmentIndex) => {
    const segmentLeft = segmentIndex * dayMs;
    const midX = x(segmentLeft + dayMs / 2);

    // 段顶日期标签：每段固定交易轴的"标题"。
    const dateLabel = svgNode("text", {
      x: midX,
      y: padding.top - 9,
      class: "pnl-day-label",
      "text-anchor": "middle",
    });
    dateLabel.textContent = shortDateLabel(segment.date);
    svg.append(dateLabel);

    // 段间分隔线贯穿两栏（第一段左侧不画）。
    if (segmentIndex > 0) {
      svg.append(svgNode("line", {
        x1: x(segmentLeft),
        x2: x(segmentLeft),
        y1: dailyPane.top,
        y2: cumPane.bottom,
        class: "pnl-day-separator",
      }));
    }

    // 段内午休折叠线（11:30 / 13:00），两栏各画一条。
    for (const pane of [dailyPane, cumPane]) {
      svg.append(svgNode("line", {
        x1: x(segmentLeft + middayOffset),
        x2: x(segmentLeft + middayOffset),
        y1: pane.top,
        y2: pane.bottom,
        class: "pnl-session-join",
      }));
    }

    if (segment.status !== "ok") {
      const statusLabel = svgNode("text", {
        x: midX,
        y: dailyPane.top + dailyPane.height / 2,
        class: "pnl-history-status-label",
        "text-anchor": "middle",
      });
      statusLabel.textContent = {
        waiting: "等待数据",
        "empty-today": "今日未记录",
        backfillable: "未记录·可补全",
        unbackfillable: "无法重建",
      }[segment.status] || "无记录";
      if (segment.message) {
        const statusTitle = svgNode("title");
        statusTitle.textContent = segment.message;
        statusLabel.append(statusTitle);
      }
      svg.append(statusLabel);
      return;
    }

    const projected = segment.series.map((point) => ({
      ...point,
      plotTimeValue: segmentLeft + point.plotTimeValue,
    }));
    const cumProjected = cumulativeSegments[segmentIndex].series.map((point) => ({
      ...point,
      plotTimeValue: segmentLeft + point.plotTimeValue,
    }));

    // 上栏：当日盈亏线（记录段实线、回补段虚线）。
    for (const run of splitHistoryRunsBySource(projected)) {
      const backfillRun = run.source === "backfill";
      if (run.points.length === 1) {
        svg.append(svgNode("circle", {
          cx: x(run.points[0].plotTimeValue),
          cy: yDaily(run.points[0].pnl),
          r: 2.2,
          class: `pnl-line-dot${backfillRun ? " is-backfill" : ""}`,
        }));
        continue;
      }
      svg.append(svgNode("path", {
        d: run.points.map((point, index) => `${index ? "L" : "M"}${x(point.plotTimeValue).toFixed(2)},${yDaily(point.pnl).toFixed(2)}`).join(" "),
        class: `pnl-line-path${backfillRun ? " is-backfill" : ""}`,
      }));
    }

    // 做T买卖点标记：仅个股曲线、且该股通过适格判定时由服务端给出。
    const tPlan = segment.tPlan;
    if (tPlan?.suitable && Array.isArray(tPlan.trades)) {
      for (const trade of tPlan.trades) {
        const entryPoint = findHistoryPointByTime(projected, trade.entry_time);
        if (!entryPoint) continue;
        const exitPoint = findHistoryPointByTime(projected, trade.exit_time);
        const entryAction = trade.action_entry || (tPlan.direction === "long" ? "buy" : "sell");
        const exitAction = trade.action_exit || (tPlan.direction === "long" ? "sell" : "buy");
        if (exitPoint) {
          svg.append(svgNode("line", {
            x1: x(entryPoint.plotTimeValue),
            y1: yDaily(entryPoint.pnl),
            x2: x(exitPoint.plotTimeValue),
            y2: yDaily(exitPoint.pnl),
            class: "t-marker-link",
          }));
        }
        appendTMarker(
          svg,
          x(entryPoint.plotTimeValue),
          yDaily(entryPoint.pnl),
          entryAction,
          `${segment.date} ${tPlan.direction_label}：${entryAction === "buy" ? "买入" : "卖出"} ${formatIntradayTimeDetailed(trade.entry_time)} @${trade.entry_price} · 建议 ${trade.shares} 股 ≈ ${formatAmount(trade.notional)}${trade.completed ? "" : "（此笔未触发目标位，按收盘了结统计）"}`,
        );
        if (exitPoint) {
          appendTMarker(
            svg,
            x(exitPoint.plotTimeValue),
            yDaily(exitPoint.pnl),
            exitAction,
            `${segment.date} ${trade.completed ? "目标位了结" : "收盘平仓"} ${formatIntradayTimeDetailed(trade.exit_time)} @${trade.exit_price} · 本笔 ${trade.raw_bps > 0 ? "+" : ""}${trade.raw_bps}bp（未计费用）`,
          );
        }
      }
    }

    // 下栏：期间累计线（同一来源拆分，保持视觉一致）。
    for (const run of splitHistoryRunsBySource(cumProjected)) {
      const backfillRun = run.source === "backfill";
      if (run.points.length === 1) {
        svg.append(svgNode("circle", {
          cx: x(run.points[0].plotTimeValue),
          cy: yCum(run.points[0].pnl),
          r: 1.8,
          class: `pnl-cum-dot${backfillRun ? " is-backfill" : ""}`,
        }));
        continue;
      }
      svg.append(svgNode("path", {
        d: run.points.map((point, index) => `${index ? "L" : "M"}${x(point.plotTimeValue).toFixed(2)},${yCum(point.pnl).toFixed(2)}`).join(" "),
        class: `pnl-cum-line${backfillRun ? " is-backfill" : ""}`,
      }));
    }

    const closePoint = projected[projected.length - 1];
    if (closePoint) {
      const closeDot = svgNode("circle", { cx: x(closePoint.plotTimeValue), cy: yDaily(closePoint.pnl), r: 2.6, class: `pnl-latest-dot ${directionClass(closePoint.pnl)}` });
      const closeTitle = svgNode("title");
      closeTitle.textContent = `${segment.date} 当日收于 ${formatAmount(closePoint.pnl, true)}`;
      closeDot.append(closeTitle);
      svg.append(closeDot);
      if (showCloseLabels) {
        const closeLabel = svgNode("text", {
          x: x(closePoint.plotTimeValue),
          y: Math.max(dailyPane.top + 8, yDaily(closePoint.pnl) - 6),
          class: `pnl-history-close-label ${directionClass(closePoint.pnl)}`,
          "text-anchor": "middle",
        });
        closeLabel.textContent = formatChartAmount(closePoint.pnl);
        svg.append(closeLabel);
      }

      // 累计栏每日收盘点：即"昨日收盘 → 今日开盘"的衔接锚点。
      const cumClosePoint = cumProjected[cumProjected.length - 1];
      const cumCloseDot = svgNode("circle", { cx: x(cumClosePoint.plotTimeValue), cy: yCum(cumClosePoint.pnl), r: 2.2, class: `pnl-cum-close-dot ${directionClass(cumClosePoint.pnl)}` });
      const cumCloseTitle = svgNode("title");
      cumCloseTitle.textContent = `${segment.date} 期间累计 ${formatAmount(cumClosePoint.pnl, true)}`;
      cumCloseDot.append(cumCloseTitle);
      svg.append(cumCloseDot);
      if (showCloseLabels) {
        const cumCloseLabel = svgNode("text", {
          x: x(cumClosePoint.plotTimeValue),
          y: Math.min(cumPane.bottom - 3, Math.max(cumPane.top + 9, yCum(cumClosePoint.pnl) + 11)),
          class: `pnl-cum-close-label ${directionClass(cumClosePoint.pnl)}`,
          "text-anchor": "middle",
        });
        cumCloseLabel.textContent = formatChartAmount(cumClosePoint.pnl);
        svg.append(cumCloseLabel);
      }

      for (let index = 0; index < projected.length; index += 1) {
        const point = projected[index];
        modelPoints.push({
          date: segment.date,
          time: point.time,
          pnl: point.pnl,
          cum: cumProjected[index].pnl,
          source: point.source || "recorded",
          pnlRecorded: isFiniteNumber(point.pnl_recorded) ? point.pnl_recorded : null,
          x: x(point.plotTimeValue),
          y: yDaily(point.pnl),
          yCum: yCum(cumProjected[index].pnl),
        });
      }
    }
  });

  // 栏目标签：上栏说明口径；下栏附统计期间最终累计值（红涨绿跌）。
  const dailyPaneLabel = svgNode("text", {
    x: padding.left + 4,
    y: dailyPane.top + 10,
    class: "pnl-pane-label",
    "text-anchor": "start",
  });
  dailyPaneLabel.textContent = "当日盈亏（每日归零）";
  svg.append(dailyPaneLabel);
  const cumPaneLabel = svgNode("text", {
    x: padding.left + 4,
    y: cumPane.top + 10,
    class: `pnl-pane-label ${directionClass(finalCumulative)}`,
    "text-anchor": "start",
  });
  cumPaneLabel.textContent = `期间累计 ${formatAmount(finalCumulative, true)}`;
  svg.append(cumPaneLabel);

  // 时间刻度只标注第一段：所有段共享同一固定交易轴；段数多、首段过窄时
  // 仅保留首尾刻度，避免文字重叠（午休折叠线仍逐段绘制）。
  if (segments.length) {
    const timeTicks = [
      { value: 0, label: "09:30", anchor: "start" },
      { value: 60 * 60_000, label: "10:30", anchor: "middle" },
      { value: 2 * 60 * 60_000, label: "11:30/13:00", anchor: "middle" },
      { value: 3 * 60 * 60_000, label: "14:00", anchor: "middle" },
      { value: dayMs, label: "15:00", anchor: "end" },
    ].filter((tick) => segments.length <= 5 || tick.value === 0 || tick.value === dayMs);
    for (const tick of timeTicks) {
      const tickLabel = svgNode("text", {
        x: x(tick.value),
        y: height - 6,
        class: "pnl-axis-label pnl-time-axis-label",
        "text-anchor": tick.anchor,
      });
      tickLabel.textContent = tick.label;
      svg.append(tickLabel);
    }
  }

  const hoverLine = svgNode("line", {
    y1: dailyPane.top,
    y2: cumPane.bottom,
    class: "pnl-hover-line",
    visibility: "hidden",
  });
  const hoverDot = svgNode("circle", {
    r: 3.5,
    class: "pnl-hover-dot",
    visibility: "hidden",
  });
  const hoverDotCum = svgNode("circle", {
    r: 2.8,
    class: "pnl-hover-dot pnl-hover-dot-cum",
    visibility: "hidden",
  });
  svg.append(hoverLine, hoverDot, hoverDotCum);

  modelPoints.sort((a, b) => a.x - b.x || a.y - b.y);
  const latestPoint = modelPoints[modelPoints.length - 1];
  svg.setAttribute(
    "aria-label",
    `历史分时回看，${segments.length}个交易日，${modelPoints.length}个数据点${
      latestPoint ? `，最新${shortDateLabel(latestPoint.date)}当日${formatAmount(latestPoint.pnl, true)}、期间累计${formatAmount(latestPoint.cum, true)}` : ""
    }`,
  );
  state.intradayHistoryChartModel = {
    width,
    height,
    padding,
    area: { top: dailyPane.top, bottom: cumPane.bottom },
    points: modelPoints,
    activeIndex: -1,
    hoverLine,
    hoverDot,
    hoverDotCum,
  };
}

function findHistoryPointByTime(projected, isoTime) {
  const target = parseDate(isoTime)?.getTime();
  if (!isFiniteNumber(target)) return null;
  let best = null;
  let bestDistance = Infinity;
  for (const point of projected) {
    const stamp = parseDate(point.time)?.getTime();
    if (!isFiniteNumber(stamp)) continue;
    const distance = Math.abs(stamp - target);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = point;
    }
  }
  return bestDistance <= 150_000 ? best : null;
}

function appendTMarker(svg, cx, cy, action, titleText) {
  // 买入：曲线下方向上的三角（尖触曲线）；卖出：上方向下的三角。
  const up = action === "buy";
  const marker = svgNode("path", {
    d: up
      ? `M${cx.toFixed(2)},${(cy + 3).toFixed(2)} L${(cx - 4).toFixed(2)},${(cy + 9).toFixed(2)} L${(cx + 4).toFixed(2)},${(cy + 9).toFixed(2)} Z`
      : `M${cx.toFixed(2)},${(cy - 3).toFixed(2)} L${(cx - 4).toFixed(2)},${(cy - 9).toFixed(2)} L${(cx + 4).toFixed(2)},${(cy - 9).toFixed(2)} Z`,
    class: up ? "t-marker-buy" : "t-marker-sell",
  });
  const markerTitle = svgNode("title");
  markerTitle.textContent = titleText;
  marker.append(markerTitle);
  svg.append(marker);
}

function splitHistoryRunsBySource(series) {
  if (!series.length) return [];
  const coordinate = (point) => (isFiniteNumber(point.plotTimeValue) ? point.plotTimeValue : 0);
  const gaps = series
    .slice(1)
    .map((point, index) => coordinate(point) - coordinate(series[index]))
    .filter((value) => value > 0)
    .sort((a, b) => a - b);
  const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 60_000;
  const threshold = Math.min(30 * 60_000, Math.max(5 * 60_000, median * 6));
  const sourceBucket = (point) => (point.source === "backfill" ? "backfill" : "recorded");
  const runs = [{ source: sourceBucket(series[0]), points: [series[0]] }];
  for (let index = 1; index < series.length; index += 1) {
    const previous = series[index - 1];
    const current = series[index];
    const bucket = sourceBucket(current);
    if (coordinate(current) - coordinate(previous) > threshold || bucket !== runs[runs.length - 1].source) {
      runs.push({ source: bucket, points: [] });
    }
    runs[runs.length - 1].points.push(current);
  }
  return runs;
}

function handleIntradayHistoryPointer(event) {
  const model = state.intradayHistoryChartModel;
  if (!model?.points?.length) return;
  const rect = els.intradayHistoryChart.getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return;
  const chartX = (event.clientX - rect.left) / rect.width * model.width;
  const chartY = (event.clientY - rect.top) / rect.height * model.height;
  const { left, right } = model.padding;
  const area = model.area || { top: model.padding.top, bottom: model.height - model.padding.bottom };
  if (chartX < left || chartX > model.width - right || chartY < area.top || chartY > area.bottom) {
    hideIntradayHistoryTooltip();
    return;
  }
  const index = nearestIntradayPnlPoint(model.points, chartX, chartY);
  if (index >= 0) showIntradayHistoryTooltip(index);
}

function handleIntradayHistoryKeyboard(event) {
  const model = state.intradayHistoryChartModel;
  const points = model?.points || [];
  if (event.key === "Escape") {
    hideIntradayHistoryTooltip();
    return;
  }
  if (!points.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  let index = model.activeIndex;
  if (event.key === "Home") index = 0;
  else if (event.key === "End") index = points.length - 1;
  else if (event.key === "ArrowLeft") index = index < 0 ? points.length - 1 : Math.max(0, index - 1);
  else index = index < 0 ? 0 : Math.min(points.length - 1, index + 1);
  showIntradayHistoryTooltip(index);
}

function showIntradayHistoryTooltip(index) {
  const model = state.intradayHistoryChartModel;
  const point = model?.points?.[index];
  if (!point || !els.intradayHistoryTooltip) return;
  model.activeIndex = index;
  model.hoverLine.setAttribute("x1", point.x);
  model.hoverLine.setAttribute("x2", point.x);
  model.hoverLine.removeAttribute("visibility");
  model.hoverDot.setAttribute("cx", point.x);
  model.hoverDot.setAttribute("cy", point.y);
  model.hoverDot.removeAttribute("visibility");
  if (model.hoverDotCum) {
    model.hoverDotCum.setAttribute("cx", point.x);
    model.hoverDotCum.setAttribute("cy", point.yCum ?? point.y);
    model.hoverDotCum.removeAttribute("visibility");
  }

  els.intradayHistoryTooltipDate.textContent =
    `${shortDateLabel(point.date)} ${formatIntradayTimeDetailed(point.time)}`;
  const sourcePrefix = point.source === "backfill" ? "回补·" : point.source === "corrected" ? "已矫正·" : "";
  const originalSuffix =
    point.source === "corrected" && isFiniteNumber(point.pnlRecorded)
      ? `（原值 ${formatAmount(point.pnlRecorded, true)}）`
      : "";
  els.intradayHistoryTooltipValue.textContent =
    `${sourcePrefix}当日 ${formatAmount(point.pnl, true)}${originalSuffix}`;
  els.intradayHistoryTooltipValue.className = `intraday-pnl-tooltip-value ${directionClass(point.pnl)}`;
  if (els.intradayHistoryTooltipCumulative) {
    els.intradayHistoryTooltipCumulative.textContent = `累计 ${formatAmount(point.cum ?? point.pnl, true)}`;
    els.intradayHistoryTooltipCumulative.className =
      `intraday-history-tooltip-cumulative ${directionClass(point.cum ?? point.pnl)}`;
  }
  els.intradayHistoryTooltip.hidden = false;

  const svgRect = els.intradayHistoryChart.getBoundingClientRect();
  const wrapRect = els.intradayHistoryChartWrap.getBoundingClientRect();
  const rawLeft = svgRect.left - wrapRect.left + point.x / model.width * svgRect.width;
  const rawTop = svgRect.top - wrapRect.top + point.y / model.height * svgRect.height - 7;
  const horizontalMargin = Math.min(72, wrapRect.width / 2);
  const left = Math.min(Math.max(rawLeft, horizontalMargin), Math.max(horizontalMargin, wrapRect.width - horizontalMargin));
  els.intradayHistoryTooltip.style.left = `${left}px`;
  els.intradayHistoryTooltip.style.top = `${Math.max(27, rawTop)}px`;
}

function hideIntradayHistoryTooltip() {
  if (els.intradayHistoryTooltip) els.intradayHistoryTooltip.hidden = true;
  const model = state.intradayHistoryChartModel;
  if (!model) return;
  model.activeIndex = -1;
  model.hoverLine?.setAttribute("visibility", "hidden");
  model.hoverDot?.setAttribute("visibility", "hidden");
  model.hoverDotCum?.setAttribute("visibility", "hidden");
}

function renderDailyPnlHistory() {
  const snapshot = state.dailyPnlHistory;
  const records = snapshot?.records || [];
  const latestRecord = records.length ? records[records.length - 1] : null;
  const periodTotalIncomplete = Boolean(
    snapshot?.periodTotalDeclared
    && (!isFiniteNumber(snapshot.periodTotalPnl) || snapshot.periodComplete === false),
  );
  const latestCumulative = snapshot?.periodTotalDeclared
    ? (periodTotalIncomplete ? null : snapshot.periodTotalPnl)
    : firstNumber(latestRecord?.cumulativePnl);

  els.dailyPnlHistoryTotal.textContent = periodTotalIncomplete
    ? "累计不完整 --"
    : isFiniteNumber(latestCumulative)
    ? `累计 ${formatAmount(latestCumulative, true)}`
    : "累计 --";
  els.dailyPnlHistoryTotal.className = `daily-pnl-history-total ${directionClass(latestCumulative)}`;

  const firstDate = snapshot?.startDate || records[0]?.date || DAILY_PNL_HISTORY_START_DATE;
  const lastDate = snapshot?.endDate || latestRecord?.date || "今天";
  const meta = [`${firstDate} 至 ${lastDate}`];
  if (records.length) meta.push(`${records.length} 个交易日`);
  if (latestRecord) meta.push(dailyPnlCoverageLabel(latestRecord, true));
  if (state.dailyPnlHistoryReceivedAt) {
    meta.push(ageLabel(state.dailyPnlHistoryReceivedAt, state.dailyPnlHistoryBaseAgeSeconds));
  }
  els.dailyPnlHistoryMeta.textContent = meta.filter(Boolean).join(" · ");

  const notes = ["按8/28静态数量×未复权收盘价涨跌回算；不含历史交易、税费、融资费用及现金分红，非券商实际账单"];
  if (snapshot?.basis) notes.push(String(snapshot.basis).slice(0, 180));
  if (snapshot?.qualityStatus) notes.push(`质量 ${snapshot.qualityStatus}`);
  if (snapshot?.stale) notes.push("当前展示缓存记录");
  if (snapshot?.periodTotalDeclared && snapshot?.periodComplete === false) {
    notes.push("全期价格覆盖不完整，未计算严格累计盈亏");
    if (isFiniteNumber(snapshot.periodCoveredPnl)) {
      notes.push(`已覆盖部分合计 ${formatAmount(snapshot.periodCoveredPnl, true)}（非完整累计）`);
    }
  }
  if (snapshot?.warnings?.length) notes.push(...snapshot.warnings.slice(0, 2));
  if (state.dailyPnlHistoryError) notes.push(`历史更新失败：${state.dailyPnlHistoryError}`);
  notes.push("缺失金额不按零处理；部分覆盖日期不绘制日盈亏柱");
  els.dailyPnlHistoryNote.textContent = notes.join(" · ");

  renderDailyPnlHistoryTable(records);
  hideDailyPnlHistoryTooltip();
  state.dailyPnlHistoryChartModel = null;
  const hasChartData = records.some((record) => dailyPnlRecordAmount(record) != null || isFiniteNumber(record.cumulativePnl));
  if (!hasChartData) {
    els.dailyPnlHistoryChart.setAttribute("hidden", "");
    els.dailyPnlHistoryEmpty.hidden = false;
    els.dailyPnlHistoryEmpty.textContent = dailyPnlHistoryEmptyMessage(snapshot);
    return;
  }

  els.dailyPnlHistoryEmpty.hidden = true;
  els.dailyPnlHistoryChart.removeAttribute("hidden");
  drawDailyPnlHistoryChart(records);
}

function renderDailyPnlHistoryTable(records) {
  els.dailyPnlHistoryBody.replaceChildren();
  if (!records.length) {
    const row = document.createElement("tr");
    const cell = td(dailyPnlHistoryEmptyMessage(state.dailyPnlHistory), "empty");
    cell.colSpan = 5;
    row.append(cell);
    els.dailyPnlHistoryBody.append(row);
    return;
  }

  for (const record of [...records].reverse()) {
    const row = document.createElement("tr");
    const dailyPnl = dailyPnlRecordAmount(record);
    const dailyPnlCell = td("", `numeric sensitive ${directionClass(dailyPnl)}`);
    dailyPnlCell.append(span(formatAmount(dailyPnl, true), "daily-pnl-history-amount-main"));
    if (!isFiniteNumber(dailyPnl) && isFiniteNumber(record.coveredDailyPnl)) {
      dailyPnlCell.append(span(
        `已覆盖 ${formatAmount(record.coveredDailyPnl, true)}`,
        `daily-pnl-history-amount-detail ${directionClass(record.coveredDailyPnl)}`,
      ));
      dailyPnlCell.title = `${dailyPnlCoverageDetail(record)}；该金额仅为已覆盖部分，不是总持仓当日盈亏`;
    }
    const coverageCell = td(dailyPnlCoverageLabel(record), "daily-pnl-history-coverage");
    coverageCell.title = dailyPnlCoverageDetail(record);
    const statusCell = td(
      dailyPnlStatusLabel(record.status),
      `daily-pnl-history-status is-${dailyPnlStatusClass(record.status)}`,
    );
    statusCell.title = [record.source, record.basis].filter(Boolean).join(" · ") || "数据口径待核验";
    row.append(
      td(record.date, ""),
      dailyPnlCell,
      td(formatAmount(record.cumulativePnl, true), `numeric sensitive ${directionClass(record.cumulativePnl)}`),
      coverageCell,
      statusCell,
    );
    els.dailyPnlHistoryBody.append(row);
  }
}

function dailyPnlRecordAmount(record) {
  if (!record || String(record.status || "").toLowerCase() === "partial") return null;
  return isFiniteNumber(record.dailyPnl) ? record.dailyPnl : null;
}

function dailyPnlStatusClass(value) {
  const status = String(value || "").trim().toLowerCase();
  return ["final", "provisional", "partial"].includes(status) ? status : "unknown";
}

function dailyPnlStatusLabel(value) {
  const status = String(value || "").trim().toLowerCase();
  if (status === "final") return "已收盘";
  if (status === "provisional") return "盘中";
  if (status === "partial") return "部分覆盖";
  return status ? String(value).slice(0, 24) : "待核验";
}

function dailyPnlCoverageLabel(record, verbose = false) {
  if (!record) return verbose ? "覆盖待核验" : "--";
  const compact = isFiniteNumber(record.coveredPositions) && isFiniteNumber(record.eligiblePositions)
    ? `${record.coveredPositions}/${record.eligiblePositions}`
    : isFiniteNumber(record.coveragePct)
      ? `${record.coveragePct.toFixed(record.coveragePct % 1 ? 1 : 0)}%`
      : "--";
  if (!verbose) return compact;
  const details = [`覆盖 ${compact}`];
  if (isFiniteNumber(record.unlistedPositions) && record.unlistedPositions > 0) {
    details.push(`未上市 ${record.unlistedPositions}`);
  }
  if (isFiniteNumber(record.missingPositions) && record.missingPositions > 0) {
    details.push(`缺失 ${record.missingPositions}`);
  }
  return details.join(" · ");
}

function dailyPnlCoverageDetail(record) {
  const details = [dailyPnlCoverageLabel(record, true)];
  if (isFiniteNumber(record.eligiblePositions)) details.push(`应计 ${record.eligiblePositions}`);
  if (isFiniteNumber(record.unlistedPositions)) details.push(`未上市 ${record.unlistedPositions}`);
  if (isFiniteNumber(record.missingPositions)) details.push(`缺失 ${record.missingPositions}`);
  return [...new Set(details.filter(Boolean))].join(" · ");
}

function dailyPnlHistoryEmptyMessage(snapshot) {
  if (state.dailyPnlHistoryLoading && !snapshot) return "正在获取每日总持仓盈亏…";
  if (state.dailyPnlHistoryError && !snapshot) return `每日盈亏历史暂不可用：${state.dailyPnlHistoryError}`;
  if (snapshot?.available === false) return snapshot.message || "2026-07-01 起暂无可用的每日盈亏记录";
  if (snapshot?.message) return String(snapshot.message).slice(0, 180);
  return "2026-07-01 起暂无可展示的每日盈亏记录";
}

function drawDailyPnlHistoryChart(records) {
  const svg = els.dailyPnlHistoryChart;
  const width = 960;
  const height = 190;
  const padding = { top: 18, right: 62, bottom: 27, left: 62 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const dailyValues = records.map(dailyPnlRecordAmount).filter(isFiniteNumber);
  const cumulativeValues = records.map((record) => record.cumulativePnl).filter(isFiniteNumber);
  const dailyDomain = paddedChartDomain(dailyValues, true);
  const cumulativeDomain = paddedChartDomain(cumulativeValues, false);
  const slotWidth = plotWidth / Math.max(1, records.length);
  const barWidth = Math.max(1.5, Math.min(15, slotWidth * 0.7));
  const x = (index) => padding.left + (index + 0.5) * slotWidth;
  const yDaily = (value) => padding.top
    + (dailyDomain.max - value) / (dailyDomain.max - dailyDomain.min) * plotHeight;
  const yCumulative = (value) => padding.top
    + (cumulativeDomain.max - value) / (cumulativeDomain.max - cumulativeDomain.min) * plotHeight;

  svg.replaceChildren();
  const title = svgNode("title");
  title.textContent = `每日总持仓盈亏，${records.length}个交易日；红绿柱为日盈亏，折线为接口返回的累计盈亏`;
  svg.append(title);
  svg.append(svgNode("rect", {
    x: padding.left,
    y: padding.top,
    width: plotWidth,
    height: plotHeight,
    class: "daily-pnl-hover-capture",
  }));

  const dailyGridValues = [...new Set([dailyDomain.max, 0, dailyDomain.min].map((value) => Number(value.toFixed(8))))];
  for (const value of dailyGridValues) {
    const line = svgNode("line", {
      x1: padding.left,
      x2: width - padding.right,
      y1: yDaily(value),
      y2: yDaily(value),
      class: value === 0 ? "daily-pnl-zero-line" : "daily-pnl-grid-line",
    });
    const label = svgNode("text", {
      x: padding.left - 7,
      y: yDaily(value) + 3,
      class: "daily-pnl-axis-label",
      "text-anchor": "end",
    });
    label.textContent = formatChartAmount(value);
    svg.append(line, label);
  }

  const zeroY = yDaily(0);
  records.forEach((record, index) => {
    const value = dailyPnlRecordAmount(record);
    if (!isFiniteNumber(value)) return;
    const valueY = yDaily(value);
    const bar = svgNode("rect", {
      x: x(index) - barWidth / 2,
      y: Math.min(zeroY, valueY),
      width: barWidth,
      height: Math.max(1, Math.abs(valueY - zeroY)),
      class: `daily-pnl-bar ${directionClass(value)}`,
      "data-date": record.date,
    });
    const barTitle = svgNode("title");
    barTitle.textContent = `${record.date} 当日 ${formatAmount(value, true)}`;
    bar.append(barTitle);
    svg.append(bar);
  });

  if (cumulativeValues.length) {
    const rightValues = [...new Set([cumulativeDomain.max, cumulativeDomain.min].map((value) => Number(value.toFixed(8))))];
    for (const value of rightValues) {
      const label = svgNode("text", {
        x: width - padding.right + 7,
        y: yCumulative(value) + 3,
        class: "daily-pnl-axis-label is-cumulative",
        "text-anchor": "start",
      });
      label.textContent = formatChartAmount(value);
      svg.append(label);
    }
    for (const segment of dailyPnlCumulativeSegments(records)) {
      if (segment.length === 1) {
        const point = segment[0];
        svg.append(svgNode("circle", {
          cx: x(point.index),
          cy: yCumulative(point.value),
          r: 2.2,
          class: "daily-pnl-cumulative-dot",
        }));
        continue;
      }
      svg.append(svgNode("path", {
        d: segment.map((point, segmentIndex) => `${segmentIndex ? "L" : "M"}${x(point.index).toFixed(2)},${yCumulative(point.value).toFixed(2)}`).join(" "),
        class: "daily-pnl-cumulative-line",
      }));
    }
  }

  for (const index of dailyPnlDateTickIndexes(records.length)) {
    const label = svgNode("text", {
      x: x(index),
      y: height - 6,
      class: "daily-pnl-axis-label",
      "text-anchor": index === 0 ? "start" : index === records.length - 1 ? "end" : "middle",
      "data-date-label": records[index].date,
    });
    label.textContent = records[index].date.slice(5);
    svg.append(label);
  }

  const dailyLegend = svgNode("text", { x: padding.left, y: 11, class: "daily-pnl-axis-label" });
  dailyLegend.textContent = "柱：当日盈亏";
  const cumulativeLegend = svgNode("text", {
    x: width - padding.right,
    y: 11,
    class: "daily-pnl-axis-label is-cumulative",
    "text-anchor": "end",
  });
  cumulativeLegend.textContent = "线：累计盈亏";
  svg.append(dailyLegend, cumulativeLegend);

  const hoverLine = svgNode("line", {
    y1: padding.top,
    y2: height - padding.bottom,
    class: "daily-pnl-hover-line",
    visibility: "hidden",
  });
  svg.append(hoverLine);
  const points = records.map((record, index) => {
    const dailyPnl = dailyPnlRecordAmount(record);
    const focusY = isFiniteNumber(dailyPnl)
      ? yDaily(dailyPnl)
      : isFiniteNumber(record.cumulativePnl)
        ? yCumulative(record.cumulativePnl)
        : null;
    return { ...record, dailyPnl, x: x(index), y: focusY };
  }).filter((point) => isFiniteNumber(point.y));
  state.dailyPnlHistoryChartModel = {
    width,
    height,
    padding,
    points,
    activeIndex: -1,
    hoverLine,
  };
  svg.setAttribute("aria-label", `每日总持仓盈亏历史图，共${records.length}个交易日，${dailyValues.length}个完整日盈亏点`);
}

function paddedChartDomain(values, includeZero) {
  const finite = (values || []).filter(isFiniteNumber);
  let min = finite.length ? Math.min(...finite) : -1;
  let max = finite.length ? Math.max(...finite) : 1;
  if (includeZero) {
    min = Math.min(0, min);
    max = Math.max(0, max);
  }
  if (min === max) {
    const margin = Math.max(1, Math.abs(min) * 0.08);
    min -= margin;
    max += margin;
  } else {
    const margin = (max - min) * 0.07;
    min -= margin;
    max += margin;
  }
  return { min, max };
}

function dailyPnlCumulativeSegments(records) {
  const segments = [];
  let current = [];
  records.forEach((record, index) => {
    if (isFiniteNumber(record.cumulativePnl)) {
      current.push({ index, value: record.cumulativePnl });
      return;
    }
    if (current.length) segments.push(current);
    current = [];
  });
  if (current.length) segments.push(current);
  return segments;
}

function dailyPnlDateTickIndexes(count, maximum = 6) {
  if (count <= 0) return [];
  if (count <= maximum) return Array.from({ length: count }, (_, index) => index);
  const indexes = [];
  for (let index = 0; index < maximum; index += 1) {
    indexes.push(Math.round(index * (count - 1) / (maximum - 1)));
  }
  return [...new Set(indexes)];
}

function handleDailyPnlHistoryPointer(event) {
  const model = state.dailyPnlHistoryChartModel;
  if (!model?.points?.length) return;
  const rect = els.dailyPnlHistoryChart.getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return;
  const chartX = (event.clientX - rect.left) / rect.width * model.width;
  const chartY = (event.clientY - rect.top) / rect.height * model.height;
  const { left, right, top, bottom } = model.padding;
  if (chartX < left || chartX > model.width - right || chartY < top || chartY > model.height - bottom) {
    hideDailyPnlHistoryTooltip();
    return;
  }
  const index = nearestDailyPnlHistoryPoint(model.points, chartX);
  if (index >= 0) showDailyPnlHistoryTooltip(index);
}

function handleDailyPnlHistoryKeyboard(event) {
  const model = state.dailyPnlHistoryChartModel;
  const points = model?.points || [];
  if (event.key === "Escape") {
    hideDailyPnlHistoryTooltip();
    return;
  }
  if (!points.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  let index = model.activeIndex;
  if (event.key === "Home") index = 0;
  else if (event.key === "End") index = points.length - 1;
  else if (event.key === "ArrowLeft") index = index < 0 ? points.length - 1 : Math.max(0, index - 1);
  else index = index < 0 ? 0 : Math.min(points.length - 1, index + 1);
  showDailyPnlHistoryTooltip(index);
}

function nearestDailyPnlHistoryPoint(points, chartX) {
  let bestIndex = -1;
  let bestDistance = Infinity;
  points.forEach((point, index) => {
    const distance = Math.abs(point.x - chartX);
    if (distance < bestDistance) {
      bestIndex = index;
      bestDistance = distance;
    }
  });
  return bestIndex;
}

function showDailyPnlHistoryTooltip(index) {
  const model = state.dailyPnlHistoryChartModel;
  const point = model?.points?.[index];
  if (!point || !els.dailyPnlHistoryTooltip) return;
  model.activeIndex = index;
  model.hoverLine.setAttribute("x1", point.x);
  model.hoverLine.setAttribute("x2", point.x);
  model.hoverLine.removeAttribute("visibility");

  els.dailyPnlHistoryTooltipDate.textContent = point.date;
  els.dailyPnlHistoryTooltipValue.textContent = `当日 ${formatAmount(point.dailyPnl, true)}`;
  els.dailyPnlHistoryTooltipValue.className = `intraday-pnl-tooltip-value ${directionClass(point.dailyPnl)}`;
  els.dailyPnlHistoryTooltipCumulative.textContent = `累计 ${formatAmount(point.cumulativePnl, true)}`;
  els.dailyPnlHistoryTooltip.hidden = false;

  const svgRect = els.dailyPnlHistoryChart.getBoundingClientRect();
  const wrapRect = els.dailyPnlHistoryChartWrap.getBoundingClientRect();
  const rawLeft = svgRect.left - wrapRect.left + point.x / model.width * svgRect.width;
  const rawTop = svgRect.top - wrapRect.top + point.y / model.height * svgRect.height - 7;
  const horizontalMargin = Math.min(105, wrapRect.width / 2);
  const left = Math.min(Math.max(rawLeft, horizontalMargin), Math.max(horizontalMargin, wrapRect.width - horizontalMargin));
  els.dailyPnlHistoryTooltip.style.left = `${left}px`;
  els.dailyPnlHistoryTooltip.style.top = `${Math.max(27, rawTop)}px`;
}

function hideDailyPnlHistoryTooltip() {
  if (els.dailyPnlHistoryTooltip) els.dailyPnlHistoryTooltip.hidden = true;
  const model = state.dailyPnlHistoryChartModel;
  if (!model) return;
  model.activeIndex = -1;
  model.hoverLine?.setAttribute("visibility", "hidden");
}

function formatChartAmount(value) {
  if (state.maskAmounts) return "••••";
  if (!isFiniteNumber(value)) return "--";
  const absolute = Math.abs(value);
  if (absolute >= 100_000_000) return `${(value / 100_000_000).toFixed(1)}亿`;
  if (absolute >= 10_000) return `${(value / 10_000).toFixed(1)}万`;
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value);
}

function formatIntradayTime(value) {
  const raw = String(value || "");
  const matched = raw.match(/(?:T|\s)(\d{2}:\d{2})(?::\d{2})?/);
  if (matched) return matched[1];
  if (/^\d{1,2}:\d{2}/.test(raw)) return raw.slice(0, 5);
  const date = parseDate(value);
  return date ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) : raw.slice(0, 5) || "--";
}

function formatIntradayTimeDetailed(value) {
  const raw = String(value || "");
  const matched = raw.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (matched) return `${matched[1].padStart(2, "0")}:${matched[2]}:${matched[3] || "00"}`;
  const date = parseDate(value);
  return date
    ? date.toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
    : "--";
}

function isNonTradingPhase(value) {
  const raw = String(value || "").toLowerCase();
  if (!raw) return false;
  return /^(?:closed?|post(?:[_ -]?(?:market|close))?|after(?:[_ -]?hours)?|overnight|holiday|weekend|lunch|noon|pre(?:[_ -]?(?:market|open))?|before(?:[_ -]?open)?|halt(?:ed)?|休市|收盘|午间|盘前)$/.test(raw);
}

function intradayPnlRefreshEnabled(snapshot, quotePayload) {
  const marketState = quoteMarketState(quotePayload);
  if (marketState) return isActiveQuoteMarketState(quotePayload);
  if (!snapshot) return true;
  return !isNonTradingPhase(snapshot.marketPhase);
}

function intradayPnlPollIntervalMs(snapshot, quotePayload) {
  return intradayPnlRefreshEnabled(snapshot, quotePayload)
    ? INTRADAY_PNL_INTERVAL_MS
    : INTRADAY_PNL_INACTIVE_CHECK_MS;
}

function dailyPnlHistoryRefreshEnabled(quotePayload) {
  return isActiveQuoteMarketState(quotePayload);
}

function dailyPnlHistoryPollIntervalMs() {
  return DAILY_PNL_HISTORY_INTERVAL_MS;
}

function renderRisk() {
  const root = rootObject(state.riskPayload);
  const metrics = asObject(root.metrics);
  const quality = asObject(root.data_quality || root.dataQuality);
  const accountSnapshot = asObject(quality.account_snapshot || quality.accountSnapshot);
  const loaded = Boolean(state.riskPayload);
  const riskState = riskPanelStatePresentation(root);

  els.riskState.className = `risk-state ${riskState.className}`;
  els.riskState.textContent = state.riskLoading && !loaded ? "计算中" : riskState.label;

  const generatedAt = extractTimestamp(state.riskPayload);
  const cache = asObject(root.cache);
  const cacheAge = firstNumber(pickNumber(cache, ["age_seconds", "ageSeconds"]));
  const generatedParts = [];
  if (generatedAt) generatedParts.push(formatDateTime(generatedAt));
  if (isFiniteNumber(cacheAge)) generatedParts.push(`缓存 ${compactAge(cacheAge)}`);
  if (state.riskError) generatedParts.push("连接异常");
  els.riskGenerated.textContent = generatedParts.length ? `风险快照 ${generatedParts.join(" · ")}` : "风险快照 --";

  const snapshotDate = pick(accountSnapshot, ["as_of", "date", "snapshot_date", "snapshotDate"])
    || pick(quality, ["account_snapshot_date", "accountSnapshotDate", "portfolio_as_of", "portfolioAsOf"])
    || pick(metrics, ["account_snapshot_date", "snapshot_date", "portfolio_as_of"])
    || pick(root, ["account_snapshot_date", "portfolio_as_of", "as_of"]);
  const snapshotVerified = firstBoolean(
    pick(accountSnapshot, ["current_verified", "currentVerified", "verified", "is_verified", "isVerified", "visually_verified"]),
    pick(quality, ["account_snapshot_verified", "snapshot_verified", "portfolio_verified"]),
    pick(root, ["account_snapshot_verified", "snapshot_verified"]),
  );
  const snapshotStale = firstBoolean(
    pick(accountSnapshot, ["stale", "is_stale", "isStale", "expired"]),
  );
  const verificationText = snapshotVerified == null
    ? snapshotStale === true ? "时效未验证" : "待计算"
    : snapshotVerified ? "已验证" : "未验证";
  const snapshotText = snapshotDate
    ? `${formatDateOnly(snapshotDate)} · ${verificationText}`
    : "待计算";

  const newBuyAllowed = firstBoolean(root.new_buy_allowed, root.newBuyAllowed);
  const newBuyText = newBuyAllowed == null ? "待计算" : newBuyAllowed ? "允许（下单前复核）" : "已冻结";
  const qualityStatus = pick(quality, ["status", "state", "level"]);
  const qualityScore = firstNumber(pickNumber(quality, ["score", "confidence"]));
  const qualityText = formatQuality(qualityStatus, qualityScore);
  els.riskContext.replaceChildren(
    riskContextItem("账户快照", snapshotText),
    riskContextItem("新增买入", newBuyText, newBuyAllowed === false ? "risk-text-critical" : ""),
    riskContextItem("数据质量", qualityText),
  );

  const largestPosition = metricDescriptor(metrics.largest_position || metrics.largestPosition, ["name", "symbol", "thscode", "code"]);
  const largestTheme = metricDescriptor(metrics.largest_theme || metrics.largestTheme, ["label", "name", "theme"]);
  const worstScenario = stressScenarioDescriptor(metrics);
  const grossExposure = metricNumber(metrics, ["gross_exposure_pct", "grossExposurePct", "total_exposure_pct", "equity_exposure_pct"]);
  const cashCny = metricNumber(metrics, ["cash_cny", "cashCny", "cash", "cash_amount"]);
  const cashPct = metricNumber(metrics, ["cash_pct", "cashPct", "cash_ratio_pct"]);
  const debtCny = metricNumber(metrics, ["debt_cny", "debtCny", "debt", "liability_cny"]);
  const debtPct = metricNumber(metrics, ["debt_pct", "debtPct", "debt_ratio_pct", "liability_pct"]);
  const largestPositionPct = metricNumber(metrics, ["largest_position_pct", "largestPositionPct", "max_single_position_pct", "maximum_single_position_pct"]);
  const top5Pct = metricNumber(metrics, ["top5_pct", "top5Pct", "top_5_pct", "top5_concentration_pct"]);
  const largestThemePct = metricNumber(metrics, ["largest_theme_pct", "largestThemePct", "max_theme_pct", "theme_concentration_pct"]);
  const volatility = metricNumber(metrics, ["annualized_volatility_pct", "annualizedVolatilityPct", "portfolio_volatility_pct", "volatility_pct"]);
  const expectedShortfall = metricNumber(metrics, ["daily_es_97_5_pct", "dailyEs975Pct", "es_97_5_pct", "expected_shortfall_pct"]);
  const stressLoss = metricNumber(metrics, ["worst_5d_stress_loss_pct", "worst5dStressLossPct", "max_stress_loss_pct", "stress_loss_pct"]);

  els.riskMetrics.replaceChildren(
    riskMetricItem("总敞口", formatRiskPct(grossExposure)),
    riskMetricItem("现金", formatRiskAmountPct(cashCny, cashPct)),
    riskMetricItem("负债", formatRiskAmountPct(debtCny, debtPct)),
    riskMetricItem("最大单票", formatDescriptorPct(largestPosition, largestPositionPct)),
    riskMetricItem("Top5", formatRiskPct(top5Pct)),
    riskMetricItem("最大主题", formatDescriptorPct(largestTheme, largestThemePct)),
    riskMetricItem("年化波动", formatRiskPct(volatility)),
    riskMetricItem("日 ES 97.5%", formatRiskPct(expectedShortfall)),
    riskMetricItem("最差5日压力", formatDescriptorPct(worstScenario, stressLoss)),
  );

  const breaches = normalizeRiskItems(root.breaches, "breach");
  const actions = normalizeRiskItems(root.actions, "action");
  const sortedBreaches = breaches.sort((a, b) => b.rank - a.rank || a.order - b.order);
  const sortedActions = actions.sort((a, b) => b.rank - a.rank || a.order - b.order);
  const actionReserve = Math.min(2, sortedActions.length);
  const selectedBreaches = sortedBreaches.slice(0, 8 - actionReserve);
  const selectedActions = sortedActions.slice(0, 8 - selectedBreaches.length);
  renderRiskList(els.riskBreaches, selectedBreaches, "breach", loaded);
  renderRiskList(els.riskActions, selectedActions, "action", loaded);
}

function riskContextItem(label, value, className = "") {
  const item = div("risk-context-item");
  const strong = document.createElement("strong");
  strong.textContent = value || "待计算";
  strong.title = value || "待计算";
  if (className) strong.className = className;
  item.append(span(label), strong);
  return item;
}

function riskMetricItem(label, value) {
  const item = div("risk-metric");
  const strong = document.createElement("strong");
  strong.textContent = value || "待计算";
  strong.title = value || "待计算";
  item.append(span(label), strong);
  return item;
}

function renderRiskList(container, items, kind, loaded) {
  container.replaceChildren();
  if (!items.length) {
    const text = !loaded ? "待计算" : kind === "breach" ? "未发现规则违规" : "暂无新增行动";
    container.append(span(text, "muted"));
    return;
  }
  for (const item of items) {
    const row = div(`risk-list-item ${item.className}`);
    const badge = span(item.badge, "risk-severity");
    const content = div("risk-list-content");
    const title = span(item.title, "risk-list-title");
    title.title = item.title;
    content.append(title);
    if (item.detail) {
      const detail = span(item.detail, "risk-list-detail");
      detail.title = item.detail;
      content.append(detail);
    }
    row.append(badge, content);
    container.append(row);
  }
}

function renderDecisionModules() {
  if (state.activePortfolioId !== MAIN_PORTFOLIO_ID) {
    // 决策引擎目前只绑定主组合配置；自定义组合显示说明而不是主组合的建议。
    renderMarketState();
    const note = "决策模块（事件/机会/再平衡）当前仅覆盖主组合";
    for (const key of ["eventsList", "opportunitiesList", "rebalanceList"]) {
      if (els[key]) {
        els[key].replaceChildren();
        els[key].append(emptyNode(note, "compact-decision"));
      }
    }
    for (const key of ["eventsMeta", "opportunitiesMeta", "rebalanceMeta"]) {
      if (els[key]) els[key].textContent = "主组合专属 · 切换到主组合查看";
    }
    return;
  }
  renderDecisionMetadata();
  renderMarketState();
  renderEvents();
  renderOpportunities();
  renderRebalance();
}

function renderDecisionMetadata() {
  const root = rootObject(state.decisionPayload);
  const asOf = asObject(root.as_of || root.asOf);
  const quality = asObject(root.data_quality || root.dataQuality);
  const snapshotId = String(pick(root, ["snapshot_id", "snapshotId"]) || "");
  let text = "统一快照不可用 · 时点不可比对 · 质量 受限";
  let title = text;
  if (snapshotId) {
    const parts = [`快照 ${snapshotId}`];
    const account = pick(asOf, ["account", "portfolio", "account_snapshot"]);
    const quote = pick(asOf, ["quote", "quotes", "market"]);
    const completed = pick(asOf, ["completed_session", "completedSession", "daily"]);
    if (account) parts.push(`账户 ${compactDecisionTime(account, true)}`);
    if (quote) parts.push(`行情 ${compactDecisionTime(quote)}`);
    if (completed) parts.push(`日线 ${compactDecisionTime(completed, true)}`);
    parts.push(`质量 ${decisionQualityLabel(quality)}`);
    text = parts.join(" · ");
    title = [
      `snapshot_id: ${snapshotId}`,
      ...Object.entries(asOf).map(([key, value]) => `${key}: ${String(value)}`),
      `data_quality: ${decisionQualityLabel(quality)}`,
    ].join("\n");
  }
  for (const element of [els.marketStateMeta, els.eventsMeta, els.opportunitiesMeta, els.rebalanceMeta]) {
    element.textContent = text;
    element.title = title;
  }
}

function renderMarketState() {
  const market = rootObject(state.marketStatePayload);
  if (!Object.keys(market).length) {
    setDecisionBadge(els.marketRegime, state.decisionLoading ? "计算中" : "兼容模式", "is-pending");
    els.marketStateScore.textContent = "得分 -- · 置信度 --";
    els.marketStateStyle.textContent = "风格 --";
    els.marketStateMessage.textContent = "统一市场状态不可用；不得据此新增风险";
    els.marketStateMessage.title = els.marketStateMessage.textContent;
    return;
  }

  const presentation = marketRegimePresentation(pick(market, ["regime", "state"]), market.ok);
  setDecisionBadge(els.marketRegime, pick(market, ["label"]) || presentation.label, presentation.className);
  const score = firstNumber(pickNumber(market, ["score", "aggregate_score"]));
  const confidence = firstNumber(pickNumber(market, ["confidence", "confidence_score"]));
  els.marketStateScore.textContent = `得分 ${isFiniteNumber(score) ? score.toFixed(3) : "--"} · 置信度 ${formatUnitPct(confidence)}`;
  const style = asObject(market.style);
  els.marketStateStyle.textContent = `风格 ${pick(style, ["label", "leader_label", "leaderLabel"]) || "证据不足"}`;
  const guidance = asObject(market.guidance);
  const reasons = (Array.isArray(market.reason_codes) ? market.reason_codes : [])
    .map((item) => typeof item === "string" ? item : pick(asObject(item), ["text", "label", "code"]))
    .filter(Boolean)
    .slice(0, 3);
  const message = [pick(guidance, ["message"]), reasons.length ? reasons.join("、") : ""].filter(Boolean).join(" · ")
    || "市场状态已计算；仍需以完成交易日收盘数据复核";
  els.marketStateMessage.textContent = message;
  els.marketStateMessage.title = message;
}

function renderEvents() {
  const events = rootObject(state.eventsPayload);
  const raw = [events.alerts, events.events, events.items].find(Array.isArray) || [];
  const rows = raw.map((item, index) => normalizeDecisionEvent(item, index))
    .sort((a, b) => b.rank - a.rank || timestampValue(b.time) - timestampValue(a.time))
    .slice(0, 7);
  els.eventsList.replaceChildren();

  if (!rows.length) {
    const inputErrors = Array.isArray(events.input_errors) ? events.input_errors : [];
    const verifiedEmpty = Object.keys(events).length && !inputErrors.length && decisionDataTrusted();
    setDecisionBadge(
      els.eventsState,
      verifiedEmpty ? "未见高优先事件" : state.decisionLoading ? "监控中" : "证据不足",
      verifiedEmpty ? "is-ok" : "is-blocked",
    );
    els.eventsList.append(emptyNode(
      verifiedEmpty ? "当前统一快照未检出重要事件" : "事件证据不足；空列表不能解释为没有风险",
      "compact-decision",
    ));
    return;
  }

  const highCount = rows.filter((row) => row.rank >= 3).length;
  setDecisionBadge(els.eventsState, highCount ? `${highCount} 条高优先` : `${rows.length} 条待核验`, highCount ? "is-blocked" : "is-warning");
  for (const item of rows) {
    const row = div(`decision-row ${item.className}`);
    const heading = div("decision-row-heading");
    const title = safeLink(item.url, item.title, "decision-row-title");
    title.title = item.title;
    heading.append(span(item.badge, "mini-severity"), title);
    const metadata = [
      item.symbols.map(symbolName).slice(0, 3).join("、"),
      item.source,
      item.time ? formatDateTime(item.time) : "",
      isFiniteNumber(item.confidence) ? `置信 ${formatUnitPct(item.confidence)}` : "",
    ].filter(Boolean).join(" · ");
    row.append(heading, span(metadata || "来源待核验", "decision-row-meta"));
    if (item.action) {
      const action = span(`复核：${item.action}`, "decision-row-detail");
      action.title = item.action;
      row.append(action);
    }
    els.eventsList.append(row);
  }
}

function renderOpportunities() {
  const radar = rootObject(state.opportunitiesPayload);
  const rows = (Array.isArray(radar.items) ? radar.items : Array.isArray(radar.opportunities) ? radar.opportunities : [])
    .map(normalizeOpportunity)
    .slice(0, 7);
  const trusted = decisionDataTrusted();
  const gateOpen = radar.new_buy_gate_open === true && trusted && radar.watchlist_stale !== true;
  els.opportunitiesList.replaceChildren();
  setDecisionBadge(
    els.opportunitiesState,
    gateOpen ? "条件信号可核验" : "仅观察",
    gateOpen ? "is-ok" : "is-warning",
  );

  if (!rows.length) {
    els.opportunitiesList.append(emptyNode(
      Object.keys(radar).length ? "当前没有观察信号；不代表适合买入" : "机会雷达不可用；不得用旧行情推断触发",
      "compact-decision",
    ));
    return;
  }

  for (const item of rows) {
    const confirmed = item.eligible && gateOpen;
    const row = div(`decision-row opportunity-row ${confirmed ? "is-confirmed" : ""}`);
    const heading = div("decision-row-heading");
    const name = span(item.name || item.symbol || "未命名标的", "decision-row-title");
    name.title = `${item.name || ""} ${item.symbol || ""}`.trim();
    heading.append(
      span(confirmed ? "条件确认·非指令" : item.stateLabel, `mini-state ${confirmed ? "is-ok" : ""}`),
      name,
      span(`${formatPrice(item.price)}  ${formatPct(item.changePct)}`, directionClass(item.changePct)),
    );
    const trigger = opportunityTriggerText(item);
    const blocker = item.blockers.length ? `门禁：${item.blockers.slice(0, 2).map(translateDecisionCode).join("、")}` : "";
    const metadata = [item.theme, trigger, blocker].filter(Boolean).join(" · ");
    row.append(heading, span(metadata || "等待完成交易日信号", "decision-row-meta"));
    els.opportunitiesList.append(row);
  }
}

function renderRebalance() {
  const plan = rootObject(state.rebalancePayload);
  const qualityTrusted = decisionDataTrusted();
  const gate = asObject(plan.gate);
  const verified = qualityTrusted && gate.verified !== false && !/unverified/i.test(String(plan.status || ""));
  const priority = Array.isArray(plan.priority_draft) ? plan.priority_draft : [];
  const adjustments = Array.isArray(plan.adjustments) ? plan.adjustments : [];
  const unsafe = !verified;
  els.rebalanceList.replaceChildren();

  if (!Object.keys(plan).length) {
    setDecisionBadge(els.rebalanceState, "不可自动执行", "is-blocked");
    const fallbackActions = normalizeRiskItems(asObject(rootObject(state.riskPayload)).actions, "action").slice(0, 5);
    if (fallbackActions.length) {
      for (const item of fallbackActions) {
        const row = div("decision-row is-warning");
        row.append(span("兼容模式", "mini-state"), span(item.title, "decision-row-title"));
        if (item.detail) row.append(span(item.detail, "decision-row-meta"));
        els.rebalanceList.append(row);
      }
    } else {
      els.rebalanceList.append(emptyNode("统一草案不可用；不生成目标权重、金额或股数", "compact-decision"));
    }
    els.rebalanceNote.textContent = "兼容模式下各数据时点不可比对；本页不会生成或提交委托。";
    return;
  }

  setDecisionBadge(
    els.rebalanceState,
    unsafe ? "账户未核验 · 只读" : plan.executable === true ? "已核验 · 仍需人工复核" : "条件草案 · 人工复核",
    unsafe ? "is-blocked" : "is-warning",
  );

  if (unsafe) {
    for (const item of priority.slice(0, 7)) {
      const data = asObject(item);
      const title = `${pick(data, ["rank"]) ? `#${pick(data, ["rank"])} ` : ""}${pick(data, ["name", "thscode", "symbol"]) || "未命名持仓"}`;
      const reasons = arrayText(data.reasons || data.risk_flags).slice(0, 2).join("、");
      const row = div("decision-row is-warning");
      row.append(
        span("减险优先级", "mini-state"),
        span(title, "decision-row-title"),
        span(reasons || "等待人工核验风险证据", "decision-row-meta"),
      );
      els.rebalanceList.append(row);
    }
    if (!priority.length) els.rebalanceList.append(emptyNode("账户门禁未通过；未生成目标权重、金额或股数", "compact-decision"));
  } else {
    const actionable = [...adjustments]
      .sort((a, b) => rebalanceDirectionRank(pick(asObject(a), ["direction"])) - rebalanceDirectionRank(pick(asObject(b), ["direction"])))
      .filter((item) => !/hold/i.test(String(pick(asObject(item), ["direction"]) || "")))
      .slice(0, 7);
    for (const item of actionable) {
      const data = asObject(item);
      const direction = String(pick(data, ["direction"]) || "HOLD").toUpperCase();
      const badge = direction === "REDUCE" ? "减仓复核" : direction === "ADD" ? "条件增配·非指令" : "观察";
      const amount = firstNumber(pickNumber(data, ["adjustment_cny", "adjustmentCny"]));
      const current = firstNumber(pickNumber(data, ["current_weight_pct", "currentWeightPct"]));
      const target = firstNumber(pickNumber(data, ["target_weight_pct", "targetWeightPct"]));
      const detail = [
        isFiniteNumber(current) && isFiniteNumber(target) ? `${formatRiskPct(current)} → ${formatRiskPct(target)}` : "",
        isFiniteNumber(amount) ? formatAmount(amount, true) : "",
        arrayText(data.reason_codes).slice(0, 2).map(translateDecisionCode).join("、"),
      ].filter(Boolean).join(" · ");
      const row = div(`decision-row ${direction === "REDUCE" ? "is-critical" : "is-warning"}`);
      row.append(span(badge, "mini-state"), span(pick(data, ["name", "thscode"]) || "未命名持仓", "decision-row-title"), span(detail, "decision-row-meta"));
      els.rebalanceList.append(row);
    }
    if (!actionable.length) els.rebalanceList.append(emptyNode("当前草案没有越过再平衡带的调整；仍需复核风险", "compact-decision"));
  }

  const warnings = arrayText(plan.warnings).slice(0, 2);
  els.rebalanceNote.textContent = [
    unsafe ? "账户或数据门禁未通过，页面只展示去风险优先级，不展示可执行委托。" : "这是条件草案；本页不连接券商，任何动作均需人工复核。",
    ...warnings,
  ].join(" ");
}

function normalizeDecisionEvent(raw, index) {
  const item = asObject(raw);
  const severity = decisionSeverity(pick(item, ["severity", "level", "priority"]));
  const symbols = pick(item, ["affected_symbols", "symbols", "thscodes", "codes"]);
  return {
    order: index,
    ...severity,
    title: String(pick(item, ["title", "message", "headline", "category"]) || "未命名事件"),
    action: String(pick(item, ["recommended_action", "recommendedAction", "action", "suggestion"]) || ""),
    source: String(pick(item, ["source", "provider"]) || ""),
    time: pick(item, ["evidence_time", "event_time", "published_at", "time", "observed_at"]),
    confidence: firstNumber(pickNumber(item, ["confidence", "score"])),
    symbols: (Array.isArray(symbols) ? symbols : symbols ? [symbols] : []).map(normalizeSymbol).filter(Boolean),
    url: String(pick(item, ["evidence_url", "url", "link"]) || ""),
  };
}

function normalizeOpportunity(raw) {
  const item = asObject(raw);
  const zone = Array.isArray(item.pullback_zone) ? item.pullback_zone : [];
  const blockers = pick(item, ["blocked_by", "blockers", "gates"]);
  return {
    symbol: normalizeSymbol(pick(item, ["thscode", "symbol", "code"])),
    name: String(pick(item, ["name", "stock_name"]) || ""),
    theme: String(pick(item, ["theme", "industry", "sector"]) || ""),
    state: String(pick(item, ["state", "status"]) || "WATCH").toUpperCase(),
    stateLabel: String(pick(item, ["state_label", "stateLabel", "label"]) || opportunityStateLabel(pick(item, ["state", "status"]))),
    eligible: item.eligible === true,
    price: firstNumber(pickNumber(item, ["price", "last_price"])),
    changePct: firstNumber(pickNumber(item, ["change_pct", "changePct"])),
    breakout: firstNumber(pickNumber(item, ["breakout", "breakout_price"])),
    invalidation: firstNumber(pickNumber(item, ["invalidation", "invalidation_price"])),
    pullbackLow: firstNumber(toNumber(zone[0]), pickNumber(item, ["pullback_low"])),
    pullbackHigh: firstNumber(toNumber(zone[1]), pickNumber(item, ["pullback_high"])),
    blockers: arrayText(blockers),
  };
}

function decisionDataTrusted() {
  const root = rootObject(state.decisionPayload);
  const quality = asObject(root.data_quality || root.dataQuality);
  const status = String(pick(quality, ["status", "state", "level"]) || "").toUpperCase();
  const errors = Array.isArray(quality.errors) ? quality.errors : [];
  return Boolean(
    pick(root, ["snapshot_id", "snapshotId"])
    && root.ok !== false
    && root.decision_ready === true
    && quality.account_current === true
    && quality.quote_fresh === true
    && !/CRITICAL|FAILED|UNVERIFIED|UNAVAILABLE/.test(status)
    && !errors.length
  );
}

function decisionQualityLabel(quality) {
  const status = String(pick(quality, ["status", "state", "level"]) || "未知");
  let label = status;
  if (/good|healthy|ok|normal/i.test(status)) label = "可用";
  else if (/degraded|partial|warning|limited/i.test(status)) label = "受限";
  else if (/critical|failed|unverified|unavailable/i.test(status)) label = "严重";
  if (quality.account_current === false) label += " / 账户过期";
  if (quality.quote_fresh === false) label += " / 行情过期";
  return label;
}

function compactDecisionTime(value, dateOnly = false) {
  const raw = String(value || "");
  if (dateOnly && /^\d{4}-\d{2}-\d{2}/.test(raw)) return raw.slice(0, 10);
  const date = parseDate(value);
  if (!date) return raw || "--";
  const datePart = `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  if (dateOnly) return datePart;
  const timePart = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  return `${datePart} ${timePart}`;
}

function marketRegimePresentation(value, ok) {
  const raw = String(value || "").toUpperCase();
  if (raw === "RISK_OFF") return { label: "避险", className: "is-blocked" };
  if (raw === "CAUTIOUS") return { label: "谨慎", className: "is-warning" };
  if (raw === "NEUTRAL") return { label: "中性", className: "is-pending" };
  if (raw === "RISK_ON") return { label: "风险偏好改善", className: "is-ok" };
  return { label: ok === false ? "待核验" : raw || "待计算", className: ok === false ? "is-blocked" : "is-pending" };
}

function decisionSeverity(value) {
  const raw = String(value || "").toLowerCase();
  if (/critical|severe|p0|严重/.test(raw)) return { rank: 4, badge: "严重", className: "is-critical" };
  if (/high|p1|高/.test(raw)) return { rank: 3, badge: "高", className: "is-high" };
  if (/medium|warning|warn|p2|中|警/.test(raw)) return { rank: 2, badge: "警示", className: "is-warning" };
  return { rank: 1, badge: "提示", className: "is-info" };
}

function opportunityStateLabel(value) {
  const labels = {
    INVALIDATED: "失效", RISK_REDUCTION: "减险观察", PULLBACK_ZONE: "回撤区",
    BREAKOUT_PENDING: "突破待确认", CONFIRMED: "收盘确认", WATCH: "观察",
    DATA_UNVERIFIED: "数据待核验",
  };
  return labels[String(value || "WATCH").toUpperCase()] || String(value || "观察");
}

function opportunityTriggerText(item) {
  const parts = [];
  if (isFiniteNumber(item.pullbackLow) && isFiniteNumber(item.pullbackHigh)) parts.push(`回撤 ${formatPrice(item.pullbackLow)}–${formatPrice(item.pullbackHigh)}`);
  if (isFiniteNumber(item.breakout)) parts.push(`突破 ${formatPrice(item.breakout)}`);
  if (isFiniteNumber(item.invalidation)) parts.push(`失效 ${formatPrice(item.invalidation)}`);
  return parts.join(" / ");
}

function translateDecisionCode(value) {
  const raw = String(value || "");
  const labels = {
    RISK_SNAPSHOT_UNVERIFIED: "风险快照未核验",
    PORTFOLIO_NEW_BUY_GATE_CLOSED: "组合新增风险门禁关闭",
    ACCOUNT_SNAPSHOT_NOT_CURRENT: "账户快照过期",
    MARKET_STATE_UNVERIFIED: "市场状态未核验",
    MARKET_REGIME_CAUTIOUS: "市场处于谨慎状态",
    MARKET_REGIME_RISK_OFF: "市场处于避险状态",
    LIVE_QUOTE_MISSING_OR_STALE: "实时行情缺失或过期",
    OFFICIAL_QUOTE_VALIDATION_FAILED: "授权行情交叉校验未通过",
    WATCHLIST_REVIEW_EXPIRED: "观察名单待复核",
  };
  if (labels[raw]) return labels[raw];
  if (/^STATE_/.test(raw)) return opportunityStateLabel(raw.slice(6));
  return raw.replaceAll("_", " ");
}

function formatUnitPct(value) {
  if (!isFiniteNumber(value)) return "--";
  const normalized = Math.abs(value) <= 1 ? value * 100 : value;
  return `${normalized.toFixed(0)}%`;
}

function arrayText(value) {
  if (Array.isArray(value)) return value.map((item) => {
    if (typeof item === "string") return item;
    return String(pick(asObject(item), ["message", "text", "reason", "code", "title"]) || "");
  }).filter(Boolean);
  if (value == null || value === "") return [];
  return [String(value)];
}

function rebalanceDirectionRank(value) {
  const raw = String(value || "").toUpperCase();
  return raw === "REDUCE" ? 0 : raw === "ADD" ? 1 : 2;
}

function setDecisionBadge(element, text, className) {
  element.className = `decision-badge ${className || "is-pending"}`;
  element.textContent = text;
}

const POSITION_INTRADAY_TTL_MS = 150_000;
const POSITION_INTRADAY_SPARK_W = 80;
const POSITION_INTRADAY_SPARK_H = 28;
const POSITION_INTRADAY_STAGGER_MS = 8_000;

function renderPositionIntradaySparkline(svg, items, prevClose) {
  svg.replaceChildren();
  if (!items || !items.length) {
    svg.setAttribute("hidden", "");
    return false;
  }
  const w = POSITION_INTRADAY_SPARK_W;
  const h = POSITION_INTRADAY_SPARK_H;
  const pad = 2;
  const allPrices = items.flatMap((item) => [item.low, item.high]);
  if (isFiniteNumber(prevClose) && prevClose > 0) allPrices.push(prevClose);
  const lo = Math.min(...allPrices);
  const hi = Math.max(...allPrices);
  if (!isFiniteNumber(lo) || !isFiniteNumber(hi) || lo <= 0 || hi <= 0) {
    svg.setAttribute("hidden", "");
    return false;
  }
  const range = hi - lo || Math.max(Math.abs(hi) * 0.001, 0.01);
  const padded = { min: lo - range * 0.08, max: hi + range * 0.08 };
  const plotW = w - pad * 2;
  const plotH = h - pad * 2;

  const projected = [];
  for (const item of items) {
    const pos = intradayPnlAxisPosition(item.timeValue);
    if (!pos) continue;
    projected.push({ ...item, plotMinute: pos.plotTimeValue / 60_000 });
  }
  if (!projected.length) {
    svg.setAttribute("hidden", "");
    return false;
  }
  const STOCK_INTRADAY_TOTAL_MINUTES_SPARK = 240;
  const y = (val) => pad + (padded.max - val) / (padded.max - padded.min) * plotH;
  const x = (minute) => pad + minute / STOCK_INTRADAY_TOTAL_MINUTES_SPARK * plotW;

  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);

  if (isFiniteNumber(prevClose) && prevClose >= padded.min && prevClose <= padded.max) {
    svg.append(svgNode("line", {
      x1: pad, x2: w - pad, y1: y(prevClose), y2: y(prevClose),
      class: "stock-chart-reference", "stroke-dasharray": "2 2",
    }));
  }

  const latest = projected[projected.length - 1];
  const direction = isFiniteNumber(prevClose)
    ? directionClass(latest.close - prevClose)
    : directionClass(latest.close - projected[0].close);

  if (projected.length === 1) {
    svg.append(svgNode("circle", {
      cx: x(projected[0].plotMinute), cy: y(projected[0].close), r: 1.2,
      class: `stock-intraday-dot ${direction}`,
    }));
  } else {
    svg.append(svgNode("path", {
      d: projected.map((item, i) => `${i ? "L" : "M"}${x(item.plotMinute).toFixed(2)},${y(item.close).toFixed(2)}`).join(" "),
      class: `stock-intraday-line ${direction}`,
      "stroke-width": "1",
      fill: "none",
    }));
  }

  svg.append(svgNode("circle", {
    cx: x(latest.plotMinute), cy: y(latest.close), r: 1.5,
    class: `stock-intraday-dot ${direction}`,
  }));

  const title = svgNode("title");
  title.textContent = `最新 ${formatPrice(latest.close)}（${direction === "up" ? "涨" : direction === "down" ? "跌" : "平"}）`;
  svg.append(title);
  svg.removeAttribute("hidden");
  return true;
}

async function fetchPositionIntraday(symbol) {
  const cached = state.positionIntradayCache.get(symbol);
  if (cached && Date.now() - cached.receivedAt <= POSITION_INTRADAY_TTL_MS) {
    return cached;
  }
  let pending = state.positionIntradayPending.get(symbol);
  if (!pending) {
    pending = fetchJson(`/api/stock-chart?symbol=${encodeURIComponent(symbol)}`);
    state.positionIntradayPending.set(symbol, pending);
  }
  try {
    const payload = await pending;
    if (payload?.ok === false) throw new Error(payload.message || "fetch failed");
    const chart = normalizeStockChart(payload);
    const result = {
      receivedAt: Date.now(),
      items: chart.intraday.items,
      prevClose: chart.intraday.prevClose,
    };
    state.positionIntradayCache.set(symbol, result);
    return result;
  } catch {
    return null;
  } finally {
    if (state.positionIntradayPending.get(symbol) === pending) {
      state.positionIntradayPending.delete(symbol);
    }
  }
}

function paintPositionIntradayCell(cell, data) {
  const svg = cell.querySelector("svg.position-intraday-spark");
  const placeholder = cell.querySelector(".position-intraday-placeholder");
  if (!svg || !placeholder) return;
  const ok = renderPositionIntradaySparkline(svg, data.items, data.prevClose);
  placeholder.hidden = ok;
  if (!ok) placeholder.textContent = "—";
}

function renderPositionIntradayCell(symbol, rowIndex = 0) {
  const cell = document.createElement("td");
  cell.className = "position-intraday-cell";
  cell.dataset.symbol = symbol;
  const svg = svgNode("svg", { class: "position-intraday-spark", viewBox: `0 0 ${POSITION_INTRADAY_SPARK_W} ${POSITION_INTRADAY_SPARK_H}` });
  svg.setAttribute("hidden", "");
  const placeholder = span("··", "muted position-intraday-placeholder");
  cell.append(svg, placeholder);

  const cached = state.positionIntradayCache.get(symbol);
  if (cached && Date.now() - cached.receivedAt <= POSITION_INTRADAY_TTL_MS) {
    paintPositionIntradayCell(cell, cached);
  } else {
    // 只要还有分时未显示的标的，renderPositions 会一次性并发预取这些标的；
    // 这里若有 pending 请求则直接复用（不延迟），让分时尽快显示。全部可见
    // 后的周期性刷新才走逐行错峰（renderPositions 不再发起并发预取）。
    const pending = state.positionIntradayPending.get(symbol);
    const delay = pending ? 0 : rowIndex * POSITION_INTRADAY_STAGGER_MS;
    setTimeout(() => {
      if (!cell.isConnected) return;
      void (async () => {
        const data = await fetchPositionIntraday(symbol);
        if (!data) {
          if (cell.isConnected) placeholder.textContent = "—";
          return;
        }
        // 持仓表每次行情刷新都会整表重建，发起请求的单元格可能已被替换；
        // 因此数据到达后绘制当前 DOM 中该股的全部分时单元格。
        document
          .querySelectorAll(`#positions-body .position-intraday-cell[data-symbol="${symbol}"]`)
          .forEach((mounted) => paintPositionIntradayCell(mounted, data));
      })();
    }, delay);
  }
  return cell;
}

function renderPositions() {
  const focusedChartSymbol = document.activeElement?.matches?.(".stock-chart-trigger")
    ? normalizeChartSymbol(document.activeElement.dataset.symbol)
    : "";
  let replacementFocus = null;
  const unsortedPositions = resolvedPositions();
  const account = accountSnapshotDisplay();
  const schoolMap = new Map(
    ((state.schoolSignalPayload && state.schoolSignalPayload.stocks) || [])
      .filter((stock) => stock && stock.symbol)
      .map((stock) => [String(stock.symbol), stock]),
  );
  const totalMarketValue = sum(unsortedPositions.filter((item) => isFiniteNumber(item.marketValue)).map((item) => item.marketValue));
  const positions = stableSortPositions(unsortedPositions, state.positionSortKey, state.positionSortDirection, totalMarketValue);
  updatePositionSortIndicators();
  els.positionsBody.replaceChildren();
  els.positionsCount.textContent = positions.length ? String(positions.length) : "--";
  els.positionsAccountState.textContent = `${account.label} · 信号由本地规则引擎计算 · 点击行筛选新闻`;

  if (!positions.length) {
    const row = document.createElement("tr");
    const cell = td(state.quoteError ? "持仓行情暂时不可用" : "正在获取持仓行情…", "empty");
    cell.colSpan = 15;
    row.append(cell);
    els.positionsBody.append(row);
    renderTPlanSection();
    if (!els.stockChartPopover.hidden) closeStockChart();
    return;
  }

  // 分时优先：只要还有任何持仓的分时未显示（缓存缺失或过期），就一次性并发
  // 预取这些标的，数据到达即绘制对应单元格；全部可见后，才走单元格内的逐行
  // 错峰机制进行周期性刷新。fetchPositionIntraday 自带 pending 去重，单元格
  // 渲染时发起的 setTimeout 请求会复用这里已经发起的同一个 Promise。
  const now = Date.now();
  const missingIntradaySymbols = positions
    .map((item) => item.symbol)
    .filter((symbol) => {
      const cached = state.positionIntradayCache.get(symbol);
      return !cached || now - cached.receivedAt > POSITION_INTRADAY_TTL_MS;
    });
  if (missingIntradaySymbols.length) {
    void (async () => {
      await Promise.allSettled(
        missingIntradaySymbols.map((symbol) =>
          fetchPositionIntraday(symbol).then((data) => {
            if (!data) return;
            document
              .querySelectorAll(`#positions-body .position-intraday-cell[data-symbol="${symbol}"]`)
              .forEach((mounted) => paintPositionIntradayCell(mounted, data));
          }),
        ),
      );
      renderPositions();
    })();
  }

  for (let positionIndex = 0; positionIndex < positions.length; positionIndex++) {
    const item = positions[positionIndex];
    try {
        const signals = evaluatePositionSignals(item, totalMarketValue);
        const worstLevel = signals.length
          ? signals.reduce((worst, signal) => SIGNAL_LEVEL_ORDER[signal.level] < SIGNAL_LEVEL_ORDER[worst] ? signal.level : worst, signals[0].level)
          : "";
        const row = document.createElement("tr");
        row.className = `position-row${isFiniteNumber(item.price) ? "" : " is-missing"}${item.isStale ? " is-stale" : ""}${state.selectedSymbol === item.symbol ? " is-selected" : ""}${worstLevel ? ` signal-${worstLevel}` : ""}`;
        row.dataset.symbol = item.symbol;
        row.title = `点击行筛选${item.name}新闻`;

        const stock = document.createElement("td");
        stock.className = "stock-cell";
        const stockTrigger = document.createElement("button");
        stockTrigger.type = "button";
        stockTrigger.className = "stock-chart-trigger";
        stockTrigger.dataset.symbol = item.symbol;
        stockTrigger.setAttribute("aria-controls", "stock-chart-popover");
        stockTrigger.setAttribute("aria-haspopup", "dialog");
        stockTrigger.setAttribute("aria-expanded", state.stockChartSymbol === item.symbol && !els.stockChartPopover.hidden ? "true" : "false");
        stockTrigger.setAttribute("aria-label", `查看${item.name}的日K线和当日分时图`);
        stockTrigger.title = "悬停预览；点击固定图表并筛选相关新闻";
        if (focusedChartSymbol === item.symbol) replacementFocus = stockTrigger;
        stockTrigger.append(span(item.symbol, "stock-code-strong"), span(item.name, "stock-name"));
        stock.append(stockTrigger);
        if (state.stockChartSymbol === item.symbol && !els.stockChartPopover.hidden) {
          state.stockChartAnchor = stockTrigger;
        }
        const signalsCell = td("", "position-signals");
        if (signals.length) {
          const ordered = signals.slice().sort((left, right) => SIGNAL_LEVEL_ORDER[left.level] - SIGNAL_LEVEL_ORDER[right.level]);
          for (const signal of ordered.slice(0, 3)) {
            const text = `${signal.label}${signal.detail ? ` ${signal.detail}` : ""}`;
            const chip = span(text, `signal-chip is-${signal.level}`);
            chip.title = text;
            signalsCell.append(chip);
          }
          if (ordered.length > 3) {
            const more = span(`+${ordered.length - 3}`, "signal-chip is-more");
            more.title = ordered.slice(3)
              .map((signal) => `${signal.label}${signal.detail ? ` ${signal.detail}` : ""}`)
              .join("\n");
            signalsCell.append(more);
          }
        } else {
          signalsCell.append(span("—", "muted"));
        }
        const line = state.priceLines[item.symbol];
        const suggestion = state.technicalSignalsBySymbol.get(String(item.symbol))?.suggested_lines || null;
        const linesCell = td("", "position-lines");
        if (line) {
          linesCell.textContent = formatPriceLinesText(line);
          if (line.note) linesCell.title = line.note;
        } else if (suggestion) {
          for (const [label, value] of [["止损", suggestion.stop], ["加仓", suggestion.buy], ["止盈", suggestion.sell]]) {
            linesCell.append(span(`${label} ${value ?? "--"}`, "price-line-suggestion"));
          }
          linesCell.title = `系统建议（ATR14≈${suggestion.atr14 ?? "--"}）\n${(suggestion.basis || []).join("\n")}\n⚙ 可修改后保存为自己的价格线`;
          linesCell.classList.add("is-suggested");
        } else {
          linesCell.textContent = "--";
        }
        const gearCell = td("", "position-lines-action");
        const gearButton = document.createElement("button");
        gearButton.type = "button";
        gearButton.className = "lines-gear";
        gearButton.dataset.symbol = item.symbol;
        gearButton.textContent = "⚙";
        gearButton.setAttribute("aria-haspopup", "dialog");
        gearButton.setAttribute("aria-label", `设置${item.name}的止损/加仓/止盈价格线`);
        gearButton.title = "设置止损/加仓/止盈线";
        gearCell.append(gearButton);
        const marketValueCell = td(formatAmount(item.marketValue), "numeric sensitive");
        if (account.current !== true) marketValueCell.title = `按 ${account.date} 快照数量与页面行情估算`;
        // 做T参考不再占用持仓表列位，改为页面底部「做T 决策中心」统一呈现。
        // 流派信号列：整合自「三大流派信号 · 重仓股」卡片，紧凑芯片 + 悬停明细
        const schoolCell = td("", "position-schools");
        const schoolStock = schoolMap.get(String(item.symbol));
        if (schoolStock) {
          for (const key of SCHOOL_ORDER) {
            const s = (schoolStock.schools || {})[key];
            if (!s) continue;
            const chip = span(
              `${SCHOOL_SHORT_LABELS[key] || key} ${s.holding ? "持仓" : "空仓"}`,
              `school-mini-chip level-${s.level || "avoid"}`,
            );
            chip.title = `${SCHOOL_LABELS[key] || key}：${s.holding ? "持仓中" : "空仓"}${s.switched_today ? " · 今日切换" : ""}${s.hint ? `\n${s.hint}` : ""}`;
            schoolCell.append(chip);
          }
          if (schoolStock.overall === "trigger" || schoolStock.overall === "near") {
            const overallBadge = span(
              SCHOOL_LEVEL_LABELS[schoolStock.overall] || "",
              `school-overall level-${schoolStock.overall}`,
            );
            overallBadge.title = `${schoolStock.holding_votes || 0}/3 流派持仓`;
            schoolCell.append(overallBadge);
          }
        } else {
          schoolCell.append(span("—", "muted"));
        }
        const dayPnlAmountCell = td(formatAmount(item.dayPnl, true), `numeric sensitive ${directionClass(item.dayPnl)}`);
        if (account.current !== true) dayPnlAmountCell.title = `按 ${account.date} 快照数量与当日价格变化估算`;
        row.append(
          stock,
          renderPositionIntradayCell(item.symbol, positionIndex),
          td(item.industry || "--", "muted"),
          td(formatPrice(item.price), `numeric ${directionClass(item.changePct)}`),
          td(formatPct(item.changePct), `numeric ${directionClass(item.changePct)}`),
          dayPnlAmountCell,
          signalsCell,
          schoolCell,
          td(isFiniteNumber(item.turnover) ? item.turnover.toFixed(2) : "--", "numeric"),
          td(isFiniteNumber(item.pe) ? (item.pe < 0 ? "亏" : item.pe.toFixed(1)) : "--", `numeric${isFiniteNumber(item.pe) && item.pe < 0 ? " is-negative-pe" : ""}`),
          td(isFiniteNumber(item.pb) ? item.pb.toFixed(2) : "--", "numeric"),
          positionWeightCell(item, totalMarketValue),
          marketValueCell,
          linesCell,
          gearCell,
        );
        els.positionsBody.append(row);
    } catch (error) {
      // 单行渲染失败只降级该行，绝不允许把整张持仓表清空。
      const fallbackRow = document.createElement("tr");
      fallbackRow.className = "position-row is-missing";
      fallbackRow.dataset.symbol = item.symbol;
      const fallbackCell = td(
        `${item.name ?? ""}（${item.symbol ?? ""}）行渲染失败：${error instanceof Error ? error.message : String(error)}`,
        "empty",
      );
      fallbackCell.colSpan = 15;
      fallbackRow.append(fallbackCell);
      els.positionsBody.append(fallbackRow);
    }
  }

  if (replacementFocus) replacementFocus.focus({ preventScroll: true });

  if (!els.stockChartPopover.hidden) {
    if (state.stockChartAnchor?.isConnected) positionStockChartPopover();
    else closeStockChart();
  }

  // Fire-and-forget: load technical signals for all visible holdings and
  // merge them into the signal column when they arrive. Failures are silent
  // — the local rule chips remain as the fallback view.
  void loadTechnicalSignalsForPositions(positions.map((item) => item.symbol));
  void loadTomorrowPlans();
  // 页面底部「做T 决策中心」的个股做T参考表：持仓或计划变化时重绘（内部按签名去重）。
  renderTPlanSection();
}

function pctLabel(value) {
  return value == null ? "--" : `${(value * 100).toFixed(1)}%`;
}

async function loadTomorrowPlans(force = false) {
  const now = Date.now();
  if (!force && state.tomorrowPlansFetchedAt && now - state.tomorrowPlansFetchedAt < 600_000) {
    return;
  }
  state.tomorrowPlansFetchedAt = now;
  try {
    // 后端首次调用需跑回测与逐股寻优（约 12-15s），用专用长超时
    const payload = await fetchJson("/api/tomorrow-plans", 90_000);
    state.tomorrowPlans = payload && payload.plans ? payload.plans : null;
    state.tomorrowBacktest = payload && payload.backtest ? payload.backtest : null;
    state.tomorrowModelVersion = (payload && payload.model_version) || "";
    state.marketPlan = (payload && payload.market_plan) || null;
    renderPositions();
    updateTomorrowPlanSummary();
    renderTPlanSection(true);
  } catch (_) {
    // 失败时 60s 后即可重试（成功则 10 分钟内不再请求）
    state.tomorrowPlansFetchedAt = now - 540_000;
  }
}

/**
 * 把单只股票的次日做T计划拆成可独立摆放的几块：
 * 倾向徽章（含完整依据的悬停明细）、触发价位文本、胜率文本、财报窗口提示。
 * 页面底部「做T 决策中心 · 个股次日做T参考」按列铺开这几块。
 */
function buildTPlanParts(plan, stockName) {
  if (!plan || plan.ok === false || !plan.kind) return null;
  const chip = span(String(plan.kind).replace(/（.*）/, ""), `t-plan-chip is-${plan.bias}`);
  const detail = [];
  // 按涨跌幅度的分步触发指引（相对前收的百分比 + 绝对价格）
  for (const trigger of plan.triggers || []) {
    const pctText = trigger.move_pct == null ? "" : `${trigger.move_pct > 0 ? "+" : ""}${trigger.move_pct}%`;
    detail.push(`${trigger.label} ${pctText}（${trigger.price}）— ${trigger.action}`);
  }
  const forecastDetail = plan.intraday_forecast;
  const adviceDetail = forecastDetail && forecastDetail.ok !== false ? forecastDetail.advice : null;
  if (adviceDetail && adviceDetail.text) {
    detail.push(`预测分时建议：${adviceDetail.text}（${forecastDetail.direction || ""} · 双触达胜率 ${forecastDetail.hit_rate ?? "--"}%，样本 ${forecastDetail.days ?? "--"} 日）`);
    if (forecastDetail.sell_time_hint) detail.push(`· 预测冲高时段：约 ${forecastDetail.sell_time_hint} 前后触及卖出区`);
    if (forecastDetail.buy_time_hint) detail.push(`· 预测回撤时段：约 ${forecastDetail.buy_time_hint} 前后触及买回区`);
    if (forecastDetail.note) detail.push(`· ${forecastDetail.note}`);
  }
  const fund = plan.fund;
  if (fund && fund.available) {
    const tone = fund.mean_pct_3d > 0 ? "净流入" : "净流出";
    detail.push(`资金面：3日主力${tone}占比 ${fund.mean_pct_3d > 0 ? "+" : ""}${fund.mean_pct_3d}%（最新 ${fund.last_pct > 0 ? "+" : ""}${fund.last_pct}%）`);
  }
  for (const row of plan.unlock || []) {
    detail.push(`解禁提醒：${row.date} 解禁 ${row.free_ratio_pct}%（${row.type}，东财口径）`);
  }
  const earnings = plan.earnings;
  // nw 也被下方披露窗口徽章使用，必须在 if (earnings) 块外声明，
  // 否则触发 ReferenceError 并中断整张持仓表的渲染。
  let nw = null;
  if (earnings) {
    nw = earnings.next_window;
    if (nw) {
      detail.push(`财报：下一期${nw.label}（期末 ${nw.period_end}），披露窗口 ${nw.window_start}~${nw.window_end}${nw.in_window ? " · ⚠ 当前处于披露窗口，财报前后波动加大，谨慎做T" : ""}`);
    }
    const predict = earnings.predict;
    if (predict && predict.content) {
      detail.push(`业绩预告（${predict.notice_date}）：${predict.content}`);
    }
    const report = earnings.latest_report;
    if (report && report.report_date) {
      detail.push(`最新财报：${report.report_date} 净利润同比 ${report.netprofit_yoy_pct == null ? "--" : report.netprofit_yoy_pct + "%"} · 营收同比 ${report.revenue_yoy_pct == null ? "--" : report.revenue_yoy_pct + "%"}`);
    }
  }
  const amp = plan.amplitude;
  if (amp && amp.days) {
    detail.push(`振幅结构（近${amp.days}日）：中位日内振幅 ${amp.median_range}% · 中位回撤 ${amp.median_dip}% / 中位冲高 ${amp.median_rip}%${plan.zone_basis === "percentile" ? " · 区间按振幅分位定价" : ""}`);
  }
  const news = plan.news;
  if (news && news.available) {
    const tone = (news.sentiment || 0) > 0.05 ? "偏正面" : (news.sentiment || 0) < -0.05 ? "偏负面" : "中性";
    detail.push(`新闻情绪${tone}（${news.sentiment ?? 0}）：利好${news.positive ?? 0}条 / 利空${news.negative ?? 0}条${news.fallback_to_market ? " · 按大盘折算" : ""}`);
    for (const headline of news.headlines || []) detail.push(`· 新闻：${headline.title}`);
  }
  const strategyText = plan.strategy?.source === "per-symbol" ? "逐股寻优" : "全局寻优";
  const symbolBt = plan.symbol_backtest;
  if (symbolBt && symbolBt.trades) {
    detail.push(`该策略历史：${symbolBt.trades}次 · 做T胜率${pctLabel(symbolBt.win_rate)} · 双腿率${pctLabel(symbolBt.completed_rate)} · 均${symbolBt.avg_bps ?? "--"}bp · 最差${symbolBt.worst_bps ?? "--"}bp`);
  }
  const recent = plan.recent;
  if (recent && recent.trades) {
    detail.push(`近60个评估日：${recent.trades}次 · 做T胜率${pctLabel(recent.win_rate)} · 均${recent.avg_bps ?? "--"}bp`);
  }
  const cfg = plan.strategy?.config;
  if (cfg && cfg.buy_atr != null) {
    detail.push(`参数口径：${strategyText}（买入 ${cfg.buy_atr}×ATR / 卖出 ${cfg.sell_atr}×ATR / 阈值 ${cfg.theta}）`);
  }
  detail.push("历史成功率为样本内回测，未计手续费滑点，不构成投资建议");
  chip.title = detail.join("\n");
  chip.setAttribute("aria-label", `${stockName || ""} 次日做T参考：${plan.kind}`);

  // 触发价位摘要（最多两级）
  const summaryParts = [];
  for (const trigger of (plan.triggers || []).slice(0, 2)) {
    const pctText = trigger.move_pct == null ? "" : `${trigger.move_pct > 0 ? "+" : ""}${trigger.move_pct}%`;
    summaryParts.push(`${trigger.label} ${pctText}@${trigger.price}`);
  }

  // 胜率摘要
  const winRateParts = [];
  if (symbolBt && symbolBt.trades) {
    winRateParts.push(`胜率${pctLabel(symbolBt.win_rate)}`);
  }
  const recent10 = plan.recent_10;
  if (recent10 && recent10.trades) {
    winRateParts.push(`近10日${recent10.trades}次`);
  }

  let warn = null;
  if (nw && nw.in_window) {
    warn = span(`⚠${nw.label}`, "t-plan-warn");
    warn.title = `下一期${nw.label}（期末 ${nw.period_end}），披露窗口 ${nw.window_start}~${nw.window_end}，财报前后波动加大，谨慎做T`;
  }

  return {
    chip,
    triggerText: summaryParts.join(" / "),
    statText: winRateParts.join(" · "),
    warn,
  };
}

/**
 * 渲染页面底部「做T 决策中心 · 个股次日做T参考」表格。
 * 持仓行情表已不再占用做T参考列，这里按持仓逐行给出模型倾向、触发价位、胜率与提示。
 */
function renderTPlanSection(force = false) {
  if (!els.tPlanBody) return;
  const positions = resolvedPositions();
  const plans = state.tomorrowPlans || null;
  const signature = JSON.stringify({
    symbols: positions.map((item) => `${item.symbol}:${item.name || ""}`),
    kinds: positions.map((item) => {
      const plan = plans ? plans[item.symbol] : null;
      return plan && plan.ok !== false ? `${plan.kind}|${plan.bias}|${(plan.triggers || []).length}` : "-";
    }),
    version: state.tomorrowModelVersion || "",
  });
  if (!force && signature === state.tPlanSignature) return;
  state.tPlanSignature = signature;

  if (!positions.length) {
    els.tPlanBody.replaceChildren();
    const row = document.createElement("tr");
    const cell = td("暂无持仓数据", "empty");
    cell.colSpan = 5;
    row.append(cell);
    els.tPlanBody.append(row);
    if (els.tPlanSummary) els.tPlanSummary.textContent = "等待持仓数据…";
    return;
  }

  // 模型还没出结果时不要铺 16 行「—」——那会被误读成「该股没有做T倾向」。
  if (!plans) {
    els.tPlanBody.replaceChildren();
    const row = document.createElement("tr");
    const cell = td("正在计算次日做T参考（首次需跑回测与逐股寻优，约 12–15 秒）…", "empty");
    cell.colSpan = 5;
    row.append(cell);
    els.tPlanBody.append(row);
    if (els.tPlanSummary) els.tPlanSummary.textContent = "等待做T计划模型输出…";
    return;
  }

  const fragment = document.createDocumentFragment();
  let planned = 0;
  for (const item of positions) {
    const plan = plans ? plans[item.symbol] : null;
    const parts = buildTPlanParts(plan, item.name);
    if (parts) planned += 1;
    const row = document.createElement("tr");
    row.className = "t-plan-row";
    row.dataset.symbol = item.symbol;

    const nameCell = td("", "t-plan-name");
    nameCell.append(span(String(item.symbol), "t-plan-symbol"));
    nameCell.append(span(item.name || "--", "t-plan-stock"));
    row.append(nameCell);

    const kindCell = td("", "t-plan-kind");
    if (parts) kindCell.append(parts.chip);
    else kindCell.append(span("—", "muted"));
    row.append(kindCell);

    const triggerCell = td(parts?.triggerText || "--", "t-plan-trigger");
    if (!parts?.triggerText) triggerCell.classList.add("muted");
    row.append(triggerCell);

    const statCell = td(parts?.statText || "--", "t-plan-stat-cell");
    if (!parts?.statText) statCell.classList.add("muted");
    row.append(statCell);

    const warnCell = td("", "t-plan-warn-cell");
    if (parts?.warn) warnCell.append(parts.warn);
    else warnCell.append(span("—", "muted"));
    row.append(warnCell);

    fragment.append(row);
  }
  els.tPlanBody.replaceChildren(fragment);

  if (els.tPlanSummary) {
    els.tPlanSummary.textContent = `${planned}/${positions.length} 只有可执行倾向 · ${state.tomorrowModelVersion || "模型版本 --"}`;
  }
}

function updateTomorrowPlanSummary() {
  if (!els.tCenterModelSummary) return;
  const backtest = state.tomorrowBacktest;
  if (!backtest || backtest.ok === false || !backtest.best) {
    els.tCenterModelSummary.textContent = "做T模型 回测数据不足";
    return;
  }
  const best = backtest.best;
  const mp = state.marketPlan;
  const marketPart = mp && mp.bias
    ? `大盘预判${mp.bias === "up" ? "偏多" : mp.bias === "down" ? "偏空" : "中性"}(${mp.score > 0 ? "+" : ""}${mp.score})`
    : "大盘预判 --";
  const parts = [
    marketPart,
    `做T模型 ${state.tomorrowModelVersion || ""}`.trim(),
    `方向胜率 ${pctLabel(best.direction_hit_rate)}`,
    `做T胜率 ${pctLabel(best.win_rate)}`,
    `均 ${best.avg_bps ?? "--"}bp/次`,
    `样本 ${backtest.symbols ?? "--"}股`,
  ];
  els.tCenterModelSummary.textContent = `${parts.join(" · ")}（历史回测，仅供参考）`;
  els.tCenterModelSummary.title = [
    `回测口径：walk-forward 逐日计划 + 次日OHLC模拟，未计手续费滑点`,
    `最优参数：买入 ${best.config?.buy_atr}×ATR / 卖出 ${best.config?.sell_atr}×ATR / 阈值 ${best.config?.theta}`,
    `交易 ${best.trades} 次 · 双腿成交率 ${pctLabel(best.completed_rate)} · 最差单日 ${best.worst_bps ?? "--"}bp`,
    "模型参考，不构成投资建议",
  ].join("\n");
}

async function loadTechnicalSignalsForPositions(symbols) {
  if (!symbols.length) return;
  const now = Date.now();
  const cacheAgeMs = now - (state.technicalSignalsFetchedAt || 0);
  const cachedSymbols = symbols.filter((symbol) => state.technicalSignalsBySymbol.has(symbol));
  // Use cache when fresh (≤ 5 min) and covers all visible symbols.
  if (cacheAgeMs < 300_000 && cachedSymbols.length === symbols.length) {
    applyTechnicalSignalsToRows();
    return;
  }
  if (state.technicalSignalsPending) return; // already in flight
  const params = symbols.map((s) => encodeURIComponent(s)).join(",");
  state.technicalSignalsPending = fetchJson(`/api/technical-signals-batch?symbols=${params}`)
    .then((payload) => {
      if (!payload?.ok) return;
      const results = payload.results || {};
      for (const [symbol, item] of Object.entries(results)) {
        if (item?.ok) {
          state.technicalSignalsBySymbol.set(symbol, item);
        }
      }
      state.technicalSignalsFetchedAt = Date.now();
      applyTechnicalSignalsToRows();
    })
    .catch(() => { /* silent — technical signals are advisory only */ })
    .finally(() => { state.technicalSignalsPending = null; });
}

function applyTechnicalSignalsToRows() {
  // Merge technical signals into existing position signal cells by appending
  // additional chips after the local-rule ones.
  for (const row of els.positionsBody.querySelectorAll(".position-row")) {
    const symbol = row.dataset.symbol;
    if (!symbol) continue;
    const payload = state.technicalSignalsBySymbol.get(symbol);
    if (!payload) continue;
    const signalsCell = row.querySelector(".position-signals");
    if (!signalsCell) continue;
    // 自设线列为空（"--"）且该股有系统建议时，异步补上建议价格线。
    const linesCell = row.querySelector(".position-lines");
    if (linesCell && !linesCell.classList.contains("is-suggested")) {
      const current = linesCell.textContent.trim();
      const suggestion = payload.suggested_lines;
      if ((!current || current === "--") && suggestion) {
        linesCell.textContent = "";
        for (const [label, value] of [["止损", suggestion.stop], ["加仓", suggestion.buy], ["止盈", suggestion.sell]]) {
          linesCell.append(span(`${label} ${value ?? "--"}`, "price-line-suggestion"));
        }
        linesCell.title = `系统建议（ATR14≈${suggestion.atr14 ?? "--"}）\n${(suggestion.basis || []).join("\n")}\n⚙ 可修改后保存为自己的价格线`;
        linesCell.classList.add("is-suggested");
      }
    }
    // Remove any previously injected tech chips so re-renders don't stack.
    for (const stale of signalsCell.querySelectorAll(".signal-chip.is-tech")) {
      stale.remove();
    }
    const topSignals = (payload.signals || []).slice(0, 2);
    for (const signal of topSignals) {
      const levelMap = { critical: "danger", warning: "warn", info: "info" };
      const level = levelMap[signal.severity] || "info";
      const direction = signal.direction === "bullish" ? "↑" : signal.direction === "bearish" ? "↓" : "·";
      // Map "info" to "op" (opportunity) for bullish, "more" (neutral) otherwise,
      // to reuse the existing local-rule chip palette.
      const chipLevel = level === "info" ? (signal.direction === "bullish" ? "op" : "more") : level;
      const chip = span(`${direction}${signal.rule.replace(/_/g, " ")}`, `signal-chip is-tech is-${chipLevel}`);
      chip.title = `${signal.message}（置信度 ${(signal.confidence * 100).toFixed(0)}%）`;
      signalsCell.append(chip);
    }
  }
}

function evaluatePositionSignals(item, totalMarketValue) {
  const signals = [];
  const add = (level, label, detail) => signals.push({ level, label, detail: detail || "" });
  const chg = item.changePct;
  const weight = positionSortValue(item, "weight", totalMarketValue);
  if (isFiniteNumber(chg) && chg <= SIGNAL_THRESHOLDS.dropDay) add("danger", "当日大跌", formatPct(chg));
  if (isFiniteNumber(chg) && chg >= SIGNAL_THRESHOLDS.riseDay) add("op", "当日大涨", formatPct(chg));
  if (isFiniteNumber(item.pe) && item.pe >= SIGNAL_THRESHOLDS.peHigh) add("danger", "PE高估", `${item.pe.toFixed(0)}x`);
  if (isFiniteNumber(item.pb) && item.pb >= SIGNAL_THRESHOLDS.pbHigh) add("danger", "PB高估", item.pb.toFixed(1));
  if (isFiniteNumber(item.pe) && item.pe > 0 && item.pe <= SIGNAL_THRESHOLDS.peLow
    && isFiniteNumber(item.pb) && item.pb <= SIGNAL_THRESHOLDS.pbLow) {
    add("op", "低估", `${item.pe.toFixed(1)}x/${item.pb.toFixed(2)}`);
  }
  if (isFiniteNumber(weight) && weight >= SIGNAL_THRESHOLDS.weightDanger) add("danger", "权重过高", formatWeightPct(weight));
  else if (isFiniteNumber(weight) && weight >= SIGNAL_THRESHOLDS.weightWarn) add("warn", "权重偏高", formatWeightPct(weight));
  if (isFiniteNumber(item.turnover) && isFiniteNumber(chg)) {
    if (item.turnover >= SIGNAL_THRESHOLDS.volDnTurn && chg <= SIGNAL_THRESHOLDS.volDnChg) add("danger", "放量大跌", `换手${item.turnover.toFixed(1)}%`);
    if (item.turnover >= SIGNAL_THRESHOLDS.volUpTurn && chg >= SIGNAL_THRESHOLDS.volUpChg) add("op", "放量上涨", `换手${item.turnover.toFixed(1)}%`);
  }
  const line = state.priceLines[item.symbol];
  if (line && isFiniteNumber(item.price)) {
    const stop = firstNumber(line.stop);
    const buy = firstNumber(line.buy);
    const sell = firstNumber(line.sell);
    if (isFiniteNumber(stop)) {
      if (item.price <= stop) add("danger", "跌破止损线", `${stop} (现价${item.price})`);
      else if (item.price <= stop * (1 + SIGNAL_THRESHOLDS.nearStop / 100)) add("warn", "接近止损线", `距${stop}不足${SIGNAL_THRESHOLDS.nearStop}%`);
    }
    if (isFiniteNumber(buy) && item.price <= buy) add("op", "到达加仓参考价", String(buy));
    if (isFiniteNumber(sell) && item.price >= sell) add("op", "达到止盈目标", String(sell));
  }
  return signals;
}

function formatWeightPct(value) {
  return isFiniteNumber(value) ? `${value.toFixed(1)}%` : "--";
}

function formatPriceLinesText(line) {
  if (!line) return "";
  const parts = [];
  if (isFiniteNumber(line.stop)) parts.push(`止${line.stop}`);
  if (isFiniteNumber(line.buy)) parts.push(`买${line.buy}`);
  if (isFiniteNumber(line.sell)) parts.push(`卖${line.sell}`);
  return parts.join(" ");
}

function openStockLines(symbol) {
  const item = state.positions.find((position) => position.symbol === symbol);
  const line = state.priceLines[symbol] || {};
  const suggestion = state.technicalSignalsBySymbol.get(String(symbol))?.suggested_lines || null;
  const hasOwn = isFiniteNumber(line.stop) || isFiniteNumber(line.buy) || isFiniteNumber(line.sell);
  const source = hasOwn ? line : (suggestion || {});
  state.linesDialogSymbol = symbol;
  els.stockLinesTitle.textContent = `设置价格线 · ${item?.name || ""} ${symbol}`;
  els.stockLinesInfo.textContent = item
    ? `现价 ${formatPrice(item.price)} · 当日 ${formatPct(item.changePct)}${line.note ? ` · 备注：${line.note}` : ""}${!hasOwn && suggestion ? " · 已预填系统建议值，保存后生效" : ""}`
    : "该代码暂无行情";
  els.stockLinesStop.value = isFiniteNumber(source.stop) ? String(source.stop) : "";
  els.stockLinesBuy.value = isFiniteNumber(source.buy) ? String(source.buy) : "";
  els.stockLinesSell.value = isFiniteNumber(source.sell) ? String(source.sell) : "";
  els.stockLinesNote.value = String(line.note || (suggestion && !hasOwn ? "系统建议（可修改）" : ""));
  els.stockLinesDelete.hidden = !state.priceLines[symbol];
  els.stockLinesDialog.showModal();
}

function saveStockLines() {
  const symbol = state.linesDialogSymbol;
  if (!symbol) return;
  const parse = (value) => {
    const parsed = parseFloat(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const line = {
    stop: parse(els.stockLinesStop.value),
    buy: parse(els.stockLinesBuy.value),
    sell: parse(els.stockLinesSell.value),
    note: els.stockLinesNote.value.trim(),
  };
  if (line.stop == null && line.buy == null && line.sell == null && !line.note) {
    delete state.priceLines[symbol];
  } else {
    state.priceLines[symbol] = line;
  }
  try { window.localStorage.setItem(PRICE_LINES_STORAGE_KEY, JSON.stringify(state.priceLines)); } catch (_) { /* storage may be disabled */ }
  state.linesDialogSymbol = "";
  els.stockLinesDialog.close();
  renderPositions();
}

function deleteStockLines() {
  const symbol = state.linesDialogSymbol;
  if (symbol) delete state.priceLines[symbol];
  try { window.localStorage.setItem(PRICE_LINES_STORAGE_KEY, JSON.stringify(state.priceLines)); } catch (_) { /* storage may be disabled */ }
  state.linesDialogSymbol = "";
  els.stockLinesDialog.close();
  renderPositions();
}

function stockChartSymbolName(symbol) {
  const target = normalizeChartSymbol(symbol);
  if (!target) return "";
  const bySymbol = (item) => normalizeChartSymbol(item.symbol) === target;
  return state.positions.find(bySymbol)?.name
    || state.markets.find(bySymbol)?.name
    || state.globals.find(bySymbol)?.name
    || "";
}

function showStockChart(anchor, pin = false) {
  if (!anchor || !els.stockChartPopover) return;
  const symbol = normalizeChartSymbol(anchor.dataset.symbol);
  if (!symbol) return;
  if (!pin && state.stockChartPinned && !els.stockChartPopover.hidden) return;
  if (!pin && !els.stockChartPopover.hidden && state.stockChartSymbol === symbol) {
    cancelStockChartClose();
    state.stockChartAnchor = anchor;
    setStockChartExpanded(symbol);
    positionStockChartPopover();
    return;
  }

  cancelStockChartClose();
  state.stockChartSymbol = symbol;
  state.stockChartAnchor = anchor;
  state.stockChartPinned = Boolean(pin);
  setStockChartExpanded(symbol);
  els.stockChartPopover.hidden = false;
  els.stockChartPopover.setAttribute("aria-busy", "true");
  const display = stockChartSymbolName(symbol) || symbol;
  els.stockChartTitle.textContent = `${display} · ${symbol}`;
  els.stockChartMeta.textContent = pin ? "已固定 · 正在获取行情…" : "悬停预览 · 正在获取行情…";
  renderStockChartLoading();
  positionStockChartPopover();

  const cached = state.stockChartCache.get(symbol);
  if (cached && Date.now() - cached.receivedAt <= STOCK_CHART_CACHE_TTL_MS) {
    renderStockChart(cached.chart);
    return;
  }

  const token = ++state.stockChartRequestToken;
  void loadStockChart(symbol, token);
}

async function loadStockChart(symbol, token) {
  try {
    let pending = state.stockChartPending.get(symbol);
    if (!pending) {
      pending = fetchJson(`/api/stock-chart?symbol=${encodeURIComponent(symbol)}&indicators=ma,macd,rsi,kdj,boll,atr,vol_ma`);
      state.stockChartPending.set(symbol, pending);
    }
    let payload;
    try {
      payload = await pending;
    } finally {
      if (state.stockChartPending.get(symbol) === pending) state.stockChartPending.delete(symbol);
    }
    if (payload?.ok === false) throw new Error(payload.message || payload.error || "图表接口返回失败");
    const chart = normalizeStockChart(payload);
    if (chart.schemaVersion !== "stock-chart.api.v1") {
      throw new Error("图表数据版本不兼容");
    }
    if (chart.symbol !== symbol) throw new Error("图表代码与请求不一致");
    state.stockChartCache.set(symbol, { receivedAt: Date.now(), chart });
    trimStockChartCache();
    if (token !== state.stockChartRequestToken || state.stockChartSymbol !== symbol || els.stockChartPopover.hidden) return;
    renderStockChart(chart);
  } catch (error) {
    if (token !== state.stockChartRequestToken || state.stockChartSymbol !== symbol || els.stockChartPopover.hidden) return;
    renderStockChartFailure(friendlyError(error, "图表暂时不可用"));
  }
}

function closeStockChart(returnFocus = false) {
  if (!els.stockChartPopover || els.stockChartPopover.hidden) return;
  const anchor = state.stockChartAnchor;
  cancelStockChartClose();
  state.stockChartRequestToken += 1;
  state.stockChartSymbol = "";
  state.stockChartAnchor = null;
  state.stockChartPinned = false;
  els.stockChartPopover.hidden = true;
  els.stockChartPopover.removeAttribute("aria-busy");
  setStockChartExpanded("");
  if (returnFocus && anchor?.isConnected && document.activeElement !== anchor) {
    state.stockChartSuppressFocusOnce = true;
    anchor.focus({ preventScroll: true });
    window.setTimeout(() => { state.stockChartSuppressFocusOnce = false; }, 0);
  }
}

function scheduleStockChartClose() {
  if (state.stockChartPinned || els.stockChartPopover.hidden) return;
  cancelStockChartClose();
  state.stockChartCloseTimer = window.setTimeout(() => {
    state.stockChartCloseTimer = null;
    if (!state.stockChartPinned) closeStockChart();
  }, STOCK_CHART_HOVER_CLOSE_MS);
}

function cancelStockChartClose() {
  if (!state.stockChartCloseTimer) return;
  window.clearTimeout(state.stockChartCloseTimer);
  state.stockChartCloseTimer = null;
}

function positionStockChartPopover() {
  if (!els.stockChartPopover || els.stockChartPopover.hidden || !state.stockChartAnchor?.isConnected) return;
  const anchorRect = state.stockChartAnchor.getBoundingClientRect();
  const popoverRect = els.stockChartPopover.getBoundingClientRect();
  const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
  const viewportHeight = document.documentElement.clientHeight || window.innerHeight;
  const margin = 8;
  const gap = 8;
  // 首选显示在悬浮股票名称的右侧（垂直方向与该行对齐），这样不会遮挡
  // 下方的其他股票行；右侧放不下时退到名称左侧，两者都放不下才回退为
  // 行下方并做视口钳制。
  let left = anchorRect.right + gap;
  if (left + popoverRect.width > viewportWidth - margin) {
    const leftSide = anchorRect.left - gap - popoverRect.width;
    if (leftSide >= margin) {
      left = leftSide;
    } else {
      left = Math.min(
        Math.max(margin, anchorRect.left),
        Math.max(margin, viewportWidth - popoverRect.width - margin),
      );
    }
  }
  const top = Math.max(
    margin,
    Math.min(anchorRect.top, viewportHeight - popoverRect.height - margin),
  );
  els.stockChartPopover.style.left = `${Math.round(left)}px`;
  els.stockChartPopover.style.top = `${Math.round(top)}px`;
}

function setStockChartExpanded(symbol) {
  for (const trigger of document.querySelectorAll(".stock-chart-trigger")) {
    trigger.setAttribute("aria-expanded", trigger.dataset.symbol != null && normalizeChartSymbol(trigger.dataset.symbol) === symbol ? "true" : "false");
  }
}

function trimStockChartCache() {
  while (state.stockChartCache.size > 40) {
    const oldest = state.stockChartCache.keys().next().value;
    state.stockChartCache.delete(oldest);
  }
}

function renderStockChartLoading() {
  for (const [svg, empty] of [
    [els.stockChartKline, els.stockChartKlineEmpty],
    [els.stockChartIntraday, els.stockChartIntradayEmpty],
  ]) {
    svg.replaceChildren();
    svg.setAttribute("hidden", "");
    empty.textContent = "正在获取行情…";
    empty.hidden = false;
  }
  els.stockChartNote.textContent = "日K线使用前复权数据；分时图按接口返回的真实 5 分钟点绘制。";
}

function renderStockChartFailure(message) {
  els.stockChartPopover.setAttribute("aria-busy", "false");
  els.stockChartMeta.textContent = "加载失败";
  const safeMessage = String(message || "图表暂时不可用");
  for (const [svg, empty] of [
    [els.stockChartKline, els.stockChartKlineEmpty],
    [els.stockChartIntraday, els.stockChartIntradayEmpty],
  ]) {
    svg.replaceChildren();
    svg.setAttribute("hidden", "");
    empty.textContent = `加载失败：${safeMessage}`;
    empty.hidden = false;
  }
  els.stockChartNote.textContent = "可稍后重新悬停，或点击其他股票后再试。";
  if (els.stockChartIndicators) {
    els.stockChartIndicators.replaceChildren();
    els.stockChartIndicators.hidden = true;
  }
}

function renderStockIndicatorStrip(latest) {
  if (!els.stockChartIndicators) return;
  els.stockChartIndicators.replaceChildren();
  if (!latest || typeof latest !== "object") {
    els.stockChartIndicators.hidden = true;
    return;
  }
  const fmt = (value, digits = 2) => (isFiniteNumber(value) ? Number(value).toFixed(digits) : "--");
  const chips = [
    { label: "MA5", value: fmt(latest.ma5), color: "#f59e0b" },
    { label: "MA20", value: fmt(latest.ma20), color: "#a855f7" },
    { label: "MA60", value: fmt(latest.ma60), color: "#10b981" },
    { label: "DIF", value: fmt(latest.macd_dif, 3), color: latest.macd_dif >= 0 ? "#dc2626" : "#16a34a" },
    { label: "MACD", value: fmt(latest.macd_hist, 3), color: latest.macd_hist >= 0 ? "#dc2626" : "#16a34a" },
    { label: "RSI6", value: fmt(latest.rsi6, 1), color: latest.rsi6 >= 80 ? "#dc2626" : latest.rsi6 <= 20 ? "#16a34a" : "#6b7280" },
    { label: "RSI24", value: fmt(latest.rsi24, 1), color: latest.rsi24 >= 80 ? "#dc2626" : latest.rsi24 <= 20 ? "#16a34a" : "#6b7280" },
    { label: "K", value: fmt(latest.kdj_k, 1), color: "#6b7280" },
    { label: "D", value: fmt(latest.kdj_d, 1), color: "#6b7280" },
    { label: "J", value: fmt(latest.kdj_j, 1), color: latest.kdj_j >= 100 ? "#dc2626" : latest.kdj_j <= 0 ? "#16a34a" : "#6b7280" },
    { label: "BOLL上", value: fmt(latest.boll_upper), color: "#6b7280" },
    { label: "BOLL下", value: fmt(latest.boll_lower), color: "#6b7280" },
    { label: "ATR14", value: fmt(latest.atr14, 3), color: "#6b7280" },
  ];
  for (const chip of chips) {
    const node = document.createElement("span");
    node.className = "stock-indicator-chip";
    const label = document.createElement("span");
    label.className = "stock-indicator-chip-label";
    label.textContent = chip.label;
    const value = document.createElement("span");
    value.className = "stock-indicator-chip-value";
    value.style.color = chip.color;
    value.textContent = chip.value;
    node.append(label, value);
    els.stockChartIndicators.append(node);
  }
  els.stockChartIndicators.hidden = false;
}

function renderStockChart(chart) {
  if (state.stockChartSymbol !== chart.symbol || els.stockChartPopover.hidden) return;
  els.stockChartPopover.setAttribute("aria-busy", "false");
  els.stockChartTitle.textContent = `${chart.name || stockChartSymbolName(chart.symbol) || chart.symbol} · ${chart.symbol}`;
  const meta = [chart.source, chart.generatedAt ? formatDateTime(chart.generatedAt) : "", state.stockChartPinned ? "已固定" : "悬停预览"].filter(Boolean);
  els.stockChartMeta.textContent = meta.join(" · ") || "行情图";

  const period = selectStockChartPeriod(state.stockChartPeriod || "day");
  const fullKline = chart.kline.items;
  const aggregatedKline = period === "day"
    ? fullKline
    : aggregateStockOhlcByPeriod(fullKline, period);
  const displayKline = aggregatedKline.slice(-STOCK_CHART_MAX_KLINE_POINTS);
  const periodMeta = STOCK_CHART_PERIOD_LABELS[period] || STOCK_CHART_PERIOD_LABELS.day;
  if (els.stockChartKlineCaption) {
    els.stockChartKlineCaption.textContent = periodMeta.kline;
  }
  const hasKline = renderStockKlineChart(
    els.stockChartKline,
    displayKline,
    chart.name || chart.symbol,
    period,
    chart.indicators?.series?.ma || null,
  );
  els.stockChartKlineEmpty.hidden = hasKline;
  if (!hasKline) els.stockChartKlineEmpty.textContent = "暂无可用K线数据";
  renderStockIndicatorStrip(chart.indicators?.latest || null);
  const hasIntraday = renderStockIntradayChart(
    els.stockChartIntraday,
    chart.intraday.items,
    chart.intraday.prevClose,
    chart.name || chart.symbol,
  );
  els.stockChartIntradayEmpty.hidden = hasIntraday;
  if (!hasIntraday) els.stockChartIntradayEmpty.textContent = "今日暂无可用分时数据";

  const warning = chart.warnings.slice(0, 2).join("；");
  const adjustment = chart.kline.adjustment === "qfq"
    ? "前复权"
    : chart.kline.adjustment === "none"
      ? "不复权"
      : chart.kline.adjustment || "复权状态待核验";
  const aggregateNote = period === "day"
    ? ""
    : ` · ${periodMeta.kline}由日K按${periodMeta.singular}聚合`;
  els.stockChartNote.textContent = warning
    || `${periodMeta.kline} ${adjustment} · 分时 ${chart.intraday.period || "5分钟"}（午休已压缩） · 数据仅供行情观察${aggregateNote}`;
  positionStockChartPopover();
}

function selectStockChartPeriod(period) {
  if (!Object.prototype.hasOwnProperty.call(STOCK_CHART_PERIOD_LABELS, period)) {
    period = "day";
  }
  state.stockChartPeriod = period;
  for (const button of document.querySelectorAll("#stock-chart-period-toggle [data-period]")) {
    const isActive = button.dataset.period === period;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
  }
  return period;
}

function normalizeStockChart(payload) {
  const root = rootObject(payload);
  const kline = asObject(root.kline);
  const intraday = asObject(root.intraday);
  const tradeDate = String(pick(intraday, ["trade_date", "tradeDate", "date"]) || "");
  const indicatorsRaw = asObject(root.indicators);
  const indicatorsLatest = asObject(indicatorsRaw.latest);
  return {
    schemaVersion: String(pick(root, ["schema_version", "schemaVersion"]) || ""),
    symbol: normalizeChartSymbol(pick(root, ["symbol", "thscode"])),
    name: String(pick(root, ["name", "stock_name", "stockName"]) || ""),
    generatedAt: pick(root, ["generated_at", "generatedAt", "as_of", "asOf"]),
    source: String(sourceName(root.source) || pick(root, ["source_name", "sourceName"]) || ""),
    warnings: arrayText(root.warnings).slice(0, 8),
    indicators: {
      latest: indicatorsLatest,
      series: asObject(indicatorsRaw.indicators),
    },
    kline: {
      adjustment: String(pick(kline, ["adjustment", "adjust"]) || ""),
      period: String(pick(kline, ["period"]) || "day"),
      items: normalizeStockOhlcSeries(kline.items, "", STOCK_CHART_MAX_KLINE_FETCH_POINTS),
    },
    intraday: {
      tradeDate,
      period: String(pick(intraday, ["period"]) || "m5"),
      prevClose: firstNumber(pickNumber(intraday, ["prev_close", "prevClose", "previous_close"])),
      items: normalizeStockOhlcSeries(intraday.items, tradeDate, STOCK_CHART_MAX_INTRADAY_POINTS),
    },
  };
}

function normalizeStockOhlcSeries(rawItems, tradeDate = "", limit = 120) {
  const deduped = new Map();
  for (const [, raw] of collectionEntries(rawItems)) {
    const item = asObject(raw);
    const time = Array.isArray(raw) ? raw[0] : pick(item, ["time", "date", "datetime", "timestamp"]);
    const open = Array.isArray(raw) ? toNumber(raw[1]) : pickNumber(item, ["open"]);
    const close = Array.isArray(raw) ? toNumber(raw[2]) : pickNumber(item, ["close", "price"]);
    const high = Array.isArray(raw) ? toNumber(raw[3]) : pickNumber(item, ["high"]);
    const low = Array.isArray(raw) ? toNumber(raw[4]) : pickNumber(item, ["low"]);
    const volume = Array.isArray(raw) ? toNumber(raw[5]) : pickNumber(item, ["volume", "vol"]);
    const timeValue = stockChartTimeValue(time, tradeDate);
    if (![timeValue, open, close, high, low].every(isFiniteNumber)) continue;
    if (open <= 0 || close <= 0 || high <= 0 || low <= 0) continue;
    if (high < Math.max(open, close) || low > Math.min(open, close) || high < low) continue;
    deduped.set(timeValue, {
      time: String(time),
      timeValue,
      open,
      close,
      high,
      low,
      volume: isFiniteNumber(volume) && volume >= 0 ? volume : null,
    });
  }
  return Array.from(deduped.values())
    .sort((left, right) => left.timeValue - right.timeValue)
    .slice(-Math.max(1, limit));
}

function aggregateStockOhlcByPeriod(items, period) {
  if (!items.length) return items.slice();
  if (period !== "week" && period !== "month") return items.slice();
  const keyFn = period === "week" ? personalIndexIsoWeekKey : personalIndexMonthKey;
  const groups = [];
  const index = new Map();
  for (const item of items) {
    const dateOnly = String(item.time).slice(0, 10);
    const key = keyFn(dateOnly);
    if (!index.has(key)) {
      index.set(key, groups.length);
      groups.push([]);
    }
    groups[index.get(key)].push(item);
  }
  return groups.map((group) => {
    const first = group[0];
    const last = group[group.length - 1];
    let volume = 0;
    let hasVolume = false;
    for (const item of group) {
      if (isFiniteNumber(item.volume)) { volume += item.volume; hasVolume = true; }
    }
    return {
      time: last.time,
      timeValue: last.timeValue,
      open: first.open,
      close: last.close,
      high: group.reduce((acc, item) => Math.max(acc, item.high), -Infinity),
      low: group.reduce((acc, item) => Math.min(acc, item.low), Infinity),
      volume: hasVolume ? volume : null,
    };
  });
}

function stockChartTimeValue(value, tradeDate = "") {
  if (value == null || value === "") return null;
  if (isFiniteNumber(value)) return timestampValue(value) || null;
  let raw = String(value).trim();
  if (/^\d{8}$/.test(raw)) raw = `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  if (/^\d{12,14}$/.test(raw)) {
    raw = `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}T${raw.slice(8, 10)}:${raw.slice(10, 12)}:${raw.slice(12, 14) || "00"}+08:00`;
  } else if (/^\d{4}$/.test(raw) && tradeDate) {
    raw = `${tradeDate}T${raw.slice(0, 2)}:${raw.slice(2, 4)}:00+08:00`;
  } else if (/^\d{1,2}:\d{2}(?::\d{2})?$/.test(raw) && tradeDate) {
    raw = `${tradeDate}T${raw.padStart(raw.length === 5 ? 5 : 8, "0")}${raw.length <= 5 ? ":00" : ""}+08:00`;
  } else if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    raw = `${raw}T00:00:00+08:00`;
  }
  return timestampValue(raw) || null;
}

function renderStockKlineChart(svg, items, label, period = "day", maSeriesData = null) {
  svg.replaceChildren();
  if (!items.length) {
    svg.setAttribute("hidden", "");
    return false;
  }

  const width = 340;
  const height = 172;
  const padding = { top: 9, right: 9, bottom: 21, left: 37 };
  const lows = items.map((item) => item.low);
  const highs = items.map((item) => item.high);
  // Include MA lines in y-axis bounds so the lines never clip.
  if (maSeriesData) {
    for (const series of Object.values(maSeriesData)) {
      for (const value of series) {
        if (isFiniteNumber(value)) {
          lows.push(value);
          highs.push(value);
        }
      }
    }
  }
  const bounds = paddedChartBounds(Math.min(...lows), Math.max(...highs));
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const y = (value) => padding.top + (bounds.max - value) / (bounds.max - bounds.min) * plotHeight;
  const slot = plotWidth / items.length;
  const x = (index) => padding.left + slot * (index + 0.5);

  const periodMeta = STOCK_CHART_PERIOD_LABELS[period] || STOCK_CHART_PERIOD_LABELS.day;

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  appendStockChartGrid(svg, bounds, y, width, padding);
  items.forEach((item, index) => {
    const direction = item.close > item.open ? "up" : item.close < item.open ? "down" : "flat";
    const candleX = x(index);
    const wick = svgNode("line", {
      x1: candleX, x2: candleX, y1: y(item.high), y2: y(item.low), class: `stock-candle-wick ${direction}`,
    });
    const bodyY = Math.min(y(item.open), y(item.close));
    const bodyHeight = Math.max(1, Math.abs(y(item.open) - y(item.close)));
    // 高密度周期（260根）下缩窄蜡烛体宽度，避免相邻K线重叠
    const bodyWidth = Math.max(0.6, Math.min(7, slot * 0.58));
    const body = svgNode("rect", {
      x: candleX - bodyWidth / 2, y: bodyY, width: bodyWidth, height: bodyHeight, class: `stock-candle-body ${direction}`,
    });
    const detail = svgNode("title");
    detail.textContent = `${formatStockChartDate(item.time)} 开 ${formatPrice(item.open)} 高 ${formatPrice(item.high)} 低 ${formatPrice(item.low)} 收 ${formatPrice(item.close)}`;
    body.append(detail);
    svg.append(wick, body);
  });
  // Overlay MA lines on top of candles.
  if (maSeriesData) {
    const MA_COLORS = { ma5: "#f59e0b", ma10: "#3b82f6", ma20: "#a855f7", ma60: "#10b981" };
    for (const [name, series] of Object.entries(maSeriesData)) {
      if (!Array.isArray(series) || !series.length) continue;
      const points = [];
      // series is aligned to the FULL kline; we show only the last items.length bars.
      const offset = series.length - items.length;
      for (let index = 0; index < items.length; index += 1) {
        const value = series[offset + index];
        if (isFiniteNumber(value)) {
          points.push(`${points.length ? "L" : "M"}${x(index).toFixed(2)},${y(value).toFixed(2)}`);
        }
      }
      if (points.length > 1) {
        svg.append(svgNode("path", {
          d: points.join(" "),
          class: "stock-chart-ma-line",
          stroke: MA_COLORS[name] || "#6b7280",
          fill: "none",
          "stroke-width": 1,
        }));
      }
    }
  }
  appendStockChartTimeLabels(svg, items, width, height, padding, true);
  const latest = items[items.length - 1];
  const title = svgNode("title");
  title.textContent = `${label}${periodMeta.kline}，共${items.length}根，最新收盘${formatPrice(latest.close)}`;
  svg.prepend(title);
  svg.setAttribute("aria-label", title.textContent);
  svg.removeAttribute("hidden");
  return true;
}

// 固定交易轴：09:30–11:30 (0–120 分钟) + 13:00–15:00 (120–240 分钟)。
// 跨午休连续衔接，把午休从分时轴上压缩掉。
const STOCK_INTRADAY_TOTAL_MINUTES = 240;

function renderStockIntradayChart(svg, items, prevClose, label) {
  svg.replaceChildren();
  if (!items.length) {
    svg.setAttribute("hidden", "");
    return false;
  }

  const width = 340;
  const height = 172;
  const padding = { top: 9, right: 9, bottom: 21, left: 37 };
  const allPrices = items.flatMap((item) => [item.low, item.high]);
  if (isFiniteNumber(prevClose) && prevClose > 0) allPrices.push(prevClose);
  const bounds = paddedChartBounds(Math.min(...allPrices), Math.max(...allPrices));
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  const projected = [];
  for (const item of items) {
    const pos = intradayPnlAxisPosition(item.timeValue);
    if (!pos) continue;
    projected.push({ ...item, plotMinute: pos.plotTimeValue / 60_000 });
  }
  if (!projected.length) {
    svg.setAttribute("hidden", "");
    return false;
  }
  const y = (value) => padding.top + (bounds.max - value) / (bounds.max - bounds.min) * plotHeight;
  const x = (minute) => padding.left + minute / STOCK_INTRADAY_TOTAL_MINUTES * plotWidth;

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  appendStockChartGrid(svg, bounds, y, width, padding);
  if (isFiniteNumber(prevClose) && prevClose >= bounds.min && prevClose <= bounds.max) {
    svg.append(svgNode("line", {
      x1: padding.left, x2: width - padding.right, y1: y(prevClose), y2: y(prevClose), class: "stock-chart-reference",
    }));
  }
  const latest = projected[projected.length - 1];
  const direction = isFiniteNumber(prevClose)
    ? directionClass(latest.close - prevClose)
    : directionClass(latest.close - projected[0].close);
  if (projected.length === 1) {
    svg.append(svgNode("circle", {
      cx: x(projected[0].plotMinute), cy: y(projected[0].close), r: 1.8, class: `stock-intraday-dot ${direction}`,
    }));
  } else {
    svg.append(svgNode("path", {
      d: projected.map((item, index) => `${index ? "L" : "M"}${x(item.plotMinute).toFixed(2)},${y(item.close).toFixed(2)}`).join(" "),
      class: `stock-intraday-line ${direction}`,
    }));
  }
  const latestDot = svgNode("circle", { cx: x(latest.plotMinute), cy: y(latest.close), r: 2.5, class: `stock-intraday-dot ${direction}` });
  const latestDetail = svgNode("title");
  latestDetail.textContent = `${formatStockChartTime(latest.time)} ${formatPrice(latest.close)}`;
  latestDot.append(latestDetail);
  svg.append(latestDot);
  appendStockChartIntradayTimeLabels(svg, width, height, padding);
  const title = svgNode("title");
  title.textContent = `${label}当日分时，共${items.length}个点，最新${formatPrice(latest.close)}（午休已压缩）`;
  svg.prepend(title);
  svg.setAttribute("aria-label", title.textContent);
  svg.removeAttribute("hidden");
  return true;
}

function appendStockChartIntradayTimeLabels(svg, width, height, padding) {
  const plotWidth = width - padding.left - padding.right;
  const labels = [
    { minute: 0, text: "09:30", anchor: "start" },
    { minute: 120, text: "11:30/13:00", anchor: "middle" },
    { minute: STOCK_INTRADAY_TOTAL_MINUTES, text: "15:00", anchor: "end" },
  ];
  for (const label of labels) {
    const text = svgNode("text", {
      x: padding.left + label.minute / STOCK_INTRADAY_TOTAL_MINUTES * plotWidth,
      y: height - 5,
      class: "stock-chart-axis-label",
      "text-anchor": label.anchor,
    });
    text.textContent = label.text;
    svg.append(text);
  }
}

function splitStockChartSeriesAtGaps(items, maximumGapMs = 45 * 60_000) {
  const segments = [];
  let current = [];
  for (const item of items) {
    const previous = current[current.length - 1];
    if (previous && item.timeValue - previous.timeValue > maximumGapMs) {
      segments.push(current);
      current = [];
    }
    current.push(item);
  }
  if (current.length) segments.push(current);
  return segments;
}

function paddedChartBounds(minimum, maximum) {
  const center = (minimum + maximum) / 2;
  const naturalRange = maximum - minimum;
  const padding = naturalRange > 0 ? naturalRange * 0.06 : Math.max(Math.abs(center) * 0.01, 0.01);
  return { min: minimum - padding, max: maximum + padding };
}

function appendStockChartGrid(svg, bounds, y, width, padding) {
  for (let index = 0; index <= 3; index += 1) {
    const value = bounds.max - (bounds.max - bounds.min) * index / 3;
    svg.append(svgNode("line", {
      x1: padding.left, x2: width - padding.right, y1: y(value), y2: y(value), class: "stock-chart-grid-line",
    }));
    const label = svgNode("text", { x: padding.left - 4, y: y(value) + 3, class: "stock-chart-axis-label", "text-anchor": "end" });
    label.textContent = formatPrice(value);
    svg.append(label);
  }
}

function appendStockChartTimeLabels(svg, items, width, height, padding, dateOnly) {
  const first = svgNode("text", { x: padding.left, y: height - 5, class: "stock-chart-axis-label", "text-anchor": "start" });
  const last = svgNode("text", { x: width - padding.right, y: height - 5, class: "stock-chart-axis-label", "text-anchor": "end" });
  first.textContent = dateOnly ? formatStockChartDate(items[0].time) : formatStockChartTime(items[0].time);
  last.textContent = dateOnly ? formatStockChartDate(items[items.length - 1].time) : formatStockChartTime(items[items.length - 1].time);
  svg.append(first, last);
}

function formatStockChartDate(value) {
  const raw = String(value || "");
  if (/^\d{8,14}$/.test(raw)) return `${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  const matched = raw.match(/(?:^|\D)(\d{4})[-/]?(\d{2})[-/]?(\d{2})(?:\D|$)/);
  if (matched) return `${matched[2]}-${matched[3]}`;
  return raw.slice(0, 10);
}

function formatStockChartTime(value) {
  const raw = String(value || "");
  if (/^\d{12,14}$/.test(raw)) return `${raw.slice(8, 10)}:${raw.slice(10, 12)}`;
  if (/^\d{4}$/.test(raw)) return `${raw.slice(0, 2)}:${raw.slice(2, 4)}`;
  const matched = raw.match(/(?:T|\s)(\d{2}):?(\d{2})(?::?\d{2})?(?:[+\-Z\s]|$)/)
    || raw.match(/^(\d{1,2}):(\d{2})(?::\d{2})?(?:\s|$)/);
  return matched ? `${matched[1]}:${matched[2]}` : raw.slice(-5);
}

function stableSortPositions(positions, key, direction, totalMarketValue) {
  const sign = direction === "asc" ? 1 : -1;
  return positions.map((item, index) => ({ item, index })).sort((left, right) => {
    const a = positionSortValue(left.item, key, totalMarketValue);
    const b = positionSortValue(right.item, key, totalMarketValue);
    const aMissing = !isFiniteNumber(a);
    const bMissing = !isFiniteNumber(b);
    if (aMissing || bMissing) {
      if (aMissing !== bMissing) return aMissing ? 1 : -1;
      return left.index - right.index;
    }
    if (a === b) return left.index - right.index;
    return (a - b) * sign;
  }).map(({ item }) => item);
}

function positionSortValue(item, key, totalMarketValue) {
  if (key === "weight") {
    return firstNumber(
      item.weightSecurities,
      isFiniteNumber(item.marketValue) && totalMarketValue > 0 ? item.marketValue / totalMarketValue * 100 : null,
    );
  }
  return POSITION_SORT_KEYS.has(key) ? item[key] : null;
}

function updatePositionSortIndicators() {
  for (const button of els.positionsTable.querySelectorAll(".table-sort-button")) {
    const active = button.dataset.sortKey === state.positionSortKey;
    const th = button.closest("th");
    const direction = active ? state.positionSortDirection : null;
    th.setAttribute("aria-sort", direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none");
    button.classList.toggle("is-active", active);
    const label = button.textContent.trim();
    button.setAttribute("aria-label", active
      ? `${label}，当前${direction === "asc" ? "升序" : "降序"}，点击切换排序`
      : `${label}，点击按降序排序`);
  }
}

function renderNews() {
  const selected = state.selectedSymbol;
  const filtered = selected === "all" ? state.news : state.news.filter((item) => item.symbols.includes(selected));
  els.newsList.replaceChildren();
  els.newsCount.textContent = `${filtered.length} 条`;

  if (!filtered.length) {
    const refreshing = Boolean(rootObject(state.newsPayload).refreshing);
    const message = state.newsError
      ? (state.news.length ? "该股票暂无匹配新闻；新闻源当前不可用" : "新闻源暂时不可用，稍后自动重试")
      : (refreshing || !state.newsReceivedAt ? "正在获取相关新闻…" : "当前筛选下暂无重要新闻");
    els.newsList.append(emptyNode(message));
    return;
  }

  for (const item of filtered) {
    const article = document.createElement("article");
    article.className = "news-item";
    const title = safeLink(item.url, item.title || "无标题新闻", "news-title");
    const meta = div("news-item-meta");
    const names = item.symbols.map(symbolName).filter(Boolean).slice(0, 3).join("、");
    if (item.important) meta.append(span("重要", "tag important"));
    if (names) meta.append(span(names));
    if (item.source) meta.append(span(item.source));
    if (item.publishedAt) meta.append(span(formatDateTime(item.publishedAt)));
    article.append(title, meta);
    els.newsList.append(article);
  }
}

function renderConnectionState() {
  const payload = rootObject(state.quotePayload);
  const cache = asObject(payload.cache);
  const status = asObject(payload.market_status || payload.marketStatus);
  const clientAgeSeconds = state.quoteReceivedAt
    ? Math.max(0, (Date.now() - state.quoteReceivedAt) / 1000)
    : Infinity;
  const cacheAgeSeconds = clientAgeSeconds + Math.max(0, state.quoteBaseAgeSeconds || 0);
  const maxStaleSeconds = firstNumber(
    pickNumber(cache, ["max_stale_seconds", "maxStaleSeconds"]),
    300,
  );
  const liveWindow = isActiveQuoteMarketState(payload);
  const serverStale = Boolean(
    cache.expired === true
    || pick(status, ["feed_stale", "feedStale"]) === true
    || (liveWindow && cacheAgeSeconds >= maxStaleSeconds)
    || (liveWindow && state.positions.some((item) => item.isStale))
    || payload.ok === false
  );
  const stale = !state.quoteReceivedAt || quoteClientResponseStale(payload, clientAgeSeconds) || serverStale;
  els.connectionStatus.className = "status-dot";
  if (state.quoteError) {
    els.connectionStatus.classList.add("is-error");
    els.connectionStatus.textContent = state.quoteReceivedAt ? "行情重试中" : "行情不可用";
  } else if (!state.quoteReceivedAt) {
    els.connectionStatus.classList.add("is-loading");
    els.connectionStatus.textContent = "连接中";
  } else {
    els.connectionStatus.classList.add(stale ? "is-loading" : "is-ok");
    els.connectionStatus.textContent = stale ? "行情延迟" : "已连接";
  }

  const rawPhase = pick(status, ["label", "state", "status", "phase"])
    || pick(payload, ["market_phase", "marketPhase", "session", "trade_status"])
    || pick(asObject(payload.market), ["status", "phase", "session"]);
  els.marketPhase.textContent = rawPhase ? `市场状态 ${translatePhase(rawPhase)}` : "市场状态 --";
}

function renderSourceLabels() {
  const quoteSource = sourceDescription(state.quotePayload, "行情");
  const newsSource = sourceDescription(state.newsPayload, "新闻");
  const quoteDegraded = isDegraded(state.quotePayload);
  const newsDegraded = isDegraded(state.newsPayload);
  els.quoteSource.replaceChildren(span(`行情源 ${quoteSource}`));
  const quoteValidation = asObject(rootObject(state.quotePayload).quote_validation || rootObject(state.quotePayload).quoteValidation);
  if (quoteValidation.required_for_execution === true || quoteValidation.requiredForExecution === true) {
    const verified = quoteValidation.decision_verified === true || quoteValidation.decisionVerified === true;
    els.quoteSource.append(" ", span(verified ? "双源已核" : "校验待核", verified ? "tag" : "tag degraded"));
  }
  if (quoteDegraded) els.quoteSource.append(" ", span("降级", "tag degraded"));
  els.newsSource.replaceChildren(span(`新闻源 ${newsSource}`));
  if (newsDegraded) els.newsSource.append(" ", span("降级", "tag degraded"));
}

// 主备线页脚摘要：汇总每条数据链路最近一次实际生效的层级（纯函数，可单测）。
function dataSourceChainSummary(chainsInput) {
  const chains = Object.values(asObject(chainsInput)).filter((c) => c && c.tier);
  if (!chains.length) return { text: "", title: "" };
  const tierLabel = (tier) => (tier === "primary" ? "主源" : tier === "backup" ? "备源" : tier === "down" ? "中断" : "未观测");
  const backups = chains.filter((c) => c.tier === "backup");
  const downs = chains.filter((c) => c.tier === "down");
  const unknowns = chains.filter((c) => c.tier !== "primary" && c.tier !== "backup" && c.tier !== "down");
  const text = !backups.length && !downs.length
    ? (unknowns.length ? ` · 数据源主源运行(${unknowns.length}条未观测)` : " · 数据源全主源")
    : ` · ⚠ ${backups.map((c) => `${c.label}→备源`).concat(downs.map((c) => `${c.label}→中断`)).join(" ")}`;
  const title = chains
    .map((c) => `${c.label}：${tierLabel(c.tier)}${c.source ? `（${c.source}）` : ""}${c.updated_at ? ` · ${c.updated_at}` : ""}${c.detail ? ` · 降级原因 ${c.detail}` : ""}`)
    .join("\n");
  return { text, title };
}

function renderHealth() {
  if (state.healthError) {
    els.healthText.textContent = `服务状态：${state.healthError}`;
    return;
  }
  const health = rootObject(state.healthPayload);
  const status = pick(health, ["status", "state", "message"]);
  const portfolio = asObject(health.portfolio);
  const asOf = pick(portfolio, ["as_of", "asOf"]);
  const reloadError = pick(portfolio, ["reload_error", "reloadError"]);
  const official = asObject(health.official_quote_validation || health.officialQuoteValidation);
  const officialStatus = String(pick(official, ["status"]) || "");
  const base = status ? `服务状态：${translateHealth(status)}` : (state.healthPayload ? "服务状态：正常" : "服务检查中");
  const chainSummary = dataSourceChainSummary(health.data_source_chains);
  if (chainSummary.title) els.healthText.title = chainSummary.title;
  els.healthText.textContent = `${base}${officialStatus ? ` · 同花顺 ${translateHealth(officialStatus)}` : ""}${asOf ? ` · 账户快照 ${formatDateOnly(asOf)}` : ""}${reloadError ? " · 配置沿用旧版" : ""}${chainSummary.text}`;
}

function renderNotice() {
  const messages = [];
  const account = accountSnapshotDisplay();
  if (state.decisionError && !state.decisionPayload) messages.push(`统一快照：${state.decisionError}；已切换兼容模式，新模块不作跨时点判断`);
  if (state.quoteError) messages.push(`行情：${state.quoteError}；保留最近一次成功数据`);
  if (state.riskError) messages.push(`风险：${state.riskError}；保留最近一次快照`);
  if (state.newsError) messages.push(`新闻：${state.newsError}；稍后自动重试`);
  if (state.marketNewsError && !state.marketNews.length) messages.push("市场快讯源暂不可用；等待后台重试");
  if (!state.quoteError && isDegraded(state.quotePayload)) messages.push("部分行情源异常，当前展示最近可用数据");
  if (!state.newsError && isDegraded(state.newsPayload)) messages.push("部分新闻源不可用，当前展示已获取内容");
  const quoteValidation = asObject(rootObject(state.quotePayload).quote_validation || rootObject(state.quotePayload).quoteValidation);
  if ((quoteValidation.required_for_execution === true || quoteValidation.requiredForExecution === true)
      && quoteValidation.decision_verified !== true && quoteValidation.decisionVerified !== true) {
    messages.push("股票行情未通过同花顺交叉校验；当前仅观察，不形成可执行操作");
  }
  if (account.current === false) messages.push(`账户数量、现金和负债来自 ${account.date} 未核验快照；实时价格不等于实时持仓`);
  if (!state.riskError && state.riskPayload) {
    const risk = rootObject(state.riskPayload);
    if (risk.ok === false) {
      const gate = String(pick(risk, ["state", "data_state", "dataState"]) || "").toUpperCase();
      const hasMetrics = Object.keys(asObject(risk.metrics)).length > 0;
      messages.push(hasMetrics && gate.includes("DATA_UNVERIFIED")
        ? "账户门禁未通过，新增买入已冻结且不生成金额或股数"
        : "风险指标暂不可用，新增买入按冻结处理");
    }
  }
  els.noticeBar.hidden = messages.length === 0;
  els.noticeBar.textContent = messages.join("　");
  els.noticeBar.classList.toggle("is-error", Boolean(state.quoteError && !state.quoteReceivedAt));
}

function updateFreshness() {
  const quoteAge = quoteFreshnessLabel(state.quotePayload, state.quoteReceivedAt, state.quoteBaseAgeSeconds);
  const riskAge = ageLabel(state.riskReceivedAt, state.riskBaseAgeSeconds);
  const newsAge = ageLabel(state.newsReceivedAt, state.newsBaseAgeSeconds);
  const marketNewsAge = ageLabel(state.marketNewsReceivedAt, state.marketNewsBaseAgeSeconds);
  const decisionAge = ageLabel(state.decisionReceivedAt, 0);
  els.freshnessText.textContent = `行情 ${quoteAge} · 快讯 ${marketNewsAge} · 决策 ${decisionAge} · 持仓新闻 ${newsAge}`;
  renderConnectionState();
}

function quoteMarketState(payload) {
  const root = rootObject(payload);
  const status = asObject(root.market_status || root.marketStatus);
  return String(
    pick(status, ["state", "status", "phase"])
      || pick(root, ["market_phase", "marketPhase", "session", "trade_status"])
      || "",
  ).trim().toLowerCase();
}

function isActiveQuoteMarketState(payload) {
  const marketState = quoteMarketState(payload);
  if (!marketState) return true;
  return !/closed|close|lunch|noon|pre[_ -]?open|before|post|after|收盘|休市|午间|盘前/.test(marketState);
}

function quotePollIntervalMs(payload) {
  return isActiveQuoteMarketState(payload) ? QUOTE_INTERVAL_MS : QUOTE_INACTIVE_INTERVAL_MS;
}

function quoteClientResponseStale(payload, clientAgeSeconds) {
  if (!Number.isFinite(clientAgeSeconds)) return true;
  if (!isActiveQuoteMarketState(payload)) return false;
  const toleranceSeconds = Math.max(20, quotePollIntervalMs(payload) / 1000 * 3);
  return clientAgeSeconds > toleranceSeconds;
}

function quoteFreshnessLabel(payload, receivedAt, baseAgeSeconds = 0) {
  const root = rootObject(payload);
  const status = asObject(root.market_status || root.marketStatus);
  const marketState = quoteMarketState(root);
  const quoteTime = pick(status, ["quote_time", "quoteTime"]);
  if (!isActiveQuoteMarketState(root) && quoteTime) {
    const formatted = formatMarketQuoteTimestamp(quoteTime);
    const phaseLabel = translatePhase(marketState);
    if (formatted) return `${formatted}${phaseLabel ? `（${phaseLabel}）` : ""}`;
  }
  return ageLabel(receivedAt, baseAgeSeconds);
}

function normalizeDailyPnlHistory(payload) {
  const outer = asObject(payload);
  const root = { ...outer, ...asObject(outer.data) };
  const recordsRaw = pick(root, ["records", "items", "days", "history"]);
  const startDate = normalizeDailyPnlDate(pick(root, ["start_date", "startDate"])) || DAILY_PNL_HISTORY_START_DATE;
  const declaredEndDate = normalizeDailyPnlDate(pick(root, ["end_date", "endDate", "as_of", "asOf"]));
  const today = shanghaiCalendarDate();
  const endDate = declaredEndDate && declaredEndDate < today ? declaredEndDate : today;
  const lowerBound = startDate > DAILY_PNL_HISTORY_START_DATE ? startDate : DAILY_PNL_HISTORY_START_DATE;
  const recordsByDate = new Map();

  for (const raw of Array.isArray(recordsRaw) ? recordsRaw : []) {
    const item = asObject(raw);
    const date = normalizeDailyPnlDate(pick(item, ["trade_date", "tradeDate", "date", "session"]));
    if (!date || date < lowerBound || date > endDate) continue;
    const coverage = asObject(item.coverage);
    const unlistedSymbolsRaw = pick(item, ["unlisted_symbols", "unlistedSymbols"]);
    const missingSymbolsRaw = pick(item, ["missing_symbols", "missingSymbols"]);
    const unlistedSymbols = arrayText(unlistedSymbolsRaw);
    const missingSymbols = arrayText(missingSymbolsRaw);
    const eligiblePositions = firstNumber(
      pickNumber(item, ["eligible_positions", "eligiblePositions", "eligible_count", "eligibleCount"]),
      pickNumber(coverage, ["eligible_positions", "eligiblePositions", "eligible_count", "eligibleCount", "eligible"]),
    );
    const coveredPositions = firstNumber(
      pickNumber(item, ["covered_positions", "coveredPositions", "covered_count", "coveredCount", "coverage_count", "coverageCount"]),
      pickNumber(coverage, ["covered_positions", "coveredPositions", "covered_count", "coveredCount", "covered"]),
    );
    const unlistedPositions = firstNumber(
      pickNumber(item, ["unlisted_positions", "unlistedPositions", "unlisted_count", "unlistedCount"]),
      pickNumber(coverage, ["unlisted_positions", "unlistedPositions", "unlisted_count", "unlistedCount", "unlisted"]),
      Array.isArray(unlistedSymbolsRaw) ? unlistedSymbols.length : null,
    );
    const missingPositions = firstNumber(
      pickNumber(item, ["missing_positions", "missingPositions", "missing_count", "missingCount"]),
      pickNumber(coverage, ["missing_positions", "missingPositions", "missing_count", "missingCount", "missing"]),
      Array.isArray(missingSymbolsRaw) ? missingSymbols.length : null,
    );
    const explicitCoveragePct = firstNumber(
      pickNumber(item, ["coverage_pct", "coveragePct"]),
      pickNumber(coverage, ["coverage_pct", "coveragePct", "pct"]),
    );
    const coverageRatio = firstNumber(
      pickNumber(item, ["coverage_ratio", "coverageRatio"]),
      pickNumber(coverage, ["coverage_ratio", "coverageRatio", "ratio"]),
    );
    const derivedCoveragePct = isFiniteNumber(coveredPositions) && isFiniteNumber(eligiblePositions) && eligiblePositions > 0
      ? coveredPositions / eligiblePositions * 100
      : null;
    const coveragePct = firstNumber(
      explicitCoveragePct,
      isFiniteNumber(coverageRatio) ? coverageRatio * 100 : null,
      derivedCoveragePct,
    );
    const dailyPnl = firstNumber(pickNumber(item, ["daily_pnl_cny", "dailyPnlCny", "daily_pnl", "dailyPnl"]));
    const status = String(pick(item, ["status", "state"]) || (isFiniteNumber(dailyPnl) ? "final" : "partial")).trim().toLowerCase();
    recordsByDate.set(date, {
      date,
      dailyPnl,
      coveredDailyPnl: firstNumber(pickNumber(item, ["covered_daily_pnl_cny", "coveredDailyPnlCny"])),
      cumulativePnl: firstNumber(pickNumber(item, ["cumulative_pnl_cny", "cumulativePnlCny"])),
      coveragePct: isFiniteNumber(coveragePct) ? Math.max(0, Math.min(100, coveragePct)) : null,
      eligiblePositions,
      coveredPositions,
      unlistedPositions,
      missingPositions,
      unlistedSymbols,
      missingSymbols,
      status,
      source: dailyPnlSourceLabel(pick(item, ["source", "provider", "data_source", "dataSource"])),
      basis: dailyPnlBasisLabel(pick(item, ["basis", "calculation_basis", "calculationBasis"])),
    });
  }

  const quality = asObject(root.quality || root.data_quality || root.dataQuality);
  const cache = asObject(root.cache);
  const periodTotals = asObject(root.period_totals || root.periodTotals);
  const nestedPeriodTotal = firstDefined(periodTotals, ["daily_pnl_cny", "dailyPnlCny"]);
  const rootPeriodTotal = firstDefined(root, ["period_total_pnl_cny", "periodTotalPnlCny"]);
  const periodCompleteRaw = firstDefined(periodTotals, ["complete_price_coverage", "completePriceCoverage", "complete"]);
  const periodTotalDeclared = nestedPeriodTotal !== undefined
    || rootPeriodTotal !== undefined
    || periodCompleteRaw !== undefined;
  const periodTotalRaw = nestedPeriodTotal !== undefined ? nestedPeriodTotal : rootPeriodTotal;
  const available = firstBoolean(root.available);
  return {
    ok: root.ok !== false,
    schemaVersion: String(pick(root, ["schema_version", "schemaVersion"]) || ""),
    available: available == null ? recordsByDate.size > 0 : available,
    startDate: lowerBound,
    endDate,
    generatedAt: pick(root, ["generated_at", "generatedAt", "updated_at", "updatedAt"]),
    message: pick(root, ["message", "note"]),
    stale: firstBoolean(
      pick(root, ["stale", "is_stale", "isStale"]),
      pick(cache, ["stale", "is_stale", "isStale"]),
    ) === true,
    basis: dailyPnlBasisLabel(root.basis || root.portfolio_binding || root.portfolioBinding),
    qualityStatus: String(pick(quality, ["status", "level", "label"]) || ""),
    periodTotalDeclared,
    periodTotalPnl: toNumber(periodTotalRaw),
    periodCoveredPnl: firstNumber(pickNumber(periodTotals, ["covered_daily_pnl_cny", "coveredDailyPnlCny"])),
    periodComplete: firstBoolean(periodCompleteRaw),
    warnings: arrayText(pick(root, ["warnings", "warning"])).map((item) => String(item).slice(0, 180)),
    records: [...recordsByDate.values()].sort((left, right) => left.date.localeCompare(right.date)),
  };
}

function normalizeDailyPnlDate(value) {
  const raw = String(value ?? "").trim();
  const matched = raw.match(/^(\d{4})[-/]?(\d{2})[-/]?(\d{2})/);
  if (!matched) return "";
  const year = Number(matched[1]);
  const month = Number(matched[2]);
  const day = Number(matched[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return "";
  return `${matched[1]}-${matched[2]}-${matched[3]}`;
}

function shanghaiCalendarDate(now = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function dailyPnlSourceLabel(value) {
  if (typeof value === "string") return value.trim();
  const source = asObject(value);
  return String(pick(source, ["label", "name", "provider", "kind", "source"]) || "").trim();
}

function dailyPnlBasisLabel(value) {
  if (typeof value === "string") return value.trim();
  const basis = asObject(value);
  const explicit = pick(basis, ["display_note", "displayNote", "description", "message", "note", "label"]);
  if (explicit) return String(explicit).trim();
  return [
    pick(basis, ["method", "mode", "kind"]),
    pick(basis, ["adjustment", "price_adjustment", "priceAdjustment"]),
    pick(basis, ["portfolio_as_of", "portfolioAsOf", "snapshot_date", "snapshotDate"]),
  ].filter((item) => item != null && item !== "").map(String).join(" · ");
}

function normalizeIntradayPnl(payload) {
  const outer = asObject(payload);
  const root = { ...outer, ...asObject(outer.data) };
  const asOf = pick(root, ["as_of", "asOf", "trade_date", "tradeDate", "date"]);
  const totalRaw = pick(root, ["total_series", "totalSeries", "portfolio_series", "portfolioSeries"])
    || pick(asObject(root.total), ["points", "series", "data"]);
  const positionsRaw = pick(root, ["positions", "position_series", "positionSeries", "series_by_symbol", "seriesBySymbol"]);
  const totalSeries = normalizePnlSeries(totalRaw, asOf);
  const positions = [];
  for (const [key, raw] of collectionEntries(positionsRaw)) {
    const item = Array.isArray(raw) ? { series: raw } : asObject(raw);
    const symbol = normalizeCanonicalMarketSymbol(
      pick(item, ["thscode", "symbol", "code", "ticker"]) || key,
    );
    if (!symbol) continue;
    const series = normalizePnlSeries(
      pick(item, ["series", "pnl_series", "pnlSeries", "points", "data"]) || (Array.isArray(raw) ? raw : []),
      asOf,
    );
    positions.push({
      symbol,
      name: String(pick(item, ["name", "security_name", "stock_name"]) || symbol),
      series,
      tPlan: (() => {
        const raw = pick(item, ["t_plan", "tPlan"]);
        return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : null;
      })(),
    });
  }
  const account = asObject(root.account_snapshot || root.accountSnapshot || root.account);
  const aggSignalRaw = pick(root, ["agg_t_signal", "aggTSignal"]);
  return {
    ok: root.ok !== false,
    asOf,
    aggTSignal: aggSignalRaw && typeof aggSignalRaw === "object" ? aggSignalRaw : null,
    generatedAt: pick(root, ["generated_at", "generatedAt", "updated_at", "updatedAt"]),
    marketPhase: String(
      pick(root, ["market_phase", "marketPhase", "trading_phase", "tradingPhase", "phase"])
        || pick(asObject(root.latest_point || root.latestPoint), ["session"])
        || "",
    ),
    stale: Boolean(pick(root, ["stale", "is_stale", "isStale"])),
    refreshing: Boolean(root.refreshing),
    message: pick(root, ["message", "note"]),
    account: {
      asOf: pick(account, ["as_of", "asOf", "date", "snapshot_date", "snapshotDate"])
        || pick(root, ["account_as_of", "accountAsOf"]),
      current: firstBoolean(
        pick(account, ["current_verified", "currentVerified", "verified", "current"]),
        pick(root, ["account_current", "accountCurrent", "account_verified", "accountVerified"]),
      ),
    },
    totalSeries,
    positions,
  };
}

function normalizePnlSeries(raw, asOf) {
  if (!Array.isArray(raw)) return [];
  const points = [];
  for (const value of raw) {
    const item = Array.isArray(value) ? {} : asObject(value);
    const time = Array.isArray(value)
      ? value[0]
      : pick(item, ["time", "timestamp", "ts", "datetime", "observed_at", "observedAt", "quote_time", "quoteTime"]);
    const pnl = Array.isArray(value)
      ? toNumber(value[1])
      : firstNumber(pickNumber(item, ["pnl", "day_pnl", "dayPnl", "daily_pnl_cny", "dailyPnlCny", "value", "amount"]));
    const timeValue = intradayTimeValue(time, asOf);
    if (!isFiniteNumber(timeValue) || !isFiniteNumber(pnl)) continue;
    const extra = {};
    for (const key of PNL_POINT_EXTRA_KEYS) {
      const value = pick(item, [key]);
      if (value !== undefined && value !== null && value !== "") extra[key] = value;
    }
    points.push({
      time,
      timeValue,
      pnl,
      session: String(pick(item, ["session", "trading_session", "tradingSession"]) || ""),
      ...extra,
    });
  }
  points.sort((a, b) => a.timeValue - b.timeValue);
  const deduplicated = [];
  for (const point of points) {
    if (deduplicated.length && deduplicated[deduplicated.length - 1].timeValue === point.timeValue) {
      deduplicated[deduplicated.length - 1] = point;
    } else {
      deduplicated.push(point);
    }
  }
  return deduplicated;
}

const PNL_POINT_EXTRA_KEYS = Object.freeze(["source", "pnl_recorded", "pnlRecorded", "covered", "required"]);

function intradayTimeValue(value, asOf) {
  const parsed = parseDate(value);
  if (parsed) return parsed.getTime();
  const matched = String(value || "").match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (!matched) return null;
  const date = String(asOf || "").match(/^\d{4}-\d{2}-\d{2}/)?.[0];
  if (date) {
    const withDate = parseDate(`${date}T${matched[1].padStart(2, "0")}:${matched[2]}:${matched[3] || "00"}+08:00`);
    if (withDate) return withDate.getTime();
  }
  return (Number(matched[1]) * 3600 + Number(matched[2]) * 60 + Number(matched[3] || 0)) * 1000;
}

function syncIntradayPnlSelector() {
  // 今日无台账（周末/未开盘）时实时 payload 没有持仓，用历史缓存里的持仓兜底，
  // 保证任意时刻都能选择个股曲线（做T标记等个股视图依赖它）。
  let positions = state.intradayPnl?.positions || [];
  if (!positions.length) {
    for (const entry of state.intradayHistory.cache.values()) {
      const snapshotPositions = entry?.snapshot?.positions;
      if (Array.isArray(snapshotPositions) && snapshotPositions.length) {
        positions = snapshotPositions;
        break;
      }
    }
  }
  const filterKey = positions.map((item) => `${item.symbol}:${item.name}`).join("|");
  if (filterKey !== state.intradayPnlFilterKey) {
    state.intradayPnlFilterKey = filterKey;
    const fragment = document.createDocumentFragment();
    const total = document.createElement("option");
    total.value = "total";
    total.textContent = "全部持仓合计";
    fragment.append(total);
    for (const item of positions) {
      const option = document.createElement("option");
      option.value = item.symbol;
      option.textContent = item.name || item.symbol;
      fragment.append(option);
    }
    els.intradayPnlSymbol.replaceChildren(fragment);
  }
  const stillExists = state.selectedPnlSymbol === "total"
    || positions.some((item) => item.symbol === state.selectedPnlSymbol);
  state.selectedPnlSymbol = stillExists ? state.selectedPnlSymbol : "total";
  els.intradayPnlSymbol.value = state.selectedPnlSymbol;
}

function normalizeAdvice(raw, fallbackTime = null) {
  if (typeof raw === "string") return { text: raw.trim(), asOf: fallbackTime || null };
  const item = asObject(raw);
  const text = pick(item, ["text", "advice", "recommendation", "suggestion", "action", "message", "current_advice", "currentAdvice"]);
  const asOf = pick(item, ["as_of", "asOf", "advised_at", "advisedAt", "advice_time", "adviceTime", "generated_at", "generatedAt", "updated_at", "updatedAt", "time"])
    || fallbackTime;
  return { text: text == null ? "" : String(text).trim(), asOf: asOf || null };
}

function normalizeAdviceMapping(raw) {
  const mapping = {};
  const envelope = asObject(raw);
  const embedded = firstDefined(envelope, ["positions", "items", "advice_by_symbol", "adviceBySymbol"])
    ?? firstDefined(asObject(envelope.data), ["positions", "items", "advice_by_symbol", "adviceBySymbol"]);
  const isSingleRow = Boolean(pick(envelope, ["thscode", "symbol", "code", "ticker"]));
  const collection = Array.isArray(raw) || isSingleRow
    ? (isSingleRow ? [raw] : raw)
    : embedded !== undefined
      ? embedded
      : raw;
  const source = collectionEntries(collection);
  for (const [key, value] of source) {
    const item = asObject(value);
    const symbol = normalizeSymbol(pick(item, ["thscode", "symbol", "code", "ticker"]) || key);
    if (!symbol) continue;
    const advice = normalizeAdvice(value);
    if (advice.text || advice.asOf) mapping[symbol] = advice;
  }
  return mapping;
}

function normalizePositions(payload, adviceBySymbol = {}) {
  const root = rootObject(payload);
  const candidates = [
    root.positions, root.quotes, root.holdings,
    asObject(root.portfolio).positions, asObject(root.portfolio).holdings,
    asObject(root.data).positions, asObject(root.data).quotes, asObject(root.data).holdings,
  ];
  let raw = candidates.find(Array.isArray);
  if (!raw && Array.isArray(root.data)) raw = root.data;
  if (!raw) raw = [];
  return raw.map(normalizePosition).filter((item) => item.symbol).map((item) => {
    const external = normalizeAdvice(adviceBySymbol[item.symbol]);
    return {
      ...item,
      advice: item.advice.text ? item.advice : external,
    };
  });
}

function normalizePosition(raw) {
  const item = asObject(raw);
  const symbol = normalizeSymbol(pick(item, ["thscode", "symbol", "code", "ticker", "security_code"]));
  const price = firstNumber(
    pickNumber(item, ["last_price", "lastPrice", "price", "current", "now", "close"]),
    pickNumber(asObject(item.quote), ["last_price", "price", "current"]),
  );
  const prevClose = firstNumber(
    pickNumber(item, ["prev_price", "prevPrice", "prev_close", "prevClose", "previous_close", "pre_close"]),
    pickNumber(asObject(item.quote), ["prev_price", "prev_close"]),
  );
  const change = firstNumber(pickNumber(item, ["price_change", "change", "change_amount"]), isFiniteNumber(price) && isFiniteNumber(prevClose) ? price - prevClose : null);
  let changePct = firstNumber(pickNumber(item, ["price_change_ratio_pct", "change_pct", "changePct", "pct_change", "percent", "change_percent"]));
  if (!isFiniteNumber(changePct) && isFiniteNumber(change) && prevClose) changePct = change / prevClose * 100;
  if (isFiniteNumber(changePct) && Math.abs(changePct) <= 0.25 && /ratio(?!_pct)|rate/i.test(findNumericKey(item, changePct))) changePct *= 100;
  const quantity = firstNumber(pickNumber(item, ["quantity", "shares", "position", "volume_held"]));
  const marketValue = firstNumber(pickNumber(item, ["market_value", "marketValue", "position_value"]), isFiniteNumber(price) && isFiniteNumber(quantity) ? price * quantity : null);
  const dayPnl = firstNumber(pickNumber(item, ["day_pnl", "dayPnl", "daily_pnl", "today_profit"]), isFiniteNumber(change) && isFiniteNumber(quantity) ? change * quantity : null);
  const weightSecurities = firstNumber(pickNumber(item, ["weight_pct_securities", "weightPctSecurities", "securities_weight_pct"]));
  const weightNav = firstNumber(pickNumber(item, ["weight_pct_nav", "weightPctNav", "weight_pct", "nav_weight_pct"]));
  const turnover = firstNumber(pickNumber(item, ["turnover_pct", "turnoverPct", "turnover"]));
  const pe = firstNumber(pickNumber(item, ["pe_ttm", "peTtm", "pe"]));
  const pb = firstNumber(pickNumber(item, ["pb", "pb_ratio"]));
  const advice = normalizeAdvice(
    pick(item, ["advice", "current_advice", "currentAdvice", "operation_advice", "operationAdvice"]),
    pick(item, ["advice_time", "adviceTime", "advised_at", "advisedAt"]),
  );
  return {
    symbol,
    name: pick(item, ["name", "security_name", "stock_name"]) || symbol,
    quantity,
    price,
    prevClose,
    change,
    changePct,
    marketValue,
    dayPnl,
    weightSecurities,
    weightNav,
    turnover,
    pe,
    pb,
    account: pick(item, ["account", "account_name", "account_type", "account_id"]) || "",
    industry: pick(item, ["industry", "sector", "theme"]) || "",
    quoteTime: pick(item, ["quote_time", "quoteTime", "trade_time", "time", "timestamp", "updated_at", "datetime"]),
    isStale: Boolean(pick(item, ["is_stale", "isStale", "stale"])),
    dataSource: String(pick(item, ["data_source", "dataSource", "source"]) || ""),
    validationState: String(pick(asObject(item.official_validation || item.officialValidation), ["state", "status"]) || ""),
    advice,
  };
}

function positionWeightCell(item, totalMarketValue) {
  const calculated = isFiniteNumber(item.marketValue) && totalMarketValue > 0 ? item.marketValue / totalMarketValue * 100 : null;
  const securities = firstNumber(item.weightSecurities, calculated);
  const cell = document.createElement("td");
  cell.className = "numeric position-weight";
  const primary = span(formatWeightPct(securities), "position-weight-primary");
  primary.title = "占证券总市值权重";
  cell.append(primary);
  if (isFiniteNumber(item.weightNav)) {
    const secondary = span(`净 ${formatWeightPct(item.weightNav)}`, "position-weight-secondary");
    secondary.title = "占净权益权重（受现金与负债影响）";
    cell.append(secondary);
  }
  return cell;
}

function resolvedPositions() {
  return state.positions.map((position) => ({ ...position }));
}

function normalizeMarkets(payload) {
  const root = rootObject(payload);
  const market = asObject(root.market);
  const markets = asObject(root.markets);
  let raw = firstCollection(root.indices, root.indexes, markets.indices, markets.indexes, market.indices, market.indexes, asObject(root.data).indices);
  const rows = collectionEntries(raw);
  return rows.map(([key, value]) => normalizeMarket(value, key)).filter((item) => item.name || item.symbol);
}

function normalizeGlobals(payload) {
  const root = rootObject(payload);
  const market = asObject(root.market);
  const markets = asObject(root.markets);
  const raw = firstCollection(root.global, root.globals, root.global_markets, markets.global, market.global, asObject(root.data).global);
  return collectionEntries(raw).map(([key, value]) => normalizeMarket(value, key)).filter((item) => item.name || item.symbol);
}

function normalizeOvernight(payload) {
  const root = rootObject(payload);
  const market = asObject(root.market);
  const markets = asObject(root.markets);
  const raw = firstCollection(markets.overnight, market.overnight, asObject(root.data).overnight);
  const rows = collectionEntries(raw).map(([key, value]) => normalizeMarket(value, key)).filter((item) => item.name || item.symbol);
  // 过渡兼容：收盘后行情按设计冻结，旧缓存里原油/黄金还在全球参考（hf_CL/hf_GC）
  // 而隔夜商品组缺 b_CL/b_GC。迁移进隔夜列表保证新旧 payload 分组一致；
  // 下个交易时段起服务端已直出 b_CL/b_GC，此分支自然失效。
  const hasSymbol = (sym) => rows.some((row) => row.symbol === sym);
  if (!hasSymbol("b_CL") || !hasSymbol("b_GC")) {
    for (const legacyRow of normalizeGlobals(payload)) {
      if (legacyRow.symbol === "hf_CL" && !hasSymbol("b_CL")) rows.push({ ...legacyRow, symbol: "b_CL", group: "商品" });
      if (legacyRow.symbol === "hf_GC" && !hasSymbol("b_GC")) rows.push({ ...legacyRow, symbol: "b_GC", group: "商品" });
    }
  }
  return rows;
}

function normalizeMarket(raw, fallbackName) {
  const item = asObject(raw);
  const symbol = String(pick(item, ["thscode", "symbol", "code", "ticker"]) || "");
  const suppliedName = pick(item, ["name", "display_name", "label"]) || fallbackName || "";
  const name = INDEX_ALIASES[suppliedName] || INDEX_ALIASES[symbol] || suppliedName;
  let changePct = firstNumber(pickNumber(item, ["price_change_ratio_pct", "change_pct", "changePct", "pct_change", "percent", "change_percent"]));
  const price = firstNumber(pickNumber(item, ["last_price", "lastPrice", "price", "current", "close", "value"]));
  const prev = firstNumber(pickNumber(item, ["prev_price", "prevPrice", "prev_close", "previous_close"]));
  if (!isFiniteNumber(changePct) && isFiniteNumber(price) && prev) changePct = (price / prev - 1) * 100;
  return {
    name,
    symbol,
    group: pick(item, ["group"]) || "",
    price,
    changePct,
    turnover: firstNumber(pickNumber(item, ["turnover", "amount", "turnover_yi", "amount_yi"])),
    quoteTime: pick(item, ["quote_time", "quoteTime", "trade_time", "time", "timestamp", "updated_at", "datetime"]),
    isStale: Boolean(pick(item, ["is_stale", "isStale", "stale"])),
  };
}

function normalizeBreadth(payload) {
  const root = rootObject(payload);
  const raw = firstCollection(root.breadth, asObject(root.markets).breadth, asObject(root.market).breadth, asObject(root.data).breadth);
  const exchanges = asObject(asObject(raw).exchanges);
  const rows = Object.keys(exchanges).length
    ? [...Object.entries(exchanges), ["沪深合计", raw]]
    : raw && typeof raw === "object" && !Array.isArray(raw) && (raw.up != null || raw.down != null)
      ? [["沪深合计", raw]]
    : collectionEntries(raw);
  return rows.map(([key, value]) => {
    const item = asObject(value);
    return {
      name: pick(item, ["name", "market", "label"]) || key || "市场",
      up: firstNumber(pickNumber(item, ["up", "advance", "advancers", "rise"])),
      flat: firstNumber(pickNumber(item, ["flat", "unchanged", "equal"])),
      down: firstNumber(pickNumber(item, ["down", "decline", "decliners", "fall"])),
      turnover: firstNumber(pickNumber(item, ["amount_yi", "turnover_yi", "amount", "turnover"])),
    };
  }).filter((item) => item.up != null || item.down != null);
}

function normalizeNews(payload) {
  const root = rootObject(payload);
  const raw = [root.news, root.items, root.articles, asObject(root.data).news, asObject(root.data).items, root.data].find(Array.isArray) || [];
  const seen = new Set();
  const normalized = [];
  for (const value of raw) {
    const item = asObject(value);
    const title = String(pick(item, ["title", "headline", "name", "content_title"]) || "").trim();
    if (!title) continue;
    let symbols = pick(item, ["symbols", "thscodes", "codes", "related_symbols", "relatedCodes"]);
    if (!Array.isArray(symbols)) symbols = symbols ? String(symbols).split(/[,，\s]+/) : [];
    const singleSymbol = pick(item, ["thscode", "symbol", "code", "stock_code"]);
    if (singleSymbol) symbols.push(singleSymbol);
    symbols = [...new Set(symbols.map(normalizeSymbol).filter(Boolean))];
    if (!symbols.length) {
      const matched = state.positions.filter((holding) => title.includes(holding.name)).map((holding) => holding.symbol);
      symbols.push(...matched);
    }
    const url = String(pick(item, ["url", "link", "article_url", "news_url"]) || "");
    const key = `${title.replace(/\s+/g, "").toLowerCase()}|${url}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const rawImportance = pick(item, ["important", "is_important", "importance", "level", "priority"]);
    normalized.push({
      title,
      url,
      symbols,
      source: String(pick(item, ["source", "media", "provider", "origin"]) || ""),
      publishedAt: pick(item, ["published_at", "publish_time", "publishedAt", "datetime", "time", "date"]),
      important: rawImportance === true || (isFiniteNumber(toNumber(rawImportance)) && toNumber(rawImportance) > 0) || /important|high|重大|重要|高/i.test(String(rawImportance || "")),
    });
  }
  return normalized.sort((a, b) => (
    Number(b.important) - Number(a.important)
    || timestampValue(b.publishedAt) - timestampValue(a.publishedAt)
  ));
}

function normalizeMarketNews(payload) {
  const root = rootObject(payload);
  const raw = [root.items, root.news, asObject(root.data).items, root.data].find(Array.isArray) || [];
  const seen = new Set();
  const normalized = [];
  for (const value of raw) {
    const item = asObject(value);
    const title = String(pick(item, ["title", "headline", "summary"]) || "").trim().slice(0, 300);
    if (!title) continue;
    const id = String(pick(item, ["id", "event_id", "eventId"]) || "");
    const publishedAt = pick(item, ["published_at", "publishedAt", "showTime", "time"]);
    const sourceIds = pick(item, ["source_ids", "sourceIds"]);
    const source = String(
      pick(item, ["source", "provider"])
        || (Array.isArray(sourceIds) ? sourceIds.map((key) => SOURCE_LABELS[key] || key).join(" / ") : ""),
    );
    const url = String(pick(item, ["url", "link", "shareurl", "share_url"]) || "");
    const key = id || `${publishedAt || ""}|${title.replace(/\s+/g, "").toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const riskTags = pick(item, ["risk_tags", "riskTags"]);
    const opportunityTags = pick(item, ["opportunity_tags", "opportunityTags"]);
    const symbolsRaw = pick(item, ["symbols"]);
    normalized.push({
      id: key,
      title,
      url,
      source,
      publishedAt,
      symbols: Array.isArray(symbolsRaw) ? symbolsRaw.map((value) => String(value).toUpperCase()).filter(Boolean) : [],
      riskTags: Array.isArray(riskTags) ? riskTags.map(String).filter(Boolean) : [],
      opportunityTags: Array.isArray(opportunityTags) ? opportunityTags.map(String).filter(Boolean) : [],
    });
  }
  return normalized.sort((a, b) => timestampValue(b.publishedAt) - timestampValue(a.publishedAt));
}

function extractPortfolioSummary(payload) {
  const root = rootObject(payload);
  const portfolio = asObject(root.portfolio);
  return asObject(root.portfolio_summary || root.portfolioSummary || root.summary || portfolio.summary || (portfolio.market_value != null ? portfolio : null) || asObject(root.data).summary);
}

function extractTimestamp(payload) {
  const root = rootObject(payload);
  return pick(root, ["generated_at", "updated_at", "as_of", "timestamp", "server_time", "quote_time"])
    || pick(asObject(root.meta), ["generated_at", "updated_at", "timestamp"])
    || null;
}

function sourceDescription(payload, kind) {
  if (!payload) return "--";
  const root = rootObject(payload);
  let source = root.source || root.provider || asObject(root.meta).source;
  if (!source && root.sources) {
    const sources = root.sources;
    if (typeof sources === "string") source = sources;
    else if (Array.isArray(sources)) source = sources.map(sourceName).filter(Boolean).slice(0, 2).join(" + ");
    else {
      const preferred = kind === "行情" ? (sources.quotes || sources.market || sources.primary) : (sources.news || sources.primary);
      source = preferred || Object.values(sources).filter((value) => typeof value === "string").slice(0, 2).join(" + ");
    }
  }
  if (!source && kind === "新闻" && root.source_status) {
    const statuses = root.source_status;
    if (typeof statuses === "string") source = statuses;
    else if (Array.isArray(statuses)) source = statuses.map(sourceName).filter(Boolean).join(" + ");
    else if (typeof statuses === "object") source = Object.entries(statuses)
      .filter(([, value]) => value !== false)
      .map(([name, value]) => sourceName(value) || SOURCE_LABELS[name] || name)
      .slice(0, 3)
      .join(" + ");
  }
  if (!source) source = asObject(root.cache).source;
  if (Array.isArray(source)) source = source.map(sourceName).filter(Boolean).join(" + ");
  else if (source && typeof source === "object") source = source.name || source.provider || source.source;
  return source ? (SOURCE_LABELS[String(source)] || String(source)) : "本地服务";
}

function isDegraded(payload) {
  if (!payload) return false;
  const root = rootObject(payload);
  if (root.degraded === true || root.stale === true || asObject(root.cache).degraded === true || /degraded|fallback|partial|降级/i.test(String(root.status || ""))) return true;
  if (Array.isArray(asObject(root.cache).errors) && asObject(root.cache).errors.length) return true;
  if (root.source_status && typeof root.source_status === "object" && Object.values(root.source_status).some((value) => value === false || asObject(value).ok === false || /error|failed|degraded|失败|降级/i.test(String(asObject(value).status || "")))) return true;
  const sources = root.sources;
  return Boolean(sources && typeof sources === "object" && Object.values(sources).some((value) => {
    const source = asObject(value);
    return source.degraded === true || source.ok === false || /degraded|fallback|error|降级/i.test(String(source.status || ""));
  }));
}

function syncNewsFilter() {
  const previous = state.selectedSymbol;
  const filterKey = state.positions.map((holding) => `${holding.symbol}:${holding.name}`).join("|");
  if (filterKey === state.newsFilterKey) return;
  state.newsFilterKey = filterKey;
  const fragment = document.createDocumentFragment();
  const allOption = document.createElement("option");
  allOption.value = "all";
  allOption.textContent = "全部持仓";
  fragment.append(allOption);
  for (const holding of state.positions) {
    const option = document.createElement("option");
    option.value = holding.symbol;
    option.textContent = holding.name;
    fragment.append(option);
  }
  els.newsSymbolFilter.replaceChildren(fragment);
  const stillExists = previous === "all" || state.positions.some((holding) => holding.symbol === previous);
  state.selectedSymbol = stillExists ? previous : "all";
  els.newsSymbolFilter.value = state.selectedSymbol;
}

function selectNewsSymbol(symbol) {
  state.selectedSymbol = symbol;
  els.newsSymbolFilter.value = symbol;
  renderNews();
  renderPositions();
  const panelTop = document.querySelector(".news-panel")?.getBoundingClientRect().top ?? 0;
  if (window.innerWidth < 1120 && panelTop > window.innerHeight) document.querySelector(".news-panel")?.scrollIntoView({ block: "start" });
}

function summaryItem(label, value, className) {
  const item = div("summary-item");
  const strong = document.createElement("strong");
  strong.textContent = value;
  if (className) strong.className = className;
  item.append(span(label), strong);
  return item;
}

function riskStatePresentation(value, ok) {
  const raw = String(value || "").toLowerCase();
  if (/reduce[_ -]?only[_ -]?critical|仅减仓/.test(raw)) return { label: "严重（仅减仓）", className: "is-critical" };
  if (/risk[_ -]?off/.test(raw)) return { label: "风险超限 · 禁止加仓", className: "is-critical" };
  if (/data[_ -]?unverified|unverified|数据未验证/.test(raw)) return { label: "数据未验证 · 已冻结", className: "is-critical" };
  if (/critical|red|严重/.test(raw)) return { label: "高风险", className: "is-critical" };
  if (ok === false || /unavailable|failed|error|不可用/.test(raw)) {
    return { label: /critical|red|严重/.test(raw) ? "高风险" : "不可用 · 已冻结", className: "is-critical" };
  }
  if (/warning|warn|yellow|amber|caution|medium|警|黄/.test(raw)) return { label: "警戒", className: "is-warning" };
  if (/green|normal|healthy|ok|pass|正常|绿/.test(raw)) return { label: "正常", className: "is-ok" };
  if (ok === true) return { label: "已计算", className: "is-ok" };
  return { label: "待计算", className: "is-pending" };
}

function riskPanelStatePresentation(root) {
  const gate = pick(root, ["state", "status"]);
  const portfolioState = pick(root, ["risk_state", "riskState"]);
  const gateRaw = String(gate || "").toLowerCase();
  if (/data[_ -]?unverified|unverified|数据未验证/.test(gateRaw)) {
    const portfolio = riskStatePresentation(portfolioState, null);
    return {
      label: `账户未核验 · 组合风险：${portfolio.label}`,
      className: portfolio.className === "is-pending" ? "is-critical" : portfolio.className,
    };
  }
  return riskStatePresentation(portfolioState || gate, root.ok);
}

function metricNumber(metrics, keys) {
  const value = pick(metrics, keys);
  if (value && typeof value === "object") {
    return firstNumber(pickNumber(value, ["value", "pct", "percent", "percentage", "amount"]));
  }
  return toNumber(value);
}

function metricDescriptor(value, keys) {
  if (value == null) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number") return "";
  const descriptor = pick(asObject(value), keys);
  return descriptor == null ? "" : String(descriptor).trim();
}

function stressScenarioDescriptor(metrics) {
  const raw = metrics.worst_stress_scenario || metrics.worstStressScenario;
  const descriptor = metricDescriptor(raw, ["label", "name", "scenario"]);
  const scenarios = Array.isArray(metrics.stress_scenarios)
    ? metrics.stress_scenarios
    : Array.isArray(metrics.stressScenarios) ? metrics.stressScenarios : [];
  const match = scenarios.find((item) => {
    const candidate = asObject(item);
    return String(pick(candidate, ["scenario", "code", "name"]) || "") === String(descriptor || raw || "");
  });
  return String(pick(asObject(match), ["label", "name", "scenario"]) || descriptor || "");
}

function formatRiskPct(value) {
  return isFiniteNumber(value) ? `${value.toFixed(2)}%` : "待计算";
}

function formatDescriptorPct(descriptor, value) {
  const percentage = formatRiskPct(value);
  return descriptor ? `${descriptor} · ${percentage}` : percentage;
}

function formatRiskAmountPct(amount, percentage) {
  const parts = [];
  if (isFiniteNumber(amount)) parts.push(formatAmount(amount));
  if (isFiniteNumber(percentage)) parts.push(formatRiskPct(percentage));
  return parts.length ? parts.join(" · ") : "待计算";
}

function formatQuality(status, score) {
  const raw = String(status || "");
  let label = "";
  if (/critical|failed|poor|unavailable|严重|失败|不可用/i.test(raw)) label = "不可用";
  else if (/warning|partial|limited|degraded|警|部分|受限|降级/i.test(raw)) label = "受限";
  else if (/good|healthy|ok|pass|normal|可用|正常/i.test(raw)) label = "可用";
  else if (raw) label = raw;
  if (isFiniteNumber(score)) {
    const normalized = Math.abs(score) <= 1 ? score * 100 : score;
    label += `${label ? " · " : ""}${normalized.toFixed(0)}分`;
  }
  return label || "待计算";
}

function compactAge(seconds) {
  if (!isFiniteNumber(seconds)) return "--";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分钟`;
  return `${Math.floor(seconds / 3600)}小时`;
}

function firstBoolean(...values) {
  for (const value of values) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number" && (value === 0 || value === 1)) return Boolean(value);
    if (/^(true|yes|1|allowed|verified|是|允许|已验证)$/i.test(String(value || "").trim())) return true;
    if (/^(false|no|0|blocked|frozen|unverified|否|禁止|冻结|未验证)$/i.test(String(value || "").trim())) return false;
  }
  return null;
}

function normalizeRiskItems(values, kind) {
  const rows = Array.isArray(values)
    ? values
    : values && typeof values === "object"
      ? Object.values(values)
      : [];
  return rows.map((value, index) => {
    const item = asObject(value);
    const rawSeverity = kind === "action"
      ? pick(item, ["priority", "severity", "level", "status"])
      : pick(item, ["severity", "level", "priority", "status"]);
    const severity = riskSeverity(rawSeverity, kind);
    if (typeof value === "string") {
      return { kind, order: index, ...severity, title: maskRiskMoney(value), detail: "" };
    }
    if (kind === "action") {
      const title = maskRiskMoney(String(pick(item, ["title", "action", "recommendation", "suggestion", "message", "text", "kind"]) || "待处理行动"));
      let detail = String(pick(item, ["detail", "description", "reason", "note"]) || "");
      const rawSymbols = pick(item, ["symbols", "thscodes", "codes"]);
      const symbols = (Array.isArray(rawSymbols) ? rawSymbols : rawSymbols ? [rawSymbols] : [])
        .map(normalizeSymbol).filter(Boolean).map(symbolName);
      if (symbols.length) {
        const symbolText = `${symbols.slice(0, 3).join("、")}${symbols.length > 3 ? `等${symbols.length}只` : ""}`;
        detail += `${detail ? " · " : ""}${symbolText}`;
      }
      return { kind, order: index, ...severity, title, detail: maskRiskMoney(detail) };
    }
    const title = maskRiskMoney(String(pick(item, ["message", "title", "description", "reason", "text", "code"]) || "风险规则触发"));
    const metric = pick(item, ["metric", "rule", "code"]);
    const actual = pick(item, ["actual", "value"]);
    const limit = pick(item, ["limit", "threshold"]);
    const unit = String(pick(item, ["unit"]) || "");
    const comparisons = [];
    if (metric) comparisons.push(String(metric));
    if (actual != null && actual !== "") comparisons.push(`当前 ${actual}${unit}`);
    if (limit != null && limit !== "") comparisons.push(`阈值 ${limit}${unit}`);
    return { kind, order: index, ...severity, title, detail: maskRiskMoney(comparisons.join(" · ")) };
  });
}

function maskRiskMoney(value) {
  const text = String(value || "");
  if (!state.maskAmounts) return text;
  return text
    .replace(/(?:人民币\s*)?(?:约\s*)?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(亿元|万元|元|CNY)/gi, "••••$1")
    .replace(/[¥￥]\s*[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?/g, "¥••••");
}

function riskSeverity(value, kind) {
  if (typeof value === "number") {
    if (value <= 0) return { rank: 4, badge: "严重", className: "is-critical" };
    if (value === 1) return { rank: 3, badge: "高", className: "is-high" };
    if (value === 2) return { rank: 2, badge: "警示", className: "is-warning" };
    return { rank: 1, badge: kind === "breach" ? "提示" : "行动", className: "is-info" };
  }
  const raw = String(value || "").toLowerCase();
  if (/critical|severe|p0|red|致命|严重/.test(raw)) return { rank: 4, badge: "严重", className: "is-critical" };
  if (/high|error|p1|高/.test(raw)) return { rank: 3, badge: "高", className: "is-high" };
  if (/warning|warn|medium|p2|yellow|警|中/.test(raw)) return { rank: 2, badge: "警示", className: "is-warning" };
  return { rank: kind === "breach" ? 2 : 1, badge: kind === "breach" ? "警示" : "行动", className: "is-info" };
}

function formatAmount(value, signed = false) {
  if (state.maskAmounts) return "••••";
  if (!isFiniteNumber(value)) return "--";
  const sign = signed && value > 0 ? "+" : "";
  const absolute = Math.abs(value);
  if (absolute >= 100_000_000) return `${sign}${(value / 100_000_000).toFixed(2)}亿`;
  if (absolute >= 10_000) return `${sign}${(value / 10_000).toFixed(2)}万`;
  return `${sign}${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value)}`;
}

function formatPrice(value) {
  if (!isFiniteNumber(value)) return "--";
  const absolute = Math.abs(value);
  const decimals = absolute < 10 ? 3 : absolute < 1000 ? 2 : 1;
  return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(value);
}

function formatPct(value) {
  if (!isFiniteNumber(value)) return "--";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function formatYi(value) {
  if (!isFiniteNumber(value)) return "";
  const yi = Math.abs(value) > 10_000_000 ? value / 100_000_000 : value;
  return `${yi.toFixed(0)}亿`;
}

function formatCompactTurnover(value) {
  return isFiniteNumber(value) ? formatYi(value) : "";
}

function formatDateTime(value) {
  const date = parseDate(value);
  if (!date) return String(value || "");
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  return date.toLocaleString("zh-CN", sameDay
    ? { hour: "2-digit", minute: "2-digit", hour12: false }
    : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function formatDateOnly(value) {
  const raw = String(value || "");
  const matched = raw.match(/^\d{4}-\d{2}-\d{2}/);
  if (matched) return matched[0];
  const date = parseDate(value);
  return date ? date.toLocaleDateString("zh-CN") : raw || "待计算";
}

function formatMarketQuoteTimestamp(value) {
  const date = parseDate(value);
  if (!date) return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

function ageLabel(timestamp, baseAgeSeconds = 0) {
  if (!timestamp) return "--";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000) + (baseAgeSeconds || 0));
  if (seconds < 2) return "刚刚";
  if (seconds < 60) return `${seconds}秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分钟前`;
  return `${Math.floor(seconds / 3600)}小时前`;
}

function directionClass(value) {
  if (!isFiniteNumber(value) || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

function safeLink(url, text, className) {
  const parsed = safeHttpUrl(url);
  const element = document.createElement(parsed ? "a" : "span");
  element.className = className;
  element.textContent = text;
  if (parsed) {
    element.href = parsed;
    element.target = "_blank";
    element.rel = "noopener noreferrer";
  }
  return element;
}

function safeHttpUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value, window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) {
    return "";
  }
}

function symbolName(symbol) {
  return state.positions.find((holding) => holding.symbol === symbol)?.name || symbol;
}

function normalizeSymbol(value) {
  if (value == null) return "";
  const raw = String(value).trim().toUpperCase();
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(raw)) return raw;
  if (/^(SH|SZ|BJ)\d{6}$/.test(raw)) return `${raw.slice(2)}.${raw.slice(0, 2)}`;
  if (/^\d{6}$/.test(raw)) {
    if (/^[0569]/.test(raw)) return `${raw}.SH`;
    if (/^[123]/.test(raw)) return `${raw}.SZ`;
    if (/^[48]/.test(raw)) return `${raw}.BJ`;
  }
  return raw;
}

// 图表代码兼容两类符号：个股走常规归一化（sh600519 → 600519.SH）；指数
// 白名单符号（sh000001、b_KOSPI 等）若也走个股规则会被误转成 000001.SH，
// 因此仅在命中白名单时按原样保留，其余一律视为无法归一化（空串）。
function normalizeChartSymbol(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  const lowered = raw.toLowerCase();
  if (INDEX_CHART_SYMBOLS.has(lowered)) return lowered;
  if (GLOBAL_CHART_SYMBOLS.has(raw)) return raw;
  return normalizeCanonicalMarketSymbol(raw);
}

function normalizeCanonicalMarketSymbol(value) {
  if (value == null) return "";
  const raw = String(value).trim().toUpperCase();
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(raw)) return raw;
  if (/^(SH|SZ|BJ)\d{6}$/.test(raw)) return `${raw.slice(2)}.${raw.slice(0, 2)}`;
  return "";
}

function parseDate(value) {
  if (value == null || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number" || /^\d{10,13}$/.test(String(value))) {
    const numeric = Number(value);
    const millis = numeric < 10_000_000_000 ? numeric * 1000 : numeric;
    const date = new Date(millis);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const normalized = String(value).replace(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/, "$1-$2-$3T$4:$5:$6+08:00");
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function timestampValue(value) {
  return parseDate(value)?.getTime() || 0;
}

function translatePhase(value) {
  const raw = String(value).toLowerCase();
  if (/open|trading|continuous|morning|afternoon|交易中|开盘/.test(raw)) return "交易中";
  if (/auction|call|集合竞价/.test(raw)) return "集合竞价";
  if (/lunch|noon|午间/.test(raw)) return "午间休市";
  if (/pre|before|盘前/.test(raw)) return "盘前";
  if (/post|after|closed|close|收盘|休市/.test(raw)) return "已收盘";
  return String(value);
}

function translateHealth(value) {
  const raw = String(value);
  if (/ok|healthy|ready|正常/i.test(raw)) return "正常";
  if (/degraded|partial|降级/i.test(raw)) return "部分降级";
  return raw;
}

function friendlyError(error, fallback) {
  const message = error && error.message ? String(error.message) : "";
  if (/Failed to fetch|NetworkError|Load failed/i.test(message)) return "无法连接本地服务";
  return message || fallback;
}

function sourceName(value) {
  if (typeof value === "string") return value;
  const source = asObject(value);
  return source.name || source.provider || source.source || source.label || "";
}

function rootObject(payload) {
  if (!payload || typeof payload !== "object") return {};
  if (payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)) {
    const keys = Object.keys(payload).filter((key) => !["ok", "data", "meta"].includes(key));
    if (!keys.length) return { ...payload.data, meta: payload.meta, ok: payload.ok };
  }
  return payload;
}

function collectionEntries(collection) {
  if (Array.isArray(collection)) return collection.map((value, index) => [String(index), value]);
  if (collection && typeof collection === "object") return Object.entries(collection);
  return [];
}

function firstCollection(...values) {
  return values.find((value) => Array.isArray(value) || (value && typeof value === "object" && Object.keys(value).length)) || [];
}

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function pick(object, keys) {
  const source = asObject(object);
  for (const key of keys) if (source[key] != null && source[key] !== "") return source[key];
  return null;
}

function firstDefined(object, keys) {
  const source = asObject(object);
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(source, key)) return source[key];
  }
  return undefined;
}

function pickNumber(object, keys) {
  return toNumber(pick(object, keys));
}

function firstNumber(...values) {
  for (const value of values) if (isFiniteNumber(value)) return value;
  return null;
}

function toNumber(value) {
  if (value == null || value === "") return null;
  const number = typeof value === "number" ? value : Number(String(value).replace(/[,%\s]/g, ""));
  return Number.isFinite(number) ? number : null;
}

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function findNumericKey(object, value) {
  return Object.keys(asObject(object)).find((key) => toNumber(object[key]) === value) || "";
}

function sum(values) {
  return values.reduce((total, value) => total + (isFiniteNumber(value) ? value : 0), 0);
}

function numberOrDash(value) {
  return isFiniteNumber(value) ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value) : "--";
}

function appendTextParts(parent, parts) {
  for (const [text, className] of parts) parent.append(span(text, className));
}

function div(className = "") {
  const element = document.createElement("div");
  if (className) element.className = className;
  return element;
}

function span(text = "", className = "") {
  const element = document.createElement("span");
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function td(text, className = "") {
  const element = document.createElement("td");
  element.textContent = text;
  if (className.trim()) element.className = className.trim();
  return element;
}

function svgNode(tagName, attributes = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tagName);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  return element;
}

function emptyNode(text, extraClass = "") {
  const element = div(`empty${extraClass ? ` ${extraClass}` : ""}`);
  element.textContent = text;
  return element;
}

function toCamel(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

function readStoredMask() {
  try { return window.localStorage.getItem("stock-dashboard-mask-amounts") === "1"; } catch (_) { return false; }
}

function readStoredPriceLines() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(PRICE_LINES_STORAGE_KEY));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (_) {
    return {};
  }
}

if (typeof globalThis !== "undefined") {
  globalThis.StockDashboardTestHooks = Object.freeze({
    normalizeAdvice,
    normalizeAdviceMapping,
    normalizeDailyPnlDate,
    normalizeDailyPnlHistory,
    normalizeIntradayPnl,
    normalizePnlSeries,
    normalizeStockChart,
    normalizeStockOhlcSeries,
    normalizePortfolioDescriptor,
    buildCompareRows,
    buildCompareTrendSeries,
    dataSourceChainSummary,
    summaryDailyPnl,
    summaryQuoteCoverageCount,
    portfolioQuery,
    decisionContextRequestUrl,
    dailyPnlCumulativeSegments,
    dailyPnlDateTickIndexes,
    dailyPnlHistoryPollIntervalMs,
    dailyPnlHistoryRefreshEnabled,
    dailyPnlHistoryRequestUrl,
    dailyPnlRecordAmount,
    formatStockChartTime,
    formatMarketQuoteTimestamp,
    intradayPnlAxisPosition,
    intradayPnlPollIntervalMs,
    intradayPnlRefreshEnabled,
    isActiveQuoteMarketState,
    nearestIntradayPnlPoint,
    nearestDailyPnlHistoryPoint,
    personalIndexRecords,
    personalIndexChartOption,
    aggregatePersonalIndexRecords,
    aggregateStockOhlcByPeriod,
    projectIntradayPnlSeries,
    quoteClientResponseStale,
    quoteFreshnessLabel,
    quoteMarketState,
    quotePollIntervalMs,
    stableSortPositions,
    splitPnlSeriesAtGaps,
    splitStockChartSeriesAtGaps,
    stockChartTimeValue,
    normalizeChartSymbol,
  });
}
