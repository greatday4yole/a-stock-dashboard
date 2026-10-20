#!/usr/bin/env python3
"""组合聚合做T精细化：修正执行口径 + 宽松共跌 + 逐日明细。"""
import json, math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/intraday-pnl-backfill/symbols"
cfg = json.load(open(ROOT / "config/portfolio.json"))
holdings = {}
for acc in cfg["accounts"]:
    for pos in acc["positions"]:
        holdings[pos["thscode"]] = holdings.get(pos["thscode"], 0) + int(pos["quantity"])
symbols = sorted(holdings)
docs = {s: json.load(open(CACHE / f"{s.replace('.','-')}.json")) for s in symbols}
cost = {s: (3.0 if s[:6] in ("159611", "512930") else 13.0) for s in symbols}
days = [d for d in sorted(docs[symbols[0]]["bars"])
        if all(len(docs[s]["bars"].get(d) or []) >= 45 and d in docs[s]["prev_close"] for s in symbols)]

def med_amp(s):
    vals = []
    for d in days:
        closes = [float(b[1]) for b in docs[s]["bars"][d]]
        pc = docs[s]["prev_close"][d]
        if pc > 0: vals.append((max(closes)-min(closes))/pc)
    return sorted(vals)[len(vals)//2]
TH_S = {s: min(1.5, max(0.5, 0.6*med_amp(s)*100)) for s in symbols}

agg_days = {}
amp_vals = []
for d in days:
    pcs = {s: docs[s]["prev_close"][d] for s in symbols}
    MV = sum(holdings[s]*pcs[s] for s in symbols)
    closes = {s: [float(b[1]) for b in docs[s]["bars"][d]] for s in symbols}
    n = len(closes[symbols[0]])
    agg = [sum(holdings[s]*(closes[s][t]-pcs[s]) for s in symbols)/MV*1e4 for t in range(n)]
    agg_days[d] = {"agg": agg, "MV": MV, "closes": closes, "mvw": {s: holdings[s]*pcs[s]/MV for s in symbols}}
    amp_vals.append(max(agg)-min(agg))
TH_BASE = min(30.0, max(4.0, 0.6*sorted(amp_vals)[len(amp_vals)//2]))

def agg_sim(direction, th_bp, mode, own_mult=1.0, detail=False):
    tries = 0; day_rows = []
    for d in days:
        rec = agg_days[d]
        agg, mvw, closes = rec["agg"], rec["mvw"], rec["closes"]
        open_ref = agg[0]
        entry = None
        for i in range(1, min(12, len(agg)-1)):
            if direction == "long" and agg[i] <= open_ref - th_bp: entry = i; break
            if direction == "short" and agg[i] >= open_ref + th_bp: entry = i; break
        if entry is None: continue
        tries += 1
        exit_i = len(agg)-1
        for j in range(entry+1, len(agg)):
            if direction == "long" and agg[j] >= agg[entry] + th_bp: exit_i = j; break
            if direction == "short" and agg[j] <= agg[entry] - th_bp: exit_i = j; break
        executed = []
        for s in symbols:
            if mode == "all":
                executed.append(s); continue
            cs = closes[s]; own_open = cs[0]; thp = TH_S[s]/100.0*own_mult
            hit = (cs[entry] <= own_open*(1-thp)) if direction == "long" else (cs[entry] >= own_open*(1+thp))
            if hit: executed.append(s)
        day_bps = 0.0
        for s in executed:
            raw = (closes[s][exit_i]/closes[s][entry]-1)*1e4 * (1 if direction == "long" else -1)
            day_bps += (raw - cost[s]) * mvw[s]
        day_rows.append({"date": d, "entry": entry, "exit": exit_i, "n": len(executed), "bps": day_bps})
    exec_days = [r for r in day_rows if r["n"] > 0]
    per_day = sum(r["bps"] for r in day_rows)/len(days)
    return {
        "tries": tries, "exec_days": len(exec_days),
        "win_rate": (sum(1 for r in exec_days if r["bps"] > 0)/len(exec_days)) if exec_days else 0.0,
        "avg": (sum(r["bps"] for r in exec_days)/len(exec_days)) if exec_days else 0.0,
        "per_day": per_day,
        "worst": min((r["bps"] for r in day_rows), default=0.0),
        "rows": day_rows,
    }

print(f"聚合阈值基准 {TH_BASE:.0f}bp（组合振幅中位×0.6）")
print(f"{'策略':<34}{'触发':>4}{'有执行':>6}{'胜率':>7}{'均净bp':>8}{'每日bp':>8}{'最差日':>8}")
results = {}
for direction in ("long", "short"):
    dlabel = "正T" if direction == "long" else "反T"
    for mode, mlabel, own_mult in (("all", "V1全买", 1.0), ("confluence", "V2共跌(严)", 1.0), ("confluence", "V2共跌(松0.5×)", 0.5)):
        for m in (0.75, 1.0, 1.5):
            r = agg_sim(direction, TH_BASE*m, mode, own_mult)
            results[(direction, mode, m)] = r
            print(f"组合{dlabel}·{mlabel}×{m:<16}{r['tries']:>4}{r['exec_days']:>6}{r['win_rate']*100:>6.0f}%{r['avg']:>8.1f}{r['per_day']:>8.1f}{r['worst']:>8.1f}")

# 最优策略逐日明细
best_key = max(results, key=lambda k: results[k]["per_day"])
r = results[best_key]
direction, mode, m = best_key
print(f"\n=== 最优：组合{'正T' if direction=='long' else '反T'}·{'V1全买' if mode=='all' else 'V2共跌'}×{m}（每日 {r['per_day']:.1f}bp）逐日明细 ===")
for row in r["rows"]:
    print(f"  {row['date']}  入场bar{row['entry']:>2} 了结bar{row['exit']:>2}  执行{row['n']:>2}只  {row['bps']:>+8.1f}bp")
