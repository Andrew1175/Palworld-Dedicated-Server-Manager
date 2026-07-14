from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import constants

_USER_AGENT = "Windrose-Server-Manager"
_ZIP_NAME_RE = re.compile(r"^Windrose-Server-Manager-v\d+(?:\.\d+)+\.zip$", re.IGNORECASE)


def parse_version(v: str) -> tuple[int, ...]:
    parts = []
    for p in re.split(r"[^\d]+", v):
        if p.isdigit():
            parts.append(int(p))
    return tuple(parts) if parts else (0,)


def is_remote_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


def get_manager_install_dir() -> Path:
    """Directory that should receive the release zip contents (folder of the .exe when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _http_json(url: str) -> tuple[dict | None, str | None]:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except Exception as e:
        return None, str(e)


def _http_bytes(url: str) -> tuple[bytes | None, str | None]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/octet-stream"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read(), None
    except Exception as e:
        return None, str(e)


def _pick_release_zip_asset(assets: list, tag_name: str) -> tuple[str | None, str | None]:
    """Return (semver_version, browser_download_url) for Windrose-Server-Manager-vx.x.x.zip."""
    tag_norm = tag_name.lstrip("vV") if tag_name else ""
    expected = f"Windrose-Server-Manager-v{tag_norm}.zip"
    for a in assets:
        name = a.get("name") or ""
        if name.lower() == expected.lower():
            url = a.get("browser_download_url")
            if url:
                return tag_norm, url
    for a in assets:
        name = a.get("name") or ""
        if _ZIP_NAME_RE.match(name):
            url = a.get("browser_download_url")
            if not url:
                continue
            m = re.search(r"v(\d+(?:\.\d+)+)\.zip$", name, re.IGNORECASE)
            ver = m.group(1) if m else tag_norm
            return ver, url
    return None, None


def payload_root_from_extracted(extract_dir: Path) -> Path:
    """If the zip has a single top-level folder, use it; otherwise use the extract root."""
    items = [x for x in extract_dir.iterdir() if x.name != "__MACOSX"]
    if not items:
        raise ValueError("The release zip is empty.")
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return extract_dir


def run_update_pipeline(local_version: str, status_callback) -> dict:
    """
    Fetch latest GitHub release, compare version, download zip if newer, extract to a temp folder.

    Returns a dict:
      { "ok": False, "error": str }
      { "ok": True, "action": "uptodate", "remote": str }
      { "ok": True, "action": "ready", "remote": str, "payload": Path, "work": Path }
    """
    status_callback("Fetching latest release from GitHub...")
    data, err = _http_json(constants.GITHUB_LATEST_RELEASE_API_URL)
    if err or not data:
        return {"ok": False, "error": err or "Empty response from GitHub."}

    tag = data.get("tag_name") or ""
    assets = data.get("assets") or []
    remote_ver, zip_url = _pick_release_zip_asset(assets, tag)
    if not zip_url or not remote_ver:
        return {
            "ok": False,
            "error": "Could not find Windrose-Server-Manager-vx.x.x.zip in the latest GitHub release.",
        }

    if not is_remote_newer(remote_ver, local_version):
        return {"ok": True, "action": "uptodate", "remote": remote_ver}

    status_callback(f"Downloading version {remote_ver}...")
    work = Path(tempfile.mkdtemp(prefix="wr_mgr_update_"))
    zpath = work / "release.zip"
    blob, dl_err = _http_bytes(zip_url)
    if dl_err or not blob:
        _rmtree_quiet(work)
        return {"ok": False, "error": dl_err or "Download failed (empty response)."}

    zpath.write_bytes(blob)

    status_callback("Extracting update...")
    unpack = work / "unpack"
    unpack.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zpath, "r") as zf:
            zf.extractall(unpack)
        payload = payload_root_from_extracted(unpack)
    except Exception as e:
        _rmtree_quiet(work)
        return {"ok": False, "error": f"Invalid release zip: {e}"}

    return {"ok": True, "action": "ready", "remote": remote_ver, "payload": payload, "work": work}


def _rmtree_quiet(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def spawn_deferred_update(payload_dir: Path, staging_root: Path) -> tuple[bool, str | None]:
    """
    Start a detached PowerShell helper that waits for this process to exit, copies the
    extracted payload into the install directory, relaunches the app, then deletes staging_root.
    """
    if os.name != "nt":
        return False, "In-app updates are only supported on Windows."

    install_dir = get_manager_install_dir()
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        restart_exe = str(exe)
        restart_args = ""
        restart_cwd = ""
    else:
        py = Path(sys.executable).resolve()
        root = install_dir
        restart_exe = str(py)
        restart_args = "-m windrose_manager"
        restart_cwd = str(root)

    cfg = {
        "wait_pid": os.getpid(),
        "source": str(payload_dir.resolve()),
        "dest": str(install_dir.resolve()),
        "exe": restart_exe,
        "cwd": restart_cwd,
        "args": restart_args,
        "cleanup": str(staging_root.resolve()),
    }
    try:
        raw = json.dumps(cfg, separators=(",", ":")).encode("utf-8")
        b64 = base64.b64encode(raw).decode("ascii")
    except Exception as e:
        return False, str(e)

    ps1_body = r"""
