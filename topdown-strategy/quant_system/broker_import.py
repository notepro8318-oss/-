"""증권사 '미국주식 거래내역' 엑셀 → 매매일지 가져오기.

흐름: parse_statement(엑셀) → build_plan(미리보기용 계획) → apply_plan(일지에 반영)

규칙
- 읽는 행: '해외주식매수입고' / '해외주식매도출고'만. 입출금·배당·환전·이체 행(이름·계좌번호 포함)은 읽지 않고 저장하지 않는다.
- 개별주만 가져온다(ETF 제외). 종목명(한글) → 티커는 ticker_map.csv(+내장 목록)로 변환한다.
- 매수 1건 = 포지션(로트) 1건. 매도는 선입선출(FIFO)로 로트에 연결해 분할 매도 기록이 된다.
- 매수 기록이 없는 수량의 매도(1월 이전부터 보유하던 종목)는 가져오지 않는다.
- 거래 키(거래일자#거래번호)를 저장해 같은 파일을 다시 올려도 중복되지 않고 새 거래만 추가된다.
- 이미 수동으로 넣은 포지션은 같은 종목·비슷한 가격이면 실제 체결로 교체한다(메모·복기 유지).
- 수수료·세금·배당은 반영하지 않는다.
"""
from __future__ import annotations

import io
import os
import uuid
from datetime import timedelta

import pandas as pd

import journal as jn
from journal import TRADE_COLUMNS, EXIT_SIGNAL_TYPES

BUY_KIND, SELL_KIND = "해외주식매수입고", "해외주식매도출고"
REQUIRED_COLUMNS = ["거래일자", "거래번호", "거래종류", "종목명", "수량", "단가", "외화유가증권"]
NAME_ALIASES = {"마벨 테크놀로지 그룹": "마블 테크놀로지 그룹"}   # 같은 종목이 두 가지 표기로 나옴
PRICE_MATCH_TOL = 0.02        # 수동 기록과 같은 거래로 볼 가격 허용 오차
EXACT_PRICE_TOL = 1e-4        # 이 안이면 시그널 이력까지 그대로 이어받는다
TP_CONFIRM_SLACK_DAYS = 3

# 이름 → (티커, 종류). 종류: STOCK(개별주) / ETF
SEED_MAP = {
    "크리도 테크놀로지 그룹 홀딩": ("CRDO", "STOCK"), "마블 테크놀로지 그룹": ("MRVL", "STOCK"),
    "SAP(ADR)": ("SAP", "STOCK"), "오라클": ("ORCL", "STOCK"), "오클로": ("OKLO", "STOCK"),
    "타겟": ("TGT", "STOCK"), "애플": ("AAPL", "STOCK"), "제이피모간 체이스": ("JPM", "STOCK"),
    "엔비디아": ("NVDA", "STOCK"), "비자": ("V", "STOCK"), "AMD": ("AMD", "STOCK"),
    "써모 피셔 사이언티픽": ("TMO", "STOCK"), "애브비": ("ABBV", "STOCK"),
    "메타 플랫폼스(페이스북)": ("META", "STOCK"), "스텔스가스": ("GASS", "STOCK"),
    "하모니 바이오사이언시스 홀딩스": ("HRMY", "STOCK"),
    "INVESCO NASDAQ 100": ("QQQM", "ETF"), "ISHARES 20+Y TREASURY BOND": ("TLT", "ETF"),
    "JP MORGAN EQUITY PREMIUM INCOME": ("JEPI", "ETF"), "SCHWAB US DIVIDEND EQUITY": ("SCHD", "ETF"),
    "SPDR COMMUNICATION SERVICES SELE": ("XLC", "ETF"), "SPDR CONSUMER STAPLES SELECT SEC": ("XLP", "ETF"),
    "SPDR UTILITIES SELECT SECTOR": ("XLU", "ETF"), "VANGUARD DIVIDEND APPRECIATION": ("VIG", "ETF"),
}
MAP_COLUMNS = ["name", "ticker", "kind"]
MAP_REPO_PATH = "topdown-strategy/quant_system/ticker_map.csv"
MAP_LOCAL_PATH = os.path.join(jn._LOCAL_DIR, "ticker_map.csv")


