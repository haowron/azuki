"""GitHub: 本人が関わった Issue・PR ごとに、本人の操作と、その契機・結果になった他の人の出来事を時刻順に集める。

``GITHUB_TOKEN`` が見られる repository だけを対象にする。検索 ``involves:``(作成者・担当・mention・comment)と
``reviewed-by:`` で期間内に更新された Issue・PR を探し、GraphQL の ``timelineItems`` から出来事を読む。本人の出来事は
作成・comment・merge・close・label・担当・review 依頼・PR の commit など全て、他の人の出来事は作成・comment・review・
review 依頼・merge・close・reopen・ready for review だけを残す。自動で付く参照(cross-reference)と、merge に続く
branch の自動削除は残さない。closing keyword による自動の close は本人ではなく ``GitHub (自動)`` の出来事にする
(GitHub は自動の操作を merge した人の名前で記録する)。
本人の review と行の comment は thread への返信が timeline に現れないため、PR の ``reviews(author:)`` から読む。
既定 branch の commit は検索 API で読み、repository と日ごとにまとめる。検索は一つの式あたり 1000 件まで。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from trackeverything.optimizer.behavior.common import (
    SELF,
    Episodes,
    Event,
    Fetched,
    FetchError,
    Window,
    as_dict,
    as_list,
    parse_time,
    request_json,
)

GITHUB_API = "https://api.github.com"
_SEARCH_CAP = 1000
_PAGE = 100
#: 一つの GraphQL で読む Issue・PR の数と、同時に送る GraphQL の数。
_BATCH = 20
_CONCURRENCY = 10
#: 一つの PR で読む本人の review の数。
_REVIEWS = 50
_JST = ZoneInfo("Asia/Tokyo")

_ACTOR = "actor { login } createdAt"
_COMMON_ITEMS = f"""
  __typename
  ... on IssueComment {{ author {{ login }} createdAt body url }}
  ... on AssignedEvent {{ {_ACTOR} assignee {{ ... on User {{ login }} }} }}
  ... on UnassignedEvent {{ {_ACTOR} assignee {{ ... on User {{ login }} }} }}
  ... on ClosedEvent {{ {_ACTOR} closer {{ __typename ... on PullRequest {{ number repository {{ nameWithOwner }} }} ... on Commit {{ oid }} }} }}
  ... on ReopenedEvent {{ {_ACTOR} }}
  ... on LabeledEvent {{ {_ACTOR} label {{ name }} }}
  ... on UnlabeledEvent {{ {_ACTOR} label {{ name }} }}
  ... on RenamedTitleEvent {{ {_ACTOR} previousTitle currentTitle }}
  ... on MilestonedEvent {{ {_ACTOR} milestoneTitle }}
  ... on DemilestonedEvent {{ {_ACTOR} milestoneTitle }}
"""
_PULL_ITEMS = f"""
  ... on PullRequestReview {{ author {{ login }} submittedAt state body url }}
  ... on PullRequestCommit {{ url commit {{ oid message committedDate author {{ user {{ login }} }} }} }}
  ... on MergedEvent {{ {_ACTOR} }}
  ... on ReviewRequestedEvent {{ {_ACTOR} requestedReviewer {{ ... on User {{ login }} ... on Team {{ name }} }} }}
  ... on ReviewRequestRemovedEvent {{ {_ACTOR} requestedReviewer {{ ... on User {{ login }} ... on Team {{ name }} }} }}
  ... on ReadyForReviewEvent {{ {_ACTOR} }}
  ... on ConvertToDraftEvent {{ {_ACTOR} }}
  ... on HeadRefForcePushedEvent {{ {_ACTOR} }}
  ... on HeadRefDeletedEvent {{ {_ACTOR} }}
  ... on ReviewDismissedEvent {{ {_ACTOR} }}
"""
_FIELDS = "__typename id url number title body createdAt author { login } repository { nameWithOwner }"
_REVIEW_FIELDS = f"""
  reviews(first: {_REVIEWS}, author: $login) {{
    totalCount
    nodes {{ submittedAt state body url comments(first: 100) {{ nodes {{ author {{ login }} body path createdAt url }} }} }}
  }}
