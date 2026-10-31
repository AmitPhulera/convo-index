import os
from pathlib import Path

# Repository / install root (one level above this package). The DB and archive live here
# unless CONVO_INDEX_DB / CONVO_INDEX_ARCHIVE point elsewhere.
PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
# Durable data lives next to the checkout when running from a git checkout (the developer
# layout), otherwise in the user's data dir: a wheel install's site-packages is neither
# writable nor durable across virtualenv rebuilds.
_IN_CHECKOUT = (ROOT / "pyproject.toml").exists()


def user_dir(kind, windows=None):
    """The platform's per-user directory for `kind` ("data" or "config"): XDG on POSIX
    (`~/.local/share`, `~/.config`), %LOCALAPPDATA% / %APPDATA% on Windows. An XDG variable
    wins everywhere, so one setting relocates the tool on any platform."""
    env, posix, win = {"data": ("XDG_DATA_HOME", Path.home() / ".local" / "share", "LOCALAPPDATA"),
                       "config": ("XDG_CONFIG_HOME", Path.home() / ".config", "APPDATA")}[kind]
    if os.environ.get(env):
        return Path(os.environ[env])
    windows = os.name == "nt" if windows is None else windows
    if windows and os.environ.get(win):
        return Path(os.environ[win])
    return posix


DATA_DIR = ROOT if _IN_CHECKOUT else user_dir("data") / "convo-index"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
DB_PATH = Path(os.environ.get("CONVO_INDEX_DB", DATA_DIR / "convos.db"))
ARCHIVE_DIR = Path(os.environ.get("CONVO_INDEX_ARCHIVE", DATA_DIR / "archive"))
STATIC_DIR = PACKAGE_DIR / "static"

HOST = "127.0.0.1"
PORT = 9753

# Embeddings. The default is in-process (fastembed): the quantised nomic model
# (~130 MB) is fetched from Hugging Face into EMBED_CACHE_DIR on first use. `openai_compat`
# against an Ollama server is the alternative; both are 768-dimensional.
FASTEMBED_MODEL = "nomic-ai/nomic-embed-text-v1.5-Q"
EMBED_CACHE_DIR = DATA_DIR / "models"
OLLAMA_URL = "http://localhost:11434/v1"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768

# Max chars of transcript sent to the summarizer (head + tail of the conversation).
SUMMARY_HEAD_CHARS = 9000
SUMMARY_TAIL_CHARS = 5000

# Leading text of the Haiku classification prompt. Shared so the indexer can
# recognize and skip the tool's own `claude -p` sessions (which Claude Code writes
# into ~/.claude/projects/) and avoid indexing them as real conversations.
CLASSIFY_PROMPT_PREFIX = "Classify this developer conversation"
# Same idea for the summarizer prompt.
SUMMARY_PROMPT_PREFIX = "You are summarizing a coding-assistant conversation transcript"
# The other way round for a handoff: its new session is headless too (`claude -p`), and these
# openings of its prompt mark it as a real conversation to index, linked to the original.
HANDOFF_PROMPT_PREFIXES = ("You are taking over a coding conversation", "You are reviewing a coding conversation")

# Jira-style ticket key pattern.
TICKET_RE = r"\b[A-Z]{2,}-\d+\b"

# Prefixes that match TICKET_RE but are not Jira tickets (encodings, hashes, etc.).
TICKET_PREFIX_BLOCKLIST = {"UTF", "SHA", "ISO", "BASE", "MD", "RFC", "ASCII", "EC2", "S3", "IPV",
                           "HTTP", "TLS", "SSL", "AES", "ARM", "X", "OAUTH", "CVE", "GPT", "RSA", "HMAC",
                           "COVID", "IEEE", "PEP", "UUID", "ID", "TCP", "UDP", "IPV4", "IPV6", "WCAG", "ES"}

# Max chars of the first message sent to the classifier.
HAIKU_CONTEXT_CHARS = 1200
