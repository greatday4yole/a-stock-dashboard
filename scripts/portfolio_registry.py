"""Registry for user-defined portfolios layered on top of the primary one.

The primary portfolio stays in ``config/portfolio.json`` and keeps feeding every
existing engine untouched.  Additional portfolios created from the dashboard
live in ``config/portfolios.json`` (this module is the only writer) and reuse
the exact same account/position schema so that quotes, risk, intraday and daily
P&L pipelines can consume them without format conversion.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
PRIMARY_PORTFOLIO_PATH = ROOT / "config" / "portfolio.json"
EXTRA_PORTFOLIOS_PATH = ROOT / "config" / "portfolios.json"

MAIN_PORTFOLIO_ID = "main"
MAIN_PORTFOLIO_LABEL = "主组合"
REGISTRY_SCHEMA_VERSION = 1
MAX_FILE_BYTES = 512 * 1024
MAX_PORTFOLIOS = 12
MAX_POSITIONS_PER_PORTFOLIO = 100
MAX_LABEL_LENGTH = 24
MAX_NOTE_LENGTH = 200
MAX_QUANTITY = 99_999_999
MAX_CASH_OR_DEBT = 1e12

CN_TZ = timezone(timedelta(hours=8))
_PORTFOLIO_ID_RE = re.compile(r"pf-[a-f0-9]{8,16}")
_THSCODE_RE = re.compile(r"\d{6}\.(?:SH|SZ|BJ)")

_REGISTRY_LOCK = threading.RLock()


class PortfolioRegistryError(ValueError):
    """Raised for any invalid portfolio draft or registry state."""


class PortfolioNotFound(LookupError):
    """Raised when an editable portfolio id does not exist."""


def _now_iso() -> str:
    return datetime.now(tz=CN_TZ).isoformat(timespec="seconds")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("portfolios registry file exceeds its size limit")
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, parse_constant=_reject_json_constant)
    if not isinstance(value, dict):
        raise ValueError("portfolios registry root must be an object")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode() + b"\n"
    if len(body) > MAX_FILE_BYTES:
        raise ValueError("portfolios registry file exceeds its size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def symbol_from_thscode(thscode: str) -> str:
    code, exchange = thscode.strip().split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange.upper())
    if not prefix or not re.fullmatch(r"\d{6}", code):
        raise PortfolioRegistryError(f"不支持的证券代码: {thscode!r}")
    return prefix + code


def default_daily_limit_pct(thscode: str) -> int:
    code = thscode.split(".", 1)[0]
    if code.startswith(("300", "301", "688", "689")):
        return 20
    if code.startswith(("4", "8", "9")):
        return 30
    return 10


def default_instrument_type(thscode: str) -> str:
    code, exchange = thscode.split(".", 1)
    if (exchange == "SH" and code.startswith(("51", "58"))) or (
        exchange == "SZ" and code.startswith("15")
    ):
        return "etf"
    return "stock"


def flatten_positions(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten accounts into position rows with symbol/code/account metadata."""

    positions: list[dict[str, Any]] = []
    for account in config.get("accounts") or []:
        if not isinstance(account, Mapping):
            continue
        for item in account.get("positions") or []:
            if not isinstance(item, Mapping):
                continue
            record = dict(item)
            record.update(
                {
                    "account_id": account.get("account_id", ""),
                    "account_type": account.get("account_type", ""),
                    "symbol": symbol_from_thscode(str(item.get("thscode", ""))),
                    "code": str(item.get("thscode", "")).split(".", 1)[0],
                }
            )
            positions.append(record)
    return positions


def _validate_label(value: Any) -> str:
    label = str(value or "").strip()
    if not label:
        raise PortfolioRegistryError("组合名称不能为空")
    if len(label) > MAX_LABEL_LENGTH:
        raise PortfolioRegistryError(f"组合名称不能超过 {MAX_LABEL_LENGTH} 个字符")
    return label


def _validate_note(value: Any) -> str:
    note = str(value or "").strip()
    if len(note) > MAX_NOTE_LENGTH:
        raise PortfolioRegistryError(f"备注不能超过 {MAX_NOTE_LENGTH} 个字符")
    return note


