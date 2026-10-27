import asyncio
from dataclasses import replace
import json
import re
from time import monotonic

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from config import load_metadata
from test_leaderboard import KEY, ROSTER, issue, opened


def metadata(tmp_path, extra="", *, name="Example team"):
    path = tmp_path / "metadata.toml"
    path.write_text(f'name = {json.dumps(name)}\norganizations = ["acme", "other", "ACME"]\n'
        'repositories = ["acme/widgets", "indie/one", "Indie/One"]\n'
        'contributors = ["alice", {login="bob", name="Bob Example"}]\n'
        'refresh_minutes = 5\ntimezone = "UTC"\n' + extra)
    return load_metadata(path)


def test_minimal_metadata_supplies_defaults_without_a_roster(tmp_path):
    path = tmp_path / "metadata.toml"
    path.write_text('name = "Our team leaderboard"\norganizations = ["your-org"]\n'
                    'contributors = ["alice", "bob"]\n')
    config = load_metadata(path)
    assert config.name == "Our team leaderboard"
    assert config.brand == "Sprint Stars"
    assert config.organizations == ("your-org",)
    assert config.repositories == ()
    assert config.refresh_seconds == 1200
    assert config.timezone == "UTC"
    assert config.roster == {}
    assert [m["login"] for m in config.contributors] == ["alice", "bob"]
    assert config.aliases == {}


@pytest.mark.asyncio
async def test_custom_metadata_fetches_multiple_orgs_and_explicit_repos_once(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = metadata(tmp_path, '[aliases]\nbob-work = "bob"\n')
    assert config.organizations == ("acme", "other")
    assert config.repositories == ("acme/widgets", "indie/one")
    s, paths = main.Store(config), []
    def repo(name, private=False):
        return {"full_name": name, "private": private, "owner": {"login": name.split('/')[0]}}
    def handle(request):
        paths.append(request.url.path)
        assert request.url.host != "raw.githubusercontent.com"  # no implicit Sample team roster
        if request.url.host == "www.githubstatus.com":
            return httpx.Response(200, json={"components": []})
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[repo("acme/widgets"), repo("acme/private", True)])
        if request.url.path == "/orgs/other/repos":
            return httpx.Response(200, json=[repo("other/tools")])
        if request.url.path in ("/repos/acme/widgets", "/repos/indie/one"):
            return httpx.Response(200, json=repo(request.url.path.removeprefix('/repos/')))
        if request.url.path.endswith('/reviews'):
            return httpx.Response(200, json=[])
        assert request.url.path.endswith('/issues')
        name = request.url.path.removeprefix('/repos/').removesuffix('/issues')
        closed, pr = issue(), opened(author="bob-work") | {"pull_request": {}}
        for item in (closed, pr):
            item["html_url"] = item["html_url"].replace('acme/widgets', name)
        return httpx.Response(200, json=[closed, pr])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        data = await s.get(KEY)
    assert data['repositories'] == ['acme/widgets', 'indie/one', 'other/tools']
    assert data['repository_count'] == 3
    assert paths.count('/repos/acme/widgets/issues') == 1
    assert '/repos/acme/private/issues' not in paths
    assert data['roster_size'] == 2
    rows = {m['login']: m for m in data['members']}
    assert rows['alice']['closures'] == rows['bob']['prs_opened'] == 3
    assert rows['bob']['aliases'] == ['bob-work']
    assert rows['bob']['name'] == 'Bob Example'
    assert data['config_id'] == config.config_id
    assert data['refresh_seconds'] == 300
    s.cache[KEY] = (monotonic() - 301, data)
    assert s.result(KEY)['stale']