# ------------------------------------------------------------------
# 종목명 → 티커 매핑
# ------------------------------------------------------------------
def norm_name(v) -> str:
    s = " ".join(str(v).split())
    return NAME_ALIASES.get(s, s)


def load_name_map() -> dict:
    """내장 목록 + 사용자가 미리보기에서 추가한 매핑(ticker_map.csv, 같은 이름이면 사용자 값 우선)."""
    m = {k: {"ticker": v[0], "kind": v[1]} for k, v in SEED_MAP.items()}
    df = jn._read_df(MAP_REPO_PATH, MAP_LOCAL_PATH, MAP_COLUMNS)
    for _, r in df.dropna(subset=["name", "ticker"]).iterrows():
        m[norm_name(r["name"])] = {"ticker": str(r["ticker"]).strip().upper(), "kind": str(r["kind"] or "STOCK")}
    return m


def save_name_map_entries(entries: dict) -> None:
    """entries: {이름: {'ticker':..., 'kind':...}} — 사용자가 새로 정한 매핑만 저장한다."""
    if not entries:
        return
    df = jn._read_df(MAP_REPO_PATH, MAP_LOCAL_PATH, MAP_COLUMNS)
    rows = {norm_name(r["name"]): r for _, r in df.dropna(subset=["name"]).iterrows()}
    for name, v in entries.items():
        rows[norm_name(name)] = {"name": norm_name(name), "ticker": v["ticker"].strip().upper(), "kind": v["kind"]}
    out = pd.DataFrame(list(rows.values()), columns=MAP_COLUMNS)
    jn._write_df(out, MAP_REPO_PATH, MAP_LOCAL_PATH, "Update ticker map")


