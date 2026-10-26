"""References and related work (docs/design.md §9).

Every conversation is scanned for tickets, GitHub PR/issue links, branches, commits and
URLs. Each hit is stored with the message it came from, the matched span and the method,
so a link can always be explained and, if wrong, switched off. Only *linkable* references
connect conversations:

  ticket   key matches the configured regex and appears in user/assistant text (not only
           in tool output, where changelogs and docs mention unrelated keys)
  pr/issue a full URL on a configured GitHub host, or owner/repo#n; a bare "PR 123" only
           when the source recorded the conversation's own git_origin_url
  branch   the source's recorded branch (not a default branch), or an explicit
           `checkout -b X` / `--head X` in the text
  commit, url  never link; kept for display and search

Related work is computed directly (two conversations are related iff they share a linkable
key), never transitively, so one broad session cannot glue a whole repository together.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from convo_index import settings as S
from convo_index.model import Conversation, KIND_TEXT

LINKING_KINDS = ("ticket", "pr", "issue", "branch")
NEVER_LINK = ("commit", "url")   # kept for display/search only; no override can link them
URL_CAP = 50           # distinct urls stored per conversation (display/search only)
EXTRACTOR_VERSION = 2  # bump when extraction rules change: existing conversations are re-scanned
META_EXTRACTOR = "references_extractor"

URL_STRIP = ".,;:!?'\"`>)]}*_~"
COMMIT_CAP = 50
_URL_RE = re.compile(r"https?://[^\s<>()\"'\]`]+")
_GH_PATH_RE = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(pull|issues)/(\d+)(?=$|[/?#])")
# owner/repo#n: not preceded by a path character (src/pkg/file.py#12 is a file:line, not a repo)
_SHORT_REF_RE = re.compile(r"(?<![\w/.\-])([A-Za-z0-9_-][A-Za-z0-9_.-]*)/([A-Za-z0-9_-][A-Za-z0-9_.-]*)#(\d+)\b")
_CODE_EXT_RE = re.compile(r"\.(py|pyi|ts|tsx|js|jsx|mjs|go|rs|rb|java|kt|swift|c|h|cpp|hpp|cs|php|md|rst|txt|json|ya?ml|toml|ini|cfg|html|css|scss|sql|sh|bash|zsh|lock)$", re.IGNORECASE)
_BARE_PR_RE = re.compile(r"\b(?:PR|pull request|MR)\s*#?\s*(\d{1,7})\b", re.IGNORECASE)
_COMMIT_RE = re.compile(r"\b(?:commit|sha|rev(?:ision)?)\b[^0-9a-f]{0,12}([0-9a-f]{7,40})\b", re.IGNORECASE)
# `git checkout -b X` / `git switch -c X` (a git command, not any "--head" on a line) and
# `gh pr create --head [owner:]X`; quotes may open the argument and are not captured.
_CHECKOUT_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:checkout\s+-b|switch\s+-c)\s+['\"]?([A-Za-z0-9._/-]+)")
_GH_HEAD_RE = re.compile(r"\bgh\s+pr\b[^\n]*?--head[=\s]+['\"]?(?:[A-Za-z0-9_-]+:)?([A-Za-z0-9._/-]+)")


@dataclass
class Ref:
    kind: str
    key: str
    namespace: str
    message_seq: int | None
    span: str
    method: str            # regex | url | source | legacy
    confidence: float
    linkable: bool


def _host_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def repo_from_origin(origin_url, hosts):
    """'https://github.com/o/r.git' | 'git@github.com:o/r.git' | 'ssh://git@github.com:22/o/r.git' |
    'https://user:token@github.com/o/r' -> 'github.com/o/r' or None (credentials/ports dropped)."""
    if not origin_url:
        return None
    url = origin_url.strip()
    if "://" in url:
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        host, path = (parts.hostname or "").lower(), parts.path
    else:
        m = re.match(r"^(?:[A-Za-z0-9_.-]+@)?([A-Za-z0-9.-]+):(.+)$", url)   # scp-like git@host:o/r.git
        if not m:
            return None
        host, path = m.group(1).lower(), "/" + m.group(2)
    if host not in hosts:
        return None
    segs = [x for x in path.split("/") if x]
    if len(segs) < 2:
        return None
    repo = segs[1][:-4] if segs[1].endswith(".git") else segs[1]
    return f"{host}/{segs[0].lower()}/{repo.lower()}"


def branch_key(namespace, branch):
    """Branches are scoped to their repository/project so `dev` in two projects never links.
    The `@` is always present, so a branch called `SAAS-1` can never share a key with the
    ticket `SAAS-1` (related work matches on key)."""
    return f"{namespace or ''}@{branch}"


def gh_key(host, owner, repo, n):
    """One spelling per GitHub PR/issue: host, owner and repository are case-insensitive on
    GitHub and the number has no leading zeros, so `Org/Repo#08` and `org/repo#8` connect."""
    return f"{host.lower()}/{owner.lower()}/{repo.lower()}#{int(n)}"


def branch_label(key):
    return key.split("@", 1)[1] if "@" in key else key


def extract(conv: Conversation, settings=None) -> list[Ref]:
    settings = settings or S.load()
    refs_cfg = settings["references"]
    host_list = [h.lower() for h in (refs_cfg.get("github_hosts") or ["github.com"])]
    hosts = set(host_list)
    defaults = set(refs_cfg.get("default_branches") or ["main", "master", "develop", "HEAD"])
    ticket_re = re.compile(refs_cfg["ticket_regex"])
    block = set(refs_cfg.get("ticket_prefix_blocklist") or [])
    tracker = _host_of(refs_cfg.get("ticket_url") or "")
    own_repo = repo_from_origin(conv.git_origin_url, hosts)
    # owner/repo#n names no host: the conversation's own origin host wins, else the first
    # configured host (config order, not alphabetical)
    short_host = own_repo.split("/")[0] if own_repo else host_list[0]

    found: dict[tuple, Ref] = {}

    def add(kind, key, namespace, seq, span, method, conf, linkable):
        # pr and issue share GitHub's number space and the same key format: one row per key,
        # kind "pr" wins once any mention shows it is a pull request.
        k = ("gh", key) if kind in ("pr", "issue") else (kind, key)
        prev = found.get(k)
        if prev is None:
            found[k] = Ref(kind, key, namespace, seq, span[:200], method, conf, linkable)
            return True
        if kind == "pr" and prev.kind == "issue":
            prev.kind = "pr"
        if linkable and not prev.linkable:
            prev.linkable, prev.method, prev.span, prev.message_seq = True, method, span[:200], seq
        return False

    urls_kept = commits_seen = 0
    for m in conv.messages:
        text = m.text or ""
        in_prose = m.kind == KIND_TEXT and m.role in ("user", "assistant")
        # tool calls are where agents run git/gh: a reference there is the agent's own work;
        # tool *results* (docs, changelogs, listings) mention unrelated things: stored, not linked
        agent_side = in_prose or m.kind == "tool_call"
        # tickets: linkable only from prose (docs/design.md §9)
        for mt in ticket_re.finditer(text):
            key = mt.group(0)
            if key.split("-")[0].upper() in block:
                continue
            add("ticket", key, tracker, m.seq, key, "regex", 1.0 if in_prose else 0.6, in_prose)
        # URLs: GitHub pull/issue links are linkable from any message; other URLs are display-only
        for mu in _URL_RE.finditer(text):
            url = mu.group(0).rstrip(URL_STRIP)
            host = _host_of(url)
            path = urlparse(url).path if host else ""
            gh = _GH_PATH_RE.match(path) if host in hosts else None
            if gh:
                owner, repo, kind, n = gh.groups()
                add("pr" if kind == "pull" else "issue", gh_key(host, owner, repo, n), f"{host}/{owner.lower()}/{repo.lower()}",
                    m.seq, url, "url", 1.0, True)
            elif agent_side and urls_kept < URL_CAP:
                if add("url", url, host, m.seq, url, "regex", 0.5, False):
                    urls_kept += 1          # repeats of one URL do not eat the cap
        # owner/repo#n and bare PR numbers: from prose and tool calls (linkable), from tool
        # results as display-only evidence
        if m.kind in (KIND_TEXT, "tool_call", "tool_result"):
            for ms in _SHORT_REF_RE.finditer(text):
                owner, repo, n = ms.groups()
                if _CODE_EXT_RE.search(repo) or repo.count(".") > 1:
                    continue   # src/file.py#12 is a file:line, not a repository
                add("issue", gh_key(short_host, owner, repo, n), f"{short_host}/{owner.lower()}/{repo.lower()}", m.seq,
                    ms.group(0), "regex", 0.8 if agent_side else 0.4, agent_side)
            for mb in _BARE_PR_RE.finditer(text):
                n = int(mb.group(1))
                if own_repo:
                    add("pr", f"{own_repo}#{n}", own_repo, m.seq, mb.group(0), "regex", 0.7 if agent_side else 0.4, agent_side)
                else:
                    add("pr", f"#{n}", "", m.seq, mb.group(0), "regex", 0.3, False)   # ambiguous: never links
        if in_prose:
            for mc in _COMMIT_RE.finditer(text):
                if commits_seen >= COMMIT_CAP:
                    break
                commits_seen += 1
                add("commit", mc.group(1)[:12], "", m.seq, mc.group(0), "regex", 0.5, False)
        # explicit branch creation: in prose or in a tool call (that is where agents run git)
        if agent_side:
            for rx in (_CHECKOUT_RE, _GH_HEAD_RE):
                for mc in rx.finditer(text):
                    br = mc.group(1).rstrip(".,;:'\"/")
                    if br and br not in defaults:
                        ns = conv.project_root or conv.cwd or ""
                        add("branch", branch_key(ns, br), ns, m.seq, mc.group(0), "regex", 0.9, bool(ns))

    br = (conv.git_branch or "").strip()
    if br and br not in defaults:
        ns = conv.project_root or conv.cwd or ""
        add("branch", branch_key(ns, br), ns, None, br, "source", 1.0, bool(ns))   # no project: a bare name links nothing
    return list(found.values())


def extraction_fingerprint(settings=None):
    """Extractor version + the [references] configuration: when either changes, every stored
    conversation is re-scanned (ensure_current) since the indexer skips unchanged sources."""
    settings = settings or S.load()
    cfg = settings["references"]
    return f"v{EXTRACTOR_VERSION}|" + json.dumps(cfg, sort_keys=True, default=str)
