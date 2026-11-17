"""Read-only MCP bridge with explicit conversion of upstream unit-bearing values."""
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.config import settings
from app.data_providers.base import ProviderRequestError
from app.data_providers.normalizer import normalize_daily

SERVERS = {"mcp-A-stock", "mcp-A-stock-financial", "mcp-A-stock-business", "mcp-index", "macro_indicators_mcp"}


def availability():
    required = ("HUATAI_PYTHON", "HUATAI_PROFILE", "HUATAI_RUNTIME_ROOT")
    return all(os.environ.get(key) and Path(os.environ[key]).exists() for key in required), "本机 MCP 运行环境"


def call_mcp(server, tool=None, arguments=None, refresh=False):
    if server not in SERVERS or (tool and not tool.startswith(("query_", "search_", "market_query_", "macro_"))):
        raise ProviderRequestError("仅允许已配置华泰服务的只读查询工具")
    env = {**os.environ, "HUATAI_CACHE_ROOT": str(settings.data_dir / "huatai"), "PYTHONIOENCODING": "utf-8"}
    request = dict(server=server, tool=tool, arguments=arguments or {}, refresh=refresh, inventory=tool is None)
    try:
        process = subprocess.run([os.environ["HUATAI_PYTHON"], "-I", "-B",
            str(Path(__file__).with_name("worker.py"))], input=json.dumps(request),
            capture_output=True, text=True, encoding="utf-8", timeout=150, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
        result = json.loads(process.stdout)
    except (OSError, KeyError, subprocess.SubprocessError, ValueError) as exc:
        raise ProviderRequestError("华泰 MCP 调用失败，请检查本地连接配置") from exc
    if result.get("status") != "success" or result.get("result", {}).get("isError"):
        raise ProviderRequestError("华泰 MCP 未返回成功结果")
    return result


def normalize_kline(symbol, payload):
    columns = {"交易日期": "date", "开盘价": "open", "收盘价": "close", "最高价": "high",
               "最低价": "low", "成交量": "volume", "成交额": "amount"}
    if not set(columns).issubset(payload.get("fields", [])):
        raise ProviderRequestError("华泰日线字段不完整，拒绝写入")
    rows = []
    for values in payload.get("data", []):
        source = dict(zip(payload["fields"], values, strict=True))
        row = {"symbol": symbol, "date": datetime.strptime(source["交易日期"], "%Y-%m-%d").date()}
        for label, field in columns.items():
            if field == "date":
                continue
            unit = "股" if field == "volume" else "元"
            match = re.fullmatch(r"(-?\d+(?:\.\d+)?)" + unit, str(source[label]))
            if match is None:
                raise ProviderRequestError(f"华泰 {label} 单位不符合已验证契约")
            row[field] = float(match[1]) / (100 if field == "volume" else 1)
        rows.append(row)
    return normalize_daily(pl.DataFrame(rows), source="huatai") if rows else pl.DataFrame()


class HuataiProvider:
    name = "huatai"
    builtin = True

    def __init__(self):
        self.config = SimpleNamespace(name=self.name, display_name="华泰 MCP", path=None,
                                      builtin=True, datasets={"daily": None, "financial": None})

    def close(self):
        pass

    def get_financials(self, table, symbols, latest_only=True):
        """Latest disclosed period only, also when history is requested; shares unavailable."""
        from .financial import FIELDS, normalize_financial
        if table == "shares":
            return pl.DataFrame()
        if table not in (*FIELDS, "metrics"):
            raise ProviderRequestError("不支持的华泰财务表")
        if not latest_only:
            logging.getLogger(__name__).warning("华泰财务当前仅同步最新披露期，不提供历史多期补齐")
        rows = []
        for symbol in symbols:
            metrics = table == "metrics"
            tool = "query_financial_analysis_indicators" if metrics else "query_financial_statements"
            args = {"stock": symbol, "return_mode": "summary", "report_scope": "102"}
            if metrics:
                args["indicator_calc_type"] = "period"
            envelope = call_mcp("mcp-A-stock-financial", tool, args)
            payloads = [json.loads(item["text"]) for item in envelope["result"].get("content", [])
                        if item.get("type") == "text"]
            matches = [p for p in payloads if isinstance(p, dict)
                       and "报告期" in (p if metrics else p.get("data", {}))]
            if len(matches) != 1:
                raise ProviderRequestError("华泰财务响应结构不符合契约")
            rows.append(normalize_financial(symbol, table, matches[0]))
        return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()

    def get_daily(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None):
        if asset_type != "stock":
            return pl.DataFrame()
        frames = []
        for index, symbol in enumerate(symbols):
            envelope = call_mcp("mcp-A-stock", "query_kline", dict(stock=symbol, adjust_type="none",
                start_date=(start_time or datetime(2000, 1, 1)).strftime("%Y-%m-%d"),
                end_date=(end_time or datetime.now()).strftime("%Y-%m-%d")))
            payloads = [json.loads(item["text"]) for item in envelope["result"].get("content", [])
                        if item.get("type") == "text"]
            tables = [p for p in payloads if isinstance(p, dict) and "fields" in p and "data" in p]
            if len(tables) != 1:
                raise ProviderRequestError("华泰日线响应结构与已验证契约不符")
            frame = normalize_kline(symbol, tables[0])
            if not frame.is_empty():
                frames.append(frame)
            if on_chunk_done:
                on_chunk_done(index + 1, len(symbols))
        return pl.concat(frames) if frames else pl.DataFrame()

    def test_dataset(self, dataset, symbols=None):
        if dataset == "financial":
            frame = self.get_financials("metrics", symbols or ["600519.SH"])
            return {"provider": self.name, "dataset": dataset, "rows": frame.height,
                    "columns": frame.columns, "preview": frame.head(3).to_dicts()}
        if dataset != "daily":
            return {"provider": self.name, "dataset": dataset, "error": "仅支持日线"}
        end = datetime.now() - timedelta(days=1)
        frame = self.get_daily(symbols or ["600519.SH"], end - timedelta(days=7), end)
        return {"provider": self.name, "dataset": dataset, "rows": frame.height,
                "columns": frame.columns, "preview": frame.head(3).to_dicts()}
