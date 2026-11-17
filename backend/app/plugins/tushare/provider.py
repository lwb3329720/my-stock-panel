"""Raw daily bars: volume in lots, amount in yuan; adjustment events, not levels."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import time
import threading

import httpx
import polars as pl

from app import secrets_store
from app.data_providers.base import ProviderRequestError
from app.data_providers.normalizer import normalize_daily
from app.data_providers.minute_normalizer import _normalize_minute
from app.market_time import cn_now


def availability():
    return bool(secrets_store.get_env_backed_secret("tushare_api_key", "TUSHARE_TOKEN")), "TUSHARE_TOKEN"


def probe_api_key(key):
    try:
        TushareProvider(key).query("trade_cal", exchange="SSE", start_date="20260105", end_date="20260105")
        return True, "ok"
    except ProviderRequestError:
        return False, "Tushare 认证或网络请求失败"


class TushareProvider:
    name = "tushare"
    builtin = True
    daily_resume_batch_size = 1
    financial_batch_size = 200

    def __init__(self, key=None):
        self._key = key
        self._financial_state = threading.local()
        self.financial_errors = []
        self.config = SimpleNamespace(name=self.name, display_name="Tushare", path=None,
                                      builtin=True, datasets=dict.fromkeys(["daily", "adj_factor", "minute", "financial"]))

    @property
    def financial_errors(self):
        return getattr(self._financial_state, "errors", [])

    @financial_errors.setter
    def financial_errors(self, errors):
        self._financial_state.errors = errors

    def close(self):
        pass

    def query(self, api, _page_size=2000, **params):
        key = self._key or secrets_store.get_env_backed_secret("tushare_api_key", "TUSHARE_TOKEN")
        output = []
        offset = 0
        rate_retries = 0
        while True:
            try:
                response = httpx.post("https://api.tushare.pro", timeout=40,
                    json={"api_name": api, "token": key,
                          "params": {**params, "offset": offset, "limit": _page_size}})
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ProviderRequestError(f"Tushare {api} 网络或响应异常") from exc
            if body.get("code") != 0:
                if "频率超限" in str(body.get("msg", "")) and rate_retries < 2:
                    rate_retries += 1
                    time.sleep(31)
                    time.sleep(31)
                    continue
                raise ProviderRequestError(f"Tushare {api} 请求失败，代码 {body.get('code')}")
            rate_retries = 0
            data = body.get("data") or {}
            rows = [dict(zip(data.get("fields", []), row, strict=True)) for row in data.get("items", [])]
            output.extend(rows)
            if len(rows) < _page_size:
                return output
            offset += len(rows)

    def get_minute(self, symbols, start_time=None, end_time=None, asset_type="stock",
                   freq="1m", on_chunk_done=None):
        if asset_type != "stock":
            raise ProviderRequestError("Tushare 当前仅接入个股分钟，不支持指数或 ETF")
        if freq not in ("1m", "5m", "15m", "30m", "60m"):
            raise ProviderRequestError("不支持的分钟周期")
        end = end_time or cn_now().replace(tzinfo=None)
        start = start_time or end.replace(hour=0, minute=0, second=0, microsecond=0)
        frames = []
        for index, symbol in enumerate(symbols):
            cursor = start
            while cursor <= end:
                stop = min(cursor + timedelta(days=7) - timedelta(seconds=1), end)
                rows = self.query("stk_mins", ts_code=symbol, freq=freq.replace("m", "min"),
                                  start_date=cursor.strftime("%Y-%m-%d %H:%M:%S"),
                                  end_date=stop.strftime("%Y-%m-%d %H:%M:%S"))
                if rows:
                    raw = pl.DataFrame(rows).with_columns(
                        pl.col("trade_time").str.strptime(pl.Datetime, "%Y-%m-%d %H:%M:%S"),
                        (pl.col("vol") / 100).alias("vol"))
                    frames.append(_normalize_minute(raw, source=self.name))
                cursor = stop + timedelta(seconds=1)
            if on_chunk_done:
                on_chunk_done(index + 1, len(symbols))
        return (pl.concat(frames).unique(["symbol", "datetime"]).sort(["symbol", "datetime"])
                if frames else pl.DataFrame())

    def get_financials(self, table, symbols, latest_only=True):
        from .financial import TABLES, normalize_financial, normalize_shares
        if table != "shares" and table not in TABLES:
            raise ProviderRequestError("不支持的财务表")
        from app.services import preferences
        history_start = preferences.load().get("financial_history_start_date")
        if history_start:
            history_start = datetime.strptime(history_start, "%Y-%m-%d").date().isoformat()
        api = "daily_basic" if table == "shares" else TABLES[table][0]
        frames = []
        self.financial_errors = []
        for symbol in symbols:
            params = {} if table in ("metrics", "shares") else {"report_type": "1"}
            if history_start:
                # API range is announcement-based; also filter report periods below.
                params["start_date"] = history_start.replace("-", "")
            try:
                rows = self.query(api, _page_size=2000 if table == "shares" else 100, ts_code=symbol, **params)
                frame = (normalize_shares(rows, latest_only, self.financial_errors) if table == "shares"
                         else normalize_financial(rows, table, latest_only, rejected=self.financial_errors))
                if history_start and not frame.is_empty():
                    frame = frame.filter(pl.col("period_end") >= history_start)
            except ProviderRequestError as exc:
                self.financial_errors.append({"symbol": symbol, "reason": str(exc)})
                continue
            if not frame.is_empty():
                frames.append(frame)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_daily(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None):
        api = {"stock": "daily", "index": "index_daily", "etf": "fund_daily"}[asset_type]
        rows = []
        for index, symbol in enumerate(symbols):
            rows.extend(self.query(api, ts_code=symbol,
                start_date=(start_time or datetime(1990, 1, 1)).strftime("%Y%m%d"),
                end_date=(end_time or datetime.now()).strftime("%Y%m%d")))
            if on_chunk_done:
                on_chunk_done(index + 1, len(symbols))
        if not rows:
            return pl.DataFrame()
        frame = pl.DataFrame(rows).with_columns(
            pl.col("trade_date").str.strptime(pl.Date, "%Y%m%d"),
            (pl.col("amount") * 1000).alias("amount"))
        return normalize_daily(frame, source=self.name)

    def get_adj_factors(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None):
        if asset_type == "index":
            return pl.DataFrame()
        api = "fund_adj" if asset_type == "etf" else "adj_factor"
        output = []
        for index, symbol in enumerate(symbols):
            # Query preceding history to convert cumulative factors into event ratios.
            rows = sorted(self.query(api, ts_code=symbol,
                end_date=(end_time or datetime.now()).strftime("%Y%m%d")), key=lambda row: row["trade_date"])
            previous = None
            for row in rows:
                factor = float(row["adj_factor"])
                day = datetime.strptime(row["trade_date"], "%Y%m%d").date()
                if factor <= 0:
                    raise ProviderRequestError("Tushare 复权因子必须大于零")
                if previous is not None and factor != previous and (start_time is None or day >= start_time.date()):
                    output.append({"symbol": symbol, "trade_date": day, "ex_factor": factor / previous})
                previous = factor
            if on_chunk_done:
                on_chunk_done(index + 1, len(symbols))
        return pl.DataFrame(output) if output else pl.DataFrame()

    def test_dataset(self, dataset, symbols=None):
        if dataset not in self.config.datasets:
            return {"provider": self.name, "dataset": dataset, "error": "不支持的数据集"}
        end = datetime.now() - timedelta(days=1)
        if dataset == "financial":
            frame = self.get_financials("metrics", symbols or ["600519.SH"])
        elif dataset == "minute":
            frame = self.get_minute(symbols or ["600519.SH"], end - timedelta(days=7), end)
        else:
            method = self.get_daily if dataset == "daily" else self.get_adj_factors
            frame = method(symbols or ["600519.SH"], end - timedelta(days=365), end)
        return {"provider": self.name, "dataset": dataset, "rows": frame.height,
                "columns": frame.columns, "preview": frame.head(3).to_dicts()}
