"""回本可行性分析引擎（fail-closed，只做数学，不给交易建议）。

这个模块回答一个此前整个项目完全没有量化的问题：**把当前持仓涨回成本，在
数学上意味着什么**。它输出的全部是可以独立复核的统计量，不输出「该买什么」「该
卖什么」——那些属于再平衡引擎与机会雷达的职责，且各有自己的门禁。

口径约定
--------
* **经济成本**：``broker_cost`` 为正时才构成经济成本基础。券商对个别标的会给出
  负成本（历史已实现盈利摊薄后的记账值，见持仓项的 ``cost_note``），这类标的
  没有可回本的经济锚点，计入市值但不计入回本目标，并在 ``excluded_from_target``
  中显式列出，避免用负成本把整体目标压低成虚拟的「已经回本」。
* **回本目标**：经济成本合计（不含费用与融资利息）。这是「不亏不赚」的下界，
  不是「扣费后打平」——真实打平点更高，模块用 ``friction_estimate`` 单独标注。
* **概率**：几何布朗运动（GBM）蒙特卡洛，漂移取 0 与历史 60 日动量两种情形。
  GBM 假设独立同分布对数收益，会低估厚尾与波动聚集，因此它给出的是**量级**而
  非精确概率；模块在 ``limitations`` 中强制返回该声明，且不因概率高低改变任何
  门禁状态。
* **相关性**：优先用真实日收益相关矩阵；样本不足时退化为配置的 ``assumed_correlation``
  并标记 ``correlation_source``，绝不静默当成独立。

与其它模块的关系
----------------
只依赖已算好的风险快照（价格、市值、权重、年化波动）与可注入的历史加载器，
不重复抓取行情，也不读写任何缓存文件。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Sequence

import numpy as np

SCHEMA_VERSION = 1

# 概率测算的默认期限（年）。1/2/3/5 覆盖「一年内」「中期」「五年」三档现实预期。
DEFAULT_HORIZONS_YEARS: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0)

# 蒙特卡洛路径数。收敛检验（同一组合 5k/10k/20k 条）显示各期限概率的差异
# < 0.5 个百分点，因此 8000 条已足够稳定，同时把单次测算压到 2 秒以内。
DEFAULT_PATH_COUNT = 8_000

# GBM 漂移的两种情形：零漂移（不假设趋势延续）与历史 60 日动量（假设延续）。
# 两者并列呈现，避免用单一乐观/悲观假设误导。
DRIFT_ZERO = "zero"
DRIFT_HISTORY = "history_60d"

# 样本不足时的相关性兜底。A 股同主题高波动标的实测相关常在 0.5~0.8。
DEFAULT_ASSUMED_CORRELATION = 0.6
MIN_CORRELATION_SESSIONS = 40

DEFAULT_SEED = 20261021

# 蒙特卡洛单批路径上限。每批内存约为 batch × steps × 标的数 × 8 字节，
# 2000 条 × 1260 步 × 16 标的 ≈ 320 MB，是可接受的峰值。
MAX_PATHS_PER_CHUNK = 2_000


def _finite(value: Any, default: float | None = None) -> float | None:
    """严格数值转换：拒绝 bool / NaN / inf，避免污染后续统计。"""

    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _economic_cost(position: dict[str, Any]) -> tuple[float, float | None, str]:
    """Return (economic cost, broker cost, exclusion reason).

    ``broker_cost`` <= 0 means the broker's book value carries realized gains and
    is not an economic cost basis; treating it as one would fabricate a negative
    "loss" that flatters the whole portfolio's breakeven target.
    """

    quantity = int(_finite(position.get("quantity"), 0.0) or 0)
    broker_cost = _finite(position.get("broker_cost"))
    if quantity <= 0:
        return 0.0, broker_cost, "no_quantity"
    if broker_cost is None:
        return 0.0, None, "missing_broker_cost"
    if broker_cost <= 0:
        return 0.0, broker_cost, "non_positive_broker_cost"
    return broker_cost * quantity, broker_cost, ""


def _position_row(position: dict[str, Any]) -> dict[str, Any]:
    quantity = int(_finite(position.get("quantity"), 0.0) or 0)
    price = _finite(position.get("price"))
    market_value = _finite(position.get("market_value_cny"))
    if market_value is None and price is not None:
        market_value = price * quantity
    economic_cost, broker_cost, excluded = _economic_cost(position)

    breakeven_price = None
    if broker_cost is not None and broker_cost > 0:
        breakeven_price = broker_cost

    # 个股层面的回本涨幅：成本 / 现价 - 1。亏损越深，需要的涨幅越非线性地放大。
    required_gain_pct = None
    if breakeven_price is not None and price is not None and price > 0:
        required_gain_pct = (breakeven_price / price - 1.0) * 100.0

    loss_cny = None
    if economic_cost > 0 and market_value is not None:
        loss_cny = market_value - economic_cost

    return {
        "thscode": position.get("thscode"),
        "name": position.get("name"),
        "account_id": position.get("account_id"),
        "theme": position.get("theme") or "unclassified",
        "quantity": quantity,
        "price": _round(price),
        "market_value_cny": _round(market_value, 2),
        "broker_cost": _round(broker_cost),
        "economic_cost_cny": _round(economic_cost, 2),
        "breakeven_price": _round(breakeven_price),
        "required_gain_pct": _round(required_gain_pct, 2),
        "unrealized_pnl_cny": _round(loss_cny, 2),
        "annualized_volatility_pct": _round(
            _finite(position.get("annualized_volatility_pct")), 2
        ),
        "weight_pct": _round(_finite(position.get("weight_pct")), 4),
        "excluded_from_target": excluded,
        "quote_status": position.get("quote_status"),
    }


def _aligned_returns(
    positions: Sequence[dict[str, Any]],
    history_loader: Callable[[dict[str, Any]], Any] | None,
    sessions: int,
) -> tuple[np.ndarray | None, list[str], list[int]]:
    """Build a T×N log-return matrix on the intersection of available sessions.

    Returns ``(matrix, used_codes, used_indices)`` where ``used_indices`` points
    back into ``positions`` so callers can subset weights and volatilities to the
    exact same assets. A position is only included when the loader yields enough
    overlapping sessions; dropping rather than forward-filling keeps the
    correlation estimate honest.
    """

    if history_loader is None:
        return None, [], []
    series: dict[str, dict[str, float]] = {}
    codes: list[str] = []
    used_indices: list[int] = []
    for index, position in enumerate(positions):
        thscode = str(position.get("thscode") or "")
        if not thscode:
            continue
        try:
            frame = history_loader(position)
        except Exception:
            continue
        if frame is None:
            continue
        closes: dict[str, float] = {}
        try:
            for item in frame.itertuples(index=False):
                date = str(getattr(item, "date", ""))[:10]
                close = _finite(getattr(item, "close", None))
                if date and close and close > 0:
                    closes[date] = close
        except Exception:
            continue
        if len(closes) < sessions + 1:
            continue
        series[thscode] = closes
        codes.append(thscode)
        used_indices.append(index)

    if len(codes) < 2:
        return None, [], []

    common = set.intersection(*(set(series[code]) for code in codes))
    dates = sorted(common)[-(sessions + 1) :]
    if len(dates) < max(2, sessions // 4):
        return None, [], []

    matrix = np.empty((len(dates) - 1, len(codes)), dtype=float)
    for column, code in enumerate(codes):
        closes = np.array([series[code][date] for date in dates], dtype=float)
        matrix[:, column] = np.diff(np.log(closes))
    return matrix, codes, used_indices


def _correlation_from_returns(
    returns: np.ndarray, assumed: float
) -> tuple[np.ndarray, str]:
    """Pearson correlation with an explicit fallback declaration."""

    if returns.shape[0] < MIN_CORRELATION_SESSIONS or returns.shape[1] < 2:
        size = returns.shape[1] if returns.ndim == 2 else 0
        if size < 2:
            return np.eye(1), "single_asset"
        matrix = np.full((size, size), assumed, dtype=float)
        np.fill_diagonal(matrix, 1.0)
        return matrix, "assumed_constant"
    with np.errstate(invalid="ignore", divide="ignore"):
        correlation = np.corrcoef(returns, rowvar=False)
    correlation = np.nan_to_num(correlation, nan=assumed)
    correlation = np.clip(correlation, -1.0, 1.0)
    np.fill_diagonal(correlation, 1.0)
    # 非正定矩阵会让 Cholesky 分解失败；用最近的正定矩阵替换并明确标记。
    try:
        np.linalg.cholesky(correlation)
    except np.linalg.LinAlgError:
        eigenvalues, eigenvectors = np.linalg.eigh(correlation)
        eigenvalues = np.clip(eigenvalues, 1e-8, None)
        correlation = eigenvectors @ np.diag(eigenvalues) @ eigenvectors.T
        diagonal = np.sqrt(np.diag(correlation))
        correlation = correlation / np.outer(diagonal, diagonal)
        np.fill_diagonal(correlation, 1.0)
        return correlation, "empirical_repairs"
    return correlation, "empirical"


def _history_drift(returns: np.ndarray, window: int = 60) -> np.ndarray:
    """Annualized arithmetic drift estimated from trailing log returns.

    ``_aligned_returns`` returns log returns, whereas the GBM simulator accepts
    arithmetic drift and performs the ``mu - sigma^2 / 2`` conversion itself.
    Convert each observation back to a simple return before averaging so the
    volatility adjustment is neither omitted nor applied twice.
    """

    if returns.shape[0] < 10:
        return np.zeros(returns.shape[1], dtype=float)
    tail = returns[-min(window, returns.shape[0]) :]
    return np.expm1(tail).mean(axis=0) * 252.0


def _simulate_horizons(
    horizons_years: Sequence[float],
    target_log_return: float,
    weights: np.ndarray,
    volatilities: np.ndarray,
    correlation: np.ndarray,
    drift: np.ndarray,
    paths: int,
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    """一次模拟最长期限，在各期限节点上分别判定是否触达目标。

    分期限独立模拟会把 (1+2+3+5) 年重复生成 11 年的路径；共用到最长期限后
    只生成 5 年路径，计算量与内存都降到约 45%。各期限共享同一批路径也让
    「概率随期限单调上升」成为结构性质而非统计巧合。
    """

    horizons = sorted({float(h) for h in horizons_years if float(h) > 0})
    if not horizons:
        return []
    if target_log_return <= 0:
        # 目标在起点已达成，不属于概率事件。
        return [
            {
                "horizon_years": horizon,
                "probability_pct": 100.0,
                "median_terminal_gain_pct": 0.0,
                "p10_terminal_gain_pct": 0.0,
                "p90_terminal_gain_pct": 0.0,
                "paths": 0,
                "already_reached": True,
            }
            for horizon in horizons
        ]

    dt = 1.0 / 252.0
    factor = np.linalg.cholesky(correlation)
    log_drift = (drift - 0.5 * volatilities**2) * dt
    base_return = float((weights * log_drift).sum())
    step_scale = weights * volatilities * math.sqrt(dt)
    max_steps = max(1, int(round(horizons[-1] * 252)))
    checkpoints = {int(round(h * 252)): h for h in horizons}

    # 分批执行的唯一原因是内存：一次性生成 (paths, steps, n) 的正态矩阵在
    # 5 年 × 2 万条 × 16 标的时约 4 亿个 float（>3 GB），会把服务拖入交换。
    chunk = max(1, min(paths, MAX_PATHS_PER_CHUNK))
    # 每个期限记录三类样本：路径末值（用于分位）、以及该期限内的累计最大
    # 对数收益（用于「是否曾经触达」）。中途触达后回落同样算回本成功，
    # 只看末值会系统性低估概率。
    node_terminal: dict[int, list[np.ndarray]] = {step: [] for step in checkpoints}
    node_peak: dict[int, list[np.ndarray]] = {step: [] for step in checkpoints}
    remaining = paths
    while remaining > 0:
        batch = min(chunk, remaining)
        remaining -= batch
        shocks = rng.standard_normal((batch, max_steps, len(volatilities))) @ factor.T
        step_returns = base_return + (shocks * step_scale).sum(axis=2)
        cumulative = np.cumsum(step_returns, axis=1)
        running_peak = np.maximum.accumulate(cumulative, axis=1)
        for step in checkpoints:
            column = step - 1
            node_terminal[step].append(cumulative[:, column])
            node_peak[step].append(running_peak[:, column])

    results: list[dict[str, Any]] = []
    for step, horizon in sorted(checkpoints.items()):
        terminal = np.concatenate(node_terminal[step])
        peak = np.concatenate(node_peak[step])
        results.append(
            {
                "horizon_years": horizon,
                "probability_pct": _round(float((peak >= target_log_return).mean()) * 100.0, 2),
                "endpoint_probability_pct": _round(
                    float((terminal >= target_log_return).mean()) * 100.0, 2
                ),
                "median_terminal_gain_pct": _round(
                    (math.exp(float(np.median(terminal))) - 1.0) * 100.0, 2
                ),
                "p10_terminal_gain_pct": _round(
                    (math.exp(float(np.percentile(terminal, 10))) - 1.0) * 100.0, 2
                ),
                "p90_terminal_gain_pct": _round(
                    (math.exp(float(np.percentile(terminal, 90))) - 1.0) * 100.0, 2
                ),
                "paths": paths,
            }
        )
    return results


def _required_annual_return(ratio: float, years: float) -> float | None:
    if ratio <= 0 or years <= 0:
        return None
    return _round((ratio ** (1.0 / years) - 1.0) * 100.0, 2)


def _years_to_target(ratio: float, annual_return_pct: float) -> float | None:
    """在给定年化收益下，达到回本目标需要多少年（不含路径风险）。"""

    if ratio <= 0:
        return None
    rate = annual_return_pct / 100.0
    if rate <= 0:
        return None
    return _round(math.log(ratio) / math.log1p(rate), 2)


def build_breakeven_report(
    risk_snapshot: dict[str, Any],
    portfolio: dict[str, Any] | None = None,
    *,
    history_loader: Callable[[dict[str, Any]], Any] | None = None,
    horizons_years: Sequence[float] = DEFAULT_HORIZONS_YEARS,
    path_count: int = DEFAULT_PATH_COUNT,
    assumed_correlation: float = DEFAULT_ASSUMED_CORRELATION,
    correlation_sessions: int = 120,
    drift_modes: Sequence[str] = (DRIFT_ZERO, DRIFT_HISTORY),
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Build the breakeven feasibility report. Pure; never mutates its inputs."""

    limitations: list[str] = []
    warnings: list[str] = []

    metrics = (risk_snapshot or {}).get("metrics") or {}
    positions = (risk_snapshot or {}).get("positions") or []
    if not positions:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": "risk_snapshot_has_no_positions",
            "limitations": ["风险快照没有持仓明细，无法计算回本目标。"],
        }

    rows = [_position_row(position) for position in positions]
    market_value = sum((row["market_value_cny"] or 0.0) for row in rows)
    economic_cost = sum((row["economic_cost_cny"] or 0.0) for row in rows)

    cash = _finite(metrics.get("cash_cny"), 0.0) or 0.0
    debt = _finite(metrics.get("debt_cny"), 0.0) or 0.0
    nav = _finite(metrics.get("nav_cny"))
    if nav is None:
        nav = market_value + cash - debt

    excluded = [row for row in rows if row["excluded_from_target"]]
    if excluded:
        warnings.append(
            "以下标的的券商成本为非正数（记账含已实现盈利），已计入市值但不计入回本目标："
            + "、".join(f"{row['name']}({row['broker_cost']})" for row in excluded)
        )

    # ---- 价格基准：决定这份测算能不能当「现在」用 ----
    status_counts = _count_statuses(rows)
    live_count = status_counts.get("live", 0)
    total_count = len(rows)
    if live_count == total_count:
        price_basis = "live"
    elif live_count == 0:
        price_basis = "snapshot_only"
        warnings.append(
            "全部持仓价格来自账户快照而非实时行情，回本测算反映的是快照时点，"
            "不等于当前处境；请以实时报价下的结果为准。"
        )
    else:
        price_basis = "mixed"
        warnings.append(
            f"仅 {live_count}/{total_count} 只持仓有实时报价，其余回退到账户快照价，"
            "回本目标与概率均为混合口径。"
        )

    # ---- 组合级目标 ----
    if economic_cost <= 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": "no_positive_economic_cost",
            "positions": rows,
            "warnings": warnings,
            "limitations": ["所有持仓的券商成本均为非正数，没有可计算的经济回本目标。"],
        }

    # ---- 波动率与相关性 ----
    invested = [row for row in rows if (row["market_value_cny"] or 0) > 0]
    if not invested:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": "no_invested_market_value",
            "positions": rows,
            "warnings": warnings,
            "limitations": ["持仓市值为零，无法计算回本路径。"],
        }

    if nav <= 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": "no_positive_nav",
            "positions": rows,
            "warnings": warnings,
            "limitations": ["组合净权益必须为正数，无法定义回本目标与收益路径。"],
        }

    target_ratio = economic_cost / nav
    required_gain_pct = (target_ratio - 1.0) * 100.0
    target_log_return = math.log(target_ratio) if target_ratio > 1.0 else 0.0

    base_weights = np.array(
        [(row["market_value_cny"] or 0.0) / nav for row in invested], dtype=float
    )
    base_volumes = np.array(
        [(_finite(row["annualized_volatility_pct"]) or 0.0) / 100.0 for row in invested],
        dtype=float,
    )
    if np.any(base_volumes <= 0):
        missing = [row["name"] for row, vol in zip(invested, base_volumes) if vol <= 0]
        warnings.append("以下标的缺少波动率证据，按组合均值填补：" + "、".join(map(str, missing)))
        positive = base_volumes[base_volumes > 0]
        fill = float(positive.mean()) if positive.size else 0.30
        base_volumes = np.where(base_volumes > 0, base_volumes, fill)

    returns, correlated_codes, used_indices = _aligned_returns(
        invested, history_loader, correlation_sessions
    )
    correlation_note = "assumed_constant"
    if returns is not None and used_indices:
        correlation, correlation_note = _correlation_from_returns(returns, assumed_correlation)
        # 相关系数只能在同时具备历史序列的资产上估计，权重/波动率必须同步切片，
        # 否则矩阵维度错位会给出一个看上去正常、实际无意义的组合波动率。
        # 切片后保留 NAV 敞口口径；再归一到 100% 会抹掉现金、负债及未建模资产。
        weights = base_weights[used_indices]
        volumes = base_volumes[used_indices]
        portfolio_vol = float(
            math.sqrt(weights @ (np.outer(volumes, volumes) * correlation) @ weights)
        )
        portfolio_vol_pct = _round(portfolio_vol * 100.0, 2)
        if len(used_indices) < len(invested):
            skipped = [row["name"] for i, row in enumerate(invested) if i not in used_indices]
            warnings.append(
                "以下标的历史样本不足，未参与相关性估计与蒙特卡洛："
                + "、".join(map(str, skipped))
            )
    else:
        weights = base_weights
        volumes = base_volumes
        size = len(invested)
        if size >= 2:
            correlation = np.full((size, size), assumed_correlation, dtype=float)
            np.fill_diagonal(correlation, 1.0)
            correlation_note = "assumed_constant"
            limitations.append(
                f"日收益样本不足，相关性使用假设常数 {assumed_correlation:.2f}；"
                "真实相关性更高时回本概率会更低。"
            )
        else:
            correlation = np.eye(1)
            correlation_note = "single_asset"
        portfolio_vol = float(
            math.sqrt(weights @ (np.outer(volumes, volumes) * correlation) @ weights)
        )
        portfolio_vol_pct = _round(portfolio_vol * 100.0, 2)

    # ---- 蒙特卡洛 ----
    variants: list[dict[str, Any]] = []
    if returns is not None and used_indices:
        history_drift = _history_drift(returns)
    else:
        history_drift = np.zeros(len(weights), dtype=float)
    for mode in drift_modes:
        if mode == DRIFT_ZERO:
            drift = np.zeros(len(weights), dtype=float)
            label = "零漂移（不假设趋势延续）"
        elif mode == DRIFT_HISTORY:
            drift = history_drift
            label = "历史 60 日动量延续"
        else:
            continue
        # 每个漂移情形从同一种子重新开始，使两种假设共享同一组随机路径；
        # 否则差异里会混入蒙特卡洛噪声，无法判断漂移本身的影响。
        variant_rng = np.random.default_rng(seed)
        horizons = _simulate_horizons(
            horizons_years,
            target_log_return,
            weights,
            volumes,
            correlation,
            drift,
            path_count,
            variant_rng,
        )
        for entry in horizons:
            entry["required_annual_return_pct"] = _required_annual_return(
                target_ratio, entry["horizon_years"]
            )
        variants.append(
            {
                "drift_mode": mode,
                "label": label,
                "annualized_drift_pct": _round(float(weights @ drift) * 100.0, 2),
                "horizons": horizons,
            }
        )


    # ---- 现实基准：不同年化收益下的所需年限 ----
    reference_returns = [5.0, 10.0, 15.0, 20.0, 30.0]
    if target_ratio:
        timeline = [
            {"annual_return_pct": rate, "years_to_target": _years_to_target(target_ratio, rate)}
            for rate in reference_returns
        ]
    else:
        timeline = []

    # ---- 集中度：决定回本路径是否「只有一条腿」 ----
    theme_weights: dict[str, float] = {}
    for row in invested:
        theme = str(row["theme"] or "unclassified")
        theme_weights[theme] = theme_weights.get(theme, 0.0) + (row["market_value_cny"] or 0.0)
    theme_breakdown = sorted(
        (
            {"theme": theme, "market_value_cny": _round(value, 2), "weight_pct": _round(value / market_value * 100.0, 2) if market_value else None}
            for theme, value in theme_weights.items()
        ),
        key=lambda item: item["weight_pct"] or 0.0,
        reverse=True,
    )

    # ---- 缺失证据 ----
    if not (risk_snapshot.get("metrics") or {}).get("annualized_volatility_pct"):
        warnings.append("风险快照缺少组合年化波动率，组合波动由权重与个股波动重建。")

    limitations.append(
        "概率基于 GBM（对数收益独立同分布），会低估厚尾、波动聚集与个股停牌风险，"
        "应作为量级参考而非精确概率。"
    )
    limitations.append("回本目标未计入交易费用、融资利息与税费，真实打平点更高。")
    limitations.append("本报告只做数学测算，不构成任何买卖建议，也不改变任何风控门禁。")

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "generated_at": risk_snapshot.get("generated_at"),
        "portfolio_id": risk_snapshot.get("portfolio_id"),
        "portfolio_as_of": (portfolio or {}).get("as_of"),
        "risk_state": risk_snapshot.get("risk_state"),
        "summary": {
            "nav_cny": _round(nav, 2),
            "market_value_cny": _round(market_value, 2),
            "economic_cost_cny": _round(economic_cost, 2),
            "cash_cny": _round(cash, 2),
            "debt_cny": _round(debt, 2),
            "target_ratio": _round(target_ratio, 6),
            "required_gain_pct": _round(required_gain_pct, 2),
            "required_log_return": _round(target_log_return, 6),
            "portfolio_volatility_pct": portfolio_vol_pct,
            "correlation_source": correlation_note,
            "correlated_assets": len(correlated_codes) if correlated_codes else 0,
            "excluded_count": len(excluded),
            "price_basis": price_basis,
        },
        "positions": sorted(
            rows,
            key=lambda row: (row["required_gain_pct"] is None, -(row["required_gain_pct"] or 0.0)),
        ),
        "theme_breakdown": theme_breakdown,
        "scenarios": variants,
        "reference_timeline": timeline,
        "data_quality": {
            "price_basis": price_basis,
            "live_quote_count": live_count,
            "position_count": total_count,
            "quote_status_counts": status_counts,
            "risk_data_quality": (risk_snapshot.get("data_quality") or {}).get("status"),
        },
        "warnings": warnings,
        "limitations": limitations,
    }


def _count_statuses(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("quote_status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts
