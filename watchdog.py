"""
Watchdog for run_live.py + dashboard.py.

Start everything with a single command:
    .venv\\Scripts\\python watchdog.py

The watchdog:
- Launches dashboard.py once (stays up for the whole session).
- Launches run_live.py (the trading bot), streaming its stdout here.
- Checks data/restart_request.flag every 5 s.
  If found: deletes it, gracefully terminates the bot, waits 5 s, relaunches.
- If the bot crashes on its own: relaunches after 10 s.
- Ctrl+C: terminates both the bot and the dashboard, then exits.

Dashboard is at http://127.0.0.1:5000 once started.
"""
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

# Nautilus prints the node's real os.getpid() in its startup banner, e.g.
#   TRADER-001.TradingNode: PID: 18328
# We compare it against the PID Popen handed us: if they differ, the process we
# signal (CTRL_BREAK / terminate / kill) is not the node, so a restart could
# orphan the live node and spawn a duplicate.
_NODE_PID_RE = re.compile(r"TradingNode:\s*PID:\s*(\d+)")

BASE_DIR       = Path(__file__).parent
BOT_SCRIPT     = BASE_DIR / "run_live.py"
DASH_SCRIPT    = BASE_DIR / "dashboard.py"
FLAG_PATH      = BASE_DIR / "data" / "restart_request.flag"
DASHBOARD_PORT = 5000

POLL_INTERVAL         = 5   # seconds between flag checks
CRASH_RELAUNCH_WAIT   = 10  # seconds to wait after an unplanned exit
RESTART_RELAUNCH_WAIT = 5   # seconds to wait after a requested restart
GRACEFUL_TIMEOUT      = 15  # seconds before escalating terminate → kill


def _find_python() -> str:
    """Return the .venv python; fall back to sys.executable."""
    candidates = [
        BASE_DIR / ".venv" / "Scripts" / "python.exe",  # Windows (uv-managed)
        BASE_DIR / ".venv" / "bin"     / "python",       # Unix
        BASE_DIR / "venv"  / "Scripts" / "python.exe",
        BASE_DIR / "venv"  / "bin"     / "python",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return sys.executable


def _stream(proc: subprocess.Popen, prefix: str, expected_pid: int | None = None) -> None:
    """Daemon thread: copy proc stdout → our stdout with a prefix tag.

    When expected_pid is given, watch for the node's PID banner and warn once if
    the node's real PID differs from the PID Popen returned (see _NODE_PID_RE).
    """
    checked = expected_pid is None
    for line in iter(proc.stdout.readline, ""):
        sys.stdout.write(f"[{prefix}] {line}" if not line.startswith("[") else line)
        sys.stdout.flush()
        if not checked:
            match = _NODE_PID_RE.search(line)
            if match:
                checked = True
                node_pid = int(match.group(1))
                if node_pid != expected_pid:
                    print(
                        f"[watchdog] WARNING: node PID {node_pid} != launched PID "
                        f"{expected_pid}. Stop signals target {expected_pid}; the node "
                        f"may be a child and could be orphaned (or duplicated) on restart.",
                        flush=True,
                    )
                else:
                    print(
                        f"[watchdog] PID check OK: node PID {node_pid} matches launched PID.",
                        flush=True,
                    )


def _terminate(proc: subprocess.Popen, label: str) -> None:
    """Ask the process to exit gracefully; escalate to kill after GRACEFUL_TIMEOUT.

    On Windows, proc.terminate() is TerminateProcess — an uncatchable hard kill,
    so the bot's on_stop (which closes all open positions) would never run. Send
    CTRL_BREAK_EVENT instead: the bot is launched in its own process group (see
    _launch_bot) and turns it into KeyboardInterrupt for a clean shutdown.
    """
    if proc.poll() is not None:
        return
    print(f"[watchdog] Stopping {label} (PID={proc.pid}) ...", flush=True)
    try:
        if _IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except (OSError, ValueError):
        proc.terminate()  # group gone / not a group leader — fall back
    try:
        proc.wait(timeout=GRACEFUL_TIMEOUT)
        print(f"[watchdog] {label} exited cleanly.", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[watchdog] Timeout — force-killing {label} (process tree).", flush=True)
        _force_kill_tree(proc)
        proc.wait()


def _force_kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process AND its children. The .venv launcher may spawn the node
    as a child (PID differs from proc.pid); a plain proc.kill() on Windows would
    terminate only the launcher and orphan the node. taskkill /T kills the tree."""
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.kill()


def _launch_bot(python: str) -> subprocess.Popen:
    """Start run_live.py and stream its output.

    On Windows the bot runs in its own process group (CREATE_NEW_PROCESS_GROUP)
    so the watchdog can send it CTRL_BREAK_EVENT for a graceful stop, and so a
    console Ctrl+C on the watchdog does not hard-kill the bot before we ask it
    to close positions.
    """
    kwargs = {}
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [python, str(BOT_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
        **kwargs,
    )
    threading.Thread(target=_stream, args=(proc, "bot", proc.pid), daemon=True).start()
    return proc


def _launch_dashboard(python: str) -> subprocess.Popen:
    """Start dashboard.py in the background; suppress its noisy startup lines."""
    import os
    env = os.environ.copy()
    env["PORT"] = str(DASHBOARD_PORT)
    proc = subprocess.Popen(
        [python, str(DASH_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
        env=env,
    )
    threading.Thread(target=_stream, args=(proc, "dash"), daemon=True).start()
    return proc


def main() -> None:
    for script in (BOT_SCRIPT, DASH_SCRIPT):
        if not script.exists():
            print(f"[watchdog] ERROR: {script} not found.", flush=True)
            sys.exit(1)

    python = _find_python()
    FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"[watchdog] Python    : {python}", flush=True)
    print(f"[watchdog] Bot       : {BOT_SCRIPT}", flush=True)
    print(f"[watchdog] Dashboard : http://127.0.0.1:{DASHBOARD_PORT}", flush=True)
    print(f"[watchdog] Flag      : {FLAG_PATH}", flush=True)

    # Start dashboard once — it runs for the whole session
    dash_proc = _launch_dashboard(python)
    print(f"[watchdog] Dashboard started  PID={dash_proc.pid}", flush=True)

    # Start the trading bot
    bot_proc = _launch_bot(python)
    print(f"[watchdog] Bot started        PID={bot_proc.pid}", flush=True)

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # ── Restart flag (from dashboard "Restart Bot" button) ─────────────
            if FLAG_PATH.exists():
                print("[watchdog] Restart flag detected.", flush=True)
                try:
                    FLAG_PATH.unlink()
                except OSError:
                    pass
                _terminate(bot_proc, "bot")
                print(
                    f"[watchdog] Waiting {RESTART_RELAUNCH_WAIT}s before relaunch ...",
                    flush=True,
                )
                time.sleep(RESTART_RELAUNCH_WAIT)
                bot_proc = _launch_bot(python)
                print(f"[watchdog] Bot relaunched  PID={bot_proc.pid}", flush=True)
                continue

            # ── Bot crashed / exited unexpectedly ─────────────────────────────
            rc = bot_proc.poll()
            if rc is not None:
                print(
                    f"[watchdog] Bot exited unexpectedly (code={rc}). "
                    f"Relaunching in {CRASH_RELAUNCH_WAIT}s ...",
                    flush=True,
                )
                time.sleep(CRASH_RELAUNCH_WAIT)
                bot_proc = _launch_bot(python)
                print(f"[watchdog] Bot relaunched  PID={bot_proc.pid}", flush=True)

            # ── Dashboard crashed — restart it too ────────────────────────────
            if dash_proc.poll() is not None:
                print("[watchdog] Dashboard exited — restarting ...", flush=True)
                dash_proc = _launch_dashboard(python)
                print(f"[watchdog] Dashboard restarted  PID={dash_proc.pid}", flush=True)

    except KeyboardInterrupt:
        print("\n[watchdog] Ctrl+C — shutting down ...", flush=True)
        _terminate(bot_proc, "bot")
        _terminate(dash_proc, "dashboard")
        print("[watchdog] Done.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
