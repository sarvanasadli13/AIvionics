"""Start the frozen application and prove it actually came up.

Written because the first frozen build was reported as working when it was
not. The check had been "is the process still alive?", and it was — sitting
there after an ImportError with no window. A live process is not a running
application.

So this asserts two things instead:

  * **stderr is empty.** A `console=False` build writes its traceback to a
    stream nobody reads, which is exactly how a startup failure becomes
    invisible.
  * **a real window exists, with the expected title.** That is the first
    thing the user sees, and nothing before it can have failed.

    python scripts/smoke_frozen.py
    python scripts/smoke_frozen.py --exe dist/AIvionics/AIvionics.exe
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_EXE = ROOT / "dist" / "AIvionics" / "AIvionics.exe"
EXPECTED = "AIvionics"


def window_titles(pid: int) -> list[str]:
    """Top-level window titles owned by a process, via PowerShell."""
    script = (f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
              f"ForEach-Object {{ $_.MainWindowTitle }}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    ap.add_argument("--wait", type=float, default=25.0,
                    help="seconds to allow for startup")
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.exists():
        print(f"FAIL  no build at {exe} — run pyinstaller first")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="aivionics-smoke-"))
    err_path, out_path = tmp / "stderr.txt", tmp / "stdout.txt"
    with open(err_path, "wb") as err, open(out_path, "wb") as out:
        proc = subprocess.Popen([str(exe)], stderr=err, stdout=out,
                                cwd=str(exe.parent))
        deadline = time.time() + args.wait
        titles: list[str] = []
        while time.time() < deadline:
            time.sleep(1.5)
            if proc.poll() is not None:
                break
            titles = window_titles(proc.pid)
            if any(EXPECTED.lower() in t.lower() for t in titles):
                break

    exited = proc.poll()
    if exited is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    stderr = err_path.read_text(encoding="utf-8", errors="replace").strip()
    failures: list[str] = []
    if exited is not None:
        failures.append(f"the process exited during startup (code {exited})")
    if stderr:
        first = stderr.splitlines()[-1]
        failures.append(f"stderr is not empty — last line: {first}")
    if not any(EXPECTED.lower() in t.lower() for t in titles):
        failures.append(f"no window titled like {EXPECTED!r} appeared "
                        f"(saw: {titles or 'nothing'})")

    print(f"exe        : {exe}")
    print(f"window     : {titles[0] if titles else '<none>'}")
    print(f"stderr     : {'clean' if not stderr else 'NOT CLEAN'}")
    if failures:
        print("\nFAIL")
        for f in failures:
            print(f"  - {f}")
        if stderr:
            print("\n--- stderr ---")
            print(stderr[-2000:])
        return 1
    print("\nPASS  the frozen application started and showed its window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
