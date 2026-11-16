"""Building blocks for the playground's fictional conversations.

Every person, company, repository, ticket and URL here is made up. Content modules describe a
conversation with `conv()`, `U()` (user), `A()` (assistant) and `T()` (tool call); `conv()` fills in
timestamps and token usage deterministically, so the baked database is the same on every build.
"""
from datetime import datetime, timedelta, timezone

HOME = "/home/dev"

# name -> (working directory, GitHub URL or None)
REPOS = {
    "acme-shop": (f"{HOME}/work/acme-shop", "https://github.com/acme-labs/acme-shop"),
    "ledgerly": (f"{HOME}/work/ledgerly", "https://github.com/acme-labs/ledgerly"),
    "infra-playbooks": (f"{HOME}/work/infra-playbooks", "https://github.com/acme-labs/infra-playbooks"),
    "tidewatch": (f"{HOME}/code/tidewatch", "https://github.com/tidewatch-rs/tidewatch"),
    "garden-planner": (f"{HOME}/code/garden-planner", "https://github.com/plotwise/garden-planner"),
}


def U(text):
    return {"role": "user", "text": text}


def A(text="", thinking=None, tools=()):
    turn = {"role": "assistant", "text": text, "tools": list(tools)}
    if thinking:
        turn["thinking"] = thinking
    return turn


def T(name, inp, output=""):
    """A tool call. A string input is a shell command (or a path for read-style tools)."""
    if isinstance(inp, str):
        key = "file_path" if name.lower() in ("read", "edit", "write", "view_file") else "command"
        inp = {key: inp}
    return {"name": name, "input": inp, "output": output}


def S(problem, solution, learning="", resolved="Yes"):
    """A summary as the summarizer writes it; resolved is "Yes", "No" or "Partial"."""
    return {"problem": problem, "solution": solution, "learning": learning, "is_resolved": resolved}


def conv(cid, source, model, start, repo, title, turns, branch=None, summary=None):
    cwd, url = REPOS[repo]
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    # sizes like real sessions: a system prompt and repository context are cached up front
    at, context, out_turns = 0, 24000, []
    for i, turn in enumerate(turns):
        turn = dict(turn)
        if turn["role"] == "user":
            at += 45 if i else 0
        else:
            at += 20 + 25 * len(turn["tools"])
            # a growing context window: what real sessions look like in the cost tables
            text_len = len(turn["text"]) + len(turn.get("thinking") or "") + sum(len(t["output"]) for t in turn["tools"])
            context += 6000 + 2 * text_len
            turn["usage"] = {"input": 1500 + 200 * i, "cache_read": context, "cache_write": 18000 if i == 1 else 2500,
                             "output": 400 + len(turn["text"]) + 350 * len(turn["tools"])}
        turn["at"] = at
        out_turns.append(turn)
    provider = "deepseek" if model.startswith("deepseek") else None   # the writers guess the rest from the name
    return {"id": cid, "source": source, "model": model, "provider": provider, "title": title, "cwd": cwd, "branch": branch,
            "repo_url": url, "start": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "turns": out_turns,
            "summary": summary, "_end": (t0 + timedelta(seconds=at)).strftime("%Y-%m-%dT%H:%M:%SZ")}
