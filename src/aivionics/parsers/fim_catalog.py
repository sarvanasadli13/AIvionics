"""FIM catalogue + maintenance-message routing from IFIM plaintext metadata.

Discovery 2026-08-08: procedure bodies in the IFIM are encrypted, but the
metadata layer is plaintext JS:
  nav.js       -> every FIM task number -> title, path, applicRefs;
                  plus a per-airplane effectivity table (msn/line/eff ref)
  mmsg.js      -> msd.mmg: maintenance-message code -> fault text -> FIM task(s)
  stepIndex.js -> msd.fimTasks: flat task list (cross-check)

No decryption is performed or needed: routing + locators only, which is all
standing rule 1 permits rendering anyway.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TASK_KEY = re.compile(r'^[A-Z]?\d{2}-\d{2}-\d{2}-\d{3}-(?:[A-Z]\d{2}|\d{3}[A-Z]?)$')


def _brace_object(text: str, start: int) -> str:
    """Return the balanced {...} substring beginning at text[start]=='{'."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    raise ValueError("unbalanced braces")


def _extract_named(text: str, name: str) -> dict:
    i = text.find(name)
    if i < 0:
        return {}
    j = text.find("{", i)
    return json.loads(_brace_object(text, j))


def parse_nav(nav_path: Path) -> tuple[dict, dict]:
    """Return (urls: task->{title,path,applicRefs}, airplanes: ref->info).

    nav.js is a single `loader.register({...})` call. Verified against the
    real file 2026-08-08: the effectivity table is keyed `fleet`, NOT
    `airplanes` — `airplanes` exists only as a list of line numbers nested
    inside `applics`, which is what broke the first implementation.
    """
    text = Path(nav_path).read_text(encoding="utf-8", errors="replace")
    i = text.find("loader.register(")
    if i < 0:
        raise ValueError(f"{nav_path}: no loader.register( call found")
    root = json.loads(_brace_object(text, text.index("{", i)))
    urls = {k: v for k, v in root.get("urls", {}).items() if TASK_KEY.match(k)}
    return urls, root.get("fleet", {})


def parse_mmsg(mmsg_path: Path) -> dict:
    """Return msd.mmg: code -> {text:{...}, task:{'1':...}, corr}."""
    text = Path(mmsg_path).read_text(encoding="utf-8", errors="replace")
    return _extract_named(text, "msd.mmg")


def parse_step_index(step_path: Path) -> list[str]:
    text = Path(step_path).read_text(encoding="utf-8", errors="replace")
    m = re.search(r"msd\.fimTasks\s*=\s*\[", text)
    if not m:
        return []
    end = text.find("]", m.end())
    arr = json.loads(text[m.end() - 1:end + 1])
    return [t for t in arr if TASK_KEY.match(t)]
