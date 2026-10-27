import asyncio
import csv
import io
import json
import logging
import os
from collections import deque
from math import ceil
from hashlib import sha256
from html import escape
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import load_metadata

ROOT = Path(__file__).parent
SCHEMA_VERSION = 7
IDENTITY_VERSION = 1
MAX_PENDING_REFRESHES = 4
MAX_NEW_RANGES_PER_MINUTE = 10
MAX_SNAPSHOTS_PER_REFRESH = 3
METADATA = load_metadata(ROOT / "metadata.toml")
REFRESH_SECONDS = METADATA.refresh_seconds
DEFAULT_TIMEZONE = os.getenv("SPRINT_TIMEZONE", METADATA.timezone)
GITHUB_STATUS_URL = "https://www.githubstatus.com/api/v2/summary.json"
log = logging.getLogger("sprint-stars")


class GitHubIncident(ValueError):
    pass


def current_week(tz=DEFAULT_TIMEZONE):
    today = datetime.now(ZoneInfo(tz)).date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6), tz


def bounds(start, end, tz):
    if end < start or (end - start).days > 92:
        raise ValueError("Choose a date range of 1–93 days, with the end on or after the start.")
    zone = ZoneInfo(tz)
    return (datetime.combine(start, datetime.min.time(), zone).astimezone(UTC),
            datetime.combine(end + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC))


def parse_roster(text):
    return {r[1].casefold(): {"login": r[1], "name": r[0], "roles": ["core"]}
            for r in csv.reader(io.StringIO(text))
            if len(r) >= 4 and r[1] and not r[3]}


def include_contributors(roster, contributors):
    roster = {login: {**member, "roles": list(member["roles"])} for login, member in roster.items()}
    for member in contributors:
        key = member["login"].casefold()
        if key in roster:
            roster[key]["roles"] = list(dict.fromkeys([*roster[key]["roles"], *member["roles"]]))
        else:
            roster[key] = {**member, "roles": list(member["roles"])}
    return roster


def repository(item):
    # REST issue/search payloads and our compact events share this identity.
    return item.get("repository") or "/".join(item["html_url"].split("/")[3:5])


def item_key(item):
    return repository(item).casefold(), item["number"]


