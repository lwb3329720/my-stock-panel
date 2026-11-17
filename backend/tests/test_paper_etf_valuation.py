from datetime import timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import cn_today
from app.strategy import paper

pytest_plugins = ["test_paper_api", "test_quote_demand"]


def test_held_etf_demand(market):
    service, quotes, _ = market
    root = service._repo.store.data_dir
    paper.create_account(root, 100000)
    paper.create_order(root, "510300.SH", "buy", qty=100, asset_type="etf")
    paper.evaluate_intraday(root, {"510300.SH": 4.0})
    assert paper.load_positions(root)["510300.SH"]["qty"] == 100
    service._fetch_full_market_quotes()
    assert "510300.SH" in quotes.get.call_args.kwargs["symbols"]


@pytest.mark.parametrize("source", ["live", "cache", "daily"])
def test_etf_valuation(client, source):
    root = client.app.state.repo.store.data_dir
    paper.create_account(root, 100000)
    paper.create_order(root, "159915.SZ", "buy", qty=100, asset_type="etf")
    paper.evaluate_intraday(root, {"159915.SZ": 2.4})
    if source == "daily":
        folder = root / "kline_etf_daily" / "history"
        folder.mkdir(parents=True)
        pl.DataFrame({"symbol": ["159915.SZ", "159915.SZ"],
                      "date": [cn_today() - timedelta(days=1), cn_today() + timedelta(days=1)],
                      "close": [2.5, 99.0]}).write_parquet(folder / "part.parquet")
    else:
        client.app.state.quote_service = SimpleNamespace(
            get_enriched_today=lambda: (pl.DataFrame(), cn_today()),
            paper_etf_snapshot=lambda: {"159915.SZ": 2.5} if source == "live" else {},
        )
        client.app.state.repo.get_enriched_latest_asset = lambda *a, **kw: (
            pl.DataFrame({"symbol": ["159915.SZ"], "raw_close": [2.3 if source == "live" else 2.5], "close": [9.9]}), cn_today()
        )
    ov = client.get("/api/paper/overview").json()
    holding = ov["holdings"][0]
    assert holding["last_price"] == 2.5
    assert holding["pnl"] == round((2.5 - holding["avg_cost"]) * 100, 2)
    compared = client.get("/api/paper/compare").json()["accounts"][0]
    assert (compared["live_total"] if source != "daily" else compared["total"]) == ov["total"]


def test_old_etf_cache_does_not_override_latest_daily(client):
    from app.api.paper import _valuation_snapshot
    client.app.state.repo.get_enriched_latest_asset = lambda *a, **kw: (
        pl.DataFrame({"symbol": ["159915.SZ"], "raw_close": [99.0]}),
        cn_today() - timedelta(days=1),
    )
    assert _valuation_snapshot(SimpleNamespace(app=client.app)) == {}


def test_empty_etf_prices_keep_account_readable(client):
    root = client.app.state.repo.store.data_dir
    paper.create_account(root, 100000)
    paper.create_order(root, "159915.SZ", "buy", qty=100, asset_type="etf")
    paper.evaluate_intraday(root, {"159915.SZ": 2.4})
    response = client.get("/api/paper/overview")
    assert response.status_code == 200
    assert response.json()["holdings"][0]["pnl"] == 0