param([string]$JsonB64)
$selfPath = $MyInvocation.MyCommand.Path
$jsonText = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($JsonB64))
$j = $jsonText | ConvertFrom-Json
$logPath = Join-Path $j.dest "update-helper.log"
function LogLine([string]$msg) {
  $line = "$(Get-Date -Format s)  $msg"
  Add-Content -Path $logPath -Value $line -ErrorAction SilentlyContinue
}
function Copy-WithRetry([string]$sourcePath, [string]$destPath, [int]$maxAttempts = 25, [int]$delayMs = 800) {
  for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    try {
      Copy-Item -LiteralPath $sourcePath -Destination $destPath -Recurse -Force -ErrorAction Stop
      if ($attempt -gt 1) {
        LogLine ("Copy succeeded after retry attempt {0} for {1}" -f $attempt, [System.IO.Path]::GetFileName($sourcePath))
      }
      return
    } catch {
      $msg = $_.Exception.Message
      LogLine ("Copy attempt {0}/{1} failed for {2}: {3}" -f $attempt, $maxAttempts, [System.IO.Path]::GetFileName($sourcePath), $msg)
      if ($attempt -eq $maxAttempts) { throw }
      Start-Sleep -Milliseconds $delayMs
    }
  }
}
function Launch-PreviousAndNotify {
  try {
    if ($j.cwd) {
      if ($j.args) {
        Start-Process -FilePath $j.exe -ArgumentList $j.args -WorkingDirectory $j.cwd -WindowStyle Normal
      } else {
        Start-Process -FilePath $j.exe -WorkingDirectory $j.cwd -WindowStyle Normal
      }
    } else {
      if ($j.args) {
        Start-Process -FilePath $j.exe -ArgumentList $j.args -WindowStyle Normal
      } else {
        Start-Process -FilePath $j.exe -WindowStyle Normal
      }
    }
    LogLine "Fallback relaunch attempted."
  } catch {
    LogLine ("Fallback relaunch failed: " + $_.Exception.Message)
  }
  try {
    Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue
    [System.Windows.MessageBox]::Show(
      "There was an error during the update process. Please review the update-helper.log for more information.",
      "Windrose Server Manager Update Error",
      [System.Windows.MessageBoxButton]::OK,
      [System.Windows.MessageBoxImage]::Error
    ) | Out-Null
  } catch {
    LogLine ("Failed to show update error popup: " + $_.Exception.Message)
  }
}
try {
  LogLine "Updater helper started."
  $waitFor = [int]$j.wait_pid
  while ($true) {
    $proc = Get-Process -Id $waitFor -ErrorAction SilentlyContinue
    if (-not $proc) { break }
    Start-Sleep -Milliseconds 500
  }
  Start-Sleep -Milliseconds 800
  LogLine "Main process exited. Applying files..."
  $src = $j.source
  $dst = $j.dest
  if (-not (Test-Path -LiteralPath $dst)) {
    New-Item -ItemType Directory -Path $dst -Force | Out-Null
  }
  # Best-effort cleanup for stale manager processes that may still lock DLLs.
  $exeName = [System.IO.Path]::GetFileNameWithoutExtension($j.exe)
  if ($exeName) {
    Get-Process -Name $exeName -ErrorAction SilentlyContinue | ForEach-Object {
      try {
        Stop-Process -Id $_.Id -Force -ErrorAction Stop
        LogLine ("Stopped stale process {0} ({1}) before copy." -f $_.ProcessName, $_.Id)
      } catch {
        LogLine ("Failed to stop stale process {0} ({1}): {2}" -f $_.ProcessName, $_.Id, $_.Exception.Message)
      }
    }
  }
  Get-ChildItem -LiteralPath $src -Force | ForEach-Object {
    Copy-WithRetry -sourcePath $_.FullName -destPath $dst
  }
  LogLine "File copy completed."
  if ($j.cwd) {
    if ($j.args) {
      Start-Process -FilePath $j.exe -ArgumentList $j.args -WorkingDirectory $j.cwd -WindowStyle Normal
    } else {
      Start-Process -FilePath $j.exe -WorkingDirectory $j.cwd -WindowStyle Normal
    }
  } else {
    if ($j.args) {
      Start-Process -FilePath $j.exe -ArgumentList $j.args -WindowStyle Normal
    } else {
      Start-Process -FilePath $j.exe -WindowStyle Normal
    }
  }
  LogLine "Restart launched."
  Start-Sleep -Seconds 2
  if ($j.cleanup) {
    Remove-Item -LiteralPath $j.cleanup -Recurse -Force -ErrorAction SilentlyContinue
    LogLine "Staging cleaned."
  }
} catch {
  LogLine ("Updater helper failed: " + $_.Exception.Message)
  Launch-PreviousAndNotify
}
Remove-Item -LiteralPath $selfPath -Force -ErrorAction SilentlyContinue
""".strip()

    try:
        fd, ps1_path = tempfile.mkstemp(suffix=".ps1", prefix="wr_mgr_apply_")
        os.close(fd)
        Path(ps1_path).write_text(ps1_body, encoding="utf-8")
    except OSError as e:
        return False, str(e)

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                ps1_path,
                "-JsonB64",
                b64,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=False,
            creationflags=creationflags,
        )
    except OSError as e:
        try:
            Path(ps1_path).unlink()
        except OSError:
            pass
        return False, str(e)

    return True, None
