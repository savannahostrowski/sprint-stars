import asyncio
import json
from functools import partial
from pathlib import Path
from datetime import date
from hashlib import sha256
from time import monotonic

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import ROOT, Store, aggregate as aggregate_activity, app, bounds, parse_roster, include_contributors, store, GitHubIncident
from config import load_metadata

# Keep fixtures independent of the deployment configuration.
TEST_METADATA = load_metadata(Path(__file__).parent / "fixtures" / "metadata.toml")
REFRESH_SECONDS = TEST_METADATA.refresh_seconds
aggregate = partial(aggregate_activity, metadata=TEST_METADATA)

START, END, TZ = date(2026, 10, 19), date(2026, 10, 25), "America/Los_Angeles"
KEY = START, END, TZ
ROSTER = "Alice,alice,2020-01-01,,\nBob,bob,2020-01-01,,\nFormer,former,2020-01-01,2024-01-01,\n"


def issue(number=1, actor="alice", closed_at="2026-10-19T07:00:00Z", pr=False):
    item = {"number": number, "title": "Fix <a thing>", "html_url": f"https://github.com/acme/widgets/issues/{number}",
            "closed_at": closed_at, "closed_by": {"login": actor}}
    if pr:
        item["pull_request"] = {"merged_at": closed_at}
    return item


def opened(number=1, author="bob", created_at="2026-10-19T07:00:00Z"):
    return {"number": number, "user": {"login": author}, "created_at": created_at,
            "title": "A new PR", "html_url": f"https://github.com/acme/widgets/pull/{number}"}


def test_openings_and_closures_are_distinct_actions_with_distinct_actors():
    closed = issue(pr=True)
    closed["user"] = {"login": "bob"}
    prs = [opened(), opened(), opened(2, "ALICE"), opened(3, "bot[bot]"),
           opened(4, created_at="2026-10-19T06:59:59Z"),
           opened(5, created_at="2026-10-26T07:00:00Z")]
    result = aggregate([closed, issue(7)], parse_roster(ROSTER), *KEY, prs)
    rows = {m["login"]: m for m in result["members"]}
    assert (rows["alice"]["closures"], rows["alice"]["prs_opened"], rows["alice"]["total"]) == (2, 1, 3)
    assert (rows["bob"]["closures"], rows["bob"]["prs_opened"], rows["bob"]["total"]) == (0, 1, 1)
    assert {i["activity"] for i in rows["alice"]["items"]} == {"closure", "pr_opened"}
    assert result["excluded"] == 1


def test_opened_pr_reports_current_merged_status_instead_of_open():
    result = aggregate([issue(pr=True)], parse_roster(ROSTER), *KEY, [opened(author="alice")])
    events = result["members"][0]["items"]
    assert len(events) == 2  # two scored actions, grouped into one item by the UI
    assert {event["activity"] for event in events} == {"closure", "pr_opened"}
    assert {event["status"] for event in events} == {"merged"}


def test_opening_status_uses_merge_outside_the_selected_dates():
    merged_later = issue(pr=True, closed_at="2026-10-27T07:00:00Z")
    result = aggregate([merged_later], parse_roster(ROSTER), *KEY, [opened(author="alice")])
    member = result["members"][0]
    assert (member["closures"], member["prs_opened"], member["total"]) == (0, 1, 1)
    assert member["items"][0]["status"] == "merged"


def test_current_status_distinguishes_open_and_unmerged_closed_prs():
    unmerged = issue(pr=True)
    unmerged["pull_request"]["merged_at"] = None
    result = aggregate([unmerged], parse_roster(ROSTER), *KEY,
                       [opened(author="alice"), opened(2, author="alice")])
    statuses = {i["number"]: i["status"] for i in result["members"][0]["items"]}
    assert statuses == {1: "closed", 2: "open"}


