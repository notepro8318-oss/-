"""증권사 '미국주식 거래내역' 엑셀 → 매매일지 가져오기.

흐름: parse_statement(엑셀) → build_plan(미리보기용 계획) → apply_plan(일지에 반영)

규칙
- 읽는 행: '해외주식매수입고' / '해외주식매도출고'만. 입출금·배당·환전·이체 행(이름·계좌번호 포함)은 읽지 않고 저장하지 않는다.
- 개별주만 가져온다(ETF 제외). 종목명(한글) → 티커는 ticker_map.csv(+내장 목록)로 변환한다.
- 매수 1건 = 포지션(로트) 1건. 매도는 선입선출(FIFO)로 로트에 연결해 분할 매도 기록이 된다.
- 매수 기록이 없는 수량의 매도(1월 이전부터 보유하던 종목)는 가져오지 않는다.
- 거래 키(거래일자#거래번호)를 저장해 같은 파일을 다시 올려도 중복되지 않고 새 거래만 추가된다.
- 이미 수동으로 넣은 포지션은 같은 종목·비슷한 가격이면 실제 체결로 교체한다(메모·복기 유지).
- 거래 수수료·제세금은 포지션(매수)·매도 기록에 저장하고, 배당금(세전/세금/세후)은 따로 저장한다.
- ETF 거래는 시그널 없이 '장기 보유 ETF' 현황(수량·평균단가)용으로만 저장한다.
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
DIV_KIND, DIV_TAX_KIND = "배당금외화입금", "배당세금출금"
DIV_COLUMNS = ["id", "import_key", "date", "name", "ticker", "kind", "gross", "tax", "net"]
DIV_REPO_PATH = "topdown-strategy/quant_system/trade_dividends.csv"
DIV_LOCAL_PATH = os.path.join(jn._LOCAL_DIR, "trade_dividends.csv")
LEDGER_COLUMNS = ["key", "date", "no", "kind", "ticker", "side", "qty", "balance", "usd_cash", "krw_cash",
                  "flow_krw", "fx_hint"]
LEDGER_REPO_PATH = "topdown-strategy/quant_system/account_ledger.csv"
LEDGER_LOCAL_PATH = os.path.join(jn._LOCAL_DIR, "account_ledger.csv")
# 이 종류의 행은 '외화예수금'(USD 현금)을, 아래 행은 '예수금'(KRW 현금)을 체결 후 잔고로 보고한다
USD_CASH_KINDS = {"해외주식매수출금", "해외주식매도입금", DIV_KIND, "외화예탁금이용료입금", "외화매도외화출금"}
KRW_CASH_KINDS = {"예탁금이용료입금", "외화예탁금세금출금", "외화매도원화입금", DIV_TAX_KIND, "이체입금", "이체송금"}
ETF_COLUMNS = ["import_key", "date", "ticker", "name", "side", "qty", "price", "fee", "balance"]
ETF_REPO_PATH = "topdown-strategy/quant_system/etf_trades.csv"
ETF_LOCAL_PATH = os.path.join(jn._LOCAL_DIR, "etf_trades.csv")
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
        "fee": (pd.to_numeric(t.get("수수료"), errors="coerce").fillna(0)
                + pd.to_numeric(t.get("제세금합"), errors="coerce").fillna(0)).round(4),
    }).sort_values(["date", "no"]).reset_index(drop=True)
    info = {"rows_total": n_all, "rows_trade": len(out), "dividends": _parse_dividends(raw),
            "ledger": _parse_ledger(raw),
            "start": raw["_day"].min().date() if n_all else None, "end": raw["_day"].max().date() if n_all else None}
    return out, info


def _parse_ledger(raw: pd.DataFrame) -> pd.DataFrame:
    """계좌 잔고 복원용 숫자 내역: 체결 후 보유수량, USD/KRW 현금 잔고, 외부 입출금(KRW). 이름·계좌 정보는 담지 않는다."""
    keep = {BUY_KIND, SELL_KIND} | USD_CASH_KINDS | KRW_CASH_KINDS
    d = raw[raw["거래종류"].isin(keep)].dropna(subset=["_day"]).copy()
    rows = []
    for _, r in d.iterrows():
        kind, day = r["거래종류"], r["_day"]
        row = {"key": f"{day:%Y-%m-%d}#{int(r['거래번호'])}", "date": day.date(), "no": int(r["거래번호"]), "kind": kind,
               "ticker": "", "side": "", "qty": float("nan"), "balance": float("nan"), "usd_cash": float("nan"),
               "krw_cash": float("nan"), "flow_krw": float("nan"), "fx_hint": float("nan")}
        if kind in (BUY_KIND, SELL_KIND):
            row["name"] = norm_name(r["종목명"])
            row["side"] = "BUY" if kind == BUY_KIND else "SELL"
            row["qty"] = _num(r["수량"]) * (1 if kind == BUY_KIND else -1)
            row["balance"] = _num(r["외화유가증권"])
        if kind in USD_CASH_KINDS:
            row["usd_cash"] = _num(r["외화예수금"])
        if kind in KRW_CASH_KINDS:
            row["krw_cash"] = _num(r["예수금"])
        if kind in ("이체입금", "이체송금"):
            amt = _num(r["입출금액"])
            row["flow_krw"] = amt if kind == "이체입금" else -amt
            same = d[(d["_day"] == day) & (d["거래종류"].isin(["외화매도원화입금", "외화매도외화출금"]))]
            krw = sum(_num(v) for v in same[same["거래종류"] == "외화매도원화입금"]["외화입출금액"])
            usd = sum(_num(v) for v in same[same["거래종류"] == "외화매도외화출금"]["외화거래금액"])
            if krw > 0 and usd > 0:
                row["fx_hint"] = krw / usd
        rows.append(row)
    cols = LEDGER_COLUMNS + ["name"]
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


def _num(v) -> float:
    x = pd.to_numeric(v, errors="coerce")
    return 0.0 if pd.isna(x) else float(x)


def _parse_dividends(raw: pd.DataFrame) -> pd.DataFrame:
    """배당금 입금 행 → 세전(gross)/세금(tax)/세후(net) (USD).
    세금 = 외화로 떼인 제세금·수수료 + 같은 날 같은 종목의 '배당세금출금'(원화)을 그날 환율로 환산한 값."""
    cols = ["key", "date", "name", "gross", "tax", "net"]
    d = raw[raw["거래종류"] == DIV_KIND].dropna(subset=["_day"])
    if d.empty:
        return pd.DataFrame(columns=cols)
    tax_krw = raw[raw["거래종류"] == DIV_TAX_KIND].copy()
    tax_krw["_n"] = tax_krw["종목명"].map(norm_name)
    rows = []
    for _, r in d.iterrows():
        name, day = norm_name(r["종목명"]), r["_day"]
        gross = _num(r["외화거래금액"])
        tax = _num(r["제세금합"]) + _num(r["수수료"])
        fx = _num(r["단가"])
        same = tax_krw[(tax_krw["_day"] == day) & (tax_krw["_n"] == name)]
        if fx > 0 and not same.empty:
            tax += sum(_num(v) for v in same["거래금액"]) / fx
        rows.append({"key": f"{day:%Y-%m-%d}#{int(r['거래번호'])}", "date": day.date(), "name": name,
                     "gross": round(gross, 4), "tax": round(tax, 4), "net": round(gross - tax, 4)})
    return pd.DataFrame(rows, columns=cols).sort_values(["date", "key"]).reset_index(drop=True)


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
                         "entry_fee": float(r.fee), "sells": []})
            continue
        need = int(r.qty)
        for lot in lots:
            if need == 0:
                break
            take = min(lot["left"], need)
            if take > 0:
                lot["sells"].append({"key": f"{r.key}>{lot['key']}", "date_kr": r.date, "price": float(r.price),
                                     "shares": take, "fee": round(float(r.fee) * take / float(r.qty), 4)})
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
               name_map: dict, get_df, dividends: pd.DataFrame | None = None,
               dividends_have: set | None = None, etf_have: set | None = None,
               ledger: pd.DataFrame | None = None, ledger_have: set | None = None) -> dict:
    """미리보기용 계획. get_df(ticker) → 일봉 DataFrame(실패 시 빈 DataFrame). 아무것도 쓰지 않는다.
    dividends: parse_statement가 준 info['dividends']. *_have는 이미 저장된 거래 키(테스트용으로 주입 가능)."""
    info_dividends = dividends
    parsed_all = parsed
    plan = {"lots": [], "unmapped": [], "excluded_etf": {}, "dropped_sells": [], "fractional": [],
            "balance_warnings": [], "kept_manual": [], "period": (None, None),
            "fee_updates": {"trades": [], "exits": []}, "dividends_new": pd.DataFrame(), "etf_new": pd.DataFrame(),
            "ledger_new": pd.DataFrame()}
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
    existing_exit_fee: dict = {}
    if not trades_df.empty:
        for _, t in trades_df[trades_df["import_key"].astype(str) != ""].iterrows():
            existing[t["import_key"]] = t
    if exits_df is not None and not exits_df.empty:
        existing_exit_keys = set(exits_df["import_key"].astype(str)) - {""}
        existing_exit_fee = dict(zip(exits_df["import_key"].astype(str), exits_df["fee"]))

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
            # 수수료 열이 생기기 전에 가져온 기록은 파일 값으로 보강한다
            if abs(float(old["entry_fee"]) - lot["entry_fee"]) > 0.005:
                plan["fee_updates"]["trades"].append((old["id"], lot["entry_fee"]))
            for s_ in lot["sells"]:
                if s_["key"] in existing_exit_keys and abs(float(existing_exit_fee.get(s_["key"], 0)) - s_["fee"]) > 0.005:
                    plan["fee_updates"]["exits"].append((s_["key"], s_["fee"]))
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
    # 배당금: 이미 저장된 거래 키는 건너뛰고 새 것만. 종목명 → 티커(모르면 빈 값)
    divs = info_dividends if info_dividends is not None else pd.DataFrame()
    if not divs.empty:
        have = set(load_dividends()["import_key"]) if dividends_have is None else dividends_have
        new = divs[~divs["key"].isin(have)].copy()
        new["ticker"] = new["name"].map(lambda n: name_map.get(n, {}).get("ticker", ""))
        new["kind"] = new["name"].map(lambda n: name_map.get(n, {}).get("kind", ""))
        plan["dividends_new"] = new
    # ETF 거래(시그널 없는 장기 보유 현황용): 새 거래만
    etf = parsed_all[parsed_all["name"].map(lambda n: name_map.get(n, {}).get("kind") == "ETF")].copy()
    if not etf.empty:
        have = set(load_etf_trades()["import_key"]) if etf_have is None else etf_have
        etf = etf[~etf["key"].isin(have)]
        etf["ticker"] = etf["name"].map(lambda n: name_map[n]["ticker"])
        plan["etf_new"] = etf
    # 계좌 잔고 내역(자산 곡선용): 새 키만. 종목명 → 티커
    if ledger is not None and not ledger.empty:
        have = set(load_ledger()["key"]) if ledger_have is None else ledger_have
        lg = ledger[~ledger["key"].isin(have)].copy()
        lg["ticker"] = lg["name"].map(lambda n: name_map.get(n, {}).get("ticker", "") if isinstance(n, str) else "")
        plan["ledger_new"] = lg[LEDGER_COLUMNS]
    plan["summary"] = {
        "new_lots": sum(1 for l in plan["lots"] if l["mode"] == "new"),
        "replaced": sum(1 for l in plan["lots"] if l["mode"] in ("update", "replace")),
        "existing": sum(1 for l in plan["lots"] if l["mode"] == "exists"),
        "new_sells": sum(len(l["new_sells"]) for l in plan["lots"]),
        "to_apply": sum(1 for l in plan["lots"] if l["mode"] != "exists" or l["new_sells"]),
        "dividends": len(plan["dividends_new"]), "etf_rows": len(plan["etf_new"]),
        "ledger_rows": len(plan["ledger_new"]),
        "fee_updates": len(plan["fee_updates"]["trades"]) + len(plan["fee_updates"]["exits"]),
    }
    plan["summary"]["to_apply_total"] = (plan["summary"]["to_apply"] + plan["summary"]["dividends"]
                                         + plan["summary"]["etf_rows"] + plan["summary"]["fee_updates"]
                                         + plan["summary"]["ledger_rows"])
    return plan


# ------------------------------------------------------------------
# 배당 / ETF 저장소
# ------------------------------------------------------------------
def load_dividends() -> pd.DataFrame:
    df = jn._read_df(DIV_REPO_PATH, DIV_LOCAL_PATH, DIV_COLUMNS)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        for c in ("gross", "tax", "net"):
            df[c] = pd.to_numeric(df[c]).fillna(0.0)
        for c in ("import_key", "ticker", "kind", "name"):
            df[c] = df[c].astype(object).where(df[c].notna(), "")
    return df


def load_ledger() -> pd.DataFrame:
    df = jn._read_df(LEDGER_REPO_PATH, LEDGER_LOCAL_PATH, LEDGER_COLUMNS)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        for c in ("no", "qty", "balance", "usd_cash", "krw_cash", "flow_krw", "fx_hint"):
            df[c] = pd.to_numeric(df[c])
        for c in ("key", "kind", "ticker", "side"):
            df[c] = df[c].astype(object).where(df[c].notna(), "")
    return df


def load_etf_trades() -> pd.DataFrame:
    df = jn._read_df(ETF_REPO_PATH, ETF_LOCAL_PATH, ETF_COLUMNS)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        for c in ("qty", "price", "fee", "balance"):
            df[c] = pd.to_numeric(df[c])
        df["import_key"] = df["import_key"].astype(object).where(df["import_key"].notna(), "")
    return df


def etf_holdings(etf_df: pd.DataFrame) -> pd.DataFrame:
    """장기 보유 ETF 현황. 수량은 파일의 마지막 보유수량(외화유가증권), 평균단가는 이동평균(수수료 포함).
    파일에 없는 기초 보유분이 있어 거래로 계산한 수량과 보유수량이 다르면 cost_complete=False(평균단가 신뢰 불가)."""
    cols = ["ticker", "name", "qty", "avg_cost", "cost", "cost_complete", "first_buy"]
    if etf_df is None or etf_df.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for ticker, g in etf_df.sort_values(["date", "import_key"]).groupby("ticker"):
        qty, cost = 0.0, 0.0
        for r in g.itertuples():
            if r.side == "BUY":
                cost += r.qty * r.price + r.fee
                qty += r.qty
            elif qty > 0:
                take = min(r.qty, qty)
                cost -= cost / qty * take
                qty -= take
        held = float(g.iloc[-1]["balance"]) if pd.notna(g.iloc[-1]["balance"]) else qty
        if held <= 0:
            continue
        complete = abs(qty - held) < 1e-9
        rows.append({"ticker": ticker, "name": g.iloc[-1]["name"], "qty": held,
                     "avg_cost": (cost / qty) if complete and qty else float("nan"),
                     "cost": cost if complete else float("nan"), "cost_complete": complete,
                     "first_buy": g[g["side"] == "BUY"]["date"].min() if (g["side"] == "BUY").any() else None})
    return pd.DataFrame(rows, columns=cols)


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
                "shares": int(s["shares"]), "memo": "증권사 거래내역", "import_key": s["key"], "fee": s["fee"]}

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
            trades.at[i, "entry_fee"] = lot["entry_fee"]
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
            "lesson": carry["lesson"], "import_key": lot["key"], "entry_fee": lot["entry_fee"],
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

    for tid, fee in plan["fee_updates"]["trades"]:
        trades.loc[trades["id"] == tid, "entry_fee"] = fee
    for key, fee in plan["fee_updates"]["exits"]:
        exits.loc[exits["import_key"] == key, "fee"] = fee
    res["fee_updates"] = len(plan["fee_updates"]["trades"]) + len(plan["fee_updates"]["exits"])
    jn.save_trades(trades.reset_index(drop=True), "Import broker statement: trades")
    jn.save_exits(exits.reset_index(drop=True), "Import broker statement: exits")
    if drop_signal_trades:
        jn.save_signals(signals.reset_index(drop=True), "Import broker statement: replace signals")
    if extra_map:
        save_name_map_entries(extra_map)
    nd = plan["dividends_new"]
    if len(nd):
        cur = load_dividends()
        add = pd.DataFrame({"id": [uuid.uuid4().hex[:12] for _ in range(len(nd))], "import_key": nd["key"].values,
                            "date": nd["date"].values, "name": nd["name"].values, "ticker": nd["ticker"].values,
                            "kind": nd["kind"].values, "gross": nd["gross"].values, "tax": nd["tax"].values,
                            "net": nd["net"].values}, columns=DIV_COLUMNS)
        jn._write_df(add if cur.empty else pd.concat([cur, add], ignore_index=True), DIV_REPO_PATH, DIV_LOCAL_PATH,
                     "Import broker statement: dividends")
    res["dividends_added"] = len(nd)
    ne = plan["etf_new"]
    if len(ne):
        cur = load_etf_trades()
        add = pd.DataFrame({"import_key": ne["key"].values, "date": ne["date"].values, "ticker": ne["ticker"].values,
                            "name": ne["name"].values, "side": ne["side"].values, "qty": ne["qty"].values,
                            "price": ne["price"].values, "fee": ne["fee"].values, "balance": ne["balance"].values},
                           columns=ETF_COLUMNS)
        jn._write_df(add if cur.empty else pd.concat([cur, add], ignore_index=True), ETF_REPO_PATH, ETF_LOCAL_PATH,
                     "Import broker statement: ETF trades")
    res["etf_added"] = len(ne)
    nl = plan["ledger_new"]
    if len(nl):
        cur = load_ledger()
        add = nl.reset_index(drop=True)
        jn._write_df(add if cur.empty else pd.concat([cur, add], ignore_index=True), LEDGER_REPO_PATH, LEDGER_LOCAL_PATH,
                     "Import broker statement: account ledger")
    res["ledger_added"] = len(nl)

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
