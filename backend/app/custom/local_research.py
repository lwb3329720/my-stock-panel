"""On-demand research queries; no unverified financial mapping into backtests."""
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.extensions import BACKEND_EXTENSION_API_VERSION
from app.plugins.huatai.provider import call_mcp
from app.plugins.tushare.provider import TushareProvider

EXTENSION_ID = "local.research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


class ResearchQuery(BaseModel):
    kind: str
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    day: date
    refresh: bool = False


def query_research(body: ResearchQuery):
    tools = {
        "valuation": ("mcp-A-stock", "query_valuation", {"end_date": body.day.isoformat()}),
        "financial": ("mcp-A-stock-financial", "query_financial_statements", {}),
        "business": ("mcp-A-stock-business", "query_company_businessdata", {}),
    }
    if body.kind in tools:
        server, tool, args = tools[body.kind]
        return call_mcp(server, tool, {"stock": body.symbol, **args}, body.refresh)
    if body.kind == "dragon_tiger":
        rows = TushareProvider().query("top_list", ts_code=body.symbol, trade_date=body.day.strftime("%Y%m%d"))
        return {"source": {"name": "Tushare top_list"}, "received_at": datetime.now(UTC).isoformat(),
                "cache_status": "LIVE", "rows": rows}
    raise HTTPException(400, "不支持的研究类型")


def setup(registrar):
    router = APIRouter(prefix="/api/custom/local-research", tags=["local-research"])
    router.post("/query")(query_research)
    registrar.include_router(router)
