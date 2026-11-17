from datetime import date, datetime

import httpx
import pytest

from app.data_providers.base import ProviderRequestError
from app.plugins.huatai.provider import normalize_kline
from app.plugins.tushare.provider import TushareProvider


def test_financial_batches_persist_before_later_failure(tmp_path, monkeypatch):
    import polars as pl
    from app.services import financial_sync as sync
    from app.data_providers import custom
    class Provider:
        financial_batch_size = 1
        financial_errors = []
        def get_financials(self, table, symbols, **kwargs):
            if symbols == ["B"]:
                raise RuntimeError("interrupted")
            return pl.DataFrame({"symbol": symbols, "period_end": ["2025-09-30"],
                                 "announce_date": ["2025-10-31"], "roe": [20.]})
    monkeypatch.setattr(sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(custom, "get_provider", lambda name: Provider())
    with pytest.raises(RuntimeError):
        sync._sync_history_table_for_symbols("metrics", ["A", "B"], tmp_path, None)
    assert sync.get_financial_df(tmp_path, "metrics")["symbol"].to_list() == ["A"]


def test_tushare_financial_bad_date_is_quarantined(monkeypatch):
    p = TushareProvider()
    monkeypatch.setattr(p, "query", lambda *a, **k: [
        dict(ts_code=k["ts_code"], end_date="20250930", ann_date=None, roe=99),
        dict(ts_code=k["ts_code"], end_date="20250331", ann_date="20250430", roe=20)])
    frame = p.get_financials("metrics", ["600519.SH", "000001.SZ"], latest_only=False)
    assert frame.height == 2
    assert len(p.financial_errors) == 2
    assert frame["roe"].to_list() == [20, 20]


def test_tushare_rate_limit_retries_without_exposing_credentials(monkeypatch):
    from app.plugins.tushare import provider as module
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    replies = iter([{"code": 40203, "msg": "频率超限(1次/分钟)"},
                    {"code": 0, "data": {"fields": ["x"], "items": [[1]]}}])
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return next(replies)
    monkeypatch.setattr(module.httpx, "post", lambda *a, **k: Response())
    assert TushareProvider("test").query("stk_mins") == [{"x": 1}]
    assert sum(sleeps) == 62


def test_tushare_minute_units_period_and_asset_guard(monkeypatch):
    p = TushareProvider()
    calls = []
    def query(api, **params):
        calls.append((api, params))
        return [{"ts_code": "600519.SH", "trade_time": "2026-11-13 09:31:00",
                 "open": 10, "high": 11, "low": 9, "close": 10, "vol": 1000, "amount": 10000}]
    monkeypatch.setattr(p, "query", query)
    frame = p.get_minute(["600519.SH"], datetime(2026, 11, 13), datetime(2026, 11, 13, 15), freq="5m")
    assert frame["volume"][0] == 10
    assert frame["amount"][0] == 10000
    assert frame["datetime"][0] == datetime(2026, 11, 13, 9, 31)
    assert calls[0][1]["freq"] == "5min"
    with pytest.raises(ProviderRequestError):
        p.get_minute(["000001.SH"], asset_type="index")


def test_tushare_financial_report_scope_and_announcement():
    from app.plugins.tushare.financial import normalize_financial
    rows = [dict(ts_code="600519.SH", end_date="20250930", ann_date="20251030",
                 f_ann_date="20251031", report_type="1", n_income=120, n_income_attr_p=100),
            dict(ts_code="600519.SH", end_date="20250930", ann_date="20251030",
                 report_type="2", n_income=999)]
    frame = normalize_financial(rows, "income", False)
    assert frame.height == 1
    assert frame["net_income"][0] == 120
    assert frame["announce_date"][0] == "2025-10-31"
    with pytest.raises(ProviderRequestError):
        normalize_financial([{**rows[0], "f_ann_date": None, "ann_date": None}], "income", False)


def test_tushare_financial_history_and_latest():
    from app.plugins.tushare.financial import normalize_financial
    rows = [dict(ts_code="600519.SH", end_date=period, ann_date=ann, roe=20,
                 gross_margin=900, grossprofit_margin=80)
            for period, ann in [("20250331", "20250430"), ("20250930", "20251031")]]
    assert normalize_financial(rows, "metrics", False).height == 2
    latest = normalize_financial(rows, "metrics", True)
    assert latest["period_end"][0] == "2025-09-30"
    assert latest["gross_margin"][0] == 80


def test_huatai_financial_units_dates_and_scope():
    from app.plugins.huatai.financial import normalize_financial
    payload = {"报告期": "2026-09-30", "发布日期": "2026-10-31",
               "报表范围": "合并报表", "指标计算类型": "报告期累计",
               "盈利能力": {"加权平均净资产收益率": "24.47%"},
               "每股指标": {"每股净资产": "216.9496元/股"}}
    row = normalize_financial("600519.SH", "metrics", payload)
    assert row["roe"] == 24.47
    assert row["bps"] == 216.9496
    assert row["announce_date"] == "2026-10-31"
    assert row["gross_margin"] is None
    with pytest.raises(ProviderRequestError):
        normalize_financial("600519.SH", "metrics", {**payload, "报表范围": "母公司报表"})
    with pytest.raises(ProviderRequestError):
        normalize_financial("600519.SH", "metrics", {**payload, "发布日期": ""})
    with pytest.raises(ProviderRequestError):
        normalize_financial("600519.SH", "metrics", {**payload, "盈利能力": {"加权平均净资产收益率": "0.2447"}})


def test_huatai_income_keeps_parent_and_total_profit_distinct():
    from app.plugins.huatai.financial import normalize_financial
    row = normalize_financial("600519.SH", "income", {"data": {
        "报告期": "2026-09-30", "发布日期": "2026-10-31", "利润表": [{
            "净利润(含少数股东损益)": "120元", "净利润(不含少数股东损益)": "100元"}]}})
    assert row["net_income"] == 120
    assert row["net_income_attributable"] == 100


def test_daily_resume_reuses_completed_batches_and_separates_dates(tmp_path):
    import polars as pl
    from app.services.kline_sync import _fetch_resumable_daily

    class Provider:
        name = "test"
        daily_resume_batch_size = 1
        fail = True
        calls = []

        def get_daily(self, symbols, **kwargs):
            self.calls.extend(symbols)
            if symbols == ["B"] and self.fail:
                raise ProviderRequestError("disconnected")
            return pl.DataFrame({"symbol": symbols, "close": [10.0]})

    provider = Provider()
    start = datetime(2026, 1, 1)
    end = datetime(2026, 1, 2)
    with pytest.raises(ProviderRequestError):
        _fetch_resumable_daily(provider, ["A", "B"], start, end, tmp_path)
    provider.fail = False
    frame, root = _fetch_resumable_daily(provider, ["A", "B"], start, end, tmp_path)
    assert provider.calls == ["A", "B", "B"]
    assert frame["symbol"].to_list() == ["A", "B"]
    assert len(list(root.glob("*.parquet"))) == 2
    _fetch_resumable_daily(provider, ["A", "B"], start, datetime(2026, 1, 3), tmp_path)
    assert provider.calls == ["A", "B", "B", "A", "B"]


def test_tushare_daily_preserves_raw_prices_and_converts_thousand_yuan(monkeypatch):
    provider = TushareProvider()
    monkeypatch.setattr(provider, "query", lambda *a, **k: [dict(
        ts_code="600519.SH", trade_date="20261112", open=10, high=12,
        low=9, close=11, vol=100, amount=110)])
    frame = provider.get_daily(["600519.SH"], datetime(2026, 11, 12), datetime(2026, 11, 12))
    assert frame["date"][0] == date(2026, 11, 12)
    assert frame["close"][0] == 11
    assert frame["volume"][0] == 100
    assert frame["amount"][0] == 110000


def test_tushare_cumulative_factors_become_event_ratios(monkeypatch):
    provider = TushareProvider()
    monkeypatch.setattr(provider, "query", lambda *a, **k: [
        dict(ts_code="600519.SH", trade_date="20261112", adj_factor=12),
        dict(ts_code="600519.SH", trade_date="20261111", adj_factor=10),
        dict(ts_code="600519.SH", trade_date="20261110", adj_factor=10),
    ])
    frame = provider.get_adj_factors(["600519.SH"], datetime(2026, 11, 12), datetime(2026, 11, 12))
    assert frame.height == 1
    assert frame["ex_factor"][0] == pytest.approx(1.2)


def test_huatai_units_and_field_order():
    frame = normalize_kline("600519.SH", {"fields": ["交易日期", "收盘价", "开盘价", "最高价", "最低价", "成交量", "成交额"],
        "data": [["2026-11-12", "11元", "10元", "12元", "9元", "10000股", "110000元"]]})
    assert frame["close"][0] == 11
    assert frame["volume"][0] == 100
    assert frame["amount"][0] == 110000


def test_huatai_unknown_units_fail_closed():
    with pytest.raises(ProviderRequestError):
        normalize_kline("600519.SH", {"fields": ["交易日期"], "data": [["2026-11-12"]]})


def test_tushare_permission_error_is_not_an_empty_success(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200,
        request=httpx.Request("POST", "https://api.tushare.pro"),
        json={"code": -2002, "msg": "secret must not be echoed"}))
    with pytest.raises(ProviderRequestError, match="-2002") as error:
        TushareProvider("private-token").query("daily", ts_code="600519.SH")
    assert "secret" not in str(error.value)
    assert "private-token" not in str(error.value)