def test_actor_not_author_dates_dedup_and_bots():
    rows = [issue(), issue(), issue(2, pr=True), issue(3, "bob"),
            issue(4, "bot[bot]"), issue(5, "former"),
            issue(6, closed_at="2026-10-19T06:59:59Z"),
            issue(7, closed_at="2026-10-26T07:00:00Z"),
            issue(8, closed_at=None), issue(9, "ALICE")]
    rows[0]["user"] = {"login": "bob"}
    result = aggregate(rows, parse_roster(ROSTER), *KEY)
    alice, bob = result["members"]
    assert (alice["login"], alice["issues"], alice["prs"], alice["merged"], alice["total"]) == ("alice", 2, 1, 1, 3)
    assert bob["total"] == 1
    assert result["excluded"] == 2


def test_inclusive_end_and_dst_boundaries():
    lower, upper = bounds(date(2026, 11, 1), date(2026, 11, 1), TZ)
    assert (upper - lower).total_seconds() == 25 * 3600
    result = aggregate([issue(closed_at="2026-10-26T06:59:59Z")], parse_roster(ROSTER), *KEY)
    assert result["members"][0]["total"] == 1


def test_triagers_are_ranked_and_overlapping_core_members_count_once():
    core = parse_roster(ROSTER)
    team = include_contributors(core, [{"login": "ALICE", "name": "Alice", "roles": ["triager"]},
                                   {"login": "dana", "name": "dana", "roles": ["triager"]}])
    assert len(team) == len(core) + 1
    assert team["alice"]["roles"] == ["core", "triager"]
    assert core["alice"]["roles"] == ["core"]  # doesn't modify the original roster
    result = aggregate([issue(actor="dana")], team, *KEY, [opened(author="dana")])
    member = result["members"][0]
    assert member["roles"] == ["triager"]
    assert (member["closures"], member["prs_opened"], member["total"]) == (1, 1, 2)


@pytest.mark.asyncio
async def test_org_pagination_server_auth_and_shared_ranges(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    seen = []
    roster = "\n".join(f"Person {i},user{i},2020-01-01,," for i in range(60)) + "\nCarol Example,Carol,2020-01-01,,"
    def handle(request):
        if request.url.host == "example.test":
            assert "authorization" not in request.headers
            return httpx.Response(200, text=roster)
        if request.url.host == "www.githubstatus.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"components": []})
        assert request.headers["authorization"] == "Bearer test-token"
        if request.url.path == "/graphql":
            seen.append(("/graphql", "1"))
            return httpx.Response(200, json={"data": {f"p{i}": {"pullRequest": {"reviews": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}} for i in range(2)}})
        page = request.url.params["page"]
        seen.append((request.url.path, page))
        if request.url.path == "/orgs/acme/repos":
            assert request.url.params["type"] == "public"
            repos = [{"full_name": "acme/" + ("widgets" if page == "1" else "docs"),
                      "owner": {"login": "acme"}, "private": False, "archived": True,
                      "has_issues": False}]
            return httpx.Response(200, json=repos, headers={"link":
                '<https://api.github.com/orgs/acme/repos?page=2>; rel="last"'} if page == "1" else {})
        assert request.url.params["state"] == "all"
        assert request.url.params["since"] == "2026-10-19T07:00:00Z"
        repo = request.url.path.split("/")[3]
        closed = issue(1, "user1")
        pr = opened(2, "user1") | {"pull_request": {}, "state": "open"}
        for item in (closed, pr):
            item["html_url"] = item["html_url"].replace("/widgets/", f"/{repo}/")
        return httpx.Response(200, json=[closed, pr])
    s = Store(TEST_METADATA)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        result = await s.get(KEY)
        assert result["members"][0]["total"] == 4
        assert result["members"][0]["closures"] == 2
        assert result["members"][0]["prs_opened"] == 2
        assert result["repository_count"] == 2
        assert result["organizations"] == ["acme"]
        assert {i["repository"] for i in result["members"][0]["items"]} == {"acme/widgets", "acme/docs"}
        initial_calls = list(seen)
        await s.get(KEY)
        other = await s.get((date(2026, 10, 20), END, TZ))
        assert other["members"] == []
        assert other["updated_at"] == result["updated_at"]
        assert seen == initial_calls  # same snapshot for a new, covered range


@pytest.mark.asyncio
async def test_failed_repository_never_publishes_partial_org_totals():
    s = Store(TEST_METADATA)
    original = aggregate([issue()], parse_roster(ROSTER), *KEY, [opened()])
    s.cache[KEY] = (monotonic() - REFRESH_SECONDS - 1, original)
    roster = "\n".join(f"Person {i},user{i},2020-01-01,," for i in range(60)) + "\nCarol Example,Carol,2020-01-01,,"
    def handle(request):
        if request.url.host == "example.test":
            return httpx.Response(200, text=roster)
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[{"full_name": "acme/" + name,
                "owner": {"login": "acme"}, "private": False} for name in ("widgets", "docs")])
        if request.url.path == "/repos/acme/widgets/issues":
            return httpx.Response(200, json=[issue(2, "user1")])
        return httpx.Response(403)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        result = await s.get(KEY, force=True)
    assert result["stale"]
    assert result["members"] == original["members"]
    assert result["updated_at"] == original["updated_at"]


