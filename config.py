"""Validated, deployment-local leaderboard metadata (no credentials)."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


def login(value):
    if not isinstance(value, str) or not LOGIN.fullmatch(value):
        raise ValueError(f"Invalid GitHub organization or username: {value!r}")
    return value


def public_url(value):
    if not isinstance(value, str):
        raise ValueError("Roster URLs must be HTTPS URLs without credentials.")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Roster URLs must be HTTPS URLs without credentials.")
    return value


@dataclass(frozen=True)
class Metadata:
    name: str
    brand: str
    organizations: tuple
    repositories: tuple
    contributors: tuple
    aliases: dict
    refresh_minutes: int
    timezone: str
    roster: dict

    @property
    def refresh_seconds(self):
        return self.refresh_minutes * 60

    @property
    def config_id(self):
        return sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    @property
    def scope_label(self):
        if len(self.organizations) == 1 and not self.repositories:
            return f"All {self.organizations[0]} repositories"
        if len(self.repositories) == 1 and not self.organizations:
            return self.repositories[0]
        return "Configured organizations and repositories"

    @property
    def scope_url(self):
        if len(self.organizations) == 1 and not self.repositories:
            return f"https://github.com/{self.organizations[0]}"
        if len(self.repositories) == 1 and not self.organizations:
            return f"https://github.com/{self.repositories[0]}"
        return "https://github.com"

    def public(self):
        return {"config_id": self.config_id, "name": self.name, "brand": self.brand,
                "organizations": list(self.organizations), "repositories": list(self.repositories),
                "scope_label": self.scope_label, "scope_url": self.scope_url,
                "refresh_seconds": self.refresh_seconds, "timezone": self.timezone}


def load_metadata(path):
    try:
        with Path(path).open("rb") as source:
            data = tomllib.load(source)
        unknown = set(data) - {"name", "brand", "organizations", "repositories", "contributors",
                               "aliases", "refresh_minutes", "timezone", "roster"}
        if unknown:
            raise ValueError(f"Unknown metadata fields: {', '.join(sorted(unknown))}")
        name, brand = data.get("name"), data.get("brand", "Sprint Stars")
        if not all(isinstance(value, str) and value.strip() for value in (name, brand)):
            raise ValueError("name and brand must be non-empty strings.")
        organizations, repositories = data.get("organizations", []), data.get("repositories", [])
        if not isinstance(organizations, list) or not isinstance(repositories, list):
            raise ValueError("organizations and repositories must be arrays.")
        organizations = tuple(dict.fromkeys(login(value).casefold() for value in organizations))
        names = []
        for value in repositories:
            if not isinstance(value, str) or value.count("/") != 1:
                raise ValueError("repositories must use owner/repo names.")
            owner, repo = value.split("/")
            login(owner)
            if repo in {".", ".."} or not REPOSITORY.fullmatch(repo):
                raise ValueError(f"Invalid repository name: {value!r}")
            names.append(value.casefold())
        repositories = tuple(dict.fromkeys(names))
        if not organizations and not repositories:
            raise ValueError("Configure at least one organization or repository.")
        minutes = data.get("refresh_minutes", 20)
        if type(minutes) is not int or minutes < 1:
            raise ValueError("refresh_minutes must be a positive integer.")
        timezone = data.get("timezone", "UTC")
        ZoneInfo(timezone)
        roster = data.get("roster", {})
        if not isinstance(roster, dict) or set(roster) - {"url", "page_url", "label", "minimum_members"}:
            raise ValueError("roster supports url, page_url, label, and minimum_members.")
        if roster:
            roster = {"url": public_url(roster["url"]),
                      "page_url": public_url(roster.get("page_url", roster["url"])),
                      "label": roster.get("label", "Configured contributor roster"),
                      "minimum_members": roster.get("minimum_members", 1)}
            if not isinstance(roster["label"], str) or not roster["label"].strip():
                raise ValueError("roster.label must be a non-empty string.")
            if type(roster["minimum_members"]) is not int or roster["minimum_members"] < 1:
                raise ValueError("roster.minimum_members must be a positive integer.")
        contributors = data.get("contributors", [])
        if not isinstance(contributors, list):
            raise ValueError("contributors must be an array of usernames or contributor tables.")
        people = {}
        for value in contributors:
            member = {"login": value} if isinstance(value, str) else value
            if not isinstance(member, dict) or set(member) - {"login", "name", "roles"}:
                raise ValueError("Contributor tables support login, name, and roles.")
            username = login(member["login"])
            display = member.get("name", username)
            roles = member.get("roles", ["contributor"])
            if not isinstance(display, str) or not display.strip():
                raise ValueError("Contributor names must be non-empty strings.")
            if not isinstance(roles, list) or not all(isinstance(role, str) and role for role in roles):
                raise ValueError("Contributor roles must be an array of strings.")
            key = username.casefold()
            if key in people:
                people[key]["roles"] = list(dict.fromkeys([*people[key]["roles"], *roles]))
            else:
                people[key] = {"login": username, "name": display, "roles": roles}
        if not people and not roster:
            raise ValueError("Configure contributors or a roster source.")
        aliases = data.get("aliases", {})
        if not isinstance(aliases, dict):
            raise ValueError("aliases must map alternate usernames to contributor usernames.")
        aliases = {login(alias).casefold(): login(target).casefold() for alias, target in aliases.items()}
        resolved = {}
        for alias in aliases:
            target, seen = alias, set()
            while target in aliases:
                if target in seen:
                    raise ValueError("Alias mappings must not contain cycles or self-aliases.")
                seen.add(target)
                target = aliases[target]
            if not roster and target not in people:
                raise ValueError(f"Alias {alias!r} points to an unconfigured contributor.")
            if alias in people:
                raise ValueError(f"Alias {alias!r} must not also be a separate contributor.")
            resolved[alias] = target
        return Metadata(name, brand, organizations, repositories, tuple(people.values()), resolved,
                        minutes, timezone, roster)
    except (OSError, ValueError, KeyError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"Invalid metadata file {path}: {exc}") from exc