def aggregate(issues, roster, start, end, tz, opened_prs=(), reviewed_prs=(), *, metadata=METADATA):
    lower, upper = bounds(start, end, tz)
    members, excluded, seen = {}, 0, set()
    closed_items = {item_key(item): item for item in issues if item.get("closed_at")}

    def current_status(item):
        known = closed_items.get(item_key(item), item)
        if (known.get("pull_request") or {}).get("merged_at") or known.get("merged_at"):
            return "merged"
        return known.get("state", "closed" if item_key(item) in closed_items else "open")

    def member_for(login):
        return members.setdefault(login, {**roster[login], "issues": 0, "prs": 0,
            "merged": 0, "closures": 0, "prs_opened": 0, "reviews": 0, "total": 0, "items": [],
            "aliases": [alias for alias, canonical in metadata.aliases.items() if canonical == login]})

    for item in issues:
        if item_key(item) in seen or not item.get("closed_at"):
            continue
        seen.add(item_key(item))
        closed = datetime.fromisoformat(item["closed_at"].replace("Z", "+00:00"))
        if not lower <= closed < upper:
            continue
        actor = item.get("closed_by") or {}
        actor_login = actor.get("login", "")
        login = actor_login.casefold()
        login = metadata.aliases.get(login, login)
        if login not in roster:
            excluded += 1
            continue
        member = member_for(login)
        pr = item.get("pull_request")
        member["prs" if pr else "issues"] += 1
        member["merged"] += bool(pr and pr.get("merged_at"))
        member["total"] += 1
        member["closures"] += 1
        member["items"].append({"number": item["number"], "repository": repository(item), "title": item["title"],
                                "url": item["html_url"], "closed_at": item["closed_at"],
                                "kind": "pr" if pr else "issue",
                                "activity": "closure", "occurred_at": item["closed_at"],
                                "actor_login": actor_login,
                                "status": current_status(item),
                                "merged": bool(pr and pr.get("merged_at"))})
    seen_opened = set()
    for item in opened_prs:
        if item_key(item) in seen_opened:
            continue
        seen_opened.add(item_key(item))
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        if not lower <= created < upper:
            continue
        actor_login = (item.get("user") or {}).get("login", "")
        login = actor_login.casefold()
        login = metadata.aliases.get(login, login)
        if login not in roster:
            excluded += 1
            continue
        member = member_for(login)
        member["prs_opened"] += 1
        member["total"] += 1
        member["items"].append({"number": item["number"], "repository": repository(item), "title": item["title"],
            "url": item["html_url"], "kind": "pr", "activity": "pr_opened",
            "actor_login": actor_login,
            "status": current_status(item),
            "occurred_at": item["created_at"], "merged": False})
    # Filter by submission time before deduplicating: re-reviewing an older PR
    # still counts in this range, but only once per canonical person and PR.
    review_events = {}
    for item in reviewed_prs:
        author = (item.get("user") or {}).get("login", "").casefold()
        author = metadata.aliases.get(author, author)
        for review in item.get("reviews", ()):
            if (not review.get("submitted_at") or review.get("state") not in
                    {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}):
                continue
            submitted = datetime.fromisoformat(review["submitted_at"].replace("Z", "+00:00"))
            if not lower <= submitted < upper:
                continue
            actor = (review.get("user") or {}).get("login", "")
            login = actor.casefold()
            login = metadata.aliases.get(login, login)
            if author == login:
                continue  # own-PR comments/reviews don't earn review points
            key = (*item_key(item), login)
            event = {"number": item["number"], "repository": repository(item),
                     "title": item["title"], "url": item["html_url"], "kind": "pr",
                     "activity": "review", "status": current_status(item),
                     "occurred_at": review["submitted_at"], "actor_login": actor,
                     "review_state": review["state"], "review_url": review["html_url"],
                     "review_id": review["id"], "merged": False}
            previous = review_events.get(key)
            if previous is None or submitted < previous[0]:
                review_events[key] = (submitted, event)
    for (_, _, login), (_, event) in review_events.items():
        if login not in roster:
            excluded += 1
            continue
        member = member_for(login)
        member["reviews"] += 1
        member["total"] += 1
        member["items"].append(event)
    rows = sorted(members.values(), key=lambda m: (-m["total"], m["login"].casefold()))
    for member in rows:
        member["items"].sort(key=lambda i: i["occurred_at"], reverse=True)
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": tz,
            "members": rows, "excluded": excluded, "roster_size": len(roster),
            "updated_at": datetime.now(UTC).isoformat(), "refresh_seconds": metadata.refresh_seconds,
            "schema_version": SCHEMA_VERSION, "config_id": metadata.config_id,
            "organizations": list(metadata.organizations),
            "identity_version": IDENTITY_VERSION}