def _validate_history_start(value: Any) -> str:
    """Optional YYYY-MM-DD start date for the static daily P&L replay."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise PortfolioRegistryError("历史盈亏起点必须是 YYYY-MM-DD 格式的有效日期") from exc
    if parsed.isoformat() != text:
        raise PortfolioRegistryError("历史盈亏起点必须是 YYYY-MM-DD 格式的有效日期")
    if parsed > datetime.now(tz=CN_TZ).date():
        raise PortfolioRegistryError("历史盈亏起点不能晚于今天")
    return text


def _validate_money(value: Any, field: str) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        raise PortfolioRegistryError(f"{field} 必须是数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PortfolioRegistryError(f"{field} 必须是数字") from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > MAX_CASH_OR_DEBT:
        raise PortfolioRegistryError(f"{field} 必须在 0 到 {MAX_CASH_OR_DEBT:.0f} 之间")
    return round(parsed, 2)


def _validate_position_draft(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PortfolioRegistryError(f"第 {index} 行持仓必须是对象")
    thscode = str(raw.get("thscode") or "").strip().upper()
    if not _THSCODE_RE.fullmatch(thscode):
        raise PortfolioRegistryError(
            f"第 {index} 行持仓代码必须是 600519.SH 形式（六位代码 + 交易所后缀）"
        )
    raw_quantity = raw.get("quantity")
    if isinstance(raw_quantity, bool):
        raise PortfolioRegistryError(f"第 {index} 行持仓数量必须是正整数")
    if isinstance(raw_quantity, float) and not math.isfinite(raw_quantity):
        raise PortfolioRegistryError(f"第 {index} 行持仓数量必须是正整数")
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PortfolioRegistryError(f"第 {index} 行持仓数量必须是正整数") from exc
    if isinstance(raw_quantity, float) and float(raw_quantity) != quantity:
        raise PortfolioRegistryError(f"第 {index} 行持仓数量必须是正整数")
    if quantity <= 0 or quantity > MAX_QUANTITY:
        raise PortfolioRegistryError(
            f"第 {index} 行持仓数量必须在 1 到 {MAX_QUANTITY} 之间"
        )
    name = str(raw.get("name") or "").strip()[:30]
    position = {
        "thscode": thscode,
        "name": name or thscode,
        "instrument_type": str(raw.get("instrument_type") or default_instrument_type(thscode)),
        "quantity": quantity,
        "available": quantity,
        "theme": str(raw.get("theme") or "").strip()[:40],
        "industry": str(raw.get("industry") or "").strip()[:40],
        "daily_limit_pct": int(raw.get("daily_limit_pct") or default_daily_limit_pct(thscode)),
    }
    if raw.get("snapshot_price") not in (None, ""):
        if isinstance(raw.get("snapshot_price"), bool):
            raise PortfolioRegistryError(f"第 {index} 行快照价必须是数字")
        try:
            price = float(raw.get("snapshot_price"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise PortfolioRegistryError(f"第 {index} 行快照价必须是数字") from exc
        if math.isfinite(price) and price > 0:
            position["snapshot_price"] = round(price, 4)
    return position


def build_portfolio_config(
    draft: Mapping[str, Any],
    *,
    portfolio_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Validate a create/update draft into a stored portfolio config."""

    label = _validate_label(draft.get("label"))
    note = _validate_note(draft.get("note"))
    cash = _validate_money(draft.get("cash"), "现金")
    debt = _validate_money(draft.get("debt"), "负债")
    history_start = _validate_history_start(draft.get("history_start"))
    raw_positions = draft.get("positions")
    if not isinstance(raw_positions, list) or not raw_positions:
        raise PortfolioRegistryError("组合至少需要一行持仓")
    if len(raw_positions) > MAX_POSITIONS_PER_PORTFOLIO:
        raise PortfolioRegistryError(f"单个组合最多 {MAX_POSITIONS_PER_PORTFOLIO} 行持仓")
    positions = [
        _validate_position_draft(raw, index + 1) for index, raw in enumerate(raw_positions)
    ]
    seen: dict[str, int] = {}
    for position in positions:
        key = position["thscode"]
        if key in seen:
            raise PortfolioRegistryError(f"持仓代码重复：{key}，请合并为一行")
        seen[key] = 1
    config: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "label": label,
        "note": note,
        "base_portfolio_id": str(draft.get("base_portfolio_id") or MAIN_PORTFOLIO_ID),
        "created_at": created_at,
        "updated_at": _now_iso(),
        "as_of": _now_iso()[:10],
        "currency": "CNY",
        "source_note": "Dashboard user-defined portfolio; quantities are manual snapshots.",
        "accounts": [
            {
                "account_id": "main",
                "account_type": "virtual",
                "cash": cash,
                "debt": debt,
                "positions": positions,
            }
        ],
    }
    if history_start:
        # Static daily P&L replay starts here instead of the global default.
        config["history_start"] = history_start
    return config


def load_extra_portfolios(
    path: Path = EXTRA_PORTFOLIOS_PATH,
) -> list[dict[str, Any]]:
    """Read and validate every user-defined portfolio; a bad file raises."""

    if not path.is_file():
        return []
    document = _read_json(path)
    if document.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise PortfolioRegistryError("portfolios.json schema_version 不受支持")
    raw_list = document.get("portfolios") or []
    if not isinstance(raw_list, list):
        raise PortfolioRegistryError("portfolios.json 的 portfolios 必须是数组")
    portfolios: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_list:
        if not isinstance(raw, Mapping):
            raise PortfolioRegistryError("portfolios.json 中存在非法组合条目")
        portfolio_id = str(raw.get("portfolio_id") or "")
        if not _PORTFOLIO_ID_RE.fullmatch(portfolio_id):
            raise PortfolioRegistryError(f"非法组合 id: {portfolio_id!r}")
        if portfolio_id in seen_ids:
            raise PortfolioRegistryError(f"组合 id 重复: {portfolio_id}")
        seen_ids.add(portfolio_id)
        accounts = raw.get("accounts") or []
        account = accounts[0] if accounts and isinstance(accounts[0], Mapping) else {}
        draft = {
            "label": raw.get("label"),
            "note": raw.get("note"),
            "cash": account.get("cash"),
            "debt": account.get("debt"),
            "positions": account.get("positions"),
            "base_portfolio_id": raw.get("base_portfolio_id"),
            "history_start": raw.get("history_start"),
        }
        config = build_portfolio_config(draft, portfolio_id=portfolio_id, created_at=str(raw.get("created_at") or ""))
        config["created_at"] = str(raw.get("created_at") or config["updated_at"])
        config["updated_at"] = str(raw.get("updated_at") or config["updated_at"])
        config["as_of"] = str(raw.get("as_of") or config["as_of"])
        portfolios.append(config)
    if len(portfolios) > MAX_PORTFOLIOS:
        raise PortfolioRegistryError(f"最多支持 {MAX_PORTFOLIOS} 个附加组合")
    return portfolios


