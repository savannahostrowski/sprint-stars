import asyncio
from dataclasses import replace
from datetime import date

import httpx
import pytest
from fastapi import HTTPException

import main
from test_leaderboard import KEY, TEST_METADATA


def key(day):
    return date(2026, 10, day), date(2026, 10, day), "UTC"


def empty_result(range_key):
    return main.aggregate([], {}, *range_key, metadata=TEST_METADATA)


@pytest.mark.asyncio
async def test_new_range_limit_exempts_cache_hits_and_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(main, "monotonic", lambda: clock[0])
    store = main.Store(TEST_METADATA)
    calls = []

    async def collect(*range_key):
        calls.append(range_key)
        return empty_result(range_key)

    store.collect = collect
    for day in range(1, main.MAX_NEW_RANGES_PER_MINUTE + 1):
        await store.get(key(day))
    with pytest.raises(HTTPException) as error:
        await store.get(key(11))
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "60"
    for _ in range(20):
        assert await store.get(key(1))
    assert len(calls) == main.MAX_NEW_RANGES_PER_MINUTE
    clock[0] += 60
    await store.get(key(11))
    assert len(calls) == main.MAX_NEW_RANGES_PER_MINUTE + 1


@pytest.mark.asyncio
async def test_pending_cap_shares_existing_requests_and_keeps_stale_views(monkeypatch):
    clock = [2000.0]
    monkeypatch.setattr(main, "monotonic", lambda: clock[0])
    store = main.Store(TEST_METADATA)
    previous = empty_result(KEY)
    store.cache[KEY] = (0, previous)
    release = asyncio.Event()
    calls = []

    async def collect(*range_key):
        calls.append(range_key)
        await release.wait()
        return empty_result(range_key)

    store.collect = collect
    tasks = [asyncio.create_task(store.get(key(day))) for day in range(1, main.MAX_PENDING_REFRESHES + 1)]
    try:
        await asyncio.sleep(0)
        assert len(store.pending) == main.MAX_PENDING_REFRESHES
        shared = asyncio.create_task(store.get(key(1)))
        tasks.append(shared)
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as error:
            await store.get(key(5))
        assert error.value.status_code == 429
        assert error.value.headers["Retry-After"] == "5"
        stale = await store.get(KEY)
        assert stale["stale"] and stale["warning"]
        assert stale["updated_at"] == previous["updated_at"]
        assert len(store.pending) == main.MAX_PENDING_REFRESHES
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert len(calls) == main.MAX_PENDING_REFRESHES
    assert not store.pending


@pytest.mark.asyncio
async def test_snapshot_budget_returns_429_and_reuses_covered_data(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(main, "monotonic", lambda: clock[0])
    config = replace(TEST_METADATA, roster={}, aliases={},
        contributors=({"login": "alice", "name": "Alice", "roles": ["contributor"]},))
    store = main.Store(config)
    monkeypatch.setattr(main, "store", store)
    calls = []

    def handle(request):
        if request.url.host == "www.githubstatus.com":
            return httpx.Response(200, json={"components": []})
        calls.append(request.url.path)
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[{"full_name": "acme/widgets", "private": False,
                                             "owner": {"login": "acme"}}])
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as store.client:
        for day in (26, 19, 12):
            await store.get(key(day))
        assert len(calls) == 6
        snapshot = store.source
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as api:
            response = await api.get("/api/leaderboard", params={"start": "2026-10-05", "end": "2026-10-05", "tz": "UTC"})
            assert response.status_code == 429
            assert response.headers["retry-after"] == "1200"
            assert len(calls) == 6
            assert store.source is snapshot
            covered = await store.get(key(20))
            assert not covered["stale"]
            assert len(calls) == 6
            previous = empty_result(key(5))
            store.cache[key(5)] = (0, previous)
            stale = await store.get(key(5), force=True)
            assert stale["stale"] and stale["warning"] and not stale["refreshing"]
            assert stale["updated_at"] == previous["updated_at"]
            assert len(calls) == 6
            clock[0] += config.refresh_seconds
            response = await api.get("/api/leaderboard", params={"start": "2026-10-01", "end": "2026-10-01", "tz": "UTC"})
            assert response.status_code == 200
            assert len(calls) == 8


@pytest.mark.asyncio
async def test_expired_uncached_failures_are_pruned(monkeypatch):
    monkeypatch.setattr(main, "monotonic", lambda: 2000.0)
    store = main.Store(TEST_METADATA)
    store.errors[key(1)] = store.errors[KEY] = "previous failure"
    store.retry_after[key(1)] = store.retry_after[KEY] = 1900
    store.cache[KEY] = (1999, empty_result(KEY))
    await store.get(KEY)
    assert key(1) not in store.errors and key(1) not in store.retry_after
    assert KEY in store.errors and KEY in store.retry_after
