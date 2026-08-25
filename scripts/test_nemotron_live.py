"""Live smoke test against the real NVIDIA NIM endpoint.

Invoked by hand, never by the automated suite: the test suite must not depend
on NVIDIA being reachable, and a suite that silently skips when a key is
absent teaches you to ignore it.

    python scripts/test_nemotron_live.py
    python scripts/test_nemotron_live.py --model nvidia/nemotron-...

Reads the key from Windows Credential Manager or `NVIDIA_API_KEY`. It never
prints the key, and never writes it anywhere. Exits non-zero unless every
check passes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT / "src"))

from aivionics.llm import aiconfig as A                          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Live NVIDIA NIM serving test (manual, never automated)")
    ap.add_argument("--model", default=A.NEMOTRON_MODEL)
    ap.add_argument("--endpoint", default=A.NIM_ENDPOINT)
    args = ap.parse_args()

    key = A.get_api_key()
    source = A.key_source()
    print("AIvionics — live NVIDIA NIM serving test")
    print(f"  endpoint    {args.endpoint}")
    print(f"  model       {args.model}")
    # The *source* is printed, never the value.
    print(f"  credential  {source or 'NONE'}")
    if not key:
        print("\nrefused: no API key. Store one in Admin → AI assistant, or "
              f"set {A.ENV_KEY} in the environment.")
        return 2

    settings = A.AISettings(enabled=True, provider="openai",
                            endpoint=args.endpoint, model=args.model,
                            privacy_ack=True)
    print("\nrunning the full serving test (catalogue + real completion)…")
    result = A.verify_settings(settings, key)

    print(f"\n  state         {result.label}")
    print(f"  served model  {result.served_model or 'not returned'}")
    if result.listed:
        print(f"  catalogue     {len(result.listed)} models listed; requested "
              f"model {'present' if args.model in result.listed else 'ABSENT'}")
    if result.detail:
        print(f"  detail        {result.detail}")

    if result.state is A.AIState.TRUNCATED:
        print("\n  the probe reply hit the token budget — the model emitted "
              "more reasoning than the budget allowed.")

    if not result.ok:
        print("\nFAILED — the endpoint did not serve the requested model.")
        return 1

    assert key not in repr(result), "the key must never reach the output"
    print("\nPASSED — the endpoint served the requested model, complete and "
          "well formed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
