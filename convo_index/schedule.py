"""Background jobs through the platform's own scheduler — a LaunchAgent on macOS, systemd
user units on Linux, Task Scheduler tasks on Windows, crontab lines to paste anywhere else,
or (no scheduler at all, e.g. a container) the command to run by hand (docs/design.md §5).

Two jobs: `refresh` (`convo-index index` once a day: `schedule install|uninstall|status`)
and `serve` (`convo-index serve` kept running from login: `service install|uninstall|status`).
Nothing here contains a user path at rest in the repository; everything is derived at
install time from the environment."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from convo_index import config as C
from convo_index import settings as S

JOBS = {"refresh": {"label": "com.convo-index.refresh", "sub": "index", "log": "refresh.log", "unit": "convo-index",
                    "task": "convo-index refresh", "description": "convo-index: refresh the conversation index"},
        "serve": {"label": "com.convo-index.serve", "sub": "serve", "log": "serve.log", "unit": "convo-index-serve",
                  "task": "convo-index serve", "description": "convo-index: local web UI"}}
LABEL = JOBS["refresh"]["label"]
# commands whose failure means "nothing to do": the job was not loaded / not running
BEST_EFFORT = (("launchctl", "bootout"), ("schtasks", "/End"))
# environment the indexer honours; passed through when set at install time
ENV_PASSTHROUGH = ("CONVO_INDEX_DB", "CONVO_INDEX_ARCHIVE", "OPENCODE_DB", "CODEX_HOME", "ANTIGRAVITY_HOME",
                   "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HOME")


def command(sub="index"):
    """argv that runs a subcommand from a scheduler (no shell, absolute paths)."""
    exe = shutil.which("convo-index")
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name == "convo-index" and argv0.exists():
        exe = str(argv0.resolve())
    if exe:
        return [exe, sub]
    return [sys.executable, "-m", "convo_index.cli", sub]


def log_path(settings, job="refresh"):
    return settings.db_path().parent / JOBS[job]["log"]


def _env_block():
    """PATH for the job: the scheduler's environment is minimal, but the indexer shells out to
    the LLM CLIs and Ollama, so it needs the user's bin dirs (deduplicated, order kept)."""
    seen, parts = set(), []
    for part in os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(os.pathsep):
        if part and part not in seen:
            seen.add(part); parts.append(part)
    return os.pathsep.join(parts)


def _env_pairs(config_path=None):
    env = [("PATH", _env_block())]
    if config_path:
        env.append(("CONVO_INDEX_CONFIG", str(config_path)))
    for k in ENV_PASSTHROUGH:
        v = os.environ.get(k)
        if v:
            env.append((k, str(Path(v).expanduser().resolve()) if k != "HOME" and v.startswith((".", "~", "/")) else v))
    return env


