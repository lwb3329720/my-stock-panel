from threading import Barrier, Lock

import pytest

from app.services import financial_sync as sync, preferences


@pytest.mark.parametrize("failed", [None, "income"])
def test_tushare_tables_overlap_and_failure_does_not_cancel_others(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tushare")
    monkeypatch.setattr(sync, "_get_symbols", lambda root: ["A"])
    monkeypatch.setattr(sync, "_refresh_financials_views", lambda root: None)
    barrier = Barrier(5, timeout=3)
    completed = []
    lock = Lock()
    def fetch(table, *args):
        barrier.wait()
        if table == failed:
            raise ValueError("source unavailable")
        with lock:
            completed.append(table)
        return 12
    monkeypatch.setattr(sync, "_sync_history_table_for_symbols", fetch)
    scheduler = sync.FinancialScheduler()
    scheduler._data_dir = tmp_path
    recorded = []
    monkeypatch.setattr(scheduler, "_record_sync", recorded.append)
    if failed:
        with pytest.raises(RuntimeError, match="income"):
            scheduler._run_body(None)
    else:
        assert scheduler._run_body(None) == dict.fromkeys(sync.FINANCIAL_TABLES, 12)
    expected = set(sync.FINANCIAL_TABLES) - ({failed} if failed else set())
    assert set(completed) == expected
    assert set(recorded) == expected


def test_managed_tushare_tables_keep_errors_isolated(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import json
    from app.plugins.tushare.provider import TushareProvider
    from app.data_providers.custom.managed import ManagedProvider
    from app.data_providers import custom
    from app.data_providers.base import ProviderRequestError
    provider = TushareProvider("test")
    barrier = Barrier(5, timeout=5)
    def query(api, **kwargs):
        barrier.wait()
        raise ProviderRequestError(api)
    monkeypatch.setattr(provider, "query", query)
    managed = ManagedProvider(provider)
    monkeypatch.setattr(custom, "get_provider", lambda name: managed)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tushare")
    monkeypatch.setattr(sync, "_financial_is_custom", lambda: True)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(sync._sync_history_table_for_symbols, t, [t], tmp_path, None)
                   for t in sync.FINANCIAL_TABLES]
        assert [f.result() for f in futures] == [0] * 5
    for table in sync.FINANCIAL_TABLES:
        report = json.loads((tmp_path / "financials" / f"{table}-sync-report.json").read_text())
        assert [e["symbol"] for e in report["errors"]] == [table]
