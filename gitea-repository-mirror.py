#!/usr/bin/env python3
# =============================================================================
# [🐍 Python Script] [🪟 CustomTkinter GUI] [🔁 Gitea Repository Backup]
# =============================================================================
"""
Gitea Repository Backup (GUI)

What this is:
- A GUI (customtkinter) wrapper around a Gitea repo mirror/sync tool.
- All control is via GUI: load/edit/save config + dry/live run + logs.

Core behavior:
- Clones + updates every repository accessible to a Gitea user token into an output root.
- Archives local repos that no longer exist (or are no longer accessible) according to the API.

First-run safety:
- If config.json does not exist, it is auto-generated from config_default.json.

Secrets safety:
- Prefer token via env var (config.gitea.token_env). Storing token in config.json is supported,
  but not recommended.

Git SSH customization (config-driven):
- Supports forcing an SSH key + port via config:
  - git.ssh.key_path
  - git.ssh.port
  - git.ssh.host_overrides[hostname].{key_path,port}
- Port precedence:
  1) ssh_url embedded port (ssh://git@host:222/owner/repo.git)
  2) host_overrides[host].port
  3) git.ssh.port

Headless mode:
- If you pass --headless, it runs plan_and_apply() using config files and exits.
  This is optional; the intended usage is GUI control.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import typing as t
from urllib.parse import urljoin, urlparse
from tkinter import ttk
import customtkinter as ctk

Json = t.Dict[str, t.Any]


def configure_stdio_encoding() -> None:
  """
  Keep console logging usable on Windows consoles that default to cp1252.
  """
  for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name, None)
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
      continue
    try:
      reconfigure(encoding="utf-8", errors="replace")
    except Exception:
      pass


# =============================================================================
# Paths
# =============================================================================

def _assert_root_normalized(p: str) -> None:
  if "\\" in p:
    raise AssertionError(f"Backslash leaked into root path: {p!r}")

class RootPath(str):
  """
  Normalized root/output path.

  Guarantees:
  - Internally stored with forward slashes (/)
  - No trailing slash
  - Safe for config + JSON + GUI display

  Convert to OS-native path ONLY via .to_os()
  """

  def __new__(cls, value: str):
    value = value.replace("\\", "/")
    value = value.rstrip("/")
    return super().__new__(cls, value)

  def to_os(self) -> str:
    return os.path.normpath(self)

  def join(self, *parts: str) -> "RootPath":
    return RootPath("/".join([self, *parts]))

def _assert_logical(rel: LogicalPath | str) -> LogicalPath:
  lp = LogicalPath(rel)
  if "\\" in lp:
    raise AssertionError(f"Backslash leaked into logical path: {lp!r}")
  return lp

class LogicalPath(str):
  """
  POSIX-style logical path wrapper.

  Guarantees:
  - Always uses forward slashes (/)
  - Never absolute
  - No trailing slash

  Use for:
  - manifest rel_path
  - GUI state
  - internal comparisons

  Convert to OS path ONLY via .to_os()
  """

  def __new__(cls, value: str):
    value = value.replace("\\", "/")
    value = value.lstrip("/")
    value = value.rstrip("/")
    return super().__new__(cls, value)

  def join(self, *parts: str) -> "LogicalPath":
    joined = "/".join([self, *parts])
    return LogicalPath(joined)

  def to_os(self, root: str) -> str:
    """
    Convert logical path into OS-native absolute path.
    """
    return os.path.join(root, *self.split("/"))


# =============================================================================
# Run control (shared between GUI and worker)
# =============================================================================

class RunState:
  def __init__(self):
    self.running: bool = False
    self.stop_requested: bool = False

RUN_STATE = RunState()


# =============================================================================
# Windows Taskbar Identity (AppUserModelID)
# =============================================================================

def set_windows_app_user_model_id(app_id: str) -> None:
  """
  Set an explicit Windows AppUserModelID for this process (best-effort).

  Notes:
  - No-op on non-Windows.
  - Call BEFORE creating the Tk/CTk window (i.e., early in main()).
  """
  try:
    if os.name != "nt":
      return

    import ctypes  # stdlib, Windows-only usage

    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
  except Exception:
    return

APP_USER_MODEL_ID = "CureInteractive.GiteaRepositoryBackup"

# =============================================================================
# Window Icon (title bar / taskbar best-effort)
# =============================================================================

def set_window_icon(root: t.Any, ico_path: str, png_path: str) -> None:
  """
  Set a title-bar icon with best-effort cross-platform behavior.

  Windows:
    - iconbitmap(.ico) works for title bar + taskbar in most cases.
  Linux/macOS:
    - iconphoto(.png) is the common path.

  Notes:
  - We try both; failures are ignored (best effort).
  - Paths should be absolute for reliability.
  - We keep a reference to the PhotoImage on the root to prevent GC.
  """
  # Import tkinter lazily so importing this module stays safe.
  try:
    import tkinter as tk  # local import on purpose
  except Exception:
    return

  ico_abs = os.path.abspath(ico_path) if ico_path else ""
  png_abs = os.path.abspath(png_path) if png_path else ""

  # Windows: .ico
  try:
    if ico_abs and os.path.isfile(ico_abs):
      root.iconbitmap(ico_abs)
  except Exception:
    pass

  # Cross-platform: .png (Linux/macOS, sometimes Windows too)
  try:
    if png_abs and os.path.isfile(png_abs):
      img = tk.PhotoImage(file=png_abs)
      root.iconphoto(True, img)
      root._iconphoto_ref = img  # type: ignore[attr-defined]
  except Exception:
    pass


# =============================================================================
# Logging (console + file + GUI sinks)
# =============================================================================

import re

_LOG_FILE_HANDLE: t.TextIO | None = None
_LOG_FILE_PATH: str | None = None

# Extra sinks: GUI can register a callable that receives single-line strings.
_LOG_SINKS: t.List[t.Callable[[str], None]] = []


def _ansi_strip(s: str) -> str:
  """
  Strip ANSI escape sequences from a string (for file logging).
  """
  return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


def _repair_mojibake(s: str) -> str:
  """
  Repair common UTF-8 text that was decoded as Windows-1252.
  """
  if not any(marker in s for marker in ("Ã", "Â", "â", "ð", "ï")):
    return s
  try:
    return s.encode("cp1252").decode("utf-8")
  except Exception:
    return s


def _logger_write_line(line: str) -> None:
  """
  Write a single line to the log file if enabled/open.
  """
  global _LOG_FILE_HANDLE
  if _LOG_FILE_HANDLE is None:
    return
  try:
    _LOG_FILE_HANDLE.write(_ansi_strip(_repair_mojibake(line)) + "\n")
    _LOG_FILE_HANDLE.flush()
  except Exception:
    pass


def _logger_init_from_cfg(cfg: Json, *, script_dir: str) -> None:
  """
  Initialize file logging based on config.

  Behavior:
  - If cfg.logging.enabled is false => no file logging.
  - Otherwise create a timestamped log file in:
      1) cfg.logging.dir (if set)
      2) cfg.sync.output_dir + cfg.logging.default_dir_name (if output_dir set)
      3) script_dir + cfg.logging.default_dir_name
  - File name:
      1) cfg.logging.file_name (if set)
      2) gitea_repository_backup_YYYYMMDD_HHMMSS.log
  """
  global _LOG_FILE_HANDLE, _LOG_FILE_PATH

  # Close any previous handle.
  try:
    if _LOG_FILE_HANDLE is not None:
      _LOG_FILE_HANDLE.close()
  except Exception:
    pass
  _LOG_FILE_HANDLE = None
  _LOG_FILE_PATH = None

  logging_cfg = t.cast(Json, cfg.get("logging") or {})
  enabled = bool(logging_cfg.get("enabled", True))
  if not enabled:
    return

  default_dir_name = t.cast(str, logging_cfg.get("default_dir_name", "_logs"))
  dir_override = t.cast(str, logging_cfg.get("dir", "")).strip()
  file_name_override = t.cast(str, logging_cfg.get("file_name", "")).strip()

  out_root = ""
  try:
    out_root = ""
    try:
      out_root = RootPath(
        t.cast(str, (cfg.get("sync") or {}).get("output_dir") or "")
      ).to_os()
    except Exception:
      out_root = ""
  except Exception:
    out_root = ""

  if dir_override:
    log_dir = dir_override
  elif out_root:
    log_dir = os.path.join(out_root, default_dir_name)
  else:
    log_dir = os.path.join(script_dir, default_dir_name)

  ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
  file_name = file_name_override or f"gitea_repository_backup_{ts}.log"

  try:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, file_name)

    _LOG_FILE_HANDLE = open(path, "a", encoding="utf-8", buffering=1)
    _LOG_FILE_PATH = path

    print(f"[📝 LOG] Writing log file: {path}")
    _logger_write_line(f"[📝 LOG] Writing log file: {path}")
  except Exception:
    _LOG_FILE_HANDLE = None
    _LOG_FILE_PATH = None

    fallback_log_dir = os.path.join(script_dir, default_dir_name)
    if os.path.normcase(os.path.abspath(fallback_log_dir)) == os.path.normcase(os.path.abspath(log_dir)):
      return

    try:
      os.makedirs(fallback_log_dir, exist_ok=True)
      path = os.path.join(fallback_log_dir, file_name)

      _LOG_FILE_HANDLE = open(path, "a", encoding="utf-8", buffering=1)
      _LOG_FILE_PATH = path

      print(f"[LOG] Writing log file: {path}")
      _logger_write_line(f"[LOG] Writing log file: {path}")
    except Exception as e:
      _LOG_FILE_HANDLE = None
      _LOG_FILE_PATH = None
      print(f"[WARN] File logging disabled: {e}")


def _emit_to_sinks(line: str) -> None:
  """
  Emit a line to any registered sinks (GUI, etc.). Never throws.
  """
  for sink in list(_LOG_SINKS):
    try:
      sink(line)
    except Exception:
      pass


def _nl() -> None:
  """
  Newline that also writes to file and sinks.
  """
  print()
  _logger_write_line("")
  _emit_to_sinks("")


def _log(tag: str, msg: str) -> None:
  """
  Log one line with a tag.
  """
  line = _repair_mojibake(f"{tag} {msg}")
  print(line)
  _logger_write_line(line)
  _emit_to_sinks(line)


def _sep(title: str) -> None:
  """
  Section separator (multi-line).
  """
  a = "// ============================================================================="
  b = f"// {title}"
  print("\n" + a)
  print(b)
  print(a)
  _logger_write_line("")
  _logger_write_line(a)
  _logger_write_line(b)
  _logger_write_line(a)
  _emit_to_sinks("")
  _emit_to_sinks(a)
  _emit_to_sinks(b)
  _emit_to_sinks(a)


def _render_progress_bar(current: int, total: int, width: int = 36) -> str:
  if total <= 0:
    return "[████████████████████████████████] 0/0 (100%)"

  ratio = current / total
  filled = int(ratio * width)
  bar = "█" * filled + "░" * (width - filled)
  pct = int(ratio * 100)

  return f"[{bar}] {current}/{total} ({pct}%)"


# =============================================================================
# Config
# =============================================================================

def _read_json_file(path: str) -> Json:
  with open(path, "r", encoding="utf-8") as f:
    return t.cast(Json, json.load(f))


def _write_json_file(path: str, data: Json) -> None:
  os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
  with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, sort_keys=True)
    f.write("\n")


def _deep_merge(base: Json, override: Json) -> Json:
  out: Json = dict(base)
  for k, v in override.items():
    if isinstance(v, dict) and isinstance(out.get(k), dict):
      out[k] = _deep_merge(t.cast(Json, out[k]), t.cast(Json, v))
    else:
      out[k] = v
  return out


def ensure_config_json_exists(config_default_path: str, config_path: str) -> bool:
  """
  Create config.json from config_default.json if missing.
  Returns True if created.
  """
  if os.path.exists(config_path):
    return False

  if not os.path.exists(config_default_path):
    raise FileNotFoundError(f"Missing {config_default_path}; cannot bootstrap {config_path}")

  default_cfg = _read_json_file(config_default_path)

  # Never ship token into generated local config.
  try:
    if isinstance(default_cfg.get("gitea"), dict):
      default_cfg["gitea"]["token"] = ""
  except Exception:
    pass

  _write_json_file(config_path, default_cfg)
  return True


def load_config(config_default_path: str, config_path: str) -> Json:
  """
  Load config_default.json then deep-merge config.json (if exists).
  """
  cfg = _read_json_file(config_default_path)
  if os.path.exists(config_path):
    cfg = _deep_merge(cfg, _read_json_file(config_path))

  # 🔒 normalize root paths immediately
  try:
    if "sync" in cfg and "output_dir" in cfg["sync"]:
      cfg["sync"]["output_dir"] = str(RootPath(cfg["sync"]["output_dir"]))
  except Exception:
    pass

  return cfg


def resolve_config_default_path(script_dir: str) -> str:
  """
  Return the config template path shipped with this project.

  Older docs/code used config_default.json, while the repository currently ships
  config-default.json. Support both so existing local setups keep working.
  """
  candidates = [
    os.path.join(script_dir, "config_default.json"),
    os.path.join(script_dir, "config-default.json"),
  ]
  for path in candidates:
    if os.path.exists(path):
      return path
  return candidates[0]


# =============================================================================
# HTTP (requests)
# =============================================================================

try:
  import requests  # type: ignore
except Exception:
  requests = None  # type: ignore


class GiteaClient:
  def __init__(
    self,
    base_url: str,
    api_base_path: str,
    token: str,
    verify_tls: bool = True,
    timeout_s: int = 30,
    user_agent: str = "gitea-repository-backup/1.0",
  ) -> None:
    self._base_url = base_url.rstrip("/")
    self._api_base_path = api_base_path.rstrip("/")
    self._token = token
    self._verify_tls = verify_tls
    self._timeout_s = timeout_s
    self._user_agent = user_agent

    if requests is None:
      raise RuntimeError("Missing dependency: requests. Install with: pip install requests")

  def _api_url(self, path: str) -> str:
    root = self._base_url + self._api_base_path + "/"
    return urljoin(root, path.lstrip("/"))

  def _headers(self) -> t.Dict[str, str]:
    return {
      "Accept": "application/json",
      "Authorization": f"token {self._token}",
      "User-Agent": self._user_agent,
    }

  def get_current_user(self) -> Json:
    url = self._api_url("/user")
    r = requests.get(url, headers=self._headers(), timeout=self._timeout_s, verify=self._verify_tls)
    if r.status_code >= 400:
      raise RuntimeError(f"Gitea API /user failed: {r.status_code} {r.text}")
    return t.cast(Json, r.json())

  def list_current_user_repos(self, page_limit: int = 50) -> t.List[Json]:
    repos: t.List[Json] = []
    page = 1

    while True:
      url = self._api_url("/user/repos")
      params = {"page": page, "limit": page_limit}
      r = requests.get(
        url,
        headers=self._headers(),
        params=params,
        timeout=self._timeout_s,
        verify=self._verify_tls,
      )
      if r.status_code >= 400:
        raise RuntimeError(f"Gitea API /user/repos failed: {r.status_code} {r.text}")

      batch = r.json()
      if not isinstance(batch, list):
        raise RuntimeError(f"Unexpected /user/repos payload (expected list), got: {type(batch)}")

      if len(batch) == 0:
        break

      repos.extend(t.cast(t.List[Json], batch))
      page += 1

    return repos


# =============================================================================
# Git helpers
# =============================================================================

def _shell_quote(s: str) -> str:
  if not s:
    return '""'
  if any(c in s for c in [' ', '\t', '"']):
    return '"' + s.replace('"', '\\"') + '"'
  return s


def _run(cmd: t.List[str], cwd: str | None = None, dry_run: bool = False) -> int:
  pretty = " ".join([_shell_quote(x) for x in cmd])
  _log("[🛠️ CMD]", f"{pretty}" + (f" (cwd={cwd})" if cwd else ""))
  if dry_run:
    return 0

  env = os.environ.copy()

  # Enable git + ssh tracing so the REAL executed commands are visible
  env.setdefault("GIT_TRACE", "1")

  with subprocess.Popen(
    cmd,
    cwd=cwd,
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
  ) as p:
    if p.stdout:
      for line in p.stdout:
        _log("[📜 OUT]", line.rstrip("\r\n"))
    return p.wait()


def _git(
  git_exe: str,
  args: t.List[str],
  cwd: str | None,
  dry_run: bool,
  extra_git_config: t.List[str] | None = None,
) -> int:
  cmd = [git_exe]
  if extra_git_config:
    for kv in extra_git_config:
      cmd.extend(["-c", kv])
  cmd.extend(args)
  return _run(cmd, cwd=cwd, dry_run=dry_run)


def _git_post_update_gc(
  git_exe: str,
  repo_path: str,
  dry_run: bool,
  extra_git_config: t.List[str] | None = None,
) -> bool:
  """
  Best-effort mirror cleanup after refs have been updated.
  """
  _log("[GC]", f"Compacting mirror object store: {repo_path}")

  steps = [
    ["reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all"],
    ["gc", "--prune=now", "--aggressive"],
  ]
  ok = True
  for args in steps:
    rc = _git(
      git_exe,
      args,
      cwd=repo_path,
      dry_run=dry_run,
      extra_git_config=extra_git_config,
    )
    if rc != 0:
      ok = False
      _log("[WARN]", f"Post-update cleanup step failed: {' '.join(args)}")
      break
  return ok


def _git_repo_has_refs(git_exe: str, repo_path: str, dry_run: bool) -> bool:
  """
  Return True when a mirror has at least one ref.
  """
  if dry_run:
    return True

  p = subprocess.run(
    [git_exe, "show-ref", "--quiet"],
    cwd=repo_path,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    text=True,
  )
  return p.returncode == 0


def _is_git_repo(path: str) -> bool:
  """
  Return True if path is a valid git repository.

  Supports:
  - Normal repos (path/.git exists)
  - Bare / mirror repos (path is the git dir itself)
  """
  # Normal working copy
  if os.path.isdir(os.path.join(path, ".git")):
    return True

  # Bare / mirror repo: HEAD + objects must exist
  if (
    os.path.isfile(os.path.join(path, "HEAD")) and
    os.path.isdir(os.path.join(path, "objects"))
  ):
    return True

  return False


def _git_is_dirty(git_exe: str, path: str, dry_run: bool) -> bool:
  if dry_run:
    return False

  p = subprocess.run(
    [git_exe, "status", "--porcelain"],
    cwd=path,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  )
  if p.returncode != 0:
    return True
  return bool(p.stdout.strip())


def _git_origin_url(git_exe: str, path: str, dry_run: bool) -> str:
  if dry_run:
    return ""
  p = subprocess.run(
    [git_exe, "remote", "get-url", "origin"],
    cwd=path,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  )
  if p.returncode != 0:
    return ""
  return p.stdout.strip()

def _truncate_with_ellipsis(s: str, max_len: int, *, ellipsis: str = "... (truncated)") -> str:
  """
  Truncate a string and append "..." only if truncation happened.
  """
  s = str(s or "")
  max_len = int(max_len)

  if max_len <= 0:
    return ""

  if len(s) <= max_len:
    return s

  # If max is too small to fit ellipsis + any chars, just return clipped ellipsis.
  if max_len <= len(ellipsis):
    return ellipsis[:max_len]

  keep = max_len - len(ellipsis)
  return s[:keep] + ellipsis

def _git_last_commit_info(git_exe: str, path: str, dry_run: bool) -> t.Tuple[str, str, str]:
  """
  Get last commit info from a local repo path.

  Works for both working trees and bare/mirror repos because it uses `cwd=<repo path>`.

  Args:
    git_exe: Path to git executable.
    path: Repo directory (working tree or bare repo).
    dry_run: If True, returns empty values without executing git.

  Returns:
    (commit_at_iso, short_hash, message_128)
    - commit_at_iso uses strict ISO-8601 (committer date) via `%cI`.
    - message_128 is based on `%s` (subject) and truncated to 128 chars.
  """
  if dry_run:
    return ("", "", "")

  p = subprocess.run(
    [git_exe, "log", "-1", "--format=%cI%n%h%n%s"],
    cwd=path,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
  )
  if p.returncode != 0:
    return ("", "", "")

  lines = [ln.strip() for ln in p.stdout.splitlines()]
  if len(lines) < 3:
    return ("", "", "")

  commit_at = lines[0].strip()
  short_hash = lines[1].strip()

  # `%s` should be single-line, but guard anyway.
  message = " ".join([ln.strip() for ln in lines[2:] if ln.strip()])
  message = re.sub(r"[\r\n]+", " ", message).strip()
  message = _truncate_with_ellipsis(message, 128)

  return (commit_at, short_hash, message)

def _format_last_commit_parts(*, commit_at: str, commit_hash_short: str, commit_message: str) -> str:
  """
  Build the table-style commit cell string WITHOUT fallback.
  Returns "" if no commit fields are present.
  """
  commit_at_disp = str(commit_at or "").replace("T", " - ").strip()
  h = str(commit_hash_short or "").strip()
  m = str(commit_message or "").strip()

  parts = [p for p in (commit_at_disp, h, m) if p]
  return " | ".join(parts) if parts else ""


def _format_last_commit_cell(*, commit_at: str, commit_hash_short: str, commit_message: str) -> str:
  """
  Build the final commit cell string WITH fallback (mirror-only).
  """
  s = _format_last_commit_parts(
    commit_at=commit_at,
    commit_hash_short=commit_hash_short,
    commit_message=commit_message,
  )
  return s or "(BARE REPO)"


# =============================================================================
# SSH command (config-driven key + port)
# =============================================================================

def _parse_git_ssh_target(url: str) -> t.Tuple[str, int | None]:
  """
  Extract hostname and (optional) port from a git SSH remote URL.

  Supports:
    - ssh://git@host:222/owner/repo.git  -> (host, 222)
    - git@host:owner/repo.git            -> (host, None)
    - host:owner/repo.git                -> (host, None)
  """
  u = (url or "").strip()
  if u.startswith("ssh://"):
    p = urlparse(u)
    host = p.hostname or ""
    port = p.port
    return host, port

  if ":" in u and "://" not in u:
    left = u.split(":", 1)[0]
    host = left.split("@", 1)[1] if "@" in left else left
    return host, None

  return "", None


def _ssh_command_quote(s: str) -> str:
  """
  Quote a value for inclusion inside core.sshCommand string.
  """
  if not s:
    return '""'
  if any(c in s for c in [' ', '\t', '"']):
    return '"' + s.replace('"', '\\"') + '"'
  return s


def _git_ssh_rewrite_clone_url(cfg: Json, ssh_url: str) -> str:
  alias = str(
    ((cfg.get("git") or {}).get("ssh") or {}).get("alias") or ""
  ).strip()

  if not alias:
    return ssh_url

  if ssh_url.startswith("ssh://"):
    p = urlparse(ssh_url)
    return f"{alias}:{p.path.lstrip('/')}"

  if ":" in ssh_url and "://" not in ssh_url:
    _, path = ssh_url.split(":", 1)
    return f"{alias}:{path}"

  return ssh_url


def _git_ssh_config_for_url(cfg: Json, ssh_url: str) -> t.List[str]:
  """
  Build git -c core.sshCommand=... based on config + URL.

  Port precedence:
    1) Port embedded in ssh_url (ssh://...:222/...)
    2) git.ssh.host_overrides[host].port
    3) git.ssh.port

  Key precedence:
    1) git.ssh.host_overrides[host].key_path (if set)
    2) git.ssh.key_path (if set)
    else: return [] (don’t override; let normal SSH resolution happen)
  """
  alias = str(
    ((cfg.get("git") or {}).get("ssh") or {}).get("alias") or ""
  ).strip()

  if alias:
    _log("[🔑 SSH]", f"Using SSH alias '{alias}'; skipping sshCommand overrides")
    return []

  git_cfg = t.cast(Json, cfg.get("git") or {})
  ssh_cfg = t.cast(Json, git_cfg.get("ssh") or {})
  host_overrides = t.cast(Json, ssh_cfg.get("host_overrides") or {})

  host, url_port = _parse_git_ssh_target(ssh_url)

  host_override = t.cast(Json, host_overrides.get(host) or {}) if host else {}

  key_path = t.cast(str, (host_override.get("key_path") or ssh_cfg.get("key_path") or "")).strip()
  if not key_path:
    return []

  identity_only = bool(ssh_cfg.get("identity_only", True))

  port: int | None = None
  if isinstance(url_port, int):
    port = url_port
  elif isinstance(host_override.get("port"), int):
    port = t.cast(int, host_override["port"])
  elif isinstance(ssh_cfg.get("port"), int):
    port = t.cast(int, ssh_cfg["port"])

  cmd_parts: t.List[str] = ["ssh", "-i", _ssh_command_quote(key_path)]
  if identity_only:
    cmd_parts.extend(["-o", "IdentitiesOnly=yes"])
  if port is not None:
    cmd_parts.extend(["-p", str(port)])

  core_cmd = " ".join(cmd_parts)
  return [f"core.sshCommand={core_cmd}"]


# =============================================================================
# Manifest (to avoid archiving random folders)
# =============================================================================

def _manifest_path(cfg: Json) -> str:
  base = RootPath(t.cast(str, cfg["sync"]["output_dir"]))
  name = LogicalPath(t.cast(str, cfg["sync"]["manifest_file_name"]))
  return name.to_os(base.to_os())


def _sanitize_manifest(m: Json, *, path: str) -> Json:
  if not isinstance(m, dict):
    raise ValueError("manifest root is not an object")

  managed_raw = m.get("managed") or {}
  if not isinstance(managed_raw, dict):
    _log("[WARN]", f"Manifest managed section is not an object; ignoring managed entries: {path}")
    managed_raw = {}

  managed_clean: Json = {}
  for full_name_raw, info_raw in managed_raw.items():
    full_name = str(full_name_raw or "").strip()
    if not full_name:
      _log("[WARN]", "Ignoring manifest entry with empty repository name.")
      continue
    if not isinstance(info_raw, dict):
      _log("[WARN]", f"Ignoring malformed manifest entry for {full_name!r}: entry is not an object.")
      continue

    info = dict(info_raw)
    rel_raw = str(info.get("rel_path") or "").strip()
    if not rel_raw:
      _log("[WARN]", f"Ignoring malformed manifest entry for {full_name!r}: missing rel_path.")
      continue

    try:
      rel = LogicalPath(rel_raw)
      parts = [part for part in str(rel).split("/") if part]
      if not parts or any(part in (".", "..") for part in parts):
        raise ValueError(f"unsafe rel_path: {rel_raw!r}")
      info["rel_path"] = str(rel)
    except Exception as e:
      _log("[WARN]", f"Ignoring malformed manifest entry for {full_name!r}: {e}")
      continue

    managed_clean[full_name] = t.cast(Json, info)

  try:
    m["version"] = int(m.get("version") or 2)
  except Exception:
    _log("[WARN]", f"Manifest version is invalid; using version 2: {path}")
    m["version"] = 2
  m["managed"] = managed_clean
  m["last_sync_at"] = str(m.get("last_sync_at") or "")
  return m


def _load_manifest(cfg: Json) -> Json:
  path = _manifest_path(cfg)
  if os.path.exists(path):
    try:
      m = _read_json_file(path)

      # 🔒 normalize rel_path fields defensively
      return _sanitize_manifest(t.cast(Json, m), path=path)
    except Exception:
      _log("[⚠️ WARN]", f"Failed to read manifest, starting fresh: {path}")

  return {"version": 2, "managed": {}, "last_sync_at": ""}


def load_manifest_for_ui(cfg: Json) -> dict[str, Json]:
  """
  Load repo manifest for GUI display only.

  Returns:
    { full_name: manifest_entry }
  """
  try:
    m = _load_manifest(cfg)
    return t.cast(dict[str, Json], m.get("managed") or {})
  except Exception:
    return {}


def _save_manifest(cfg: Json, manifest: Json, dry_run: bool) -> None:
  path = _manifest_path(cfg)
  root = os.path.dirname(path)
  os.makedirs(root, exist_ok=True)
  manifest["last_sync_at"] = _dt.datetime.now().isoformat(timespec="seconds")

  if dry_run:
    _log("[🧪 DRY]", f"Would write manifest: {path}")
    return

  tmp = path + ".tmp"

  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")

  os.replace(tmp, path)


# =============================================================================
# Repo status events (GUI-friendly, structured)
# =============================================================================

class RepoStatus(t.TypedDict, total=False):
  """
  Repo status payload emitted from worker → GUI.

  Timing semantics:
  - last_seen_at: "repo was observed / status updated" time (ISO).
  - last_backup_at: "repo sync completed successfully" time (ISO). (Set ONLY on success.)
  - last_commit_at: last commit timestamp from the repo after sync (ISO). Empty/missing if unknown.

  Commit semantics (tracked per-repo in the manifest):
  - last_commit_hash_short: short hash (e.g., 1a2b3c4).
  - last_commit_message: first 64 chars of commit subject (no newlines).
  """
  name: str
  rel_path: str
  index: int
  total: int
  phase: str        # queued | cloning | fetching | archiving | done | error | skipped

  # ISO timestamps
  last_seen_at: str
  last_backup_at: str
  last_commit_at: str

  # Commit info (for UI)
  last_commit_hash_short: str
  last_commit_message: str

  preserve_position: bool
  success: bool | None


# =============================================================================
# Sync planning
# =============================================================================

def _normalize_layout_relpath(layout: str, owner: str, repo: str) -> LogicalPath:
  if layout == "owner/repo":
    rel = f"{owner}/{repo}"
  elif layout == "flat":
    rel = repo
  elif layout == "owner__repo":
    rel = f"{owner}__{repo}"
  else:
    raise ValueError(f"Unknown layout: {layout}")

  return LogicalPath(rel)


def _stamp(cfg: Json) -> str:
  fmt = t.cast(str, cfg["sync"]["archive_stamp_format"])
  return _dt.datetime.now().strftime(fmt)


def _archive_dir(cfg: Json) -> str:
  root = _effective_output_root(cfg)
  name = LogicalPath(cfg["sync"]["archive_dir_name"])
  return name.to_os(root)


def _archive_move(cfg: Json, src_abs: str, rel: str, dry_run: bool) -> None:
  dst_root = os.path.join(_archive_dir(cfg), _stamp(cfg))
  dst_abs = LogicalPath(rel).to_os(dst_root)

  _log("[📦 ARCHIVE]", f"Move -> {dst_abs}")
  if dry_run:
    return

  os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
  if os.path.exists(dst_abs):
    dst_abs = dst_abs + "__" + _stamp(cfg)
  shutil.move(src_abs, dst_abs)


def _effective_output_root(cfg: Json) -> str:
  """
  Mirror-only: repositories live directly under sync.output_dir (no mode subfolders).
  """
  base = RootPath(t.cast(str, cfg["sync"]["output_dir"]))
  return base.to_os()


def _ensure_parent_dir(path: str, dry_run: bool) -> None:
  parent = os.path.dirname(path)
  if not parent:
    return
  if dry_run:
    _log("[🧪 DRY]", f"Would mkdir -p: {parent}")
    return
  os.makedirs(parent, exist_ok=True)


def _basic_auth_header_value(user: str, token: str) -> str:
  raw = f"{user}:{token}".encode("utf-8")
  b64 = base64.b64encode(raw).decode("ascii")
  return f"AUTHORIZATION: basic {b64}"


def _git_auth_config_https(cfg: Json, username: str, token: str) -> t.List[str]:
  mode = t.cast(str, cfg["git"]["https_auth"]["mode"])
  if mode != "extraheader":
    return []

  basic_user = t.cast(str, cfg["git"]["https_auth"].get("extraheader_basic_user") or username)
  if not basic_user or not token:
    raise RuntimeError("https_auth.mode=extraheader requires username and token.")
  header = _basic_auth_header_value(basic_user, token)
  return [f"http.extraHeader={header}"]


def _repo_clone_url(repo: Json, protocol: str) -> str:
  if protocol == "ssh":
    return t.cast(str, repo.get("ssh_url") or "")
  if protocol == "https":
    return t.cast(str, repo.get("clone_url") or "")
  raise ValueError(f"Unknown protocol: {protocol}")


def _wiki_clone_url_from_repo_url(repo_url: str) -> str:
  url = str(repo_url or "").strip()
  if not url:
    return ""

  if url.endswith(".wiki.git"):
    return url

  if url.endswith(".git"):
    return url[:-4] + ".wiki.git"

  return url.rstrip("/") + ".wiki.git"


def _expand_with_wiki_repos(repos: t.Iterable[Json]) -> t.List[Json]:
  out: t.List[Json] = []
  seen: set[str] = set()

  for repo in repos:
    full_name = str(repo.get("full_name") or "").strip()
    if full_name and full_name not in seen:
      out.append(repo)
      seen.add(full_name)

    if not bool(repo.get("has_wiki")):
      continue
    if bool(repo.get("external_wiki")):
      continue

    owner = str((repo.get("owner") or {}).get("username") or "").strip()
    name = str(repo.get("name") or "").strip()
    if not owner or not name:
      continue

    wiki_full_name = f"{owner}/{name}.wiki"
    if wiki_full_name in seen:
      continue

    wiki_repo = dict(repo)
    wiki_repo["name"] = f"{name}.wiki"
    wiki_repo["full_name"] = wiki_full_name
    wiki_repo["clone_url"] = _wiki_clone_url_from_repo_url(str(repo.get("clone_url") or ""))
    wiki_repo["ssh_url"] = _wiki_clone_url_from_repo_url(str(repo.get("ssh_url") or ""))

    out.append(t.cast(Json, wiki_repo))
    seen.add(wiki_full_name)

  return out


def _sync_one_repo(
  cfg: Json,
  git_exe: str,
  repo: Json,
  username: str,
  token: str,
  dest_abs: str,
  dest_rel: str,
  dry_run: bool,
) -> bool:
  """
  Mirror-only repository sync.

  - If missing: `git clone --mirror <remote> <dest_abs>`
  - Else: `git remote update --prune`
  """
  if RUN_STATE.stop_requested:
    return False

  protocol = t.cast(str, cfg["git"]["protocol"])
  clone_url = _repo_clone_url(repo, protocol)
  if not clone_url:
    raise RuntimeError(f"No clone URL for repo: {repo.get('full_name')!r}")

  # For SSH mode, normalize clone URL through configured alias (if any).
  if protocol == "ssh":
    try:
      rewritten = _git_ssh_rewrite_clone_url(cfg, clone_url)
      if rewritten and rewritten != clone_url:
        _log("[🔁 SSH]", f"Clone URL rewritten via alias: {clone_url} -> {rewritten}")
        clone_url = rewritten
    except Exception:
      pass

  # Build extra git config (auth / ssh) per protocol
  extra_cfg: t.List[str] = []

  if protocol == "https":
    # Use the existing HTTPS auth config helper if present
    try:
      extra_cfg = _git_auth_config_https(cfg, username, token)
    except Exception:
      extra_cfg = []
  elif protocol == "ssh":
    # Prefer existing SSH config helper if present; otherwise no overrides
    try:
      extra_cfg = _git_ssh_config_for_url(cfg, clone_url)
    except Exception:
      extra_cfg = []

  # Clone once
  if not os.path.exists(dest_abs):
    if not t.cast(bool, cfg["sync"]["clone_missing"]):
      _log("[⏭️ SKIP]", f"Clone disabled; missing {dest_rel}")
      return False

    _sep(f"CLONE {repo.get('full_name')}")
    _ensure_parent_dir(dest_abs, dry_run=dry_run)

    rc = _git(
      git_exe,
      ["clone", "--mirror", clone_url, dest_abs],
      cwd=None,
      dry_run=dry_run,
      extra_git_config=extra_cfg,
    )
    if rc != 0:
      raise RuntimeError(f"Clone failed: {repo.get('full_name')}")
    if t.cast(bool, cfg["sync"].get("post_update_gc", False)):
      if _git_repo_has_refs(git_exe, dest_abs, dry_run=dry_run):
        _git_post_update_gc(
          git_exe,
          dest_abs,
          dry_run=dry_run,
          extra_git_config=extra_cfg,
        )
      else:
        _log("[GC]", f"Skipping post-clone cleanup for empty mirror: {dest_abs}")
    return True

  # Update existing
  if not t.cast(bool, cfg["sync"]["update_existing"]):
    _log("[⏭️ SKIP]", f"Update disabled; existing {dest_rel}")
    return False

  # In SSH mode, attempt to align existing origin URL with configured alias.
  # This is best-effort so it cannot break normal updates.
  if protocol == "ssh":
    try:
      origin_url = _git_origin_url(git_exe, dest_abs, dry_run=dry_run)
      if origin_url:
        rewritten_origin = _git_ssh_rewrite_clone_url(cfg, origin_url)
        if rewritten_origin and rewritten_origin != origin_url:
          _log("[🔁 SSH]", f"Origin URL rewritten via alias: {origin_url} -> {rewritten_origin}")
          rc_set = _git(
            git_exe,
            ["remote", "set-url", "origin", rewritten_origin],
            cwd=dest_abs,
            dry_run=dry_run,
            extra_git_config=extra_cfg,
          )
          if rc_set != 0:
            _log("[⚠️ WARN]", "Could not set origin URL to alias; continuing with existing origin.")
    except Exception as e:
      _log("[⚠️ WARN]", f"Origin alias rewrite skipped: {e}")

  _sep(f"UPDATE {repo.get('full_name')}")
  rc = _git(
    git_exe,
    ["remote", "update", "--prune"],
    cwd=dest_abs,
    dry_run=dry_run,
    extra_git_config=extra_cfg,
  )
  if rc != 0:
    raise RuntimeError(f"Update failed: {repo.get('full_name')}")

  if t.cast(bool, cfg["sync"].get("post_update_gc", False)):
    _git_post_update_gc(
      git_exe,
      dest_abs,
      dry_run=dry_run,
      extra_git_config=extra_cfg,
    )

  return True

class RepoRef(t.TypedDict):
  full_name: str
  rel_path: str
  abs_path: str


def _is_under(path: str, parent: str) -> bool:
  try:
    rp = os.path.realpath(path)
    rpar = os.path.realpath(parent)
    return os.path.commonpath([rp, rpar]) == rpar
  except Exception:
    return False


def plan_and_apply(
  cfg: Json,
  dry_run: bool,
  *,
  selected_full_names: t.Collection[str] | None = None,
  on_progress: t.Callable[[int, int, str], None] | None = None,
  on_repo_status: t.Callable[[RepoStatus], None] | None = None,
) -> int:
  _sep("LOAD CONFIG")

  out_root = _effective_output_root(cfg)
  if not dry_run:
    os.makedirs(out_root, exist_ok=True)

  token_env = t.cast(str, cfg["gitea"]["token_env"])
  token = t.cast(str, (os.environ.get(token_env) or cfg["gitea"].get("token") or "")).strip()
  if not token:
    _log("[❌ ERR]", f"No token provided. Set env {token_env} or config.gitea.token.")
    return 2

  base_url = t.cast(str, cfg["gitea"]["base_url"])
  api_base_path = t.cast(str, cfg["gitea"]["api_base_path"])
  verify_tls = t.cast(bool, cfg["gitea"]["verify_tls"])
  timeout_s = int(cfg["gitea"]["timeout_s"])
  page_limit = int(cfg["gitea"]["page_limit"])
  user_agent = t.cast(str, cfg["gitea"]["user_agent"])

  _sep("CONNECT API")
  client = GiteaClient(
    base_url=base_url,
    api_base_path=api_base_path,
    token=token,
    verify_tls=verify_tls,
    timeout_s=timeout_s,
    user_agent=user_agent,
  )

  me = client.get_current_user()
  username = t.cast(str, me.get("username") or "")
  _log("[🧩 API]", f"Authenticated as: {username or '<unknown>'}")

  _sep("LIST REPOS")
  repos = _expand_with_wiki_repos(client.list_current_user_repos(page_limit=page_limit))
  _log("[🧩 API]", f"Repos accessible (including wiki mirrors): {len(repos)}")

  selected_names: set[str] | None = None
  if selected_full_names is not None:
    selected_names = {
      str(name).strip()
      for name in selected_full_names
      if str(name).strip()
    }
    repos = [
      r for r in repos
      if str(r.get("full_name") or "").strip() in selected_names
    ]
    _log("[FILTER]", f"Selected repos requested: {len(selected_names)}; matched remotely: {len(repos)}")

  # ---------------------------------------------------------
  # EARLY MANIFEST + UI SEED (populate immediately)
  # ---------------------------------------------------------

  layout = t.cast(str, cfg["sync"]["layout"])
  out_root = _effective_output_root(cfg)

  manifest = _load_manifest(cfg)
  managed: Json = t.cast(Json, manifest.get("managed") or {})

  total_repos = len(repos)

  for idx, r in enumerate(
    sorted(repos, key=lambda x: (x.get("full_name") or "").lower()),
    start=1,
  ):
    owner = t.cast(str, (r.get("owner") or {}).get("username") or "")
    name = t.cast(str, r.get("name") or "")
    full_name = t.cast(str, r.get("full_name") or f"{owner}/{name}")

    rel = _assert_logical(
      _normalize_layout_relpath(layout, owner=owner, repo=name)
    )

    # Manifest entry (mode-agnostic rel_path)
    entry = managed.setdefault(full_name, {})

    entry["rel_path"] = str(rel)
    entry["last_seen_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    entry["protocol"] = cfg["git"]["protocol"]
    entry["base_url"] = base_url

    # Emit queued status to GUI
    now_iso = _dt.datetime.now().isoformat(timespec="seconds")
    if on_repo_status:
      on_repo_status({
        "name": full_name,
        "rel_path": str(rel),
        "index": idx,
        "total": total_repos,
        "phase": "queued",
        "last_seen_at": now_iso,
        # Optional: lets new rows show something sane immediately
        "last_backup_at": str(entry.get("last_backup_at") or ""),
        "last_commit_at": str(entry.get("last_commit_at") or ""),
        "last_commit_hash_short": str(entry.get("last_commit_hash_short") or ""),
        "last_commit_message": str(entry.get("last_commit_message") or ""),
        "preserve_position": bool(selected_names is not None),
        "success": None,
      })

    manifest["managed"] = managed
    _save_manifest(cfg, manifest, dry_run=dry_run)


  layout = t.cast(str, cfg["sync"]["layout"])
  desired: t.Dict[str, t.Tuple[LogicalPath, str, Json]] = {}
  for r in repos:
    owner = t.cast(str, (r.get("owner") or {}).get("username") or "")
    name = t.cast(str, r.get("name") or "")
    full_name = t.cast(str, r.get("full_name") or f"{owner}/{name}")
    rel = _assert_logical(_normalize_layout_relpath(layout, owner=owner, repo=name))
    abs_path = rel.to_os(out_root)
    desired[full_name] = (rel, abs_path, r)

  _sep("LOAD MANIFEST")
  manifest = _load_manifest(cfg)
  managed: Json = t.cast(Json, manifest.get("managed") or {})

  if selected_names is None and t.cast(bool, cfg["sync"]["archive_missing_remote"]):
    _sep("ARCHIVE MISSING REMOTE")
    to_archive: t.List[t.Tuple[str, str]] = []
    for full_name, info in list(managed.items()):
      rel = LogicalPath(t.cast(str, info.get("rel_path") or ""))
      abs_path = rel.to_os(out_root)
      if full_name not in desired and rel and os.path.exists(abs_path):
        to_archive.append((full_name, rel))

    for full_name, rel in to_archive:
      abs_path = LogicalPath(rel).to_os(out_root)
      _log("[📦 ARCHIVE]", f"{full_name} -> {rel}")
      _archive_move(cfg, src_abs=abs_path, rel=rel, dry_run=dry_run)
      managed.pop(full_name, None)
  elif selected_names is not None:
    _log("[SKIP]", "Archive-missing-remote skipped for selected-only sync.")

  git_exe = t.cast(str, cfg["git"]["executable"])
  _sep("SYNC REPOS")
  total_repos = len(desired)

  # Sort repos alphabetically by owner/repo (deterministic order)
  for idx, full_name in enumerate(
    sorted(desired.keys(), key=lambda s: s.lower()),
    start=1,
  ):
    rel, abs_path, r = desired[full_name]
    if RUN_STATE.stop_requested:
      _log("[⛔ STOP]", "Run stopped by user.")
      break

    if _is_under(abs_path, _archive_dir(cfg)):
      _log("[⚠️ WARN]", f"Refusing to sync into archive path: {abs_path}")
      continue

    _sep(f"UPDATE ({idx} of {total_repos}) {full_name}")

    entry_for_ui = t.cast(Json, managed.get(full_name) or {})

    if on_repo_status:
      on_repo_status({
        "name": full_name,
        "rel_path": str(rel),
        "index": idx,
        "total": total_repos,
        "phase": "fetching",
        "last_seen_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "last_backup_at": str(entry_for_ui.get("last_backup_at") or ""),
        "last_commit_at": str(entry_for_ui.get("last_commit_at") or ""),
        "last_commit_hash_short": str(entry_for_ui.get("last_commit_hash_short") or ""),
        "last_commit_message": str(entry_for_ui.get("last_commit_message") or ""),
        "preserve_position": bool(selected_names is not None),
        "success": None,
      })

    _log(
      "[⏳ PROGRESS]",
      _render_progress_bar(idx, total_repos),
    )

    # GUI progress update
    if on_progress:
      on_progress(
        idx,
        total_repos,
        f"Processing {idx}/{total_repos}: {full_name}",
      )

    try:
      ok = _sync_one_repo(
        cfg=cfg,
        git_exe=git_exe,
        repo=r,
        username=username,
        token=token,
        dest_abs=abs_path,
        dest_rel=rel,
        dry_run=dry_run,
      )

      if ok:
        # This is the ONLY timestamp that should ever become last_backup_at:
        # the "now" moment after the repo sync succeeds.
        backup_now_iso = _dt.datetime.now().isoformat(timespec="seconds")

        # Collect last commit info from the freshly updated repo.
        # (Runs only in live mode; dry-run returns empty strings.)
        commit_at_iso, commit_hash_short, commit_msg = _git_last_commit_info(
          git_exe,
          abs_path,
          dry_run=dry_run,
        )

        # Persist to manifest ONLY in live mode (but still show it in GUI in dry-run).
        if not dry_run:
          entry = managed.setdefault(full_name, {})
          entry["last_backup_at"] = backup_now_iso

          # Only overwrite commit fields when we successfully read them.
          if commit_at_iso:
            entry["last_commit_at"] = commit_at_iso
          if commit_hash_short:
            entry["last_commit_hash_short"] = commit_hash_short
          if commit_msg:
            entry["last_commit_message"] = commit_msg

        if on_repo_status:
          on_repo_status({
            "name": full_name,
            "rel_path": str(rel),
            "index": idx,
            "total": total_repos,
            "phase": "done",
            "last_seen_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "last_backup_at": backup_now_iso,
            "last_commit_at": commit_at_iso,
            "last_commit_hash_short": commit_hash_short,
            "last_commit_message": commit_msg,
            "preserve_position": bool(selected_names is not None),
            "success": True,
          })
          _log(
            "[🧾 COMMIT]",
            f"{full_name}: " + _format_last_commit_cell(
              commit_at=commit_at_iso,
              commit_hash_short=commit_hash_short,
              commit_message=commit_msg,
            ),
          )

      else:
        if on_repo_status:
          on_repo_status({
            "name": full_name,
            "rel_path": str(rel),
            "index": idx,
            "total": total_repos,
            "phase": "error",
            "last_seen_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "last_backup_at": str(entry_for_ui.get("last_backup_at") or ""),
            "last_commit_at": str(entry_for_ui.get("last_commit_at") or ""),
            "last_commit_hash_short": str(entry_for_ui.get("last_commit_hash_short") or ""),
            "last_commit_message": str(entry_for_ui.get("last_commit_message") or ""),
            "preserve_position": bool(selected_names is not None),
            "success": False,
          })

        _log(
          "[🧾 COMMIT]",
          f"{full_name}: " + _format_last_commit_cell(
            commit_at=str(entry_for_ui.get("last_commit_at") or ""),
            commit_hash_short=str(entry_for_ui.get("last_commit_hash_short") or ""),
            commit_message=str(entry_for_ui.get("last_commit_message") or ""),
          ),
        )

    except Exception as e:
      _log("[❌ FAIL]", f"{full_name}: {e}")

      if on_repo_status:
        on_repo_status({
          "name": full_name,
          "rel_path": str(rel),
          "index": idx,
          "total": total_repos,
          "phase": "error",
          "last_seen_at": _dt.datetime.now().isoformat(timespec="seconds"),
          "last_backup_at": str(entry_for_ui.get("last_backup_at") or ""),
          "last_commit_at": str(entry_for_ui.get("last_commit_at") or ""),
          "last_commit_hash_short": str(entry_for_ui.get("last_commit_hash_short") or ""),
          "last_commit_message": str(entry_for_ui.get("last_commit_message") or ""),
          "preserve_position": bool(selected_names is not None),
          "success": False,
        })

    # SAFETY: rel_path must be mode-agnostic in manifest v2
    if rel.startswith("mirror/") or rel.startswith("working_copy/"):
      raise AssertionError(f"BUG: rel_path contains mode prefix: {rel}")
    
    entry = managed.setdefault(full_name, {})

    entry["rel_path"] = str(rel)
    entry["last_seen_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    entry["protocol"] = cfg["git"]["protocol"]
    entry["base_url"] = base_url

    # Persist per-repo so last_seen_at and last_backup_at stay in-sync during a long run.
    _save_manifest(cfg, manifest, dry_run=dry_run)

  manifest["managed"] = managed
  _save_manifest(cfg, manifest, dry_run=dry_run)

  _sep("DONE")
  return 0


# =============================================================================
# GUI (customtkinter)
# =============================================================================

def _to_bool(s: str) -> bool:
  return str(s).strip().lower() in ("1", "true", "yes", "y", "on")


def _safe_int(s: str, default: int) -> int:
  try:
    return int(str(s).strip())
  except Exception:
    return default


def _cfg_get(cfg: Json, path: t.List[str], default: t.Any) -> t.Any:
  cur: t.Any = cfg
  for k in path:
    if not isinstance(cur, dict) or k not in cur:
      return default
    cur = cur[k]
  return cur


def _cfg_set(cfg: Json, path: t.List[str], value: t.Any) -> None:
  cur: t.Any = cfg
  for k in path[:-1]:
    if k not in cur or not isinstance(cur.get(k), dict):
      cur[k] = {}
    cur = cur[k]
  cur[path[-1]] = value


def _build_cfg_from_vars(cfg_base: Json, vars_map: t.Dict[str, t.Any]) -> Json:
  """
  Apply GUI vars into cfg_base (deep copy-ish via json roundtrip for simplicity).
  """
  cfg = t.cast(Json, json.loads(json.dumps(cfg_base)))

  # Top-level
  _cfg_set(cfg, ["dry_run"], bool(vars_map["dry_run"].get()))

  # gitea
  _cfg_set(cfg, ["gitea", "base_url"], str(vars_map["gitea_base_url"].get()).strip())
  _cfg_set(cfg, ["gitea", "api_base_path"], str(vars_map["gitea_api_base_path"].get()).strip() or "/api/v1")
  _cfg_set(cfg, ["gitea", "token_env"], str(vars_map["gitea_token_env"].get()).strip() or "GITEA_TOKEN")
  _cfg_set(cfg, ["gitea", "token"], str(vars_map["gitea_token"].get()).strip())
  _cfg_set(cfg, ["gitea", "verify_tls"], bool(vars_map["gitea_verify_tls"].get()))
  _cfg_set(cfg, ["gitea", "timeout_s"], _safe_int(vars_map["gitea_timeout_s"].get(), 30))
  _cfg_set(cfg, ["gitea", "page_limit"], _safe_int(vars_map["gitea_page_limit"].get(), 50))
  _cfg_set(cfg, ["gitea", "user_agent"], str(vars_map["gitea_user_agent"].get()).strip() or "gitea-repo-sync/1.0")

  # sync
  # sync (mirror-only; do NOT persist sync.mode in config.json)
  try:
    if isinstance(cfg.get("sync"), dict):
      cfg["sync"].pop("mode", None)
  except Exception:
    pass
  _cfg_set(cfg, ["sync", "clone_missing"], bool(vars_map["sync_clone_missing"].get()))
  _cfg_set(cfg, ["sync", "update_existing"], bool(vars_map["sync_update_existing"].get()))
  _cfg_set(cfg, ["sync", "archive_missing_remote"], bool(vars_map["sync_archive_missing_remote"].get()))
  _cfg_set(cfg, ["sync", "post_update_gc"], bool(vars_map["sync_post_update_gc"].get()))
  _cfg_set(cfg, ["sync", "output_dir"], str(RootPath(vars_map["sync_output_dir"].get())))
  _cfg_set(cfg, ["sync", "layout"], str(vars_map["sync_layout"].get()).strip() or "owner/repo")
  _cfg_set(cfg, ["sync", "archive_dir_name"], str(vars_map["sync_archive_dir_name"].get()).strip() or "_archive")
  _cfg_set(cfg, ["sync", "manifest_file_name"], str(vars_map["sync_manifest_file_name"].get()).strip() or "_repo_sync_manifest.json")
  _cfg_set(cfg, ["sync", "archive_stamp_format"], str(vars_map["sync_archive_stamp_format"].get()).strip() or "%Y%m%d_%H%M%S")

  # git
  _cfg_set(cfg, ["git", "executable"], str(vars_map["git_executable"].get()).strip() or "git")
  _cfg_set(cfg, ["git", "protocol"], str(vars_map["git_protocol"].get()).strip() or "ssh")
  _cfg_set(cfg, ["git", "fetch_prune"], bool(vars_map["git_fetch_prune"].get()))
  _cfg_set(cfg, ["git", "reset_hard_to_origin"], bool(vars_map["git_reset_hard_to_origin"].get()))
  _cfg_set(cfg, ["git", "on_dirty_repo"], str(vars_map["git_on_dirty_repo"].get()).strip() or "skip")

  # git.ssh
  _cfg_set(cfg, ["git", "ssh", "key_path"], str(vars_map["git_ssh_key_path"].get()).strip())
  _cfg_set(cfg, ["git", "ssh", "port"], _safe_int(vars_map["git_ssh_port"].get(), 22))
  _cfg_set(cfg, ["git", "ssh", "identity_only"], bool(vars_map["git_ssh_identity_only"].get()))
  _cfg_set(cfg, ["git", "ssh", "alias"], str(vars_map["git_ssh_alias"].get()).strip())
  _cfg_set(cfg, ["git", "ssh", "clone_url_style"], str(vars_map["git_ssh_clone_url_style"].get()).strip() or "scp")
  _cfg_set(cfg, ["git", "ssh", "clone_url_include_user"], bool(vars_map["git_ssh_clone_url_include_user"].get()))

  # git.https_auth
  _cfg_set(cfg, ["git", "https_auth", "mode"], str(vars_map["git_https_auth_mode"].get()).strip() or "prompt")
  _cfg_set(cfg, ["git", "https_auth", "extraheader_basic_user"], str(vars_map["git_https_basic_user"].get()).strip())

  # logging
  _cfg_set(cfg, ["logging", "enabled"], bool(vars_map["logging_enabled"].get()))
  _cfg_set(cfg, ["logging", "default_dir_name"], str(vars_map["logging_default_dir_name"].get()).strip() or "_logs")
  _cfg_set(cfg, ["logging", "dir"], str(vars_map["logging_dir"].get()).strip())
  _cfg_set(cfg, ["logging", "file_name"], str(vars_map["logging_file_name"].get()).strip())

  return cfg


def _apply_cfg_to_vars(cfg: Json, vars_map: t.Dict[str, t.Any]) -> None:
  """
  Populate GUI vars from cfg.
  """
  vars_map["dry_run"].set(bool(_cfg_get(cfg, ["dry_run"], True)))

  vars_map["gitea_base_url"].set(str(_cfg_get(cfg, ["gitea", "base_url"], "")))
  vars_map["gitea_api_base_path"].set(str(_cfg_get(cfg, ["gitea", "api_base_path"], "/api/v1")))
  vars_map["gitea_token_env"].set(str(_cfg_get(cfg, ["gitea", "token_env"], "GITEA_TOKEN")))
  vars_map["gitea_token"].set(str(_cfg_get(cfg, ["gitea", "token"], "")))
  vars_map["gitea_verify_tls"].set(bool(_cfg_get(cfg, ["gitea", "verify_tls"], True)))
  vars_map["gitea_timeout_s"].set(str(_cfg_get(cfg, ["gitea", "timeout_s"], 30)))
  vars_map["gitea_page_limit"].set(str(_cfg_get(cfg, ["gitea", "page_limit"], 50)))
  vars_map["gitea_user_agent"].set(str(_cfg_get(cfg, ["gitea", "user_agent"], "gitea-repo-sync/1.0")))

  vars_map["sync_clone_missing"].set(bool(_cfg_get(cfg, ["sync", "clone_missing"], True)))
  vars_map["sync_update_existing"].set(bool(_cfg_get(cfg, ["sync", "update_existing"], True)))
  vars_map["sync_archive_missing_remote"].set(bool(_cfg_get(cfg, ["sync", "archive_missing_remote"], True)))
  vars_map["sync_post_update_gc"].set(bool(_cfg_get(cfg, ["sync", "post_update_gc"], False)))
  vars_map["sync_output_dir"].set(str(_cfg_get(cfg, ["sync", "output_dir"], "")))
  vars_map["sync_layout"].set(str(_cfg_get(cfg, ["sync", "layout"], "owner/repo")))
  vars_map["sync_archive_dir_name"].set(str(_cfg_get(cfg, ["sync", "archive_dir_name"], "_archive")))
  vars_map["sync_manifest_file_name"].set(str(_cfg_get(cfg, ["sync", "manifest_file_name"], "_repo_sync_manifest.json")))
  vars_map["sync_archive_stamp_format"].set(str(_cfg_get(cfg, ["sync", "archive_stamp_format"], "%Y%m%d_%H%M%S")))

  vars_map["git_executable"].set(str(_cfg_get(cfg, ["git", "executable"], "git")))
  vars_map["git_protocol"].set(str(_cfg_get(cfg, ["git", "protocol"], "ssh")))
  vars_map["git_fetch_prune"].set(bool(_cfg_get(cfg, ["git", "fetch_prune"], True)))
  vars_map["git_reset_hard_to_origin"].set(bool(_cfg_get(cfg, ["git", "reset_hard_to_origin"], False)))
  vars_map["git_on_dirty_repo"].set(str(_cfg_get(cfg, ["git", "on_dirty_repo"], "skip")))

  vars_map["git_ssh_key_path"].set(str(_cfg_get(cfg, ["git", "ssh", "key_path"], "")))
  vars_map["git_ssh_port"].set(str(_cfg_get(cfg, ["git", "ssh", "port"], 22)))
  vars_map["git_ssh_identity_only"].set(bool(_cfg_get(cfg, ["git", "ssh", "identity_only"], True)))
  vars_map["git_ssh_alias"].set(str(_cfg_get(cfg, ["git", "ssh", "alias"], "")))
  vars_map["git_ssh_clone_url_style"].set(str(_cfg_get(cfg, ["git", "ssh", "clone_url_style"], "scp")))
  vars_map["git_ssh_clone_url_include_user"].set(bool(_cfg_get(cfg, ["git", "ssh", "clone_url_include_user"], False)))

  vars_map["git_https_auth_mode"].set(str(_cfg_get(cfg, ["git", "https_auth", "mode"], "prompt")))
  vars_map["git_https_basic_user"].set(str(_cfg_get(cfg, ["git", "https_auth", "extraheader_basic_user"], "")))

  vars_map["logging_enabled"].set(bool(_cfg_get(cfg, ["logging", "enabled"], True)))
  vars_map["logging_default_dir_name"].set(str(_cfg_get(cfg, ["logging", "default_dir_name"], "_logs")))
  vars_map["logging_dir"].set(str(_cfg_get(cfg, ["logging", "dir"], "")))
  vars_map["logging_file_name"].set(str(_cfg_get(cfg, ["logging", "file_name"], "")))

def _ttk_font(size: int, *, weight: str = "normal"):
  """
  Create a Tk font tuple suitable for ttk widgets.
  Uses Segoe UI on Windows, falls back safely elsewhere.
  """
  return ("Segoe UI", size, weight)

def _ctk_color(c):
  """
  Resolve a CustomTkinter color (single or light/dark tuple)
  into a concrete color string for ttk/Tk.
  """
  if isinstance(c, (tuple, list)):
    # index 1 = dark mode
    return c[1]
  return c

def apply_dark_ttk_treeview_style(root):
  """
  Force ttk.Treeview to visually match CustomTkinter dark mode.
  Safe to call multiple times.
  """

  style = ttk.Style(master=root)

  try:
    style.theme_use("default")
  except Exception:
    pass

  # Pull colors from CustomTkinter theme
  bg     = _ctk_color(ctk.ThemeManager.theme["CTkFrame"]["fg_color"])
  bg_alt = _ctk_color(ctk.ThemeManager.theme["CTkFrame"]["top_fg_color"])
  fg     = _ctk_color(ctk.ThemeManager.theme["CTkLabel"]["text_color"])
  sel_bg = _ctk_color(ctk.ThemeManager.theme["CTkButton"]["hover_color"])
  sel_fg = fg

  style.configure(
    "Treeview",
    background=bg,
    fieldbackground=bg,
    foreground=fg,
    rowheight=28,
    borderwidth=0,
    relief="flat",
    font=_ttk_font(12),
  )

  style.map(
    "Treeview",
    background=[("selected", sel_bg)],
    foreground=[("selected", sel_fg)],
  )

  style.configure(
    "Treeview.Heading",
    background=bg_alt,
    foreground=fg,
    relief="flat",
    borderwidth=0,
    font=_ttk_font(13, weight="bold"),
  )

  style.map(
    "Treeview.Heading",
    background=[("active", bg_alt)],
    foreground=[("active", fg)],
  )

def apply_dark_ttk_scrollbar_style(root):
  """
  Dark-mode ttk.Scrollbar styling to match CustomTkinter theme.
  Windows-compatible.

  Styles:
    - Dark.Vertical.TScrollbar
    - Dark.Horizontal.TScrollbar
  """
  style = ttk.Style(master=root)

  try:
    style.theme_use("default")
  except Exception:
    pass

  # Pull colors from CTk theme
  bg       = _ctk_color(ctk.ThemeManager.theme["CTkFrame"]["fg_color"])
  accent   = _ctk_color(ctk.ThemeManager.theme["CTkButton"]["fg_color"])
  accent_h = _ctk_color(ctk.ThemeManager.theme["CTkButton"]["hover_color"])

  def _configure_scrollbar_style(style_name: str) -> None:
    style.configure(
      style_name,
      background=accent,        # thumb
      troughcolor=bg,           # track
      bordercolor=bg,
      lightcolor=accent,
      darkcolor=accent,
      arrowcolor="#777777",     # default arrow color
      relief="flat",
      borderwidth=0,
      arrowsize=18,
      width=16,
    )

    style.map(
      style_name,
      # Thumb (slider)
      background=[
        ("active", accent_h),
        ("!active", accent),
      ],

      # Arrow buttons — pinned for all states
      arrowcolor=[
        ("active", "#9aa4af"),
        ("pressed", "#9aa4af"),
        ("!active", "#9aa4af"),
        ("disabled", "#666666"),
      ],
    )

  _configure_scrollbar_style("Dark.Vertical.TScrollbar")
  _configure_scrollbar_style("Dark.Horizontal.TScrollbar")


APP_TITLE = "Gitea Repository Mirror - Cure Interactive"

def _gui() -> int:
  """
  Launch the customtkinter GUI.
  """
  try:
    import tkinter as tk
    from tkinter import filedialog
    import customtkinter as ctk
    from CTkToolTip import CTkToolTip
    from tkinter import ttk
  except Exception as e:
    print("ERROR: customtkinter is required for GUI mode.")
    print("Install: pip install customtkinter")
    print(str(e))
    return 3

  script_dir = os.path.dirname(os.path.abspath(__file__))
  default_config_default = resolve_config_default_path(script_dir)
  default_config = os.path.join(script_dir, "config.json")

  ensure_config_json_exists(default_config_default, default_config)

  cfg_loaded = load_config(default_config_default, default_config)

  # Log queue for thread-safe UI updates.
  q: "queue.Queue[str]" = queue.Queue()

  def sink(line: str) -> None:
    q.put(line)

  _LOG_SINKS.append(sink)

  ctk.set_appearance_mode("dark")
  ctk.set_default_color_theme("blue")

  app = ctk.CTk()

  apply_dark_ttk_treeview_style(app)
  apply_dark_ttk_scrollbar_style(app)

  set_window_icon(
    app,
    os.path.join(script_dir, "icon.ico"),
    os.path.join(script_dir, "icon.png"),
  )

  app.title(APP_TITLE)
  app.geometry("1100x760")

  # Vars
  vars_map: t.Dict[str, t.Any] = {}

  vars_map["dry_run"] = tk.BooleanVar(value=True)

  vars_map["gitea_base_url"] = tk.StringVar()
  vars_map["gitea_api_base_path"] = tk.StringVar()
  vars_map["gitea_token_env"] = tk.StringVar()
  vars_map["gitea_token"] = tk.StringVar()
  vars_map["gitea_verify_tls"] = tk.BooleanVar(value=True)
  vars_map["gitea_timeout_s"] = tk.StringVar()
  vars_map["gitea_page_limit"] = tk.StringVar()
  vars_map["gitea_user_agent"] = tk.StringVar()

  vars_map["sync_clone_missing"] = tk.BooleanVar(value=True)
  vars_map["sync_update_existing"] = tk.BooleanVar(value=True)
  vars_map["sync_archive_missing_remote"] = tk.BooleanVar(value=True)
  vars_map["sync_post_update_gc"] = tk.BooleanVar(value=False)
  vars_map["sync_output_dir"] = tk.StringVar()
  vars_map["sync_layout"] = tk.StringVar()
  vars_map["sync_archive_dir_name"] = tk.StringVar()
  vars_map["sync_manifest_file_name"] = tk.StringVar()
  vars_map["sync_archive_stamp_format"] = tk.StringVar()

  vars_map["git_executable"] = tk.StringVar()
  vars_map["git_protocol"] = tk.StringVar()
  vars_map["git_fetch_prune"] = tk.BooleanVar(value=True)
  vars_map["git_reset_hard_to_origin"] = tk.BooleanVar(value=False)
  vars_map["git_on_dirty_repo"] = tk.StringVar()

  vars_map["git_ssh_key_path"] = tk.StringVar()
  vars_map["git_ssh_port"] = tk.StringVar()
  vars_map["git_ssh_identity_only"] = tk.BooleanVar(value=True)
  vars_map["git_ssh_alias"] = tk.StringVar()
  vars_map["git_ssh_clone_url_style"] = tk.StringVar()
  vars_map["git_ssh_clone_url_include_user"] = tk.BooleanVar(value=False)

  vars_map["git_https_auth_mode"] = tk.StringVar()
  vars_map["git_https_basic_user"] = tk.StringVar()

  vars_map["logging_enabled"] = tk.BooleanVar(value=True)
  vars_map["logging_default_dir_name"] = tk.StringVar()
  vars_map["logging_dir"] = tk.StringVar()
  vars_map["logging_file_name"] = tk.StringVar()

  _apply_cfg_to_vars(cfg_loaded, vars_map)

  def _repo_count_from_manifest() -> int:
    """
    Return number of repos currently known to the UI (manifest-backed).
    """
    try:
      return len(load_manifest_for_ui(cfg_loaded))
    except Exception:
      return 0

  # Progress state
  progress_var = tk.DoubleVar(value=0.0)
  _repo_count = _repo_count_from_manifest()
  progress_label_var = tk.StringVar(
    value=f"Idle ({_repo_count} repo{'s' if _repo_count != 1 else ''})"
  )

  # UI helpers
  def autosize_treeview_columns(tree: ttk.Treeview, *, padding: int = 16) -> None:
    """
    Resize Treeview columns to fit header + cell contents.

    Notes:
    - Must be called AFTER rows are inserted/updated
    - Uses current Treeview font
    """
    style = ttk.Style()
    font = style.lookup("Treeview", "font")

    tkfont = None
    try:
      import tkinter.font as tkfont_mod
      tkfont = tkfont_mod.nametofont(font)
    except Exception:
      return

    for col in tree["columns"]:
      # Start with header width
      header = tree.heading(col, "text")
      max_width = tkfont.measure(header)

      # Measure all rows
      for iid in tree.get_children():
        val = tree.set(iid, col)
        if val:
          max_width = max(max_width, tkfont.measure(str(val)))

      tree.column(col, width=max(int(max_width + padding), 32))

  def row(parent, r, label, widget, *, tooltip_text=None, padx=8, pady=6):
    l = ctk.CTkLabel(parent, text=label, anchor="w")
    l.grid(row=r, column=0, sticky="w", padx=padx, pady=pady)
    widget.grid(row=r, column=1, sticky="ew", padx=padx, pady=pady)

    if tooltip_text:
      tooltip(l, widget, text=tooltip_text)

  def row_with_button(parent, r, label, entry, btn, *, tooltip_text=None, tooltip_text_button=None):
    l = ctk.CTkLabel(parent, text=label, anchor="w")
    l.grid(row=r, column=0, sticky="w", padx=8, pady=6)
    entry.grid(row=r, column=1, sticky="ew", padx=8, pady=6)
    btn.grid(row=r, column=2, sticky="ew", padx=8, pady=6)

    if tooltip_text:
      tooltip(l, entry, text=tooltip_text)

    if tooltip_text_button:
      tooltip(btn, text=tooltip_text_button)

  def row_checkbox(parent, r, label, checkbox, *, tooltip_text=None, padx=8, pady=6):
    l = ctk.CTkLabel(parent, text=label, anchor="w")
    l.grid(row=r, column=0, sticky="w", padx=padx, pady=pady)

    checkbox.configure(text="")
    checkbox.grid(row=r, column=1, sticky="w", padx=padx, pady=pady)

    if tooltip_text:
      tooltip(l, checkbox, text=tooltip_text)

    return l

  def section_title(parent: t.Any, text: str) -> None:
    ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=8, pady=(10, 6))

  def safe_combobox(parent, *, values, var, default):
    """
    Create a readonly ComboBox that self-heals invalid values.

    - Prevents typing
    - Forces value ∈ values
    - Repairs bad config.json entries on load
    """
    if var.get() not in values:
      var.set(default)

    return ctk.CTkComboBox(
      parent,
      values=values,
      variable=var,
      state="readonly",
    )

  def enum_dropdown(parent, *, values, var, default):
    """
    HARD enum dropdown (no typing possible).
    Uses CTkOptionMenu (true dropdown).
    """
    if var.get() not in values:
      var.set(default)

    return ctk.CTkOptionMenu(
      parent,
      values=values,
      variable=var,
    )

  def tooltip(*widgets, text: str) -> None:
    """
    Attach (or update) the same tooltip on one or more CTk widgets.

    IMPORTANT:
      CTkToolTip doesn't expose a stable "set text" API across all versions, so we
      cache the instance on the widget and best-effort destroy/close the old one
      before creating a new tooltip.

    Usage:
      tooltip(entry, label, checkbox, text="Explanation")
    """
    for w in widgets:
      if w is None:
        continue

      # Best-effort: remove old tooltip instance if we've attached one before.
      old = getattr(w, "_cure_tooltip", None)
      if old is not None:
        # Try common close/hide methods across versions.
        for fn in ("hide", "hidetip", "destroy", "close", "deactivate"):
          if hasattr(old, fn):
            try:
              getattr(old, fn)()
            except Exception:
              pass

        # Also try destroying any underlying tip window if exposed.
        try:
          tip_win = getattr(old, "tipwindow", None) or getattr(old, "_tip_window", None)
          if tip_win is not None:
            try:
              tip_win.destroy()
            except Exception:
              pass
        except Exception:
          pass

      tip = CTkToolTip(w, message=text)
      setattr(w, "_cure_tooltip", tip)

  def _ui_set_progress(current: int, total: int, label: str) -> None:
    if total <= 0:
      progress_var.set(0)
      progress_label_var.set(label)
      return

    progress_var.set(current / total)
    progress_label_var.set(label)

  def _progress_from_worker(current: int, total: int, label: str) -> None:
    app.after(
      0,
      _ui_set_progress,
      current,
      total,
      label,
    )

  def _repo_status_from_worker(status: RepoStatus):
    app.after(0, _repo_update_row, status)

  # Layout
  tabs = ctk.CTkTabview(app)
  tabs.pack(fill="both", expand=True, padx=12, pady=12)

  tab_run = tabs.add("Run")
  tab_cfg = tabs.add("Config")
  tab_logs = tabs.add("Log")

  cfg_root = ctk.CTkFrame(tab_cfg)
  cfg_root.pack(fill="both", expand=True)

  top_bar = ctk.CTkFrame(cfg_root)
  top_bar.pack(fill="x", padx=8, pady=(8, 4))

  top_bar.grid_columnconfigure(0, weight=1)  # path expands
  top_bar.grid_columnconfigure(1, weight=0)  # status fixed

  # Header state vars (MUST exist before widgets)
  config_path_var = tk.StringVar()
  config_status_var = tk.StringVar()

  # Column 0 — Config path (absolute)
  config_path_lbl = ctk.CTkLabel(
    top_bar,
    textvariable=config_path_var,
    anchor="w",
  )
  config_path_lbl.grid(row=0, column=0, sticky="w", padx=(8, 12))

  # Column 1 — Status text only
  config_status_lbl = ctk.CTkLabel(
    top_bar,
    textvariable=config_status_var,
    anchor="w",
  )
  config_status_lbl.grid(row=0, column=1, sticky="w", padx=(0, 12))

  # Config tab content
  cfg_scroll = ctk.CTkScrollableFrame(cfg_root)
  cfg_scroll.pack(fill="both", expand=True)

  def _update_config_info(*, saved: bool = False, error: str | None = None) -> None:
    """
    Update the config header status.
    Column 0: absolute config path
    Column 1: status text only
    """
    config_path_var.set(os.path.abspath(default_config))

    if saved:
      ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
      config_status_var.set(f"Last saved: {ts}")
    elif error:
      config_status_var.set(f"ERROR: {error}")
    else:
      config_status_var.set("Last saved: —")

  _update_config_info(saved=False)

  def _save_to_disk() -> None:
    nonlocal cfg_loaded
    try:
      cfg_new = _build_cfg_from_vars(cfg_loaded, vars_map)
      cfg_loaded = cfg_new
      _write_json_file(default_config, cfg_new)
      _update_config_info(saved=True)
      _log("[🧩 GUI]", "Saved config.json from UI.")
      _update_config_info(saved=True)
    except Exception as e:
      _update_config_info(error=str(e))
      _log("[❌ ERR]", f"Save failed: {e}")

  # ---------------------------------------------------------
  # Auto-save config on any UI change (debounced)
  # ---------------------------------------------------------

  _autosave_after_id = {"id": None}

  def _autosave_cfg() -> None:
    # Cancel pending save
    if _autosave_after_id["id"] is not None:
      try:
        app.after_cancel(_autosave_after_id["id"])
      except Exception:
        pass

    # Schedule save shortly after last change
    _autosave_after_id["id"] = app.after(300, _save_to_disk)

  # ---------------------------------------------------------
  # Attach auto-save to all Tk variables (AFTER autosave exists)
  # ---------------------------------------------------------

  for k, v in vars_map.items():
    if hasattr(v, "trace_add"):
      try:
        v.trace_add("write", lambda *_args: _autosave_cfg())
      except Exception:
        pass

  # Dry run toggle
  dry_frame = ctk.CTkFrame(cfg_scroll)
  dry_frame.pack(fill="x", padx=8, pady=6)
  dry_run_checkbox = ctk.CTkCheckBox(dry_frame, text="Dry Run (no changes)", variable=vars_map["dry_run"])
  dry_run_checkbox.pack(anchor="w", padx=8, pady=8)
  tooltip(dry_run_checkbox,
    text = "Dry Run mode:\n"
    "- Commands are printed\n"
    "- No files are modified\n"
    "- No git operations are executed"
  )

  # Gitea section
  section_title(cfg_scroll, "Gitea")
  g = ctk.CTkFrame(cfg_scroll)
  g.pack(fill="x", padx=8, pady=6)
  g.grid_columnconfigure(1, weight=1)

  base_url_entry = ctk.CTkEntry(g, textvariable=vars_map["gitea_base_url"])
  row(
    g,
    0,
    "Base URL",
    base_url_entry,
    tooltip_text=(
      "Base URL of the Gitea instance\n"
      "Example: https://git.example.com"
    )
  )

  api_path_entry = ctk.CTkEntry(g, textvariable=vars_map["gitea_api_base_path"])
  row(
    g,
    1,
    "API Base Path",
    api_path_entry,
    tooltip_text=(
      "Gitea API base path\n"
      "Default: /api/v1"
    ),
  )

  token_env_entry = ctk.CTkEntry(g, textvariable=vars_map["gitea_token_env"])
  row(
    g,
    2,
    "Token Env Var",
    token_env_entry,
    tooltip_text=(
      "Environment variable name that contains your Gitea API token\n"
      "Recommended over storing token in config.json"
    ),
  )

  token_entry = ctk.CTkEntry(g, textvariable=vars_map["gitea_token"], show="•")
  row(
    g,
    3,
    "Token (optional; env recommended)",
    token_entry,
    tooltip_text=(
      "Optional: paste token directly\n"
      "NOT recommended for shared machines"
    ),
  )

  timeout_entry = ctk.CTkEntry(g, textvariable=vars_map["gitea_timeout_s"])
  row(
    g,
    4,
    "Timeout (seconds)",
    timeout_entry,
    tooltip_text=(
      "HTTP request timeout in seconds\n"
      "Increase if API requests are slow"
    ),
  )

  page_limit_entry = ctk.CTkEntry(g, textvariable=vars_map["gitea_page_limit"])
  row(
    g,
    5,
    "Page Limit",
    page_limit_entry,
    tooltip_text=(
      "Number of repositories fetched per API page\n"
      "Higher = fewer requests"
    ),
  )

  user_agent_entry = ctk.CTkEntry(g, textvariable=vars_map["gitea_user_agent"])
  row(
    g,
    6,
    "User-Agent",
    user_agent_entry,
    tooltip_text="Custom User-Agent string sent to Gitea API",
  )

  verify_tls_cb = ctk.CTkCheckBox(
    g,
    text="",
    variable=vars_map["gitea_verify_tls"],
  )
  row_checkbox(
    g,
    7,
    "Verify TLS",
    verify_tls_cb,
    tooltip_text=(
      "Enable TLS certificate validation\n"
      "Disable only for self-signed certificates"
    ),
  )

  section_title(cfg_scroll, "Sync")
  s = ctk.CTkFrame(cfg_scroll)
  s.pack(fill="x", padx=8, pady=6)
  s.grid_columnconfigure(1, weight=1)

  def _browse_output_dir() -> None:
    d = filedialog.askdirectory()
    if d:
      vars_map["sync_output_dir"].set(str(RootPath(d)))

  out_entry = ctk.CTkEntry(s, textvariable=vars_map["sync_output_dir"])
  out_btn = ctk.CTkButton(s, text="Browse…", command=_browse_output_dir, width=120)
  row_with_button(
    s,
    0,
    "Output Dir",
    out_entry,
    out_btn,
    tooltip_text="Root directory where repositories are cloned",
    tooltip_text_button="Browse for output directory",
  )

  layout_box = enum_dropdown(
    s,
    values=["owner/repo", "flat", "owner__repo"],
    var=vars_map["sync_layout"],
    default="owner/repo",
  )
  row(
    s,
    1,
    "Layout",
    layout_box,
    tooltip_text=(
      "Folder structure for repositories:\n"
      "- owner/repo\n"
      "- flat\n"
      "- owner__repo"
    ),
  )

  archive_dir_entry = ctk.CTkEntry(s, textvariable=vars_map["sync_archive_dir_name"])
  row(
    s,
    2,
    "Archive Dir Name",
    archive_dir_entry,
    tooltip_text="Directory name used to store archived repositories",
  )

  manifest_entry = ctk.CTkEntry(s, textvariable=vars_map["sync_manifest_file_name"])
  row(
    s,
    3,
    "Manifest File Name",
    manifest_entry,
    tooltip_text=(
      "Internal manifest file tracking managed repositories\n"
      "Do not edit manually"
    ),
  )

  stamp_entry = ctk.CTkEntry(s, textvariable=vars_map["sync_archive_stamp_format"])
  row(
    s,
    4,
    "Archive Stamp Format",
    stamp_entry,
    tooltip_text=(
      "Datetime format for archive folders\n"
      "Uses Python strftime syntax"
    ),
  )

  clone_missing_cb = ctk.CTkCheckBox(
    s,
    text="",
    variable=vars_map["sync_clone_missing"],
  )
  row_checkbox(
    s,
    5,
    "Clone missing repos",
    clone_missing_cb,
    tooltip_text="Clone repositories that do not yet exist locally",
  )

  update_existing_cb = ctk.CTkCheckBox(
    s,
    text="",
    variable=vars_map["sync_update_existing"],
  )
  row_checkbox(
    s,
    6,
    "Update existing repos",
    update_existing_cb,
    tooltip_text="Fetch and update repositories that already exist",
  )

  archive_missing_cb = ctk.CTkCheckBox(
    s,
    text="",
    variable=vars_map["sync_archive_missing_remote"],
  )
  row_checkbox(
    s,
    7,
    "Archive missing remote repos",
    archive_missing_cb,
    tooltip_text="Move local repos to archive if they no longer exist on the server",
  )

  post_gc_cb = ctk.CTkCheckBox(
    s,
    text="",
    variable=vars_map["sync_post_update_gc"],
  )
  row_checkbox(
    s,
    8,
    "Compact mirrors after sync",
    post_gc_cb,
    tooltip_text=(
      "Opt-in mirror cleanup after clone/update.\n"
      "Runs git reflog expire + git gc --prune=now --aggressive.\n"
      "Useful when remote history is rewritten, but slower on large repos."
    ),
  )

  # Git section
  section_title(cfg_scroll, "Git")
  gg = ctk.CTkFrame(cfg_scroll)
  gg.pack(fill="x", padx=8, pady=6)
  gg.grid_columnconfigure(1, weight=1)

  git_exec_entry = ctk.CTkEntry(gg, textvariable=vars_map["git_executable"])
  row(
    gg,
    0,
    "Git Executable",
    git_exec_entry,
    tooltip_text="Path or command name for git executable",
  )

  proto_box = enum_dropdown(
    gg,
    values=["ssh", "https"],
    var=vars_map["git_protocol"],
    default="ssh",
  )
  row(
    gg,
    1,
    "Protocol",
    proto_box,
    tooltip_text=(
      "ssh = SSH key authentication\n"
      "https = HTTPS token authentication"
    ),
  )

  fetch_prune_cb = ctk.CTkCheckBox(
    gg,
    text="",
    variable=vars_map["git_fetch_prune"],
  )
  fetch_prune_lbl = row_checkbox(
    gg,
    2,
    "Fetch prune",
    fetch_prune_cb,
    tooltip_text="(Disabled in mirror mode).\nRemove remote-tracking branches that no longer exist",
  )

  reset_cb = ctk.CTkCheckBox(
    gg,
    text="",
    variable=vars_map["git_reset_hard_to_origin"],
  )
  reset_lbl = row_checkbox(
    gg,
    3,
    "Reset hard to origin/<default_branch>",
    reset_cb,
    tooltip_text=(
      "(Disabled in mirror mode).\n"
      "Force local branch to match origin/<default_branch>\n"
      "WARNING: Discards local changes"
    ),
  )

  dirty_lbl = ctk.CTkLabel(gg, text="On dirty repo", anchor="w")
  dirty_lbl.grid(row=4, column=0, sticky="w", padx=8, pady=6)
  dirty_box = enum_dropdown(
    gg,
    values=["skip"],
    var=vars_map["git_on_dirty_repo"],
    default="skip",
  )
  dirty_box.grid(row=4, column=1, sticky="ew", padx=8, pady=6)
  tooltip(
    dirty_lbl,
    dirty_box,
    text="(Disabled in mirror mode).\nBehavior when local repo has uncommitted changes",
  )

  def _label_default_text_color():
    return ctk.ThemeManager.theme["CTkLabel"]["text_color"]

  def _checkbox_default_colors():
    t = ctk.ThemeManager.theme["CTkCheckBox"]
    return {
      "fg_color": t["fg_color"],
      "hover_color": t["hover_color"],
      "border_color": t["border_color"],
      "checkmark_color": t["checkmark_color"],
      "text_color": t["text_color"],
    }

  def _optionmenu_default_colors():
    t = ctk.ThemeManager.theme["CTkOptionMenu"]
    return {
      "fg_color": t["fg_color"],
      "button_color": t["button_color"],
      "button_hover_color": t["button_hover_color"],
      "text_color": t["text_color"],
    }

  _DISABLED_LABEL_COLOR = "#777777"
  _DISABLED_CB_BORDER = "#555555"
  _DISABLED_CB_BG = "#444444"
  _DISABLED_CB_CHECK = "#666666"
  _DISABLED_OM_BG = "#444444"
  _DISABLED_OM_BTN = "#555555"

  def _checkbox_disabled_colors():
    return {
      "fg_color": _DISABLED_CB_BG,
      "border_color": _DISABLED_CB_BORDER,
      "checkmark_color": _DISABLED_CB_CHECK,
      "hover_color": _DISABLED_CB_BG,
      "text_color": _DISABLED_LABEL_COLOR,
    }

  def _optionmenu_disabled_colors():
    return {
      "fg_color": _DISABLED_OM_BG,
      "button_color": _DISABLED_OM_BTN,
      "button_hover_color": _DISABLED_OM_BTN,
      "text_color": _DISABLED_LABEL_COLOR,
    }

  # SSH subsection
  section_title(cfg_scroll, "Git SSH")
  ssh = ctk.CTkFrame(cfg_scroll)
  ssh.pack(fill="x", padx=8, pady=6)
  ssh.grid_columnconfigure(1, weight=1)

  def _browse_key_file() -> None:
    f = filedialog.askopenfilename(title="Select SSH private key")
    if f:
      vars_map["git_ssh_key_path"].set(f)

  ssh_port_entry = ctk.CTkEntry(ssh, textvariable=vars_map["git_ssh_port"])
  row(
    ssh,
    0,
    "SSH Port",
    ssh_port_entry,
    tooltip_text="SSH port used when connecting to Gitea",
  )

  key_entry = ctk.CTkEntry(ssh, textvariable=vars_map["git_ssh_key_path"])
  key_btn = ctk.CTkButton(ssh, text="Browse…", command=_browse_key_file, width=120)
  row_with_button(
    ssh,
    1,
    "SSH Key Path (optional)",
    key_entry,
    key_btn,
    tooltip_text="Private SSH key used for authentication",
    tooltip_text_button="Browse for SSH private key file",
  )

  alias_entry = ctk.CTkEntry(ssh, textvariable=vars_map["git_ssh_alias"])
  row(
    ssh,
    2,
    "SSH Alias (overrides SSH Key Path)",
    alias_entry,
    tooltip_text=(
      "SSH host alias defined in ~/.ssh/config\n"
      "Overrides key_path and port"
    ),
  )

  style_box = enum_dropdown(
    ssh,
    values=["scp", "ssh_url"],
    var=vars_map["git_ssh_clone_url_style"],
    default="scp",
  )
  row(
    ssh,
    3,
    "Clone URL Style (when ssh_alias used)",
    style_box,
    tooltip_text="How clone URLs are rewritten when alias is used",
  )

  include_user_cb = ctk.CTkCheckBox(
    ssh,
    text="",
    variable=vars_map["git_ssh_clone_url_include_user"],
  )
  row_checkbox(
    ssh,
    4,
    "Include user in rewritten URL",
    include_user_cb,
    tooltip_text="Include username in rewritten SSH URL",
  )

  identity_cb = ctk.CTkCheckBox(
    ssh,
    text="",
    variable=vars_map["git_ssh_identity_only"],
  )
  row_checkbox(
    ssh,
    5,
    "IdentitiesOnly=yes",
    identity_cb,
    tooltip_text=(
      "Force SSH to use only the specified key\n"
      "Prevents SSH agent interference"
    ),
  )

  # HTTPS auth subsection
  section_title(cfg_scroll, "Git HTTPS Auth")
  ha = ctk.CTkFrame(cfg_scroll)
  ha.pack(fill="x", padx=8, pady=6)
  ha.grid_columnconfigure(1, weight=1)

  mode_box = enum_dropdown(
    ha,
    values=["prompt", "extraheader"],
    var=vars_map["git_https_auth_mode"],
    default="prompt",
  )
  row(
    ha,
    0,
    "HTTPS Auth Mode",
    mode_box,
    tooltip_text=(
      "prompt = git prompts for credentials\n"
      "extraheader = inject Authorization header"
    ),
  )

  basic_user_entry = ctk.CTkEntry(ha, textvariable=vars_map["git_https_basic_user"])
  row(
    ha,
    1,
    "extraheader basic user (optional)",
    basic_user_entry,
    tooltip_text="Username used in HTTPS basic authentication header",
  )

  # Logging subsection
  section_title(cfg_scroll, "Logging")
  lg = ctk.CTkFrame(cfg_scroll)
  lg.pack(fill="x", padx=8, pady=6)
  lg.grid_columnconfigure(1, weight=1)

  logging_enabled_cb = ctk.CTkCheckBox(
    lg,
    text="",
    variable=vars_map["logging_enabled"],
  )
  row_checkbox(
    lg,
    0,
    "Enable file logging",
    logging_enabled_cb,
    tooltip_text="Enable writing logs to disk",
  )

  log_dir_name_entry = ctk.CTkEntry(lg, textvariable=vars_map["logging_default_dir_name"])
  row(
    lg,
    1,
    "Default log dir name",
    log_dir_name_entry,
    tooltip_text="Directory name created under output_dir for logs",
  )

  log_dir_override_entry = ctk.CTkEntry(lg, textvariable=vars_map["logging_dir"])
  row(
    lg,
    2,
    "Log dir override (optional)",
    log_dir_override_entry,
    tooltip_text=(
      "Override log directory location\n"
      "Leave empty to use default"
    ),
  )

  log_file_entry = ctk.CTkEntry(lg, textvariable=vars_map["logging_file_name"])
  row(
    lg,
    3,
    "Log file name override (optional)",
    log_file_entry,
    tooltip_text=(
      "Override log file name\n"
      "Leave empty for timestamped default"
    ),
  )

  # Run tab
  run_frame = ctk.CTkFrame(tab_run)
  run_frame.pack(fill="both", expand=True, padx=12, pady=12)

  run_frame.grid_columnconfigure(0, weight=1)
  run_frame.grid_rowconfigure(3, weight=1)

  run_btns = ctk.CTkFrame(run_frame)
  run_btns.grid(row=0, column=0, sticky="ew", padx=10, pady=10)

  run_btns.grid_columnconfigure(0, weight=1)  # Run / SAFE STOP expands
  run_btns.grid_columnconfigure(1, weight=0)  # Sync selected button stays tight
  run_btns.grid_columnconfigure(2, weight=0)  # Clone selected button stays tight

  # Create selection buttons EARLY so later code can .configure() them safely.
  # (We wire their commands + enable/disable state after the Treeview exists.)
  sync_selected_btn = ctk.CTkButton(
    run_btns,
    text="Sync Selected…",
    state="disabled",
  )
  sync_selected_btn.grid(row=0, column=1, sticky="e", padx=(4, 4), pady=6)
  tooltip(
    sync_selected_btn,
    text=(
      "Sync only the selected repositories\n"
      "from the remote into their local mirrors.\n"
      "Selected-only sync does not archive other repos."
    ),
  )

  clone_selected_btn = ctk.CTkButton(
    run_btns,
    text="Clone Selected…",
    state="disabled",
  )
  clone_selected_btn.grid(row=0, column=2, sticky="e", padx=(4, 6), pady=6)
  tooltip(
    clone_selected_btn,
    text=(
      "Clone the selected repositories\n"
      "from your local mirrors into a folder you choose."
    ),
  )

  # ---------------------------------------------------------
  # Progress Bar (between Run buttons and logs)
  # ---------------------------------------------------------

  progress_frame = ctk.CTkFrame(run_frame)
  progress_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
  progress_frame.grid_columnconfigure(0, weight=1)

  progress_bar = ctk.CTkProgressBar(
    progress_frame,
    variable=progress_var,
  )
  progress_bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
  progress_bar.set(0)
  tooltip(progress_bar, text = "Overall progress of repository synchronization")

  progress_label = ctk.CTkLabel(
    progress_frame,
    textvariable=progress_label_var,
    anchor="w",
  )
  progress_label.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 6))
  tooltip(progress_label, text = "Current repository being processed")

  # ---------------------------------------------------------
  # Repo Status Table (REAL ttk.Treeview)
  # ---------------------------------------------------------

  repo_table_frame = ctk.CTkFrame(run_frame)
  repo_table_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 6))
  run_frame.grid_rowconfigure(3, weight=1)

  repo_table_frame.grid_columnconfigure(0, weight=1)
  repo_table_frame.grid_rowconfigure(0, weight=1)

  repo_tree = ttk.Treeview(
    repo_table_frame,
    columns=("idx", "repo", "last_commit", "status", "last_seen", "last_backup"),
    show="headings",
    height=12,
    selectmode="extended",
  )

  repo_tree.configure(show="headings")
  repo_tree.configure(style="Treeview")

  repo_tree.tag_configure(
    "odd",
    background=_ctk_color(ctk.ThemeManager.theme["CTkFrame"]["fg_color"]),
  )

  repo_tree.tag_configure(
    "even",
    background=_ctk_color(ctk.ThemeManager.theme["CTkFrame"]["top_fg_color"]),
  )

  # Status row colors (tinted zebra variants so odd/even still show)
  repo_tree.tag_configure("status_done_even", background="#1f3d2b")     # dark green (even)
  repo_tree.tag_configure("status_done_odd",  background="#244632")     # dark green (odd)

  repo_tree.tag_configure("status_error_even", background="#3d1f1f")    # dark red (even)
  repo_tree.tag_configure("status_error_odd",  background="#462424")    # dark red (odd)

  repo_tree.tag_configure("status_fetching_even", background="#1f2f3d") # dark blue (even)
  repo_tree.tag_configure("status_fetching_odd",  background="#243746") # dark blue (odd)

  repo_tree.tag_configure("status_cloning_even", background="#3d3320")  # dark amber (even)
  repo_tree.tag_configure("status_cloning_odd",  background="#463a24")  # dark amber (odd)

  repo_tree.heading("idx", text="#", anchor="center")
  repo_tree.heading("repo", text="Repository", anchor="w")
  repo_tree.heading("last_commit", text="Last Commit", anchor="w")
  repo_tree.heading("status", text="Status", anchor="center")
  repo_tree.heading("last_seen", text="Last Seen", anchor="w")
  repo_tree.heading("last_backup", text="Last Backup", anchor="w")

  repo_tree.column("idx", width=48, minwidth=32, stretch=False, anchor="center")
  repo_tree.column("repo", width=360, minwidth=32, stretch=True, anchor="w")
  repo_tree.column("last_commit", width=520, minwidth=32, stretch=True, anchor="w")
  repo_tree.column("status", width=120, minwidth=32, stretch=False, anchor="center")
  repo_tree.column("last_seen", width=180, minwidth=32, stretch=False, anchor="w")
  repo_tree.column("last_backup", width=180, minwidth=32, stretch=False, anchor="w")

  ysb = ttk.Scrollbar(
    repo_table_frame,
    orient="vertical",
    command=repo_tree.yview,
    style="Dark.Vertical.TScrollbar",
  )

  xsb = ttk.Scrollbar(
    repo_table_frame,
    orient="horizontal",
    command=repo_tree.xview,
    style="Dark.Horizontal.TScrollbar",
  )

  repo_tree.configure(yscroll=ysb.set, xscroll=xsb.set)

  repo_tree.grid(row=0, column=0, sticky="nsew")
  ysb.grid(row=0, column=1, sticky="ns")
  xsb.grid(row=1, column=0, sticky="ew")

  repo_table_frame.grid_rowconfigure(1, weight=0)

  # ---------------------------------------------------------
  # Auto-fit Treeview columns to container width (NO initial spill)
  # - Fixed columns keep their widths.
  # - Stretch columns ("repo", "last_commit") share remaining space.
  # ---------------------------------------------------------

  _fit_after_id = {"id": None}

  def _repo_tree_fit_columns_now() -> None:
    """
    Fit stretch columns to current container width so table does not spill past the right edge.
    Runs safely even during initial layout when width may be tiny; will retry.
    """
    try:
      frame_w = int(repo_table_frame.winfo_width() or 0)
    except Exception:
      frame_w = 0

    # During initial window mapping this can be 1px; retry shortly.
    if frame_w < 100:
      app.after(50, _repo_tree_fit_columns_now)
      return

    # Subtract vertical scrollbar width if available.
    try:
      sb_w = int(ysb.winfo_width() or ysb.winfo_reqwidth() or 16)
    except Exception:
      sb_w = 16

    avail = max(frame_w - sb_w - 6, 64)

    # Fixed columns: keep as-is
    fixed_cols = ("idx", "status", "last_seen", "last_backup")
    fixed_w = 0
    for c in fixed_cols:
      try:
        fixed_w += int(repo_tree.column(c, "width") or 0)
      except Exception:
        pass

    stretch_avail = max(avail - fixed_w, 64)

    # Use current widths as weights
    try:
      w_repo = int(repo_tree.column("repo", "width") or 1)
    except Exception:
      w_repo = 360
    try:
      w_commit = int(repo_tree.column("last_commit", "width") or 1)
    except Exception:
      w_commit = 520

    total = max(w_repo + w_commit, 1)

    min_w = 32
    new_repo = max(min_w, int(stretch_avail * (w_repo / total)))
    new_commit = max(min_w, int(stretch_avail - new_repo))

    # Apply
    repo_tree.column("repo", width=new_repo)
    repo_tree.column("last_commit", width=new_commit)

  def _repo_tree_fit_columns_debounced(_event=None) -> None:
    """
    Debounce resize events (Treeview can fire many).
    """
    if _fit_after_id["id"] is not None:
      try:
        app.after_cancel(_fit_after_id["id"])
      except Exception:
        pass
    _fit_after_id["id"] = app.after(30, _repo_tree_fit_columns_now)

  # Fit on container resize + initial show
  repo_table_frame.bind("<Configure>", _repo_tree_fit_columns_debounced)

  # ---------------------------------------------------------
  # Treeview cell tooltip (hover shows the currently-hovered cell value)
  # ---------------------------------------------------------

  _tree_tip_state = {
    "win": None,
    "lbl": None,
    "font": None,  # cached tk Font for tooltip
    "last": None,  # (iid, col_id, value)
  }

  def _tree_tip_hide() -> None:
    win = _tree_tip_state.get("win")
    if win is not None:
      try:
        win.destroy()
      except Exception:
        pass
    _tree_tip_state["win"] = None
    _tree_tip_state["lbl"] = None
    _tree_tip_state["last"] = None

  def _tree_tip_show(*, text: str, x_root: int, y_root: int) -> None:
    # Create if needed
    if _tree_tip_state["win"] is None:
      win = tk.Toplevel(app)
      win.withdraw()
      win.overrideredirect(True)
      try:
        win.attributes("-topmost", True)
      except Exception:
        pass

      bg = _ctk_color(ctk.ThemeManager.theme["CTkFrame"]["top_fg_color"])
      fg = _ctk_color(ctk.ThemeManager.theme["CTkLabel"]["text_color"])

      # Build a larger tooltip font based on the current Tk default font
      from tkinter import font as tkfont

      if _tree_tip_state["font"] is None:
        f = tkfont.nametofont("TkDefaultFont").copy()
        # Increase by +4; adjust this number to taste
        try:
          f.configure(size=int(f.cget("size")) + 4)
        except Exception:
          f.configure(size=14)
        _tree_tip_state["font"] = f

      lbl = tk.Label(
        win,
        text="",
        justify="left",
        anchor="w",
        padx=8,
        pady=4,
        bg=bg,
        fg=fg,
        font=_tree_tip_state["font"],
        bd=1,
        relief="solid",
      )

      lbl.pack()

      _tree_tip_state["win"] = win
      _tree_tip_state["lbl"] = lbl

    win = _tree_tip_state["win"]
    lbl = _tree_tip_state["lbl"]
    if win is None or lbl is None:
      return

    lbl.configure(text=text)

    # Offset from cursor so it doesn't sit under the mouse
    x = x_root + 14
    y = y_root + 16

    try:
      win.geometry(f"+{x}+{y}")
      win.deiconify()
    except Exception:
      pass

  def _repo_tree_on_hover(event) -> None:
    iid = repo_tree.identify_row(event.y)
    col = repo_tree.identify_column(event.x)  # "#1", "#2", ...
    if not iid or not col or col == "#0":
      _tree_tip_hide()
      return

    # Map "#N" -> actual column id from repo_tree["columns"]
    cols = list(repo_tree["columns"])
    try:
      idx = int(col[1:]) - 1
    except Exception:
      _tree_tip_hide()
      return

    if idx < 0 or idx >= len(cols):
      _tree_tip_hide()
      return

    col_id = cols[idx]

    try:
      val = repo_tree.set(iid, col_id)
    except Exception:
      _tree_tip_hide()
      return

    text = str(val)

    key = (iid, col_id, text)
    if _tree_tip_state["last"] != key:
      _tree_tip_state["last"] = key
      _tree_tip_show(text=text, x_root=event.x_root, y_root=event.y_root)
    else:
      # Same cell/value: just track cursor position smoothly
      _tree_tip_show(text=text, x_root=event.x_root, y_root=event.y_root)

  # Mouse bindings
  repo_tree.bind("<Motion>", _repo_tree_on_hover)
  repo_tree.bind("<Leave>", lambda _e: _tree_tip_hide())
  repo_tree.bind("<ButtonPress>", lambda _e: _tree_tip_hide())
  repo_tree.bind("<MouseWheel>", lambda _e: _tree_tip_hide())     # Windows
  repo_tree.bind("<Button-4>", lambda _e: _tree_tip_hide())       # Linux scroll up
  repo_tree.bind("<Button-5>", lambda _e: _tree_tip_hide())       # Linux scroll down

  def _resolve_repo_refs_from_iids(iids: t.Iterable[str]) -> list[RepoRef]:
    """
    Resolve selected rows → local mirror paths.
    """
    repos: list[RepoRef] = []
    manifest = load_manifest_for_ui(cfg_loaded)
    root = _effective_output_root(cfg_loaded)

    for iid in iids:
      values = repo_tree.item(iid, "values")
      if not values:
        continue

      full_name = str(values[1]).strip()
      info = manifest.get(full_name) or {}
      rel = str(info.get("rel_path") or "").strip()
      if not rel:
        continue

      repos.append({
        "full_name": full_name,
        "rel_path": rel,
        "abs_path": LogicalPath(rel).to_os(root),
      })

    return repos

  def _clone_out_to_selected_folder(repos: list[RepoRef]) -> None:
    """
    Prompt for destination folder, then clone out as working copies from local mirrors.
    """
    if not repos:
      return

    dest_root = filedialog.askdirectory(title="Select folder to clone into")
    if not dest_root:
      return

    cfg_now = _build_cfg_from_vars(cfg_loaded, vars_map)
    git_exe = t.cast(str, cfg_now["git"]["executable"])
    layout = t.cast(str, cfg_now["sync"]["layout"])
    dry_run = bool(cfg_now.get("dry_run", True))

    def _worker():
      _sep("CLONE OUT (LOCAL)")
      for r in repos:
        full_name = r["full_name"]
        src_abs = r["abs_path"]

        owner = full_name.split("/", 1)[0] if "/" in full_name else ""
        repo_name = full_name.split("/", 1)[1] if "/" in full_name else full_name

        rel_out = _assert_logical(_normalize_layout_relpath(layout, owner=owner, repo=repo_name))
        dst_abs = LogicalPath(str(rel_out)).to_os(dest_root)

        _log("[📤 CLONE]", f"{full_name} -> {dst_abs}")

        if os.path.exists(dst_abs):
          _log("[⚠️ WARN]", f"Destination exists; skipping: {dst_abs}")
          continue

        if not os.path.exists(src_abs):
          _log("[❌ ERR]", f"Mirror missing; cannot clone out: {src_abs}")
          continue

        rc = _git(
          git_exe,
          ["clone", src_abs, dst_abs],
          cwd=None,
          dry_run=dry_run,
          extra_git_config=None,
        )
        if rc != 0:
          _log("[❌ ERR]", f"Clone failed: {full_name}")

      _log("[✅ DONE]", "Clone out complete.")

    threading.Thread(target=_worker, daemon=True).start()

  def _selected_full_names_from_tree() -> list[str]:
    try:
      iids = repo_tree.selection()
    except Exception:
      return []

    names: list[str] = []
    for iid in iids:
      values = repo_tree.item(iid, "values")
      if not values:
        continue
      full_name = str(values[1]).strip()
      if full_name:
        names.append(full_name)
    return names

  def _update_selected_action_buttons_state(*_e):
    try:
      any_selected = bool(repo_tree.selection())
    except Exception:
      any_selected = False
    state = "normal" if any_selected and not RUN_STATE.running else "disabled"
    sync_selected_btn.configure(state=state)
    clone_selected_btn.configure(state=state)

  def _sync_selected_clicked():
    selected_full_names = _selected_full_names_from_tree()
    if not selected_full_names:
      return
    _start_run(force_dry=None, selected_full_names=selected_full_names)

  def _clone_selected_clicked():
    repos = _resolve_repo_refs_from_iids(repo_tree.selection())
    _clone_out_to_selected_folder(repos)

  sync_selected_btn.configure(command=_sync_selected_clicked)
  clone_selected_btn.configure(command=_clone_selected_clicked)
  repo_tree.bind("<<TreeviewSelect>>", _update_selected_action_buttons_state)
  _update_selected_action_buttons_state()

  repo_menu = tk.Menu(repo_tree, tearoff=0)
  repo_menu.add_command(
    label="Sync Selected from Remote",
    command=lambda: _start_run(force_dry=None, selected_full_names=_selected_full_names_from_tree()),
  )
  repo_menu.add_command(
    label="Clone to Folder…",
    command=lambda: _clone_out_to_selected_folder(_resolve_repo_refs_from_iids(repo_tree.selection())),
  )

  def _on_repo_right_click(event):
    iid = repo_tree.identify_row(event.y)
    if iid:
      repo_tree.selection_set(iid)
      _update_selected_action_buttons_state()
      repo_menu.tk_popup(event.x_root, event.y_root)

  repo_tree.bind("<Button-3>", _on_repo_right_click)

  _repo_tree_iids: dict[str, str] = {}

  def _repo_seed_from_manifest():
    repo_tree.delete(*repo_tree.get_children())
    _repo_tree_iids.clear()

    manifest_repos = load_manifest_for_ui(cfg_loaded)

    for idx, (full_name, info) in enumerate(
      sorted(manifest_repos.items()),
      start=1,
    ):
      commit_cell = _format_last_commit_cell(
        commit_at=str(info.get("last_commit_at") or ""),
        commit_hash_short=str(info.get("last_commit_hash_short") or ""),
        commit_message=str(info.get("last_commit_message") or ""),
      )

      iid = repo_tree.insert(
        "",
        "end",
        values=(
          idx,
          full_name,
          commit_cell,
          "Idle",
          str(info.get("last_seen_at", "—")).replace("T", " - "),
          str(info.get("last_backup_at", "Never")).replace("T", " - "),
        ),
        tags=("even" if idx % 2 == 0 else "odd",),
      )
      _repo_tree_iids[full_name] = iid

    # Keep configured widths; do NOT autosize (commit messages can be huge).
    repo_tree.column("idx", stretch=False, width=48, minwidth=32)
    repo_tree.column("status", stretch=False, width=120, minwidth=32)

  def _repo_update_row(status: RepoStatus):
    name = status["name"]
    phase = status["phase"]
    preserve_position = bool(status.get("preserve_position"))
    idx = status.get("index", "")
    idx_int: int | None = None
    try:
      idx_int = int(str(idx))
    except Exception:
      idx_int = None

    if phase == "done":
      label = "Done"
      status_tag_base = "status_done"
    elif phase == "error":
      label = "Failed"
      status_tag_base = "status_error"
    elif phase == "fetching":
      label = "Syncing"
      status_tag_base = "status_fetching"
    elif phase == "cloning":
      label = "Cloning"
      status_tag_base = "status_cloning"
    elif phase == "queued":
      label = "Queued"
      status_tag_base = None
    else:
      label = str(phase).capitalize()
      status_tag_base = None

    last_seen_raw = status.get("last_seen_at") or status.get("seen_at") or "—"
    last_seen = str(last_seen_raw).replace("T", " - ")

    last_backup_raw = status.get("last_backup_at") or ""
    last_backup = str(last_backup_raw).replace("T", " - ") if last_backup_raw else ""

    incoming_commit_cell = _format_last_commit_cell(
      commit_at=str(status.get("last_commit_at") or ""),
      commit_hash_short=str(status.get("last_commit_hash_short") or ""),
      commit_message=str(status.get("last_commit_message") or ""),
    )

    if name in _repo_tree_iids:
      iid = _repo_tree_iids[name]
      old_vals = repo_tree.item(iid, "values") or ()
      row_idx = old_vals[0] if (preserve_position and old_vals) else (idx if idx_int is not None else (old_vals[0] if old_vals else idx))
      try:
        row_idx_int = int(str(row_idx))
      except Exception:
        row_idx_int = idx_int
      zebra = "even" if (row_idx_int is not None and row_idx_int % 2 == 0) else "odd"
      tag = f"{status_tag_base}_{zebra}" if status_tag_base else zebra

      repo_tree.item(
        iid,
        values=(
          row_idx,
          name,
          incoming_commit_cell,
          label,
          last_seen,
          last_backup,
        ),
        tags=(tag,),
      )

      # Keep visual order aligned with current run index.
      if idx_int is not None and not preserve_position:
        children = repo_tree.get_children()
        if children:
          target = max(0, min(idx_int - 1, len(children) - 1))
          repo_tree.move(iid, "", target)
      return

    if idx_int is not None:
      zebra = "even" if (idx_int % 2 == 0) else "odd"
    else:
      zebra = "odd"
    tag = f"{status_tag_base}_{zebra}" if status_tag_base else zebra

    iid = repo_tree.insert(
      "",
      "end",
      values=(
        idx,
        name,
        incoming_commit_cell,
        label,
        last_seen,
        last_backup,
      ),
      tags=(tag,),
    )
    _repo_tree_iids[name] = iid

    # New rows discovered during a run should still land at their run index.
    if idx_int is not None:
      children = repo_tree.get_children()
      if children:
        target = max(0, min(idx_int - 1, len(children) - 1))
        repo_tree.move(iid, "", target)

  def _validate_cfg_for_run(cfg: Json) -> t.Tuple[bool, str]:
    base_url = t.cast(str, _cfg_get(cfg, ["gitea", "base_url"], "")).strip()
    out_dir = t.cast(str, _cfg_get(cfg, ["sync", "output_dir"], "")).strip()
    token_env = t.cast(str, _cfg_get(cfg, ["gitea", "token_env"], "GITEA_TOKEN")).strip()
    token = t.cast(str, (os.environ.get(token_env) or _cfg_get(cfg, ["gitea", "token"], "") or "")).strip()

    if not base_url:
      return False, "gitea.base_url is required."
    if not out_dir:
      return False, "sync.output_dir is required."
    if not token:
      return False, f"No token found. Set env {token_env!r} or enter gitea.token."
    return True, "OK"

  def _restore_run_button() -> None:
    progress_var.set(0)
    n = _repo_count_from_manifest()
    progress_label_var.set(
      f"Idle ({n} repo{'s' if n != 1 else ''})"
    )

    run_btn.configure(
      text="Sync From Remote",
      fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
      hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"],
      command=lambda: _start_run(force_dry=None),
    )
    tooltip(run_btn, text="Start repository backup using current configuration values")
    _update_selected_action_buttons_state()

  def _run_worker(cfg, selected_full_names: t.Collection[str] | None = None):
    try:
      plan_and_apply(
        cfg,
        dry_run=bool(cfg.get("dry_run")),
        selected_full_names=selected_full_names,
        on_progress=_progress_from_worker,
        on_repo_status=_repo_status_from_worker,
      )
    except Exception as e:
      _log("[❌ ERROR]", str(e))
    finally:
      RUN_STATE.running = False
      RUN_STATE.stop_requested = False

      # ALWAYS restore Run button (UI thread)
      app.after(0, _restore_run_button)

  def _reset_queued_rows_to_idle() -> None:
    """
    Reset any still-queued rows back to Idle when SAFE STOP is clicked.

    This is UI-only cleanup so the table doesn't leave a bunch of rows stuck on "Queued"
    after a stop request.
    """
    try:
      tree = repo_tree
    except Exception:
      return

    for iid in tree.get_children(""):
      vals = tree.item(iid, "values") or ()
      if len(vals) < 4:
        continue

      # Status column is index 3 in our values tuple
      status_label = str(vals[3]).strip().lower()
      if status_label != "queued":
        continue

      new_vals = list(vals)
      new_vals[3] = "Idle"
      tree.item(iid, values=tuple(new_vals))

      # Restore zebra tag (and strip any status_* tags if present)
      try:
        row_n = int(str(new_vals[0]))
        zebra = "even" if (row_n % 2 == 0) else "odd"
      except Exception:
        zebra = None

      if zebra:
        tree.item(iid, tags=(zebra,))
      else:
        tags = tree.item(iid, "tags") or ()
        tags = tuple(t for t in tags if not str(t).startswith("status_"))
        tree.item(iid, tags=tags)

  def _stop_run() -> None:
    if not RUN_STATE.running:
      return

    RUN_STATE.stop_requested = True
    _log("[⛔ STOP]", "Stop requested — will finish current operation.")

    # UI cleanup: queued rows should go back to Idle immediately.
    app.after(0, _reset_queued_rows_to_idle)

  def _start_run(*, force_dry: bool | None, selected_full_names: t.Collection[str] | None = None) -> None:
    if RUN_STATE.running:
      _log("[⚠️ WARN]", "Run already in progress.")
      return

    selected_names = tuple(selected_full_names) if selected_full_names is not None else None
    if selected_names is not None and not selected_names:
      _log("[⚠️ WARN]", "No repositories selected for scoped sync.")
      return

    cfg_new = _build_cfg_from_vars(cfg_loaded, vars_map)

    if force_dry is not None:
      cfg_new["dry_run"] = bool(force_dry)

    ok, msg = _validate_cfg_for_run(cfg_new)
    if not ok:
      _log("[❌ ERR]", msg)
      return

    RUN_STATE.running = True
    RUN_STATE.stop_requested = False
    _update_selected_action_buttons_state()

    # Switch button to SAFE STOP
    run_btn.configure(
      text="SAFE STOP",
      fg_color="#8b0000",     # dark red
      hover_color="#b22222",  # lighter red
      command=_stop_run,
    )
    tooltip(
      run_btn,
      text=(
        "SAFE STOP (non-destructive)\n\n"
        "• Finishes the current repo operation\n"
        "• Stops before starting the next repo"
      ),
    )

    if selected_names is None:
      _log("[🧩 GUI]", "Starting run…")
    else:
      _log("[🧩 GUI]", f"Starting selected-only sync for {len(selected_names)} repos…")

    th = threading.Thread(target=_run_worker, args=(cfg_new, selected_names), daemon=True)

    progress_var.set(0)
    progress_label_var.set("Running...")

    th.start()

  run_btn = ctk.CTkButton(
    run_btns,
    text="Sync From Remote",
    command=lambda: _start_run(force_dry=None),
  )
  run_btn.grid(row=0, column=0, sticky="ew", padx=(6, 4), pady=6)
  tooltip(run_btn, text = "Start repository backup using current configuration values")

  # ---------------------------------------------------------
  # Log Tab (Live Log)
  # ---------------------------------------------------------

  logs_root = ctk.CTkFrame(tab_logs)
  logs_root.pack(fill="both", expand=True, padx=12, pady=12)

  logs_root.grid_columnconfigure(0, weight=1)
  logs_root.grid_rowconfigure(1, weight=1)

  # Title
  logs_title = ctk.CTkLabel(
    logs_root,
    text="Live Log",
    font=ctk.CTkFont(size=16, weight="bold"),
  )
  logs_title.grid(row=0, column=0, sticky="w", padx=8, pady=(0, 6))

  tooltip(
    logs_title,
    text=(
      "Live execution output\n\n"
      "Includes:\n"
      "- Git commands\n"
      "- SSH diagnostics\n"
      "- API activity\n"
      "- Errors and warnings\n\n"
      "This view mirrors console output and file logs."
    ),
  )

  # Log output box
  log_box = ctk.CTkTextbox(
    logs_root,
    wrap="none",
  )
  log_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

  tooltip(
    log_box,
    text=(
      "Streaming log output (read-only)\n\n"
      "• Automatically scrolls during runs\n"
      "• ANSI colors preserved\n"
      "• Output is also written to disk if logging is enabled\n\n"
      "Tip: You can select and copy text freely."
    ),
  )

  # Clear logs button
  clear_logs_button = ctk.CTkButton(
    logs_root,
    text="Clear Log",
    command=lambda: log_box.delete("1.0", "end"),
    width=140,
  )
  clear_logs_button.grid(row=2, column=0, sticky="e", padx=8, pady=(0, 8))

  tooltip(
    clear_logs_button,
    text=(
      "Clear the on-screen log view only\n\n"
      "• Does NOT stop a running job\n"
      "• Does NOT delete log files\n"
      "• New output will continue appearing"
    ),
  )

  # Optional: hover hint anywhere in Log tab
  tooltip(
    logs_root,
    text="View live logs and execution output",
  )

  # Periodically flush queue into textbox
  def _flush_logs() -> None:
    try:
      while True:
        line = q.get_nowait()
        if line == "":
          log_box.insert("end", "\n")
        else:
          log_box.insert("end", line + "\n")
        log_box.see("end")
    except queue.Empty:
      pass
    app.after(80, _flush_logs)

  _flush_logs()

  def _on_close() -> None:
    try:
      _LOG_SINKS.remove(sink)
    except Exception:
      pass
    app.destroy()

  app.protocol("WM_DELETE_WINDOW", _on_close)

  _repo_seed_from_manifest()

  # Force a fit after initial layout so no manual resize is needed.
  app.after(0, _repo_tree_fit_columns_now)

  _log("[🧩 GUI]", "Ready.")
  app.mainloop()

  return 0


# =============================================================================
# Entry point
# =============================================================================

def main(argv: t.List[str]) -> int:
  """
  GUI-first entry point.

  Optional:
  - --headless  : run once using config files and exit (no GUI)
  """
  configure_stdio_encoding()

  # Must happen early for best taskbar behavior on Windows.
  set_windows_app_user_model_id(APP_USER_MODEL_ID)

  script_dir = os.path.dirname(os.path.abspath(__file__))

  default_config_default = resolve_config_default_path(script_dir)
  default_config = os.path.join(script_dir, "config.json")

  ensure_config_json_exists(default_config_default, default_config)

  if "--headless" in argv:
    cfg = load_config(default_config_default, default_config)
    _logger_init_from_cfg(cfg, script_dir=script_dir)
    dry_run = bool(cfg.get("dry_run"))
    _log("[🧪 DRY]" if dry_run else "[✅ LIVE]", f"dry_run={dry_run}")
    try:
      return plan_and_apply(cfg, dry_run=dry_run)
    except Exception as e:
      _log("[❌ ERR]", str(e))
      return 1

  return _gui()


if __name__ == "__main__":
  raise SystemExit(main(sys.argv[1:]))
