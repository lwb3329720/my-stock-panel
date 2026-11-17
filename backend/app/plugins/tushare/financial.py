"""Tushare consolidated cumulative statements, yuan and percentage points."""
from datetime import datetime, timedelta
import math

import polars as pl

from app.data_providers.base import ProviderRequestError

TABLES = {
    "metrics": ("fina_indicator", {"eps": "eps_basic", "bps": "bps", "roe": "roe",
        "roa": "roa", "grossprofit_margin": "gross_margin", "netprofit_margin": "net_margin",
        "debt_to_assets": "debt_to_asset_ratio", "or_yoy": "revenue_yoy",
        "netprofit_yoy": "net_income_yoy", "inv_turn": "inventory_turnover"}),
    "income": ("income", {"revenue": "revenue", "oper_cost": "operating_cost",
        "n_income": "net_income", "n_income_attr_p": "net_income_attributable",
        "operate_profit": "operating_profit", "total_profit": "total_profit",
        "income_tax": "income_tax", "basic_eps": "basic_eps", "sell_exp": "selling_expense",
        "admin_exp": "admin_expense", "fin_exp": "financial_expense"}),
    "balance_sheet": ("balancesheet", {"total_assets": "total_assets", "total_liab": "total_liabilities",
        "total_hldr_eqy_inc_min_int": "total_equity", "money_cap": "cash_and_equivalents",
        "accounts_receiv": "accounts_receivable", "total_cur_assets": "total_current_assets",
        "total_nca": "total_non_current_assets"}),
    "cash_flow": ("cashflow", {"n_cashflow_act": "net_operating_cash_flow",
        "n_cashflow_inv_act": "net_investing_cash_flow", "n_cash_flows_fnc_act": "net_financing_cash_flow",
        "c_pay_acq_const_fiolta": "capex", "n_incr_cash_cash_equ": "net_cash_change"}),
}


def normalize_financial(rows, table, latest_only, rejected=None):
    mapped = []
    for row in rows:
        if table != "metrics" and str(row.get("report_type")) != "1":
            continue  # Only consolidated cumulative original reports, not single-quarter/parent.
        try:
            period = datetime.strptime(row["end_date"], "%Y%m%d").date()
            announced = datetime.strptime(row.get("f_ann_date") or row["ann_date"], "%Y%m%d").date()
            if announced < period:
                raise ValueError("announcement before period")
        except (KeyError, TypeError, ValueError) as exc:
            if rejected is not None:
                rejected.append({"symbol": row.get("ts_code"), "period_end": row.get("end_date"),
                                 "reason": "报告期或公告日期无效"})
                continue
            raise ProviderRequestError("Tushare 财务报告期或公告日期无效") from exc
        mapped.append({"symbol": row["ts_code"], "period_end": period.isoformat(),
                       "announce_date": announced.isoformat(),
                       **{dst: float(row[src]) if row.get(src) is not None else None
                          for src, dst in TABLES[table][1].items()}})
    if not mapped:
        return pl.DataFrame()
    frame = pl.DataFrame(mapped, infer_schema_length=None).sort(["period_end", "announce_date"])
    frame = frame.unique(subset=["symbol", "period_end"], keep="last", maintain_order=True)
    return frame.tail(1) if latest_only else frame


def normalize_shares(rows, latest_only, rejected):
    """Daily snapshots in shares, available conservatively from the following day."""
    mapped = []
    for row in rows:
        try:
            day = datetime.strptime(row["trade_date"], "%Y%m%d").date()
            values = {dst: float(row[src]) * 10000 if row.get(src) is not None else None
                      for src, dst in (("total_share", "total_shares"),
                                       ("float_share", "float_shares"), ("free_share", "free_shares"))}
            if any(v is not None and (not math.isfinite(v) or v < 0) for v in values.values()):
                raise ValueError("invalid share capital")
            if not values["total_shares"] or not values["float_shares"]:
                raise ValueError("missing share capital")
            mapped.append({"symbol": row["ts_code"], "period_end": day.isoformat(),
                           "trade_date": day.isoformat(), "announce_date": None,
                           "available_date": (day + timedelta(days=1)).isoformat(),
                           "source": "tushare.daily_basic", **values})
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected.append({"symbol": row.get("ts_code"), "period_end": row.get("trade_date"),
                             "reason": "股本日期或数值无效"})
    if not mapped:
        return pl.DataFrame()
    frame = pl.DataFrame(mapped, infer_schema_length=None).with_columns(
        pl.col("announce_date").cast(pl.String), pl.col("free_shares").cast(pl.Float64),
    ).unique(["symbol", "period_end"], keep="last").sort(["symbol", "period_end"])
    return frame.group_by("symbol", maintain_order=True).tail(1) if latest_only else frame