def save_extra_portfolios(
    portfolios: list[Mapping[str, Any]],
    path: Path = EXTRA_PORTFOLIOS_PATH,
) -> None:
    document = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "portfolios": [copy.deepcopy(dict(item)) for item in portfolios],
    }
    _atomic_write_json(path, document)


def find_portfolio(
    portfolios: list[dict[str, Any]], portfolio_id: str
) -> dict[str, Any] | None:
    for config in portfolios:
        if str(config.get("portfolio_id")) == portfolio_id:
            return config
    return None


def create_portfolio(
    draft: Mapping[str, Any],
    *,
    path: Path = EXTRA_PORTFOLIOS_PATH,
    id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    with _REGISTRY_LOCK:
        portfolios = load_extra_portfolios(path)
        if len(portfolios) >= MAX_PORTFOLIOS:
            raise PortfolioRegistryError(f"最多支持 {MAX_PORTFOLIOS} 个附加组合")
        factory = id_factory or (lambda: f"pf-{uuid.uuid4().hex[:10]}")
        portfolio_id = factory()
        if not _PORTFOLIO_ID_RE.fullmatch(portfolio_id):
            raise PortfolioRegistryError(f"非法组合 id: {portfolio_id!r}")
        if find_portfolio(portfolios, portfolio_id) is not None:
            raise PortfolioRegistryError("组合 id 冲突，请重试")
        config = build_portfolio_config(
            draft, portfolio_id=portfolio_id, created_at=_now_iso()
        )
        portfolios.append(config)
        save_extra_portfolios(portfolios, path)
        return copy.deepcopy(config)


def update_portfolio(
    portfolio_id: str,
    draft: Mapping[str, Any],
    *,
    path: Path = EXTRA_PORTFOLIOS_PATH,
) -> dict[str, Any]:
    with _REGISTRY_LOCK:
        portfolios = load_extra_portfolios(path)
        existing = find_portfolio(portfolios, portfolio_id)
        if existing is None:
            raise PortfolioNotFound(f"组合不存在：{portfolio_id}")
        # ``existing`` is a stored config (positions nested under accounts);
        # normalize it to the flat draft shape before merging the patch.
        account = (existing.get("accounts") or [{}])[0]
        merged = {
            "label": existing.get("label"),
            "note": existing.get("note"),
            "cash": account.get("cash"),
            "debt": account.get("debt"),
            "positions": copy.deepcopy(account.get("positions") or []),
            "base_portfolio_id": existing.get("base_portfolio_id") or MAIN_PORTFOLIO_ID,
            "history_start": existing.get("history_start") or "",
        }
        for key in ("label", "note", "cash", "debt", "positions", "history_start"):
            if key in draft:
                merged[key] = draft[key]
        config = build_portfolio_config(
            merged, portfolio_id=portfolio_id, created_at=str(existing.get("created_at") or "")
        )
        config["created_at"] = str(existing.get("created_at") or config["updated_at"])
        portfolios = [config if item["portfolio_id"] == portfolio_id else item for item in portfolios]
        save_extra_portfolios(portfolios, path)
        return copy.deepcopy(config)


def delete_portfolio(
    portfolio_id: str,
    *,
    path: Path = EXTRA_PORTFOLIOS_PATH,
) -> dict[str, Any]:
    with _REGISTRY_LOCK:
        portfolios = load_extra_portfolios(path)
        existing = find_portfolio(portfolios, portfolio_id)
        if existing is None:
            raise PortfolioNotFound(f"组合不存在：{portfolio_id}")
        remaining = [item for item in portfolios if item["portfolio_id"] != portfolio_id]
        save_extra_portfolios(remaining, path)
        return copy.deepcopy(existing)


__all__ = [
    "EXTRA_PORTFOLIOS_PATH",
    "MAIN_PORTFOLIO_ID",
    "MAIN_PORTFOLIO_LABEL",
    "MAX_PORTFOLIOS",
    "PortfolioNotFound",
    "PortfolioRegistryError",
    "build_portfolio_config",
    "create_portfolio",
    "default_daily_limit_pct",
    "default_instrument_type",
    "delete_portfolio",
    "flatten_positions",
    "load_extra_portfolios",
    "save_extra_portfolios",
    "symbol_from_thscode",
    "update_portfolio",
]
