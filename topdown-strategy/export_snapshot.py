"""
웹 대시보드용 스냅샷 데이터 추출 스크립트
====================================================
main.py의 1~4단계 로직을 그대로 호출해 현재 시점의 스캔 결과를
JSON으로 저장한다. product-maker 블로그 사이트의 topdown-strategy.html이
이 JSON을 정적 데이터로 임베드해서 보여준다.

주의: 1단계 시장 필터가 '매매 중단'이어도 2~4단계는 참고용으로 항상 계산해
      화면에서는 "시장 필터 통과 시 이런 시그널이 나온다"를 함께 보여준다.
"""

import json
from datetime import datetime, timezone

from market import check_market_trend
from sector import rank_sectors, select_leading_sectors
from stock import scan_leading_stocks
from entry import check_entry_signal, calc_position_size
from config import INITIAL_CASH, RISK_PER_TRADE_PCT, TOP_SECTOR_COUNT

def main():
    snapshot = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    market = check_market_trend()
    snapshot["market"] = market

    ranked = rank_sectors()
    leaders = select_leading_sectors()
    sector_returns = ranked["ret_1m"].to_dict()

    snapshot["sector_ranking"] = [
        {"ticker": t, "ret_1w": round(row["ret_1w"], 2), "ret_1m": round(row["ret_1m"], 2),
         "rank_1w": int(row["rank_1w"]), "rank_1m": int(row["rank_1m"])}
        for t, row in ranked.iterrows()
    ]
    snapshot["leading_sectors"] = leaders
    snapshot["top_sector_count"] = TOP_SECTOR_COUNT

    candidates = scan_leading_stocks(leaders, sector_returns)
    stock_rows = []
    signal_rows = []
    for c in candidates:
        entry = check_entry_signal(c["df"])
        row = {
            "ticker": c["ticker"], "sector": c["sector"], "close": round(c["close"], 2),
            "ma20": round(c["ma20"], 2), "ma50": round(c["ma50"], 2), "ma200": round(c["ma200"], 2),
            "high_52w": round(c["high_52w"], 2),
            "stock_ret_1m": round(c["stock_ret_1m"], 2), "sector_ret_1m": round(c["sector_ret_1m"], 2),
        }
        stock_rows.append(row)

        if entry["signal"]:
            sizing = calc_position_size(INITIAL_CASH, entry["entry_price"], entry["stop_loss"])
            signal_rows.append({
                "ticker": c["ticker"], "sector": c["sector"], "type": entry["type"],
                "entry_price": round(entry["entry_price"], 2),
                "stop_loss": round(entry["stop_loss"], 2),
                "take_profit": round(entry["take_profit"], 2),
                "shares": sizing["shares"],
                "risk_amount": round(sizing["risk_amount"], 2),
                "position_value": round(sizing["position_value"], 2),
            })

    snapshot["stock_candidates"] = stock_rows
    snapshot["entry_signals"] = signal_rows
    snapshot["assumed_capital"] = INITIAL_CASH
    snapshot["risk_per_trade_pct"] = RISK_PER_TRADE_PCT

    with open("snapshot.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