@pytest.mark.asyncio
async def test_explicit_private_repo_is_rejected(tmp_path):
    config = replace(metadata(tmp_path), organizations=(), repositories=("acme/private",))
    s = main.Store(config)
    def handle(request):
        if request.url.host == "www.githubstatus.com":
            return httpx.Response(200, json={"components": []})
        return httpx.Response(200, json={"private": True, "full_name": "acme/private"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as s.client:
        with pytest.raises(ValueError, match="Only public repositories"):
            await s.activity(main.bounds(*KEY)[0], {})
    assert s.source is None


def test_snapshot_cannot_leak_between_configurations(tmp_path):
    original = metadata(tmp_path, '[aliases]\nbob-work = "bob"\n')
    seed = main.aggregate([issue()], main.parse_roster(ROSTER), *KEY, metadata=original) | {'repositories': ['acme/widgets']}
    assert main.Store(original).load_seed(seed)
    other = main.Store(metadata(tmp_path))
    assert not other.load_seed(seed)
    assert not other.cache
    changed_aliases = main.Store(replace(original, aliases={}))
    assert not changed_aliases.load_seed(seed)
    assert not main.Store(original).load_seed(seed | {'schema_version': 6})


def test_rendering_uses_custom_metadata_and_safely_bootstraps_json(tmp_path, monkeypatch):
    name = 'Other team </script><script>alert("x")</script>'
    config = metadata(tmp_path, name=name)
    monkeypatch.setattr(main, 'METADATA', config)
    page = TestClient(main.app).get('/')
    assert page.status_code == 200
    assert 'Sample team' not in page.text
    assert 'github.com/acme' not in page.text
    assert 'Every 5 min' in page.text
    assert '&lt;/script&gt;' in page.text
    boot = re.search(r'<script id="app-config" type="application/json">(.*?)</script>', page.text).group(1)
    assert json.loads(boot) == config.public()
    assert '</script>' not in boot
    assert page.text.count('<script') == 2
    assert '{{' not in page.text


@pytest.mark.asyncio
async def test_background_interval_comes_from_metadata(tmp_path, monkeypatch):
    s = main.Store(metadata(tmp_path))
    async def refresh():
        pass
    async def sleep(seconds):
        assert 299 < seconds <= 300
        raise asyncio.CancelledError
    s.refresh_all = refresh
    monkeypatch.setattr(main.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await s.background()


@pytest.mark.parametrize('text', [
    'name="Team"\norganizations=["acme"]\ncontributors=[]',
    'name="Team"\norganizations=[]\ncontributors=["alice"]',
    'name="Team"\norganizations=["https://github.com/acme"]\ncontributors=["alice"]',
    'name="Team"\nrepositories=["acme/../secret"]\ncontributors=["alice"]',
    'name="Team"\nrepositories="acme/widgets"\ncontributors=["alice"]',
    'name="Team"\norganizations=["acme"]\ncontributors=["alice"]\nrefresh_minutes=true',
    'name="Team"\norganizations=["acme"]\ncontributors=["alice"]\nrefresh_minutes=0',
    'name="Team"\norganizations=["acme"]\ncontributors=["alice"]\ntimezone="No/SuchZone"',
    'name="Team"\norganizations=["acme"]\ncontributors=["alice"]\n[aliases]\nbob="charlie"',
    'name="Team"\norganizations=["acme"]\ncontributors=["alice"]\n[aliases]\nbob="charlie"\ncharlie="bob"',
    'name="Team"\norganizations=["acme"]\ncontributors=["alice"]\n[aliases]\nalice="alice"',
    'name="Team"\norganizations=["acme"]\ncontributors=["alice"]\nGITHUB_TOKEN="secret"',
    'name="Team"\norganizations=["acme"]\n[roster]\nurl="http://example.org/roster.csv"',
])
def test_invalid_metadata_fails_with_file_context(tmp_path, text):
    path = tmp_path / 'metadata.toml'
    path.write_text(text)
    with pytest.raises(ValueError, match='Invalid metadata file .*metadata.toml'):
        load_metadata(path)


def test_missing_metadata_fails_instead_of_defaulting_to_widgets(tmp_path):
    with pytest.raises(ValueError, match='Invalid metadata file'):
        load_metadata(tmp_path / 'missing.toml')


def test_repository_only_setup_and_alias_chain(tmp_path):
    path = tmp_path / 'metadata.toml'
    path.write_text('name="Single repo"\nrepositories=["acme/widgets"]\ncontributors=["alice", "ALICE"]\n'
                    '[aliases]\nalice-work="alice-other"\nalice-other="alice"')
    config = load_metadata(path)
    assert config.organizations == ()
    assert len(config.contributors) == 1
    assert config.aliases == {'alice-work': 'alice', 'alice-other': 'alice'}
    assert config.scope_label == 'acme/widgets'
    assert config.scope_url == 'https://github.com/acme/widgets'