@pytest.mark.asyncio
async def test_failed_refresh_retains_data_and_timestamp():
    s = Store(TEST_METADATA)
    original = aggregate([issue()], parse_roster(ROSTER), *KEY)
    s.cache[KEY] = (monotonic() - REFRESH_SECONDS - 1, original)
    async def fail(*args):
        raise httpx.ConnectError("offline")
    s.collect = fail
    result = await s.get(KEY, force=True)
    assert result["stale"] and result["warning"]
    assert result["updated_at"] == original["updated_at"]
    assert result["members"] == original["members"]
    with pytest.raises(HTTPException):
        await s.get((date(2026, 10, 12), date(2026, 10, 18), TZ))


@pytest.mark.asyncio
async def test_stale_cache_is_immediate_and_refresh_is_shared():
    s = Store(TEST_METADATA)
    previous = aggregate([issue()], parse_roster(ROSTER), *KEY)
    s.cache[KEY] = (monotonic() - REFRESH_SECONDS - 1, previous)
    release = asyncio.Event()
    calls = []
    async def collect(*args):
        calls.append(True)
        await release.wait()
        return aggregate([issue(), issue(2)], parse_roster(ROSTER), *KEY)
    s.collect = collect
    first = await s.get(KEY)
    second = await s.get(KEY)
    assert first["stale"] and first["refreshing"]
    assert first["members"] == second["members"] == previous["members"]
    assert len(s.pending) == 1
    await asyncio.sleep(0)
    release.set()
    fresh = await s.get(KEY, force=True)
    assert len(calls) == 1
    assert not fresh["stale"] and not fresh["refreshing"]
    assert fresh["warning"] is None
    assert fresh["members"][0]["total"] == 2


