"""Cost estimates at query time (docs/design.md §10). Nothing is stored.

Two numbers, never merged:
  reported_cost   what the tool itself recorded (Opencode's `cost`); NULL means "not reported".
  estimated_cost  priced buckets x list rates from pricing.toml (seed shipped in the package,
                  ~/.config/convo-index/pricing.toml overrides and extends it).
Every aggregate carries `coverage`: the share of token-bearing requests whose model has a
rate. Exact model id, then explicit alias, else unpriced — no prefix guessing. Models from a
provider in `zero_cost_providers` (local runtimes) are priced at 0 and count as priced.
Everything is an API-equivalent estimate; subscriptions are not billed this way.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from convo_index import settings as S
from convo_index.config import PACKAGE_DIR

SEED_PATH = PACKAGE_DIR / "pricing.toml"
BUCKETS = ("input_uncached", "cache_read", "cache_write", "cache_write_1h", "output")
RATE_KEYS = {"input_uncached": "input", "cache_read": "cache_read", "cache_write": "cache_write",
             "cache_write_1h": "cache_write_1h", "output": "output"}
RATE_FIELDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")


class Pricing:
    def __init__(self, models, aliases, zero_cost_providers, revision, user_path=None, errors=None):
        self.models = models
        self.aliases = aliases
        self.zero_cost_providers = set(zero_cost_providers or [])
        self.revision = revision
        self.user_path = user_path
        self.errors = list(errors or [])   # problems in the user file: reported by doctor, never fatal

    def rates(self, model, provider=None):
        """{input, cache_read, cache_write, output} per million tokens, or None when unpriced."""
        if provider and provider in self.zero_cost_providers:
            return {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "cache_write_1h": 0.0, "output": 0.0}
        if not model:
            return None
        key = model if model in self.models else self.aliases.get(model)
        r = self.models.get(key) if key else None
        if not isinstance(r, dict):
            return None
        inp = float(r.get("input", 0.0))
        cw = float(r.get("cache_write", inp))
        return {"input": inp, "output": float(r.get("output", 0.0)),
                "cache_read": float(r.get("cache_read", inp)), "cache_write": cw,
                "cache_write_1h": float(r.get("cache_write_1h", cw))}   # unsplit vendors: same as cache_write

    def estimate(self, model, provider, tokens):
        """USD for one request or one aggregate of the same model; None when unpriced.
        `tokens` maps bucket name -> count (None = not reported = 0)."""
        r = self.rates(model, provider)
        if r is None:
            return None
        return sum((tokens.get(b) or 0) * r[RATE_KEYS[b]] for b in BUCKETS) / 1_000_000


def user_path(settings=None):
    settings = settings or S.load()
    p = (settings.get("pricing") or {}).get("file") or ""
    return Path(p).expanduser() if p else S.CONFIG_DIR / "pricing.toml"


def _read(path, errors=None):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        if errors is not None:
            errors.append(f"{path}: {e}")
        return {}


def _validate(models, aliases, errors, where):
    """Drop entries that cannot be priced and say why: a model entry must be a table with
    numeric input/output (other rates numeric when given); an alias must name a model."""
    for name in list(models):
        r = models[name]
        if not isinstance(r, dict):
            errors.append(f"{where}: [models].\"{name}\" must be a table like {{ input = 1.0, output = 5.0 }}; ignored")
            del models[name]; continue
        bad = [k for k in RATE_FIELDS if k in r and not isinstance(r[k], (int, float))]
        if bad or "input" not in r or "output" not in r:
            errors.append(f"{where}: [models].\"{name}\" needs numeric input and output"
                          + (f" (non-numeric: {', '.join(bad)})" if bad else "") + "; ignored")
            del models[name]
    for a, target in list(aliases.items()):
        if not isinstance(target, str) or target not in models:
            errors.append(f"{where}: [aliases].\"{a}\" points to unknown model \"{target}\"; ignored")
            del aliases[a]


def load(settings=None):
    errors = []
    seed = _read(SEED_PATH, errors)
    up = user_path(settings)
    user = _read(up, errors) if up.exists() else {}
    models = dict(seed.get("models") or {})
    aliases = dict(seed.get("aliases") or {})
    _validate(models, aliases, errors, str(SEED_PATH))
    # the user's entries are validated on their own first: a broken override leaves the seed entry in place
    umodels = dict(user.get("models") or {}) if isinstance(user.get("models"), dict) else {}
    ualiases = dict(user.get("aliases") or {}) if isinstance(user.get("aliases"), dict) else {}
    _validate(umodels, {}, errors, str(up))
    models.update(umodels)
    for a, target in list(ualiases.items()):
        if isinstance(target, str) and target in models:
            aliases[a] = target
        else:
            errors.append(f"{up}: [aliases].\"{a}\" points to unknown model \"{target}\"; ignored")
    zero = list(seed.get("zero_cost_providers") or []) + list(user.get("zero_cost_providers") or []
                                                                if isinstance(user.get("zero_cost_providers"), list) else [])
    revision = user.get("revision") or seed.get("revision") or "unknown"
    return Pricing(models, aliases, zero, revision, up if up.exists() else None, errors)


def aggregate(rows, pricing):
    """Roll up usage rows grouped by (provider, model) into one summary.
    `rows`: iterable of (provider, model, requests, input_uncached, cache_read, cache_write,
    output, reasoning, reported_cost_sum, reported_count[, cache_write_1h[, token_requests]]);
    `token_requests` is how many of the group's requests carried tokens (all, when absent)."""
    out = {b: 0 for b in BUCKETS}
    out.update({"reasoning": 0, "requests": 0, "token_requests": 0, "priced_requests": 0, "reported_requests": 0,
                "estimated_cost": 0.0, "reported_cost": 0.0, "unpriced_models": []})
    for provider, model, n, inp, cr, cw, outp, reas, rep_sum, rep_n, *rest in rows:
        cw1 = rest[0] if rest else 0
        token_n = rest[1] if len(rest) > 1 and rest[1] is not None else n
        tokens = {"input_uncached": inp or 0, "cache_read": cr or 0, "cache_write": cw or 0, "cache_write_1h": cw1 or 0,
                  "output": outp or 0}
        for b in BUCKETS:
            out[b] += tokens[b]
        out["reasoning"] += reas or 0
        out["requests"] += n or 0
        out["reported_requests"] += rep_n or 0
        out["reported_cost"] += rep_sum or 0.0
        if sum(tokens.values()) == 0:
            continue                      # token-less rows (Claude Code synthetic turns) neither cost nor count
        out["token_requests"] += token_n or 0
        est = pricing.estimate(model, provider, tokens)
        if est is None:
            out["unpriced_models"].append(model or "?")
            continue
        out["estimated_cost"] += est
        out["priced_requests"] += token_n or 0
    out["tokens"] = sum(out[b] for b in BUCKETS)
    # coverage: share of token-bearing requests that had a rate (docs/design.md §10)
    out["coverage"] = round(out["priced_requests"] / out["token_requests"], 3) if out["token_requests"] else None
    out["estimated_cost"] = round(out["estimated_cost"], 4)
    out["reported_cost"] = round(out["reported_cost"], 4) if out["reported_requests"] else None
    out["unpriced_models"] = sorted(set(out["unpriced_models"]))
    return out