"""


def _timeline(items: str) -> str:
    return f"timelineItems(since: $since, first: 100, after: $after) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {items} }} }}"


_NODES_QUERY = (
    "query($ids: [ID!]!, $since: DateTime!, $after: String, $login: String!) { nodes(ids: $ids) { "
    f"... on Issue {{ {_FIELDS} {_timeline(_COMMON_ITEMS)} }} "
    f"... on PullRequest {{ {_FIELDS} mergedAt {_REVIEW_FIELDS} {_timeline(_COMMON_ITEMS + _PULL_ITEMS)} }} "
    "} }"
)

#: timeline の操作 → kind。
_EVENTS = {
    "AssignedEvent": "assigned", "UnassignedEvent": "unassigned", "ClosedEvent": "closed", "ReopenedEvent": "reopened",
    "LabeledEvent": "labeled", "UnlabeledEvent": "unlabeled", "RenamedTitleEvent": "renamed_title", "MilestonedEvent": "milestoned",
    "DemilestonedEvent": "demilestoned", "MergedEvent": "merged", "ReviewRequestedEvent": "review_requested",
    "ReviewRequestRemovedEvent": "review_request_removed", "ReadyForReviewEvent": "ready_for_review",
    "ConvertToDraftEvent": "converted_to_draft", "HeadRefForcePushedEvent": "force_pushed", "HeadRefDeletedEvent": "branch_deleted",
    "ReviewDismissedEvent": "review_dismissed",
}
#: 他の人の出来事のうち、本人の操作の契機・結果として残すもの。
_OTHER_KINDS = {"comment", "review", "review_requested", "merged", "closed", "reopened", "ready_for_review"}
#: GitHub が自動で行った出来事の actor。merge した人の名前で記録されるため、本人の操作と分ける。
AUTOMATIC = "GitHub (自動)"
#: merge の後この秒数以内の branch の削除は、repository の設定による自動削除とみなす。
_AUTO_DELETE_SECONDS = 60


def github_token() -> str:
    """GITHUB_TOKEN / GH_TOKEN の先頭の設定値。"""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


class _GitHub:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
        self._limit = asyncio.Semaphore(_CONCURRENCY)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        status, body = await request_json(self._client, "GET", f"{GITHUB_API}{path}", headers=self._headers, params=params)
        if status >= 400 or not isinstance(body, dict):
            message = body.get("message") if isinstance(body, dict) else None
            raise FetchError(f"GET {path} failed: HTTP {status}" + (f": {message}" if message else ""))
        return body

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        async with self._limit:
            status, body = await request_json(self._client, "POST", f"{GITHUB_API}/graphql", headers=self._headers,
                                              json={"query": query, "variables": variables})
        errors = body.get("errors") if isinstance(body, dict) else None
        if status >= 400 or errors or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            detail = "; ".join(str(as_dict(error).get("message")) for error in as_list(errors)) or (body.get("message") if isinstance(body, dict) else None)
            raise FetchError(f"GraphQL failed: HTTP {status}" + (f": {detail}" if detail else ""))
        return as_dict(body.get("data"))

    async def search(self, kind: str, query: str, notes: list[str]) -> list[dict[str, Any]]:
        """検索の全件(1000 件まで)。2 page 目以降は並行に読む。"""
        first = await self.get(f"/search/{kind}", {"q": query, "per_page": _PAGE, "page": 1})
        raw_total = first.get("total_count")
        total = raw_total if isinstance(raw_total, int) else 0
        pages = -(-min(total, _SEARCH_CAP) // _PAGE)
        rest = await asyncio.gather(*(self.get(f"/search/{kind}", {"q": query, "per_page": _PAGE, "page": page}) for page in range(2, pages + 1)))
        if any(body.get("incomplete_results") for body in (first, *rest)):
            notes.append(f"github search {query!r}: GitHub reported incomplete results")
        if total > _SEARCH_CAP:
            notes.append(f"github search {query!r}: {total} results, only the first {_SEARCH_CAP} are returned")
        return [item for body in (first, *rest) for item in as_list(body.get("items")) if isinstance(item, dict)]


def _login(node: dict[str, Any]) -> str:
    return str(as_dict(node.get("actor") or node.get("author")).get("login") or "")


def _event_text(kind: str, node: dict[str, Any]) -> str:
    if kind in ("assigned", "unassigned"):
        return f"assignee: {as_dict(node.get('assignee')).get('login') or ''}"
    if kind in ("labeled", "unlabeled"):
        return f"label: {as_dict(node.get('label')).get('name') or ''}"
    if kind == "renamed_title":
        return f"{node.get('previousTitle') or ''} → {node.get('currentTitle') or ''}"
    if kind in ("milestoned", "demilestoned"):
        return f"milestone: {node.get('milestoneTitle') or ''}"
    if kind in ("review_requested", "review_request_removed"):
        reviewer = as_dict(node.get("requestedReviewer"))
        return f"reviewer: {reviewer.get('login') or reviewer.get('name') or ''}"
    return ""


def _first_line(text: Any) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0] if lines else ""


class _Timelines:
    """Issue・PR ごとに、本人の操作と、その契機・結果になった他の人の出来事を Episode に加える。"""

    def __init__(self, api: _GitHub, login: str, window: Window, notes: list[str]) -> None:
        self._api, self._login, self._window, self._notes = api, login.lower(), window, notes
        self._since = window.since.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.episodes = Episodes()
        #: PR の commit と既定 branch の commit の重複除き。
        self.commits: set[str] = set()

    def _actor(self, login: str) -> str:
        return SELF if login.lower() == self._login else login

    def _add(self, target: dict[str, Any], event: Event) -> None:
        repo = str(as_dict(target.get("repository")).get("nameWithOwner") or "")
        kind = "PR" if target.get("__typename") == "PullRequest" else "Issue"
        self.episodes.add(f"github:{repo}#{target.get('number')}", "github", repo, f"{kind} #{target.get('number')} {target.get('title') or ''}",
                          event, url=target.get("url"))

    def _items(self, target: dict[str, Any], nodes: list[Any]) -> None:
        for raw in nodes:
            node = as_dict(raw)
            typename = node.get("__typename")
            if typename == "PullRequestCommit":
                commit = as_dict(node.get("commit"))
                at = parse_time(commit.get("committedDate"))
                author = str(as_dict(as_dict(commit.get("author")).get("user")).get("login") or "")
                oid = str(commit.get("oid") or "")
                if at and self._window.contains(at) and self._actor(author) == SELF and oid not in self.commits:
                    self.commits.add(oid)
                    self._add(target, Event(at, SELF, "commit", _first_line(commit.get("message")), node.get("url")))
                continue
            actor = self._actor(_login(node))
            if typename == "PullRequestReview":
                # 本人の review は reviews(author:) から読む。他の人の review は本人の対応の契機になる。
                at = parse_time(node.get("submittedAt"))
                if actor != SELF and at and self._window.contains(at):
                    self._add(target, Event(at, actor, "review", f"[{node.get('state')}] {node.get('body') or ''}".strip(), node.get("url")))
                continue
            at = parse_time(node.get("createdAt"))
            kind = "comment" if typename == "IssueComment" else _EVENTS.get(str(typename))
            if at is None or kind is None or not self._window.contains(at):
                continue
            merged = parse_time(target.get("mergedAt"))
            if kind == "branch_deleted" and merged and 0 <= (at - merged).total_seconds() <= _AUTO_DELETE_SECONDS:
                # merge に続く自動削除は merge から分かるため残さない。
                continue
            closer = as_dict(node.get("closer"))
            if kind == "closed" and closer:
                # PR・commit の closing keyword による自動の close は、その PR・commit の結果として残す。
                actor = AUTOMATIC
            if actor != SELF and kind not in _OTHER_KINDS:
                continue
            text = str(node.get("body") or "") if kind == "comment" else _event_text(kind, node)
            if kind == "closed" and closer:
                source = as_dict(closer.get("repository")).get("nameWithOwner")
                text = f"closed by PR {source}#{closer.get('number')}" if closer.get("__typename") == "PullRequest" else f"closed by commit {str(closer.get('oid'))[:12]}"
            self._add(target, Event(at, actor, kind, text, node.get("url") if kind == "comment" else None))

    def _reviews(self, target: dict[str, Any]) -> None:
        reviews = as_dict(target.get("reviews"))
        if isinstance(reviews.get("totalCount"), int) and reviews["totalCount"] > _REVIEWS:
            self._notes.append(f"github {target.get('url')}: {reviews['totalCount']} reviews by the person, only the first {_REVIEWS} are read")
        for raw in as_list(reviews.get("nodes")):
            review = as_dict(raw)
            at, body = parse_time(review.get("submittedAt")), str(review.get("body") or "")
            # 本文のない COMMENTED は行の comment の入れ物なので、行の comment の側で数える。
            if at and self._window.contains(at) and (body or review.get("state") != "COMMENTED"):
                self._add(target, Event(at, SELF, "review", f"[{review.get('state')}] {body}".strip(), review.get("url")))
            for raw_comment in as_list(as_dict(review.get("comments")).get("nodes")):
                comment = as_dict(raw_comment)
                created = parse_time(comment.get("createdAt"))
                if created and self._window.contains(created) and self._actor(_login(comment)) == SELF:
                    self._add(target, Event(created, SELF, "review_comment", f"{comment.get('path') or ''}: {comment.get('body') or ''}",
                                            comment.get("url")))

    async def target(self, target: dict[str, Any]) -> None:
        """一つの Issue・PR の作成・review・timeline。100 件を超える timeline は続きを読む。"""
        created = parse_time(target.get("createdAt"))
        if created:
            # 作成は期間の外でも、後の出来事の起点として残す。本文は本人が期間内に作ったときだけ残す。
            actor = self._actor(_login(target))
            body = f"{target.get('title') or ''}\n{target.get('body') or ''}" if actor == SELF and self._window.contains(created) else ""
            self._add(target, Event(created, actor, "opened", body.strip()))
        self._reviews(target)
        timeline = as_dict(target.get("timelineItems"))
        while True:
            self._items(target, as_list(timeline.get("nodes")))
            info = as_dict(timeline.get("pageInfo"))
            if not info.get("hasNextPage"):
                return
            data = await self._api.graphql(_NODES_QUERY, {"ids": [target["id"]], "since": self._since, "after": info.get("endCursor"),
                                                          "login": self._login})
            timeline = as_dict(as_dict(next(iter(as_list(data.get("nodes"))), None)).get("timelineItems"))

    async def batch(self, ids: list[str]) -> None:
        data = await self._api.graphql(_NODES_QUERY, {"ids": ids, "since": self._since, "after": None, "login": self._login})
        targets = [as_dict(raw) for raw in as_list(data.get("nodes")) if as_dict(raw).get("id")]
        await asyncio.gather(*(self.target(target) for target in targets))


def _owner(item: dict[str, Any]) -> str:
    """検索結果の repository の所有者(Issue・PR は repository_url、commit は repository.owner)。"""
    owner = as_dict(as_dict(item.get("repository")).get("owner")).get("login")
    return str(owner or str(item.get("repository_url") or "").rstrip("/").rsplit("/", 2)[-2:][0]).lower()


async def fetch_github(client: httpx.AsyncClient, person: str, window: Window, *, exclude_personal_repos: bool = False) -> Fetched:
    """``exclude_personal_repos`` が真なら、本人の login が所有する repository(個人の repository)を除く。"""
    token = github_token()
    if not token:
        raise FetchError("GITHUB_TOKEN is required")
    api = _GitHub(client, token)
    login = str((await api.get("/user")).get("login") or "") if person == "me" else person
    if not login:
        raise FetchError("GET /user returned no login")
    notes: list[str] = []
    since_day, until_day = window.since.date().isoformat(), window.until.date().isoformat()
    involved, reviewed, commits = await asyncio.gather(
        api.search("issues", f"involves:{login} updated:>={since_day}", notes),
        api.search("issues", f"reviewed-by:{login} updated:>={since_day}", notes),
        api.search("commits", f"author:{login} committer-date:{since_day}..{until_day}", notes),
    )
    if exclude_personal_repos:
        involved, reviewed, commits = ([item for item in found if _owner(item) != login.lower()] for found in (involved, reviewed, commits))
    ids = list(dict.fromkeys(str(item["node_id"]) for item in (*involved, *reviewed) if item.get("node_id")))
    timelines = _Timelines(api, login, window, notes)
    await asyncio.gather(*(timelines.batch(ids[start:start + _BATCH]) for start in range(0, len(ids), _BATCH)))
    # 既定 branch の commit は、PR の timeline で読んだものを sha で除き、repository と日ごとにまとめる。
    for commit in commits:
        sha = str(commit.get("sha") or "")
        detail = as_dict(commit.get("commit"))
        at = parse_time(as_dict(detail.get("committer")).get("date"))
        if sha in timelines.commits or at is None or not window.contains(at):
            continue
        timelines.commits.add(sha)
        repo, day = str(as_dict(commit.get("repository")).get("full_name") or ""), at.astimezone(_JST).strftime("%Y-%m-%d")
        timelines.episodes.add(f"github:{repo}:commits:{day}", "github", repo, f"既定 branch への commit {day}",
                               Event(at, SELF, "commit", _first_line(detail.get("message")), commit.get("html_url")))
    return Fetched(person=login, episodes=timelines.episodes.values(), notes=notes)