@pytest.mark.asyncio
async def test_closed_pages_are_concurrent_with_four_request_limit():
    s = Store(TEST_METADATA)
    active, peak = 0, 0
    release = asyncio.Event()
    async def handle(request):
        nonlocal active, peak
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json=[issue(1)], headers={
                "link": '<https://api.github.com/repos/acme/widgets/issues?page=5>; rel="last"'})
        active += 1
        peak = max(peak, active)
        if active == 4:
            release.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json=[issue(page)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        items = await asyncio.wait_for(s.pages("/repos/acme/widgets/issues", {}, {}), timeout=2)
    assert peak == 4
    assert {i["number"] for i in items} == {1, 2, 3, 4, 5}


@pytest.mark.asyncio
async def test_cursor_next_links_without_last_page_are_fetched_completely():
    s = Store(TEST_METADATA)
    def handle(request):
        page = int(request.url.params["page"])
        if page > 3:
            return httpx.Response(200, json=[])
        headers = {"link": f'<https://api.github.com/next?page={page + 1}&after=cursor>; rel=next'} if page < 3 else {}
        return httpx.Response(200, json=[issue(page)], headers=headers)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        items = await s.pages("/repos/acme/widgets/issues", {}, {})
    assert [i["number"] for i in items] == [1, 2, 3]


def test_same_number_across_repositories_keeps_counts_and_status_separate():
    widgets = issue(pr=True)
    docs = issue(pr=True)
    docs["html_url"] = docs["html_url"].replace("/widgets/", "/docs/")
    docs["pull_request"]["merged_at"] = None
    prs = [opened(author="alice"), opened(author="alice")]
    prs[1]["html_url"] = prs[1]["html_url"].replace("/widgets/", "/docs/")
    prs.append(opened(author="alice") | {"html_url": "https://github.com/acme/ideas/pull/1", "state": "open"})
    result = aggregate([widgets, docs, docs], parse_roster(ROSTER), *KEY, prs + [prs[1]])
    member = result["members"][0]
    assert (member["closures"], member["prs_opened"], member["total"]) == (2, 3, 5)
    assert {(i["repository"], i["status"]) for i in member["items"]} == {
        ("acme/widgets", "merged"), ("acme/docs", "closed"), ("acme/ideas", "open")}


@pytest.mark.asyncio
async def test_concurrent_ranges_share_one_org_fetch_and_failed_snapshot_is_not_saved():
    s = Store(TEST_METADATA)
    calls = []
    def handle(request):
        if request.url.host == "www.githubstatus.com":
            return httpx.Response(200, json={"components": []})
        calls.append(request.url.path)
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[{"full_name": "acme/widgets", "owner": {"login": "acme"}, "private": False}])
        return httpx.Response(200, json=[issue()])
    lower, _ = bounds(*KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        first, second = await asyncio.gather(s.activity(lower, {}), s.activity(lower, {}))
    assert first is second
    assert calls == ["/orgs/acme/repos", "/repos/acme/widgets/issues"]


@pytest.mark.asyncio
async def test_background_refreshes_without_browser(monkeypatch):
    s = Store(TEST_METADATA)
    calls = []
    async def refresh():
        calls.append(True)
    async def sleep(seconds):
        assert 1199 < seconds <= 1200
        raise asyncio.CancelledError
    s.refresh_all = refresh
    monkeypatch.setattr("main.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await s.background()
    assert calls == [True]


def test_api_validation_and_frontend():
    # No lifespan/network needed for request validation or static rendering.
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    for params in [dict(start="2026-10-25", end="2026-10-19"),
                   dict(start="2026-01-01", end="2026-12-31"),
                   dict(tz="No/Such_Zone"), dict(start="broken")]:
        assert client.get("/api/leaderboard", params=params).status_code == 422


def test_asset_urls_follow_contents_and_assets_and_errors_are_not_cached():
    client = TestClient(app)
    page = client.get("/")
    assert page.headers["cache-control"] == "no-store"
    for filename, mime in (("style.css", "text/css"), ("app.js", "text/javascript")):
        expected_hash = sha256((ROOT / "static" / filename).read_bytes()).hexdigest()[:12]
        url = f"/static/{filename}?v={expected_hash}"
        assert url in page.text
        asset = client.get(url)
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith(mime)
        assert asset.headers["cache-control"] == "no-store"
    missing = client.get("/static/missing.css")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"


def test_alternate_account_combines_openings_and_closures_without_duplicate_people():
    roster = parse_roster("Carol Example,Carol,2020-01-01,,\n")
    result = aggregate([issue(actor="CAROL-WORK", pr=True), issue(2, actor="Carol"),
                        issue(actor="CAROL-WORK", pr=True)], roster, *KEY,
                       [opened(author="carol-work"), opened(3, author="Carol"), opened(author="carol-work")])
    assert len(result["members"]) == 1
    member = result["members"][0]
    assert member["login"] == "Carol"
    assert member["aliases"] == ["carol-work"]
    assert (member["closures"], member["prs_opened"], member["total"]) == (2, 2, 4)
    assert {i["actor_login"].casefold() for i in member["items"]} == {"carol-work", "carol"}
    assert result["excluded"] == 0


def test_alternate_account_does_not_add_someone_outside_the_roster():
    result = aggregate([issue(actor="carol-work")], parse_roster(ROSTER), *KEY)
    assert result["members"] == []
    assert result["excluded"] == 1


def review(actor="alice", submitted="2026-10-20T07:00:00Z", state="APPROVED", number=1):
    return {"id": str(number), "user": {"login": actor}, "submitted_at": submitted,
            "state": state, "html_url": f"https://github.com/acme/widgets/pull/1#pullrequestreview-{number}"}


def test_reviews_use_submission_dates_unique_person_pr_and_separate_closure_points():
    pr = issue(pr=True) | {"user": {"login": "bob"}, "reviews": [
        review(submitted="2026-10-19T06:59:59Z"),
        review(number=2), review(number=3, state="COMMENTED"), review(actor="bob"),
        review(actor="bot[bot]"), review(submitted=None, state="PENDING"),
        review(submitted="2026-10-26T07:00:00Z", number=4)]}
    second_repo = {**pr, "html_url": "https://github.com/acme/docs/pull/1", "reviews": [review(state="DISMISSED")]}
    data = aggregate([pr], parse_roster(ROSTER), *KEY, reviewed_prs=[pr, pr, second_repo])
    alice = data["members"][0]
    assert (alice["reviews"], alice["closures"], alice["total"]) == (2, 1, 3)
    assert len(data["members"]) == 1  # own-PR review doesn't add the author
    assert data["excluded"] == 1
    events = [i for i in alice["items"] if i["activity"] == "review"]
    assert {i["repository"] for i in events} == {"acme/widgets", "acme/docs"}
    assert {i["status"] for i in events} == {"merged"}
    assert events[0]["occurred_at"] == "2026-10-20T07:00:00Z"


def test_review_aliases_share_one_point_and_self_reviews_compare_canonical_accounts():
    roster = parse_roster("Carol Example,Carol,2020-01-01,,\n")
    others_pr = opened(author="alice") | {"state": "open", "reviews": [
        review("carol-work", number=2), review("CAROL", submitted="2026-10-19T07:00:00Z")]}
    own_pr = opened(2, author="carol-work") | {"reviews": [review("Carol")]}
    data = aggregate([], roster, *KEY, reviewed_prs=[others_pr, own_pr])
    carol = data["members"][0]
    assert len(data["members"]) == 1
    assert (carol["reviews"], carol["total"]) == (1, 1)
    assert carol["items"][0]["actor_login"] == "CAROL"
    assert carol["items"][0]["occurred_at"] == "2026-10-19T07:00:00Z"


@pytest.mark.asyncio
async def test_graphql_review_batches_paginate_and_preserve_submitted_reviews():
    s = Store(TEST_METADATA)
    queries = []
    def handle(request):
        query = json.loads(request.content)["query"]
        queries.append(query)
        page = 2 if 'after:"cursor-1"' in query else 1
        return httpx.Response(200, json={"data": {"p0": {"pullRequest": {"reviews": {
            "nodes": [{"id": str(page), "state": "COMMENTED", "submittedAt": "2026-10-20T07:00:00Z",
                       "url": "https://github.com/acme/widgets/pull/1#review", "author": {"login": "alice"}}],
            "pageInfo": {"hasNextPage": page == 1, "endCursor": "cursor-1" if page == 1 else "cursor-2"}}}}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        result = await s.review_batch([opened()], {"Authorization": "Bearer test-token"})
    assert len(queries) == 2
    assert [r["id"] for r in result[0]] == ["1", "2"]
    assert result[0][0]["user"]["login"] == "alice"


@pytest.mark.asyncio
async def test_graphql_permission_fallback_uses_complete_rest_review_pages():
    s = Store(TEST_METADATA)
    seen = []
    def handle(request):
        seen.append(request.url.path)
        if request.url.path == "/graphql":
            return httpx.Response(403, json={"message": "Forbidden"})
        page = request.url.params["page"]
        return httpx.Response(200, json=[review(number=int(page))], headers={"link":
            '<https://api.github.com/repos/acme/widgets/pulls/1/reviews?page=2>; rel="last"'} if page == "1" else {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        result = await s.review_batch([opened()], {"Authorization": "Bearer test-token"})
    assert len(result[0]) == 2
    assert seen == ["/graphql", "/repos/acme/widgets/pulls/1/reviews", "/repos/acme/widgets/pulls/1/reviews"]
    assert s.graphql_enabled is False


@pytest.mark.asyncio
async def test_review_failure_keeps_previous_complete_snapshot():
    s = Store(TEST_METADATA)
    data = aggregate([issue()], parse_roster(ROSTER), *KEY)
    s.cache[KEY] = (monotonic() - REFRESH_SECONDS - 1, data)
    roster = "\n".join(f"Person {i},user{i},2020-01-01,," for i in range(60)) + "\nCarol Example,Carol,2020-01-01,,"
    def handle(request):
        if request.url.host == "example.test":
            return httpx.Response(200, text=roster)
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[{"full_name": "acme/widgets", "owner": {"login": "acme"}, "private": False}])
        if request.url.path == "/repos/acme/widgets/issues":
            return httpx.Response(200, json=[opened(author="user1") | {"pull_request": {}}])
        return httpx.Response(500, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        result = await s.get(KEY, force=True)
    assert result["stale"]
    assert result["members"] == data["members"]
    assert result["updated_at"] == data["updated_at"]
    assert s.source is None  # no partial source can leak into another date range


@pytest.mark.asyncio
async def test_graphql_partial_errors_are_rejected_instead_of_zero_reviews():
    s = Store(TEST_METADATA)
    def handle(request):
        return httpx.Response(200, json={"errors": [{"type": "INTERNAL", "message": "incomplete"}],
                                         "data": {"p0": None}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        with pytest.raises(ValueError, match="incomplete review data"):
            await s.review_batch([opened()], {"Authorization": "Bearer test-token"})


@pytest.mark.asyncio
async def test_active_github_incident_keeps_verified_data_and_recovers(monkeypatch):
    s = Store(TEST_METADATA)
    previous = aggregate([issue()], parse_roster(ROSTER), *KEY)
    s.cache[KEY] = (monotonic() - REFRESH_SECONDS - 1, previous)
    incident, calls = True, []
    def handle(request):
        assert request.url.host == "www.githubstatus.com"
        assert "authorization" not in request.headers
        calls.append(True)
        return httpx.Response(200, json={"components": [{"name": "Issues",
            "status": "degraded_performance" if incident else "operational"}]})
    async def collect(*args):
        await s.check_github_status()
        return aggregate([issue(), issue(2)], parse_roster(ROSTER), *KEY)
    s.collect = collect
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        first = await s.get(KEY, force=True)
        assert first["members"] == previous["members"]
        assert first["updated_at"] == previous["updated_at"]
        assert first["stale"] and "incident" in first["warning"]
        await s.get(KEY, force=True)
        assert len(calls) == 1  # cache status for a minute, including during an incident
        incident = False
        s.status = (monotonic() - 61, s.status[1])
        recovered = await s.get(KEY, force=True)
        assert recovered["members"][0]["total"] == 2
        assert not recovered["stale"] and recovered["warning"] is None


@pytest.mark.asyncio
async def test_incident_blocks_empty_successful_pages_before_they_replace_source():
    s = Store(TEST_METADATA)
    def handle(request):
        assert request.url.host == "www.githubstatus.com"  # no activity requests during incident
        return httpx.Response(200, json={"components": [{"name": "Pull Requests", "status": "partial_outage"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        with pytest.raises(GitHubIncident):
            await s.activity(bounds(*KEY)[0], {"Authorization": "Bearer test-token"})
    assert s.source is None


@pytest.mark.asyncio
async def test_actions_only_incident_does_not_freeze_issue_pr_counts():
    s = Store(TEST_METADATA)
    def handle(request):
        return httpx.Response(200, json={"components": [{"name": "Actions", "status": "major_outage"},
            {"name": "Issues", "status": "operational"}, {"name": "Pull Requests", "status": "operational"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        await s.check_github_status()
    assert s.status[1] == []