USAGE_GROUP_SQL = """SELECT u.provider, u.model, COUNT(*), SUM(u.input_uncached), SUM(u.cache_read),
                            SUM(u.cache_write), SUM(u.output), SUM(u.reasoning),
                            SUM(u.reported_cost_usd), SUM(u.reported_cost_usd IS NOT NULL), SUM(u.cache_write_1h),
                            SUM(COALESCE(u.input_uncached, 0) + COALESCE(u.cache_read, 0) + COALESCE(u.cache_write, 0)
                                + COALESCE(u.cache_write_1h, 0) + COALESCE(u.output, 0) > 0)
                     FROM usage_records u {join} {where} GROUP BY u.provider, u.model"""


def usage_for_conversation(conn, cid, pricing=None):
    """Tokens and cost of one conversation (sqlite connection)."""
    pricing = pricing or load()
    rows = conn.execute(USAGE_GROUP_SQL.format(join="", where="WHERE u.conversation_id=?"), (cid,)).fetchall()
    out = aggregate(rows, pricing)
    out["models"] = sorted({m for _, m, *_ in rows if m})
    return out


def unpriced_models(conn, pricing=None):
    """Distinct (provider, model) pairs in usage_records with tokens but no rate."""
    pricing = pricing or load()
    rows = conn.execute(
        """SELECT provider, model, COUNT(*) FROM usage_records
           WHERE COALESCE(input_uncached,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0)+COALESCE(cache_write_1h,0)+COALESCE(output,0) > 0
           GROUP BY provider, model""").fetchall()
    return [{"provider": p, "model": m, "requests": n} for p, m, n in rows if pricing.rates(m, p) is None]


def fmt_usd(x):
    if x is None:
        return "—"
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


def fmt_tokens(n):
    n = n or 0
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return str(n)