def test_tushare_network_failure_is_explicit(monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout("timeout")
    monkeypatch.setattr(httpx, "post", fail)
    with pytest.raises(ProviderRequestError):
        TushareProvider("test").query("daily")


def test_tushare_pagination_does_not_truncate(monkeypatch):
    offsets = []
    def page(*args, **kwargs):
        offset = kwargs["json"]["params"]["offset"]
        offsets.append(offset)
        return httpx.Response(200, request=httpx.Request("POST", "https://api.tushare.pro"),
            json={"code": 0, "data": {"fields": ["n"], "items": [[i] for i in range(2000 if offset == 0 else 3)]}})
    monkeypatch.setattr(httpx, "post", page)
    assert len(TushareProvider("test").query("daily")) == 2003
    assert offsets == [0, 2000]


def test_huatai_rejects_non_read_tools():
    from app.plugins.huatai.provider import call_mcp
    with pytest.raises(ProviderRequestError):
        call_mcp("mcp-A-stock", "place_order", {})


def test_research_unknown_kind_rejected():
    from fastapi import HTTPException

    from app.custom.local_research import ResearchQuery, query_research
    with pytest.raises(HTTPException) as error:
        query_research(ResearchQuery(kind="execute", symbol="600519.SH", day=date(2026, 11, 12)))
    assert error.value.status_code == 400


def test_tushare_financial_history_start_filters_report_period(monkeypatch):
    from app.plugins.tushare.provider import TushareProvider
    from app.services import preferences
    monkeypatch.setattr(preferences, "load", lambda: {"financial_history_start_date": "2023-11-16"})
    provider = TushareProvider("test")
    captured = {}
    def query(api, **kwargs):
        captured.update(kwargs)
        return [
            {"ts_code": "600519.SH", "end_date": "20230930", "ann_date": "20240401", "eps": 1},
            {"ts_code": "600519.SH", "end_date": "20231231", "ann_date": "20240401", "eps": 2},
        ]
    monkeypatch.setattr(provider, "query", query)
    frame = provider.get_financials("metrics", ["600519.SH"], latest_only=False)
    assert frame["period_end"].to_list() == ["2023-12-31"]
    assert captured["start_date"] == "20231116"


def test_tushare_share_history_units_dates_and_range(monkeypatch):
    from app.plugins.tushare.provider import TushareProvider
    from app.services import preferences
    from app.share_capital import apply_historical_float_shares
    from datetime import date
    import polars as pl
    monkeypatch.setattr(preferences, "load", lambda: {"financial_history_start_date": "2023-11-16"})
    p = TushareProvider("test")
    calls = []
    def query(api, **params):
        calls.append((api, params))
        return [
            {"ts_code": "600519.SH", "trade_date": "20231115", "total_share": 10, "float_share": 8, "free_share": 5},
            {"ts_code": "600519.SH", "trade_date": "20231116", "total_share": 12, "float_share": 9, "free_share": 6},
        ]
    monkeypatch.setattr(p, "query", query)
    frame = p.get_financials("shares", ["600519.SH"], latest_only=False)
    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["total_shares"] == 120000
    assert row["float_shares"] == 90000
    assert row["free_shares"] == 60000
    assert row["announce_date"] is None
    assert row["available_date"] == "2023-11-17"
    assert calls[0][0] == "daily_basic"
    assert calls[0][1]["start_date"] == "20231116"
    bars = pl.DataFrame({"symbol": ["600519.SH"] * 2, "date": [date(2023,11,16), date(2023,11,17)], "float_shares": [100.,100.]})
    got = apply_historical_float_shares(bars, frame, today=date(2026,11,16))
    assert got["float_shares"].to_list() == [100.,90000.]


def test_tushare_shares_latest_and_invalid_rows(monkeypatch):
    from app.plugins.tushare.provider import TushareProvider
    from app.services import preferences
    monkeypatch.setattr(preferences, "load", lambda: {})
    p = TushareProvider("test")
    monkeypatch.setattr(p, "query", lambda *a, **kw: [
        {"ts_code":"A", "trade_date":"20261112", "total_share":10, "float_share":8},
        {"ts_code":"A", "trade_date":"20261113", "total_share":12, "float_share":9},
        {"ts_code":"A", "trade_date":"bad", "total_share":12, "float_share":9},
        {"ts_code":"A", "trade_date":"20261114", "total_share":12, "float_share":float("nan")},
    ])
    got = p.get_financials("shares", ["A"], latest_only=True)
    assert got["period_end"].to_list() == ["2026-11-13"]
    assert len(p.financial_errors) == 2