# ------------------------------------------------------------------
# 파일 읽기
# ------------------------------------------------------------------
def parse_statement(source) -> tuple[pd.DataFrame, dict]:
    """엑셀(경로/바이트/파일객체)에서 매수·매도 행만 뽑는다. 다른 행·열(이체 상대방 등)은 버린다."""
    raw = pd.read_excel(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError("거래내역 양식이 아닙니다. 없는 열: " + ", ".join(missing))
    raw = raw.dropna(subset=["거래일자"]).copy()
    raw["_day"] = pd.to_datetime(raw["거래일자"].astype(str).str.replace("/", "-"), errors="coerce")
    n_all = len(raw)
    t = raw[raw["거래종류"].isin([BUY_KIND, SELL_KIND])].dropna(subset=["_day"]).copy()
    out = pd.DataFrame({
        "key": t["_day"].dt.strftime("%Y-%m-%d") + "#" + t["거래번호"].astype(int).astype(str),
        "date": t["_day"].dt.date, "no": t["거래번호"].astype(int),
        "side": t["거래종류"].map({BUY_KIND: "BUY", SELL_KIND: "SELL"}),
        "name": t["종목명"].map(norm_name), "qty": pd.to_numeric(t["수량"]),
        "price": pd.to_numeric(t["단가"]), "balance": pd.to_numeric(t["외화유가증권"]),
    }).sort_values(["date", "no"]).reset_index(drop=True)
    info = {"rows_total": n_all, "rows_trade": len(out),
            "start": raw["_day"].min().date() if n_all else None, "end": raw["_day"].max().date() if n_all else None}
    return out, info


# ------------------------------------------------------------------
# 날짜 보정 (한국 거래일자 → 미국 거래일)
# ------------------------------------------------------------------
def resolve_us_day(df: pd.DataFrame, kr_date, price: float, tol: float = 0.015):
    """증권사 거래일자는 한국 날짜라 미국 거래일과 하루 어긋날 수 있다.
    한국 날짜와 직전 거래일 중 체결가가 그날 고저 범위에 들어가는 날을 미국 거래일로 본다."""
    ts = pd.Timestamp(kr_date)
    if df is None or df.empty:
        return ts.date()
    idx = df.index
    cands = ([ts] if ts in idx else []) + ([idx[idx < ts][-1]] if (idx < ts).any() else [])
    for d in cands:
        row = df.loc[d]
        if float(row["Low"]) * (1 - tol) <= price <= float(row["High"]) * (1 + tol):
            return d.date()
    if cands:
        return cands[0].date()
    nxt = idx[idx >= ts]
    return nxt[0].date() if len(nxt) else ts.date()


# ------------------------------------------------------------------
# 계획 만들기 (미리보기)
# ------------------------------------------------------------------
def _fifo(rows: pd.DataFrame) -> tuple[list, list]:
    """한 종목의 시간순 거래 → (로트들, 매수 기록이 없어 연결하지 못한 매도들)."""
    lots, dropped = [], []
    for r in rows.itertuples():
        if r.side == "BUY":
            lots.append({"key": r.key, "date_kr": r.date, "price": float(r.price), "qty": int(r.qty), "left": int(r.qty),
                         "sells": []})
            continue
        need = int(r.qty)
        for lot in lots:
            if need == 0:
                break
            take = min(lot["left"], need)
            if take > 0:
                lot["sells"].append({"key": f"{r.key}>{lot['key']}", "date_kr": r.date, "price": float(r.price),
                                     "shares": take})
                lot["left"] -= take
                need -= take
        if need > 0:
            dropped.append({"key": r.key, "date": r.date, "qty": need, "price": float(r.price)})
    return lots, dropped


def _validate_balances(rows: pd.DataFrame) -> list:
    """파일의 '외화유가증권'(체결 후 보유수량)과 거래로 재구성한 수량을 대조한다.
    첫 거래 시점의 차이는 '1월 이전부터 보유하던 수량'으로 보고, 이후에 다시 어긋나면 경고한다."""
    warns = []
    cum, opening = 0, None
    for r in rows.itertuples():
        cum += int(r.qty) if r.side == "BUY" else -int(r.qty)
        if pd.isna(r.balance):
            continue
        gap = int(r.balance) - cum
        if opening is None:
            opening = gap
        elif gap != opening:
            warns.append(f"{r.date} {r.name}: 파일 잔고 {int(r.balance)}주 ≠ 거래로 계산한 {cum + opening}주")
            opening = gap   # 같은 오류를 반복해서 보고하지 않도록 기준을 갱신
    return warns


def build_plan(parsed: pd.DataFrame, trades_df: pd.DataFrame, exits_df: pd.DataFrame, signals_df: pd.DataFrame,
               name_map: dict, get_df) -> dict:
    """미리보기용 계획. get_df(ticker) → 일봉 DataFrame(실패 시 빈 DataFrame). 아무것도 쓰지 않는다."""
    plan = {"lots": [], "unmapped": [], "excluded_etf": {}, "dropped_sells": [], "fractional": [],
            "balance_warnings": [], "kept_manual": [], "period": (None, None)}
    if parsed.empty:
        return plan
    plan["period"] = (parsed["date"].min(), parsed["date"].max())

    unmapped = sorted({n for n in parsed["name"].unique() if n not in name_map})
    plan["unmapped"] = unmapped
    parsed = parsed[parsed["name"].isin(name_map)].copy()
    parsed["ticker"] = parsed["name"].map(lambda n: name_map[n]["ticker"])
    parsed["kind"] = parsed["name"].map(lambda n: name_map[n]["kind"])
    for name, g in parsed[parsed["kind"] == "ETF"].groupby("name"):
        plan["excluded_etf"][name] = len(g)
    stocks = parsed[parsed["kind"] == "STOCK"]
    frac = stocks[(stocks["qty"] % 1) != 0]
    plan["fractional"] = [f"{r.date} {r.name} {r.qty}주" for r in frac.itertuples()]
    stocks = stocks.drop(frac.index)

    existing = {}      # 매수 거래 키 → 이미 가져온 포지션
    existing_exit_keys: set = set()
    if not trades_df.empty:
        for _, t in trades_df[trades_df["import_key"].astype(str) != ""].iterrows():
            existing[t["import_key"]] = t
    if exits_df is not None and not exits_df.empty:
        existing_exit_keys = set(exits_df["import_key"].astype(str)) - {""}

    manual = pd.DataFrame()
    if not trades_df.empty:
        manual = trades_df[trades_df["import_key"].astype(str) == ""]
    matched_old: set = set()
    start = plan["period"][0]
    all_lots = []
    for ticker, g in stocks.groupby("ticker"):
        plan["balance_warnings"] += _validate_balances(g)
        lots, dropped = _fifo(g)
        for d in dropped:
            plan["dropped_sells"].append({**d, "ticker": ticker, "name": g.iloc[0]["name"]})
        df = get_df(ticker)
        cand = manual[(manual["ticker"] == ticker)] if not manual.empty else manual
        for lot in lots:
            lot["ticker"], lot["name"] = ticker, g.iloc[0]["name"]
            lot["date"] = resolve_us_day(df, lot["date_kr"], lot["price"])
            for s in lot["sells"]:
                s["date"] = max(resolve_us_day(df, s["date_kr"], s["price"]), lot["date"])
            lot["df"] = df
            all_lots.append(lot)

    for lot in sorted(all_lots, key=lambda x: (x["date_kr"], x["key"])):
        lot["remaining"] = lot["left"]
        old = existing.get(lot["key"])
        if old is not None:
            lot.update(mode="exists", trade_id=old["id"], matched=None)
            lot["new_sells"] = [s for s in lot["sells"] if s["key"] not in existing_exit_keys]
        else:
            lot["new_sells"] = lot["sells"]
            lot.update(mode="new", trade_id=None, matched=None)
            if not manual.empty:
                best, best_diff = None, None
                for _, m in manual[(manual["ticker"] == lot["ticker"]) & (~manual["id"].isin(matched_old))].iterrows():
                    if m["entry_date"] < start - timedelta(days=3):
                        continue
                    diff = abs(float(m["entry_price"]) / lot["price"] - 1)
                    if diff <= PRICE_MATCH_TOL and (best is None or diff < best_diff):
                        best, best_diff = m, diff
                if best is not None:
                    matched_old.add(best["id"])
                    lot["matched"] = best["id"]
                    lot["mode"] = "update" if best_diff <= EXACT_PRICE_TOL else "replace"
        plan["lots"].append(lot)

    # 같은 종목인데 파일의 어떤 거래와도 짝이 안 맞은 수동 기록(유지됨)
    if not manual.empty:
        imported_tickers = {l["ticker"] for l in plan["lots"]}
        for _, m in manual.iterrows():
            if m["ticker"] in imported_tickers and m["id"] not in matched_old and m["entry_date"] >= start - timedelta(days=3):
                plan["kept_manual"].append({"id": m["id"], "ticker": m["ticker"], "date": m["entry_date"],
                                            "price": float(m["entry_price"]), "shares": int(m["initial_shares"]),
                                            "status": m["status"]})
    plan["summary"] = {
        "new_lots": sum(1 for l in plan["lots"] if l["mode"] == "new"),
        "replaced": sum(1 for l in plan["lots"] if l["mode"] in ("update", "replace")),
        "existing": sum(1 for l in plan["lots"] if l["mode"] == "exists"),
        "new_sells": sum(len(l["new_sells"]) for l in plan["lots"]),
        "to_apply": sum(1 for l in plan["lots"] if l["mode"] != "exists" or l["new_sells"]),
    }
    return plan


# ------------------------------------------------------------------
# 반영
# ------------------------------------------------------------------
def _confirm_by_sales(signals_df: pd.DataFrame, trade_id: str, sale_dates: list, closed: bool) -> None:
    """가져온 매도로 이미 처리된 시그널을 확인 처리한다 (record_sale과 같은 규칙)."""
    if signals_df.empty:
        return
    mine = signals_df[(signals_df["trade_id"] == trade_id) & (~signals_df["confirmed"])]
    if closed:
        signals_df.loc[mine.index, "confirmed"] = True
        return
    for sd in sorted(sale_dates):
        pend = signals_df[(signals_df["trade_id"] == trade_id) & (~signals_df["confirmed"])
                          & (signals_df["type"].isin(["TP1", "TP2"]))
                          & (signals_df["date"] <= sd + timedelta(days=TP_CONFIRM_SLACK_DAYS))]
        if not pend.empty:
            signals_df.loc[pend.sort_values("date").index[0], "confirmed"] = True


def apply_plan(plan: dict, extra_map: dict | None = None) -> dict:
    """계획을 일지에 쓴다(포지션·매도 기록·시그널). 이후 새 열린 포지션의 과거 시그널은 알림 없이 기록한다."""
    trades = jn.load_trades()
    exits = jn.load_exits()
    signals = jn.load_signals()
    if trades.empty:
        trades = pd.DataFrame(columns=TRADE_COLUMNS)
    if exits.empty:
        exits = pd.DataFrame(columns=jn.EXIT_COLUMNS)

    new_trade_rows, new_exit_rows = [], []
    drop_trades, drop_exit_trades, drop_signal_trades = set(), set(), set()
    fresh_open_ids, reconcile = set(), []        # reconcile: (trade_id, sale_dates, closed)
    res = {"created": 0, "updated": 0, "replaced": 0, "exits_added": 0, "errors": []}

    def exit_row(tid, s):
        return {"id": uuid.uuid4().hex[:12], "trade_id": tid, "date": s["date"], "price": s["price"],
                "shares": int(s["shares"]), "memo": "증권사 거래내역", "import_key": s["key"]}

    for lot in plan["lots"]:
        if lot["mode"] == "exists":
            if not lot["new_sells"]:
                continue
            tid = lot["trade_id"]
            i = trades.index[trades["id"] == tid][0]
            sold = sum(s["shares"] for s in lot["new_sells"])
            left = max(0, int(trades.at[i, "remaining_shares"]) - sold)
            trades.at[i, "remaining_shares"] = left
            if left == 0:
                trades.at[i, "status"] = "CLOSED"
            new_exit_rows += [exit_row(tid, s) for s in lot["new_sells"]]
            reconcile.append((tid, [s["date"] for s in lot["new_sells"]], left == 0))
            res["updated"] += 1
            res["exits_added"] += len(lot["new_sells"])
            continue

        sold = sum(s["shares"] for s in lot["sells"])
        left = lot["qty"] - sold
        status = "CLOSED" if left == 0 else "OPEN"
        if lot["mode"] == "update":      # 같은 가격의 수동 기록 → 시그널·단계·손절 이력을 그대로 두고 체결 정보만 교체
            tid = lot["matched"]
            i = trades.index[trades["id"] == tid][0]
            drop_exit_trades.add(tid)
            trades.at[i, "entry_date"] = lot["date"]
            trades.at[i, "initial_shares"] = lot["qty"]
            trades.at[i, "remaining_shares"] = left
            trades.at[i, "status"] = status
            trades.at[i, "import_key"] = lot["key"]
            trades.at[i, "asset_name"] = lot["name"]
            new_exit_rows += [exit_row(tid, s) for s in lot["sells"]]
            reconcile.append((tid, [s["date"] for s in lot["sells"]], left == 0))
            res["replaced"] += 1
            res["exits_added"] += len(lot["sells"])
            continue

        prev = jn.preview_entry(lot["ticker"], lot["date"], lot["price"], df=lot.get("df"))
        if "error" in prev:
            res["errors"].append(f"{lot['ticker']} {lot['date']}: {prev['error']}")
            continue
        carry = {"memo": "", "mistake_tags": "", "lesson": "", "archived": False}
        if lot["mode"] == "replace":     # 가격이 다른 수동 기록 → 새로 만들고 메모·복기만 옮긴다
            old = trades[trades["id"] == lot["matched"]].iloc[0]
            carry = {k: old[k] if pd.notna(old[k]) else carry[k] for k in carry}
            drop_trades.add(lot["matched"])
            drop_exit_trades.add(lot["matched"])
            drop_signal_trades.add(lot["matched"])
            res["replaced"] += 1
        else:
            res["created"] += 1
        tid = uuid.uuid4().hex[:12]
        last_sale = max([s["date"] for s in lot["sells"]], default=None)
        new_trade_rows.append({
            "id": tid, "ticker": lot["ticker"], "asset_name": lot["name"], "entry_date": prev["entry_idx"].date(),
            "entry_price": lot["price"], "initial_shares": lot["qty"], "remaining_shares": left,
            "initial_stop": prev["initial_stop"], "current_stop": prev["initial_stop"], "atr14_at_entry": prev["atr14"],
            "status": status, "phase": "STAGE_0",
            "last_checked_date": (last_sale if status == "CLOSED" else prev["entry_idx"].date() - timedelta(days=1)),
            "memo": carry["memo"], "archived": bool(carry["archived"]), "mistake_tags": carry["mistake_tags"],
            "lesson": carry["lesson"], "import_key": lot["key"],
        })
        new_exit_rows += [exit_row(tid, s) for s in lot["sells"]]
        res["exits_added"] += len(lot["sells"])
        if status == "OPEN":
            fresh_open_ids.add(tid)
        if lot["sells"]:
            reconcile.append((tid, [s["date"] for s in lot["sells"]], left == 0))

    if drop_trades:
        trades = trades[~trades["id"].isin(drop_trades)]
        signals = signals[~signals["trade_id"].isin(drop_signal_trades)] if not signals.empty else signals
    if drop_exit_trades and not exits.empty:
        exits = exits[~exits["trade_id"].isin(drop_exit_trades)]
    if new_trade_rows:
        add = pd.DataFrame(new_trade_rows, columns=TRADE_COLUMNS)
        trades = add if trades.empty else pd.concat([trades, add], ignore_index=True)
    if new_exit_rows:
        add = pd.DataFrame(new_exit_rows, columns=jn.EXIT_COLUMNS)
        exits = add if exits.empty else pd.concat([exits, add], ignore_index=True)

    jn.save_trades(trades.reset_index(drop=True), "Import broker statement: trades")
    jn.save_exits(exits.reset_index(drop=True), "Import broker statement: exits")
    if drop_signal_trades:
        jn.save_signals(signals.reset_index(drop=True), "Import broker statement: replace signals")
    if extra_map:
        save_name_map_entries(extra_map)

    # 새로 만든 열린 포지션은 진입일부터 지금까지의 시그널을 한 번에 감지한다. 이미 지나간 시그널이라
    # 텔레그램 알림은 보내지 않도록 notified 처리한다.
    if fresh_open_ids or reconcile:
        try:
            if fresh_open_ids:
                jn.scan_open_trades()
            sig = jn.load_signals()
            if not sig.empty:
                sig.loc[sig["trade_id"].isin(fresh_open_ids), "notified"] = True
                for tid, dates, closed in reconcile:
                    _confirm_by_sales(sig, tid, dates, closed)
                jn.save_signals(sig, "Import broker statement: reconcile signals")
        except Exception as e:   # 시세 조회 실패 등 — 가져오기 자체는 이미 끝났으므로 안내만 한다
            res["errors"].append(f"시그널 재계산 중 오류: {e}")
    return res
