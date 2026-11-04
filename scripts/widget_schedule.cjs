"use strict";

// 桌面盈亏组件只在 A 股交易窗口内显示。日期按上海时区判断，避免系统时区影响；
// 法定节假日由看板接口返回的 market_phase 兜底（weekend/holiday/non_trading）。

const DISPLAY_START_MINUTES = 9 * 60 + 15; // 09:15 集合竞价开始
const DISPLAY_END_MINUTES = 15 * 60 + 5; // 15:00 收盘后保留 5 分钟收盘点位
const HIDDEN_PHASES = Object.freeze(["weekend", "holiday", "non_trading"]);

function shanghaiClock(now = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now).map((part) => [part.type, part.value]));
  return {
    weekday: parts.weekday,
    minutes: Number(parts.hour) * 60 + Number(parts.minute),
  };
}

function isTradingDisplayWindow(now = new Date()) {
  const { weekday, minutes } = shanghaiClock(now);
  if (weekday === "Sat" || weekday === "Sun") return false;
  return minutes >= DISPLAY_START_MINUTES && minutes < DISPLAY_END_MINUTES;
}

function shouldDisplay(now = new Date(), marketPhase = "") {
  if (!isTradingDisplayWindow(now)) return false;
  return !HIDDEN_PHASES.includes(String(marketPhase || "").toLowerCase());
}

module.exports = {
  DISPLAY_START_MINUTES,
  DISPLAY_END_MINUTES,
  HIDDEN_PHASES,
  shanghaiClock,
  isTradingDisplayWindow,
  shouldDisplay,
};