class Store:
    def __init__(self, metadata=METADATA):
        self.metadata = metadata
        self.cache = {}
        self.errors = {}
        self.pending = {}
        self.retry_after = {}
        self.requests = asyncio.Semaphore(4)
        self.client = None
        self.source = None
        self.source_lock = asyncio.Lock()
        self.graphql_enabled = None
        self.status = None
        self.new_ranges = deque()
        self.fetch_starts = deque()

    def claim(self, attempts, limit, window):
        now = monotonic()
        while attempts and attempts[0] <= now - window:
            attempts.popleft()
        if len(attempts) >= limit:
            retry = max(1, ceil(attempts[0] + window - now))
            raise HTTPException(429, f"Too many requests for new GitHub data. Try again in {retry} seconds.",
                                headers={"Retry-After": str(retry)})
        attempts.append(now)

    def load_seed(self, data):
        if (data.get("schema_version") != SCHEMA_VERSION
                or data.get("identity_version") != IDENTITY_VERSION
                or data.get("config_id") != self.metadata.config_id or not data.get("repositories")):
            return False
        key = (date.fromisoformat(data["start"]), date.fromisoformat(data["end"]), data["timezone"])
        age = (datetime.now(UTC) - datetime.fromisoformat(data["updated_at"])).total_seconds()
        self.cache[key] = (monotonic() - max(0, age), data)
        return True

    async def check_github_status(self):
        if self.status is None or monotonic() - self.status[0] >= 60:
            try:
                # No GitHub credential is sent to the independent status site.
                response = await self.client.get(GITHUB_STATUS_URL, timeout=10)
                response.raise_for_status()
                components = response.json()["components"]
                affected = [component["name"] for component in components
                            if component["name"] in {"API Requests", "Issues", "Pull Requests"}
                            and component["status"] != "operational"]
                self.status = (monotonic(), affected)
            except (httpx.HTTPError, ValueError, KeyError):
                # An unavailable status site alone doesn't establish an outage.
                log.warning("GitHub status could not be checked")
                return
        if self.status[1]:
            raise GitHubIncident("GitHub is reporting degraded issue/PR service.")

    async def github(self, path, headers, params):
        async with self.requests:
            response = await self.client.get("https://api.github.com" + path,
                                             headers=headers, params=params)
            response.raise_for_status()
            return response

    async def pages(self, path, headers, params):
        params = {**params, "per_page": 100, "page": 1}
        first = await self.github(path, headers, params)
        items = first.json()
        if last := first.links.get("last"):
            count = int(httpx.URL(last["url"]).params["page"])
            if count > 100:
                raise ValueError("Choose a shorter range to fetch all activity.")
            responses = await asyncio.gather(*(self.github(path, headers, {**params, "page": p})
                                               for p in range(2, count + 1)))
            return items + [item for response in responses for item in response.json()]
        # Cursor links can omit a last page. The page parameter still works;
        # fetch a bounded window and stop at the first terminal page.
        response = first
        for start_page in range(2, 101, 4):
            if "next" not in response.links:
                return items
            responses = await asyncio.gather(*(self.github(path, headers, {**params, "page": page})
                for page in range(start_page, min(101, start_page + 4))))
            for response in responses:
                items.extend(response.json())
                if "next" not in response.links:
                    return items
        if "next" in response.links:
            raise ValueError("Choose a shorter range to fetch all activity.")
        return items

    async def review_batch(self, items, headers):
        if "Authorization" in headers and self.graphql_enabled is not False:
            cursors, results = {i: None for i in range(len(items))}, {i: [] for i in range(len(items))}
            for _ in range(100):
                fields = []
                for i, cursor in cursors.items():
                    owner, name = repository(items[i]).split("/")
                    fields.append(f'p{i}: repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) {{'
                        f' pullRequest(number:{int(items[i]["number"])}) {{ reviews(first:100, after:{json.dumps(cursor)}) {{'
                        ' nodes { id state submittedAt url author { login } } pageInfo { hasNextPage endCursor } } } }')
                async with self.requests:
                    response = await self.client.post("https://api.github.com/graphql", headers=headers,
                                                       json={"query": "query {" + " ".join(fields) + "}"})
                payload = response.json()
                # Public REST remains available for tokens without GraphQL access.
                if response.status_code in (401, 403) or any(
                        error.get("type") == "FORBIDDEN" for error in payload.get("errors", ())):
                    self.graphql_enabled = False
                    break
                response.raise_for_status()
                if payload.get("errors") or not isinstance(payload.get("data"), dict):
                    raise ValueError("GitHub returned incomplete review data.")
                self.graphql_enabled = True
                next_cursors = {}
                for i, cursor in cursors.items():
                    repo = payload["data"].get(f"p{i}")
                    if not repo or not repo.get("pullRequest"):
                        raise ValueError("A pull request's reviews could not be verified.")
                    connection = repo["pullRequest"]["reviews"]
                    if not connection or connection.get("nodes") is None or any(
                            review is None for review in connection["nodes"]):
                        raise ValueError("GitHub returned incomplete review data.")
                    results[i].extend({"id": review["id"], "state": review["state"],
                        "submitted_at": review["submittedAt"], "html_url": review["url"],
                        "user": {"login": (review.get("author") or {}).get("login", "")}}
                        for review in connection["nodes"])
                    info = connection["pageInfo"]
                    if info["hasNextPage"]:
                        if not info["endCursor"] or info["endCursor"] == cursor:
                            raise ValueError("GitHub review pagination did not advance.")
                        next_cursors[i] = info["endCursor"]
                if not next_cursors:
                    return [results[i] for i in range(len(items))]
                cursors = next_cursors
            else:
                raise ValueError("Choose a shorter range to fetch all reviews.")
        return await asyncio.gather(*(self.pages(
            f"/repos/{repository(item)}/pulls/{item['number']}/reviews", headers, {}) for item in items))

    async def attach_reviews(self, items, headers):
        prs = [item for item in items if "pull_request" in item]
        async def batch(group):
            reviews = await self.review_batch(group, headers)
            for item, values in zip(group, reviews, strict=True):
                item["reviews"] = values
        try:
            async with asyncio.TaskGroup() as group:
                for start in range(0, len(prs), 20):
                    group.create_task(batch(prs[start:start + 20]))
        except* (httpx.HTTPError, ValueError, KeyError) as errors:
            raise ValueError("PR reviews could not be fetched completely.") from errors

    async def activity(self, lower, headers):
        # Share one complete organization snapshot across viewers and date ranges.
        async with self.source_lock:
            await self.check_github_status()
            if self.source and monotonic() - self.source[0] < self.metadata.refresh_seconds and lower >= self.source[1]:
                return self.source
            # This budget is shared across viewers and applies even when dates/IPs change.
            self.claim(self.fetch_starts, MAX_SNAPSHOTS_PER_REFRESH, self.metadata.refresh_seconds)
            # Warm every cached range from the same snapshot on the next cycle.
            lower = min([lower, *(bounds(*key)[0] for key in self.cache)])
            async def organization_repos(organization):
                repos = await self.pages(f"/orgs/{organization}/repos", headers,
                    {"type": "public", "sort": "full_name", "direction": "asc"})
                return [repo["full_name"] for repo in repos if not repo["private"]
                        and repo["owner"]["login"].casefold() == organization]

            async def selected_repo(name):
                response = await self.github(f"/repos/{name}", headers, {})
                repo = response.json()
                if repo["private"]:
                    raise ValueError("Only public repositories can be tracked.")
                return [repo["full_name"]]

            groups = await asyncio.gather(
                *(organization_repos(org) for org in self.metadata.organizations),
                *(selected_repo(repo) for repo in self.metadata.repositories))
            names = sorted({name.casefold(): name for group in groups for name in group}.values(), key=str.casefold)
            if not names:
                raise ValueError("The configured public repository list could not be verified.")

            async def fetch_repo(name):
                # Issues includes PRs, even when a repository has issues disabled.
                # Every item created in range was also updated since its start.
                items = await self.pages(f"/repos/{name}/issues", headers,
                    {"state": "all", "since": lower.strftime('%Y-%m-%dT%H:%M:%SZ'),
                     "sort": "updated", "direction": "desc"})
                return [{key: item[key] for key in (
                    "number", "title", "html_url", "closed_at", "created_at", "state", "pull_request")
                    if key in item} | {"repository": name,
                        "user": {"login": (item.get("user") or {}).get("login", "")},
                        "closed_by": {"login": (item.get("closed_by") or {}).get("login", "")}}
                    for item in items]

            try:
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(fetch_repo(name)) for name in names]
            except* (httpx.HTTPError, ValueError, KeyError) as errors:
                raise ValueError("An organization repository could not be fetched completely.") from errors
            items = [item for task in tasks for item in task.result()]
            await self.attach_reviews(items, headers)
            self.source = (monotonic(), lower, items, names, datetime.now(UTC).isoformat())
            return self.source

    async def collect(self, start, end, tz):
        lower, upper = bounds(start, end, tz)
        roster = {}
        if self.metadata.roster:
            response = await self.client.get(self.metadata.roster["url"])
            response.raise_for_status()
            roster = parse_roster(response.text)
            if len(roster) < self.metadata.roster["minimum_members"]:
                raise ValueError("The configured contributor roster could not be verified.")
        roster = include_contributors(roster, self.metadata.contributors)
        if any(target not in roster or alias in roster for alias, target in self.metadata.aliases.items()):
            raise ValueError("Aliases must map alternate accounts to a single configured roster identity.")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "sprint-stars"}
        if token := os.getenv("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        _, _, items, repos, updated_at = await self.activity(lower, headers)
        opened_prs = [item for item in items if "pull_request" in item]
        data = aggregate(items, roster, start, end, tz, opened_prs, opened_prs, metadata=self.metadata)
        return {**data, "repositories": repos, "repository_count": len(repos), "updated_at": updated_at}

    async def get(self, key, force=False):
        now = monotonic()
        # Failed, uncached ranges must not accumulate indefinitely.
        for failed, until in list(self.retry_after.items()):
            if until <= now and failed not in self.cache and failed not in self.pending:
                self.retry_after.pop(failed, None)
                self.errors.pop(failed, None)
        cached = self.cache.get(key)
        if cached and not force:
            if (now - cached[0] >= self.metadata.refresh_seconds
                    and now >= self.retry_after.get(key, 0)):
                try:
                    self.queue_refresh(key)
                except HTTPException as exc:
                    if exc.status_code != 429:
                        raise
                    self.errors[key] = "Refreshes are busy. Showing the last successful update; retrying automatically."
                    self.retry_after[key] = now + int(exc.headers["Retry-After"])
            return self.result(key)
        if not force and not cached and now < self.retry_after.get(key, 0):
            raise HTTPException(503, "GitHub is unavailable or rate-limited. Try again shortly.")
        if not force and not cached and key not in self.pending:
            self.claim(self.new_ranges, MAX_NEW_RANGES_PER_MINUTE, 60)
        result = await asyncio.shield(self.queue_refresh(key))
        if result is None:
            raise HTTPException(503, "GitHub is unavailable or rate-limited. Try again shortly.")
        return result

    def queue_refresh(self, key):
        if key not in self.pending:
            if len(self.pending) >= MAX_PENDING_REFRESHES:
                raise HTTPException(429, "Refreshes are busy. Try again shortly.", headers={"Retry-After": "5"})
            self.pending[key] = asyncio.create_task(self.refresh(key))
        return self.pending[key]

    async def refresh(self, key):
        try:
            try:
                data = await self.collect(*key)
            except HTTPException as exc:
                if exc.status_code != 429 or key not in self.cache:
                    raise
                self.errors[key] = "GitHub fetches are temporarily limited. Showing the last successful update; retrying automatically."
                self.retry_after[key] = monotonic() + int(exc.headers["Retry-After"])
                self.pending.pop(key, None)
                return self.result(key)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.warning("GitHub refresh failed: %s", type(exc).__name__)
                self.errors[key] = (
                    "GitHub is reporting an issue/PR service incident. Showing the last verified update; "
                    "refreshes will resume automatically when service recovers."
                    if isinstance(exc, GitHubIncident) else
                    "GitHub could not be refreshed. Showing the last successful update; retrying automatically.")
                self.retry_after[key] = monotonic() + 60
                if key in self.cache:
                    return self.result(key)
                return None
            age = max(0, (datetime.now(UTC) - datetime.fromisoformat(data["updated_at"])).total_seconds())
            self.cache[key] = (monotonic() - age, data)
            self.errors.pop(key, None)
            self.retry_after.pop(key, None)
            # Bound memory; the current week is warmed again on each background cycle.
            while len(self.cache) > 32:
                oldest = next(iter(self.cache))
                self.cache.pop(oldest)
                self.errors.pop(oldest, None)
                self.retry_after.pop(oldest, None)
            self.pending.pop(key, None)
            return self.result(key)
        finally:
            self.pending.pop(key, None)

    def result(self, key):
        stamp, data = self.cache[key]
        return {**data, "stale": key in self.errors or monotonic() - stamp >= self.metadata.refresh_seconds,
                "refreshing": key in self.pending,
                "warning": self.errors.get(key) or ("Refreshing GitHub data in the background…"
                                                    if key in self.pending else None)}

    async def refresh_all(self):
        keys = list(dict.fromkeys([current_week(os.getenv("SPRINT_TIMEZONE", self.metadata.timezone)), *self.cache]))
        for key in keys:
            try:
                await self.get(key, force=True)
            except HTTPException:
                pass

    async def background(self):
        while True:
            started = monotonic()
            await self.refresh_all()
            await asyncio.sleep(max(1, self.metadata.refresh_seconds - (monotonic() - started)))


store = Store()


@asynccontextmanager
async def lifespan(app):
    seed = ROOT / "data" / "initial.json"
    if seed.exists():
        store.load_seed(json.loads(seed.read_text()))
    async with httpx.AsyncClient(timeout=30) as client:
        store.client = client
        task = asyncio.create_task(store.background())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for pending in list(store.pending.values()):
            pending.cancel()
        await asyncio.gather(*store.pending.values(), return_exceptions=True)


app = FastAPI(title=f"{METADATA.brand} · {METADATA.name}", lifespan=lifespan)


@app.middleware("http")
async def asset_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # A cached missing stylesheet can outlive a deployment at the CDN.
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/leaderboard")
async def leaderboard(start: date | None = None, end: date | None = None,
                      tz: str = Query(DEFAULT_TIMEZONE, max_length=80)):
    try:
        default_start, default_end, _ = current_week(tz)
        start, end = start or default_start, end or default_end
        bounds(start, end, tz)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return await store.get((start, end, tz))


@app.get("/")
async def index():
    html = (ROOT / "static" / "index.html").read_text()
    roster = METADATA.roster
    if roster:
        roster_description = (f'Contributors come from the public <a href="{escape(roster["page_url"], quote=True)}" '
            f'target="_blank" rel="noreferrer">{escape(roster["label"])}</a> '
            'with a GitHub username and no departure date, plus the contributors listed in metadata.toml.')
    else:
        roster_description = 'Contributors are the GitHub accounts listed in metadata.toml.'
    alias_description = ' '.join(f'<code>{escape(alias)}</code> counts toward <code>{escape(target)}</code>.'
                                 for alias, target in METADATA.aliases.items())
    values = {"name": escape(METADATA.name), "brand": escape(METADATA.brand),
              "description": escape(f"{METADATA.name}. Track reviews, PRs opened, and issues and PRs closed."),
              "scope_url": escape(METADATA.scope_url, quote=True), "scope_label": escape(METADATA.scope_label),
              "scope_short": escape(METADATA.organizations[0] if len(METADATA.organizations) == 1 else 'GitHub'),
              "refresh_minutes": str(METADATA.refresh_minutes), "roster_description": roster_description,
              "alias_description": alias_description,
              "app_config": json.dumps(METADATA.public()).replace('<', '\\u003c')}
    for key, value in values.items():
        html = html.replace('{{' + key + '}}', value)
    for filename in ("style.css", "app.js"):
        version = sha256((ROOT / "static" / filename).read_bytes()).hexdigest()[:12]
        html = html.replace(f"/static/{filename}", f"/static/{filename}?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