def launchd_plist(argv, hour, minute, log, config_path=None, label=LABEL):
    """A calendar job when hour/minute are given, else a service kept alive from login."""
    args = "\n".join(f"    <string>{_xml(a)}</string>" for a in argv)
    env = _env_pairs(config_path)
    env_xml = "".join(f"<key>{k}</key><string>{_xml(v)}</string>" for k, v in env)
    when = (f"  <key>StartCalendarInterval</key>\n  <dict><key>Hour</key><integer>{int(hour)}</integer>"
            f"<key>Minute</key><integer>{int(minute)}</integer></dict>" if hour is not None
            else "  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict>{env_xml}</dict>
{when}
  <key>StandardOutPath</key><string>{_xml(str(log))}</string>
  <key>StandardErrorPath</key><string>{_xml(str(log))}</string>
</dict>
</plist>
"""


def _systemd_env(config_path):
    return [f'Environment="{k}={_pct(v)}"' for k, v in _env_pairs(config_path)]


def systemd_units(argv, hour, minute, log, config_path=None):
    env = _systemd_env(config_path)
    service = f"""[Unit]
Description=convo-index: refresh the conversation index

[Service]
Type=oneshot
ExecStart={' '.join(_systemd(a) for a in argv)}
StandardOutput=append:{_pct(log)}
StandardError=append:{_pct(log)}
{chr(10).join(env)}
"""
    timer = f"""[Unit]
Description=convo-index daily refresh

[Timer]
OnCalendar=*-*-* {int(hour):02d}:{int(minute):02d}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


def systemd_service(argv, log, config_path=None):
    """A long-running user service for the UI server, restarted if it dies, started at login."""
    env = _systemd_env(config_path)
    return f"""[Unit]
Description=convo-index: local web UI

[Service]
ExecStart={' '.join(_systemd(a) for a in argv)}
Restart=on-failure
StandardOutput=append:{_pct(log)}
StandardError=append:{_pct(log)}
{chr(10).join(env)}

[Install]
WantedBy=default.target
"""


def crontab_line(argv, hour, minute, log, config_path=None):
    env = " ".join(f"{k}={_sh(v)}" for k, v in _env_pairs(config_path)) + " "
    when = f"{int(minute)} {int(hour)} * * *" if hour is not None else "@reboot"
    line = f"{when} {env}{' '.join(_sh(a) for a in argv)} >> {_sh(str(log))} 2>&1"
    return line.replace("%", "\\%")   # cron turns a bare % into a newline


def _xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pct(s):
    """systemd expands %-specifiers in every directive: a literal percent sign is %%."""
    return str(s).replace("%", "%%")


def _systemd(s):
    """systemd.service(5) ExecStart quoting: double quotes with backslash escapes, no single quotes."""
    s = _pct(s)
    if s and all(c.isalnum() or c in "-_./:=@%+" for c in s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sh(s):
    s = str(s)
    return s if s and all(c.isalnum() or c in "-_./:=@%+" for c in s) else "'" + s.replace("'", "'\\''") + "'"


def _cmd(s):
    """One argument in a batch file: double quotes keep cmd's metacharacters (& | < > ^) literal;
    a percent sign is doubled because batch files expand %name% even inside quotes."""
    return '"' + str(s).replace("%", "%%") + '"'


def windows_launcher(argv, log, config_path=None):
    """The batch file a Task Scheduler task runs: the job's environment, then the command with
    its output appended to the log. A task's own action can neither set variables nor
    redirect, and its command line is capped at 261 characters."""
    lines = ["@echo off", "chcp 65001 >nul"]            # the file is UTF-8: paths may not fit the OEM code page
    for k, v in _env_pairs(config_path):
        lines.append(f'set "{k}={str(v).replace("%", "%%")}"')
    lines.append(" ".join(_cmd(a) for a in argv) + f" >> {_cmd(log)} 2>&1")
    return "\n".join(lines) + "\n"


def windows_task(launcher, hour, minute, description):
    """Task Scheduler XML (`schtasks /Create /XML`): a daily or a logon trigger, no run-time
    limit for the server (the default stops a task after 72 hours), restarted when it dies,
    run as the current user without a window (S4U), missed runs caught up. The launcher is
    started by name from its own directory so its path never goes through cmd's %-expansion.
    schtasks reads the file as UTF-16 whatever it declares, so install() writes it that way."""
    if hour is not None:
        trigger = (f"<CalendarTrigger><StartBoundary>2000-01-01T{int(hour):02d}:{int(minute):02d}:00</StartBoundary>"
                   "<Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>")
        limit, restart = "PT12H", ""
    else:
        trigger = "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
        limit, restart = "PT0S", "<RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>\n    "
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if os.environ.get("USERDOMAIN"):
        user = os.environ["USERDOMAIN"] + "\\" + user
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{_xml(description)}</Description></RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals>
    <Principal id="Author"><UserId>{_xml(user)}</UserId><LogonType>S4U</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    {restart}<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>cmd.exe</Command><Arguments>/c {_xml(Path(launcher).name)}</Arguments>
      <WorkingDirectory>{_xml(Path(launcher).parent)}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def systemd_user_available():
    """True when a systemd user manager answers. The systemctl binary alone is not enough:
    containers and some SSH logins have it but no user session (no $XDG_RUNTIME_DIR, no bus),
    and every `systemctl --user` call then fails."""
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _kind(system=None):
    return _kind_and_reason(system)[0]


def _kind_and_reason(system=None):
    """The scheduler to use and, when it is not the platform's own, why (in plain words)."""
    system = system or platform.system()
    if system == "Darwin":
        return "launchd", ""
    if system == "Windows":
        return "schtasks", ""
    if system == "Linux" and systemd_user_available():
        return "systemd", ""
    if system == "Linux" and shutil.which("systemctl"):
        why = "systemctl is installed but no systemd user session runs here (usual inside containers)"
    elif system == "Linux":
        why = "this machine has no systemd"
    else:
        why = f"no built-in scheduler support for {system}"
    if shutil.which("crontab"):
        return "cron", why
    return "none", why + ", and there is no crontab"


def _uid():
    return getattr(os, "getuid", lambda: 0)()


def plan(settings=None, hour=None, minute=None, system=None, home=None, job="refresh", existing=False):
    """What install would write and run, without doing it. `job` is "refresh" (daily index)
    or "serve" (the UI server from login). `existing` (status, uninstall) keeps systemd when
    the job's unit file is already there, even if no user manager answers right now (an SSH
    login without a user session): those units must still be found and removed."""
    settings = settings or S.load()
    spec = JOBS[job]
    if job == "refresh":
        sched = settings.get("schedule") or {}
        hour = int(sched.get("hour", 21) if hour is None else hour)
        minute = int(sched.get("minute", 0) if minute is None else minute)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("hour must be 0-23 and minute 0-59")
    else:
        hour = minute = None
    home = Path(home) if home else Path.home()
    argv = command(spec["sub"])
    log = log_path(settings, job).expanduser().resolve()
    # `home` only places the job files; whether the config path needs baking into the job
    # depends on the real default location (the job reads it from there without any env)
    default_cfg = C.user_dir("config") / "convo-index" / "config.toml"
    config_path = settings.path or (S.CONFIG_PATH if S.CONFIG_PATH != default_cfg else None)
    if config_path is not None:
        config_path = Path(config_path).expanduser().resolve()   # launchd/cron run with cwd=/: relative paths would vanish
        if not config_path.exists():
            raise ValueError(f"config file {config_path} does not exist; the job would run on defaults")
    kind, reason = _kind_and_reason(system)
    if existing and kind != "systemd" and (system or platform.system()) == "Linux" and shutil.which("systemctl"):
        unit = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "systemd" / "user" / f"{spec['unit']}.service"
        if unit.exists():
            kind, reason = "systemd", ""
    out = {"kind": kind, "reason": reason, "job": job, "hour": hour, "minute": minute, "argv": argv, "log": str(log),
           "files": {}, "commands": []}
    if kind == "launchd":
        path = home / "Library" / "LaunchAgents" / f"{spec['label']}.plist"
        out["files"][str(path)] = launchd_plist(argv, hour, minute, log, config_path, label=spec["label"])
        out["commands"] = [["launchctl", "bootout", f"gui/{_uid()}", str(path)],
                           ["launchctl", "bootstrap", f"gui/{_uid()}", str(path)]]
    elif kind == "systemd":
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "systemd" / "user"
        if job == "refresh":
            service, timer = systemd_units(argv, hour, minute, log, config_path)
            out["files"][str(base / f"{spec['unit']}.service")] = service
            out["files"][str(base / f"{spec['unit']}.timer")] = timer
            unit = f"{spec['unit']}.timer"
        else:
            out["files"][str(base / f"{spec['unit']}.service")] = systemd_service(argv, log, config_path)
            unit = f"{spec['unit']}.service"
        out["commands"] = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", unit]]
    elif kind == "schtasks":
        launcher, xml = log.parent / f"{spec['unit']}.cmd", log.parent / f"{spec['unit']}.xml"
        out["files"][str(launcher)] = windows_launcher(argv, log, config_path)
        out["files"][str(xml)] = windows_task(launcher, hour, minute, spec["description"])
        out["commands"] = [["schtasks", "/Create", "/F", "/TN", spec["task"], "/XML", str(xml)]]
        if job == "serve":
            out["commands"].append(["schtasks", "/Run", "/TN", spec["task"]])
    elif kind == "cron":
        out["crontab"] = crontab_line(argv, hour, minute, log, config_path)
    # how to run it by hand: the only way with kind "none", the fallback when an install fails
    out["manual"] = manual_line(argv, log, config_path, background=job == "serve")
    return out


def manual_line(argv, log, config_path=None, background=False):
    """The shell line that runs a job by hand, its output appended to the job's log."""
    env = f"CONVO_INDEX_CONFIG={_sh(config_path)} " if config_path else ""   # before nohup: it runs a command, not a shell
    run = f"{' '.join(_sh(a) for a in argv)} >> {_sh(str(log))} 2>&1"
    return f"{env}nohup {run} &" if background else env + run


def install(settings=None, hour=None, minute=None, dry_run=False, system=None, home=None, job="refresh"):
    p = plan(settings, hour, minute, system, home, job)
    if dry_run:
        return p
    Path(p["log"]).parent.mkdir(parents=True, exist_ok=True)      # a fresh install: no data directory yet
    for path, content in p["files"].items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-16" if path.endswith(".xml") else "utf-8")   # task XML: UTF-16
    p["results"] = _run_all(p["commands"], stop_on_failure=True)
    return p


def _run_all(cmds, stop_on_failure=False):
    results = []
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        ok = r.returncode == 0 or tuple(cmd[:2]) in BEST_EFFORT
        results.append({"cmd": cmd, "ok": ok, "stderr": r.stderr.strip()[:300]})
        if not ok and stop_on_failure:
            break
    return results


def uninstall(settings=None, system=None, home=None, dry_run=False, job="refresh"):
    p = plan(settings, system=system, home=home, job=job, existing=True)
    cmds = []
    if p["kind"] == "launchd":
        path = next(iter(p["files"]))
        cmds = [["launchctl", "bootout", f"gui/{_uid()}", path]]
    elif p["kind"] == "schtasks":                          # /Delete leaves a running task's process alive
        cmds = [["schtasks", "/End", "/TN", JOBS[job]["task"]], ["schtasks", "/Delete", "/F", "/TN", JOBS[job]["task"]]]
    elif p["kind"] == "systemd":
        unit = f"{JOBS[job]['unit']}.timer" if job == "refresh" else f"{JOBS[job]['unit']}.service"
        cmds = [["systemctl", "--user", "disable", "--now", unit]]
    after = [["systemctl", "--user", "daemon-reload"]] if p["kind"] == "systemd" else []
    out = {"kind": p["kind"], "job": job, "removed": [], "commands": cmds + after}
    if dry_run:
        out["would_remove"] = list(p["files"])
        return out
    out["results"] = _run_all(cmds)
    for path in p["files"]:
        if Path(path).exists():
            Path(path).unlink()
            out["removed"].append(path)
    out["results"] += _run_all(after)                     # units are gone from disk: let systemd forget them
    return out


def status(settings=None, system=None, home=None, job="refresh"):
    p = plan(settings, system=system, home=home, job=job, existing=True)
    if p["kind"] == "schtasks":
        r = subprocess.run(["schtasks", "/Query", "/TN", JOBS[job]["task"]], capture_output=True, text=True)
        installed = r.returncode == 0
    else:
        installed = all(Path(f).exists() for f in p["files"]) if p["files"] else None
    return {"kind": p["kind"], "job": job, "installed": installed,
            "files": list(p["files"]), "log": p["log"], "hour": p["hour"], "minute": p["minute"]}


def start_detached(p):
    """Start a job's command now, detached, appending to its log: the cron fallback of
    `service install`, where nothing else starts the server until the next boot."""
    Path(p["log"]).parent.mkdir(parents=True, exist_ok=True)
    log = open(p["log"], "a", encoding="utf-8")
    flags = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
        else {"start_new_session": True}
    subprocess.Popen(p["argv"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, **flags)
