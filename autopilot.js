/* 自动领航模拟盘子页面：取 /api/autopilot 与 /api/personal-index，渲染净值对比与操作留痕。 */
(function () {
  "use strict";

  var REFRESH_MS = 30000;
  var chart = null;

  function $(id) { return document.getElementById(id); }

  function fmtMoney(v) {
    if (v === null || v === undefined || !isFinite(v)) return "--";
    var abs = Math.abs(v);
    var sign = v < 0 ? "-" : "";
    if (abs >= 1e8) return sign + (abs / 1e8).toFixed(2) + "亿";
    if (abs >= 1e4) return sign + (abs / 1e4).toFixed(2) + "万";
    return sign + abs.toFixed(0);
  }

  function fmtPct(v, signed) {
    if (v === null || v === undefined || !isFinite(v)) return "--";
    var s = v.toFixed(2) + "%";
    if (signed && v > 0) s = "+" + s;
    return s;
  }

  function fmtNum(v) {
    if (v === null || v === undefined || !isFinite(v)) return "--";
    return Number(v).toLocaleString("zh-CN");
  }

  function setText(id, text, cls) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    if (cls) { el.classList.remove("up", "down", "flat"); el.classList.add(cls); }
  }

  function pctClass(v) {
    if (v === null || v === undefined || !isFinite(v)) return "flat";
    return v > 0 ? "up" : v < 0 ? "down" : "flat";
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function fetchJson(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  /* ---------------- 摘要 ---------------- */

  function renderSummary(data) {
    var account = data.account || {};
    var perf = data.performance || {};
    var base = data.base || {};
    setText("ap-total", fmtMoney(account.total_value));
    setText("ap-return", fmtPct(perf.return_pct, true), pctClass(perf.return_pct));
    setText("ap-real-return", fmtPct(perf.real_return_pct, true), pctClass(perf.real_return_pct));
    setText("ap-excess", fmtPct(perf.excess_pct, true), pctClass(perf.excess_pct));
    setText("ap-exposure", fmtPct(account.exposure_pct));
    setText("ap-cash", fmtMoney(account.cash));
    setText("ap-realized", fmtMoney(account.realized_pnl), pctClass(account.realized_pnl));
    setText("ap-trades", (account.trade_count || 0) + " 笔 / " + fmtMoney(account.total_fees));
    setText("ap-base-note", "基点 " + (base.date || "--") + " · 基准权益 " + fmtMoney(base.base_value));
    setText("ap-decision-window", "决策窗口 " + (data.decision_window || "--") + " · 仅交易日");
  }

  /* ---------------- 净值对比图 ---------------- */

  function renderChart(data, indexData) {
    var el = $("ap-nav-chart");
    if (!el || typeof echarts === "undefined") return;
    var days = data.nav_days || [];
    if (!days.length) {
      el.innerHTML = '<div class="empty compact">首个交易日后开始记录净值</div>';
      return;
    }
    var baseDate = (data.base || {}).date;

    var dates = days.map(function (d) { return d.date; });
    var simNav = days.map(function (d) { return d.nav; });
    var realNav = days.map(function (d) {
      return d.real_return_pct === null || d.real_return_pct === undefined
        ? null : (1 + d.real_return_pct / 100) * 1000;
    });

    // 个人指数（前复权口径，长期参照）：按日期对齐，归一到模拟盘起点
    var idxNav = null;
    var kline = (indexData && indexData.kline) || [];
    var byDate = {};
    kline.forEach(function (k) { byDate[k.date] = k.close; });
    var baseClose = baseDate ? byDate[baseDate] : null;
    if (!baseClose && kline.length) {
      // 基点日无指数记录时，用基点日前最后一个交易日收盘
      for (var i = kline.length - 1; i >= 0; i--) {
        if (!baseDate || kline[i].date <= baseDate) { baseClose = kline[i].close; break; }
      }
    }
    if (baseClose) {
      idxNav = dates.map(function (d) {
        return byDate[d] !== undefined ? (byDate[d] / baseClose) * 1000 : null;
      });
    }

    if (!chart) chart = echarts.init(el, null, { renderer: "canvas" });
    var series = [
      {
        name: "模拟净值（自动决策）",
        type: "line", data: simNav, connectNulls: true,
        lineStyle: { width: 2, color: "#b3413c" }, itemStyle: { color: "#b3413c" },
        symbol: "circle", symbolSize: 4,
      },
      {
        name: "实盘持仓市值（人工）",
        type: "line", data: realNav, connectNulls: true,
        lineStyle: { width: 2, color: "#4e687d" }, itemStyle: { color: "#4e687d" },
        symbol: "circle", symbolSize: 4,
      },
    ];
    if (idxNav) {
      series.push({
        name: "个人指数（前复权口径）",
        type: "line", data: idxNav, connectNulls: true,
        lineStyle: { width: 1, type: "dashed", color: "#8a9298" },
        itemStyle: { color: "#8a9298" }, symbol: "none",
      });
    }
    chart.setOption({
      animation: false,
      grid: { left: 48, right: 16, top: 30, bottom: 24 },
      legend: { top: 2, textStyle: { fontSize: 10 } },
      tooltip: {
        trigger: "axis",
        valueFormatter: function (v) { return v === null || v === undefined ? "--" : Number(v).toFixed(1); },
      },
      xAxis: { type: "category", data: dates, axisLabel: { fontSize: 10 } },
      yAxis: {
        type: "value", scale: true,
        axisLabel: { fontSize: 10 },
        splitLine: { lineStyle: { color: "#e6e9eb" } },
      },
      series: series,
    }, { notMerge: true });
    setText("ap-nav-meta", days.length + " 个交易日 · 基点 1000");
  }

  /* ---------------- 最近决策 ---------------- */

  function renderDecision(data) {
    var d = data.last_decision;
    var badge = $("ap-regime-badge");
    if (!d) {
      badge.textContent = "未决策";
      setText("ap-decision-meta", "首个交易日 " + (data.decision_window || "") + " 决策窗口自动执行");
      return;
    }
    badge.textContent = (d.regime_label || d.regime || "--") +
      (d.ceiling_pct !== null && d.ceiling_pct !== undefined ? " · 上限 " + d.ceiling_pct + "%" : "");
    badge.classList.remove("is-pending", "is-ok", "is-warning", "is-blocked");
    badge.classList.add(
      d.regime === "RISK_ON" ? "is-ok" :
      d.regime === "RISK_OFF" || d.action === "skip_all" ? "is-blocked" : "is-warning"
    );
    setText(
      "ap-decision-meta",
      (d.date || "--") + " · 得分 " + (d.score === null || d.score === undefined ? "--" : d.score) +
      " · 置信度 " + (d.confidence === null || d.confidence === undefined ? "--" : d.confidence) +
      " · 成交 " + (d.trades || 0) + " 笔 · 仓位 " +
      (d.exposure_after_pct === null || d.exposure_after_pct === undefined ? "--" : d.exposure_after_pct + "%")
    );
    setText("ap-decision-note", d.reason || d.guidance || "");

    var body = $("ap-plans-body");
    var plans = d.plans || [];
    if (!plans.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">' + esc(d.reason || "当日无逐股计划") + "</td></tr>";
      return;
    }
    body.innerHTML = plans.map(function (p) {
      var votes = p.votes === null || p.votes === undefined ? "--" : p.votes + "/3";
      return "<tr>" +
        "<td><div>" + esc(p.symbol) + "</div><div class=\"muted\">" + esc(p.name) + "</div></td>" +
        "<td>" + esc(votes) + "</td>" +
        "<td>" + esc(p.overall || "--") + "</td>" +
        '<td class="numeric">' + fmtPct((p.current_weight || 0) * 100) + "</td>" +
        '<td class="numeric">' + fmtPct((p.target_weight_raw || 0) * 100) + "</td>" +
        "<td>" + esc(p.note || "") + "</td>" +
        "</tr>";
    }).join("");
  }

  /* ---------------- 持仓对照 ---------------- */

  function renderPositions(data) {
    var body = $("ap-positions-body");
    var rows = data.positions || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="9" class="empty">暂无模拟持仓数据</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (r) {
      var diff = (r.shares || 0) - (r.real_shares || 0);
      var diffCls = diff > 0 ? "up" : diff < 0 ? "down" : "flat";
      var pnlCls = pctClass(r.pnl_pct);
      return "<tr>" +
        "<td><div>" + esc(r.symbol) + "</div><div class=\"muted\">" + esc(r.name) + "</div></td>" +
        '<td class="numeric">' + fmtNum(r.shares) + "</td>" +
        '<td class="numeric">' + fmtNum(r.real_shares) + "</td>" +
        '<td class="numeric ' + diffCls + '">' + (diff > 0 ? "+" : "") + fmtNum(diff) + "</td>" +
        '<td class="numeric">' + (r.price === null || r.price === undefined ? "--" : r.price.toFixed(3)) + "</td>" +
        '<td class="numeric">' + fmtMoney(r.value) + "</td>" +
        '<td class="numeric">' + fmtPct(r.weight_pct) + "</td>" +
        '<td class="numeric">' + fmtPct(r.base_weight_pct) + "</td>" +
        '<td class="numeric ' + pnlCls + '">' + fmtPct(r.pnl_pct, true) + "</td>" +
        "</tr>";
    }).join("");
  }

  /* ---------------- 操作日志 ---------------- */

  var TYPE_LABELS = { init: "初始化", decision: "决策", trade: "成交", nav: "净值" };

  function journalRow(j) {
    var type = TYPE_LABELS[j.type] || j.type || "--";
    var target = j.symbol ? (j.symbol + " " + (j.name || "")) : "—";
    var action = "—";
    var shares = "—", price = "—", amount = "—", fee = "—";
    var reason = "";
    if (j.type === "trade") {
      action = j.side === "sell" ? "卖出" : "买入";
      shares = fmtNum(j.shares);
      price = j.price !== undefined ? Number(j.price).toFixed(3) : "--";
      amount = fmtMoney(j.amount);
      fee = j.fee !== undefined ? Number(j.fee).toFixed(2) : "--";
      reason = j.reason || "";
    } else if (j.type === "decision") {
      action = j.action === "skip_all" ? "当日不交易" : "调仓 " + (j.trades || 0) + " 笔";
      reason = (j.regime_label || "") +
        (j.ceiling_pct !== null && j.ceiling_pct !== undefined ? " · 上限" + j.ceiling_pct + "%" : "") +
        (j.reason ? " · " + j.reason : "") +
        (j.exposure_after_pct !== null && j.exposure_after_pct !== undefined ? " · 仓位" + j.exposure_after_pct + "%" : "");
    } else if (j.type === "init") {
      action = "镜像实盘建仓";
      amount = fmtMoney(j.base_value);
      reason = j.note || "";
    }
    var time = String(j.ts || "").replace("T", " ").replace("+08:00", "");
    var actionCls = j.side === "sell" ? "down" : j.side === "buy" ? "up" : "";
    return "<tr>" +
      "<td>" + esc(time) + "</td>" +
      "<td>" + esc(type) + "</td>" +
      "<td>" + esc(target) + "</td>" +
      '<td class="' + actionCls + '">' + esc(action) + "</td>" +
      '<td class="numeric">' + esc(shares) + "</td>" +
      '<td class="numeric">' + esc(price) + "</td>" +
      '<td class="numeric">' + esc(amount) + "</td>" +
      '<td class="numeric">' + esc(fee) + "</td>" +
      "<td>" + esc(reason) + "</td>" +
      "</tr>";
  }

  function renderJournal(data) {
    var body = $("ap-journal-body");
    var rows = (data.recent_journal || []).filter(function (j) {
      return j.type !== "nav";
    });
    setText("ap-journal-count", (data.journal_count || 0) + " 条");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="9" class="empty">暂无自动化操作记录</td></tr>';
      return;
    }
    body.innerHTML = rows.map(journalRow).join("");
  }

  function renderRules(data) {
    var list = $("ap-rules-list");
    list.innerHTML = (data.rules || []).map(function (r) {
      return "<li>" + esc(r) + "</li>";
    }).join("");
  }

  /* ---------------- 主流程 ---------------- */

  function refresh() {
    var status = $("ap-status");
    Promise.all([
      fetchJson("/api/autopilot"),
      fetchJson("/api/personal-index").catch(function () { return null; }),
    ]).then(function (results) {
      var data = results[0];
      var indexData = results[1];
      if (!data.ok) throw new Error(data.error || "payload not ok");
      status.textContent = data.initialized ? "运行中" : "待初始化";
      status.classList.remove("is-loading");
      setText("ap-phase", data.trade_day ? "交易日" : "非交易日");
      setText("ap-freshness", "更新于 " + String(data.generated_at || "").replace("T", " ").replace("+08:00", ""));
      if (!data.initialized) {
        setText("ap-decision-meta", data.message || "");
        renderRules(data);
        return;
      }
      renderSummary(data);
      renderChart(data, indexData);
      renderDecision(data);
      renderPositions(data);
      renderJournal(data);
      renderRules(data);
    }).catch(function (err) {
      status.textContent = "连接失败";
      status.classList.add("is-loading");
      setText("ap-freshness", String(err && err.message || err));
    });
  }

  window.addEventListener("resize", function () { if (chart) chart.resize(); });
  $("ap-refresh").addEventListener("click", refresh);
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
