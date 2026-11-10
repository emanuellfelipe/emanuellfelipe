#!/usr/bin/env python3
"""Refresh data/contributions.json from the GitHub GraphQL API.

Needs a token in GH_TOKEN (or GITHUB_TOKEN). The workflow's built-in token is
enough for public data; a personal token with `read:user` also counts private
contributions when "Private contributions" is switched on in your profile.

Writes every day since the account was created (the API returns at most one
year per query, so it walks year by year) plus metadata for the featured repos
listed in profile.json. Standard library only.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "contributions.json"
API_HOST = "api.github.com"

LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


def gql(query: str, variables: dict, token: str) -> dict:
    conn = http.client.HTTPSConnection(API_HOST, timeout=30)
    try:
        conn.request(
            "POST", "/graphql",
            body=json.dumps({"query": query, "variables": variables}),
            headers={"Authorization": f"bearer {token}", "Content-Type": "application/json",
                     "User-Agent": "profile-cards"},
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
    finally:
        conn.close()
    if resp.status != 200 or payload.get("errors"):
        raise SystemExit(f"GraphQL error ({resp.status}): {payload.get('errors') or payload}")
    return payload["data"]


CALENDAR = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    createdAt
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks { contributionDays { date contributionCount contributionLevel } }
      }
    }
  }
}"""


def fetch_days(login: str, token: str) -> list[dict]:
    now = dt.datetime.now(dt.UTC)
    created = gql("query($l:String!){user(login:$l){createdAt}}", {"l": login}, token)["user"]["createdAt"]
    start = dt.datetime.fromisoformat(created)
    days: dict[str, dict] = {}
    cursor = start
    while cursor < now:
        end = min(cursor + dt.timedelta(days=365) - dt.timedelta(seconds=1), now)
        data = gql(CALENDAR, {"login": login, "from": cursor.isoformat(), "to": end.isoformat()}, token)
        for week in data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]:
            for d in week["contributionDays"]:
                days[d["date"]] = {"date": d["date"], "count": d["contributionCount"],
                                   "level": LEVELS.get(d["contributionLevel"], 0)}
        cursor = end + dt.timedelta(seconds=1)
    return [days[k] for k in sorted(days)]


def fetch_repos(token: str) -> dict:
    parts = []
    for i, item in enumerate(PROFILE.get("featured", [])):
        owner, name = item["repo"].split("/", 1)
        parts.append(
            f'r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{'
            " nameWithOwner description url stargazerCount forkCount pushedAt isArchived"
            " primaryLanguage { name color } }"
        )
    if not parts:
        return {}
    data = gql("query {" + "\n".join(parts) + "}", {}, token)
    return {v["nameWithOwner"]: v for v in data.values() if v}


def main() -> None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Set GH_TOKEN (for local runs: GH_TOKEN=$(gh auth token))")
    login = PROFILE["login"]
    days = fetch_days(login, token)
    repos = fetch_repos(token)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "login": login,
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "days": days,
        "repos": repos,
    }, indent=1) + "\n", encoding="utf-8")
    total = sum(d["count"] for d in days)
    sys.stdout.write(f"wrote {OUT.relative_to(ROOT)}: {len(days)} days, {total} contributions, {len(repos)} repos\n")


if __name__ == "__main__":
    main()
