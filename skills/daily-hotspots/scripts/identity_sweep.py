#!/usr/bin/env python3
"""Monthly identity sweep, the get_user_info producer for the §9 drift/dead guardrail.

The yield engine's ``flag_drift_and_dead(roster, user_infos)`` INGESTS a
``{handle: get_user_info_dict}`` sweep to flag renamed / dead rostered handles. Everything
downstream of that sweep was built and tested; the one missing wire was a PRODUCER of the sweep.

This is it, a pure REST caller (NO MCP, NO LLM, deterministic) over twitterapi.io:

    GET https://api.twitterapi.io/twitter/user/info?userName=<handle>
    header  X-API-Key: <TWITTERAPI_IO_TOKEN>
    -> {"status":"success","data":{userName, statusesCount, followers, ...}}

It sweeps every ENABLED rostered handle, writes ``{handle: <data>|null}`` (null = 404 / gone),
and (with --feed-yield) hands it to ``run.py --yield --user-info <sweep> --write-review`` so the
flags land in ``archive/roster-review.md`` (report-only; never auto-removes, a rename is a human
edit, §9).

The token is read from a file (default: companion-config/secrets/twitterapi-io.env) or the
TWITTERAPI_IO_TOKEN env var. It is NEVER printed.

Usage:
    python identity_sweep.py [--roster PATH] [--out PATH] [--token-file PATH]
                             [--feed-yield] [--delay 0.15] [--timeout 20]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roster as R  # noqa: E402

REST_URL = "https://api.twitterapi.io/twitter/user/info"
TOKEN_VAR = "TWITTERAPI_IO_TOKEN"


def _read_env_file(p: Path) -> str | None:
    if p and p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(TOKEN_VAR + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_token(token_file: str | None) -> str:
    """Resolve the twitterapi.io token (NEVER logged), machine-path-free for a public repo:
    1) ``$TWITTERAPI_IO_TOKEN`` env var, 2) ``--token-file`` (an env file the caller points at),
    3) ``$MARKET_INTEL_CONFIG``/secrets/twitterapi-io.env (the companion secret store).
    """
    tok = os.environ.get(TOKEN_VAR, "").strip()
    if tok:
        return tok
    candidates = []
    if token_file:
        candidates.append(Path(token_file))
    mic = os.environ.get("MARKET_INTEL_CONFIG", "").strip()
    if mic:
        candidates.append(Path(mic) / "secrets" / "twitterapi-io.env")
    for p in candidates:
        v = _read_env_file(p)
        if v:
            return v
    raise SystemExit(
        f"no twitterapi.io token: set ${TOKEN_VAR}, pass --token-file <env>, "
        f"or set $MARKET_INTEL_CONFIG (looked at: {[str(c) for c in candidates] or 'nothing'})"
    )


def fetch_one(handle: str, token: str, timeout: float, retries: int = 3) -> dict | None:
    """Return the user ``data`` dict, or None when the account is gone (404 / error).

    Distinguishes 'account gone' (-> None, a real signal) from 'transient network error'
    (-> retry, then raise so the sweep FAILS LOUDLY rather than silently marking a live
    account dead). Never returns {} for a healthy account.
    """
    url = f"{REST_URL}?userName={urllib.parse.quote(handle)}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"X-API-Key": token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            if body.get("status") == "success" and isinstance(body.get("data"), dict):
                return body["data"]
            # status != success => account not found / suspended => a real "dead" signal
            return None
        except urllib.error.HTTPError as e:
            # 404 = gone (real signal); 429/5xx = transient (retry)
            if e.code == 404:
                return None
            last_exc = e
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_exc = e
        time.sleep(1.5 * (attempt + 1))  # linear backoff
    raise RuntimeError(f"twitterapi.io failed for @{handle} after {retries} tries: {last_exc!r}")


def sweep(roster: dict, token: str, delay: float, timeout: float) -> dict:
    infos: dict = {}
    handles = [
        e["handle"]
        for e in R.entries_of(roster)
        if isinstance(e, dict) and e.get("enabled") is True and isinstance(e.get("handle"), str)
    ]
    total = len(handles)
    for i, h in enumerate(handles, 1):
        infos[h] = fetch_one(h, token, timeout)
        state = "gone" if infos[h] is None else (
            "sc=0" if (infos[h].get("statusesCount") or 0) <= 0 else "ok"
        )
        print(f"  [{i}/{total}] @{h}: {state}", flush=True)
        if i < total:
            time.sleep(delay)
    return infos


def summarize(roster: dict, infos: dict) -> dict:
    """Preview the flags flag_drift_and_dead will raise (single source of truth = yield.py)."""
    # import yield.py by path (module name 'yield' is a keyword -> importlib)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dh_yield", str(Path(__file__).resolve().parent / "yield.py")
    )
    Y = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(Y)  # type: ignore[union-attr]
    flags = Y.flag_drift_and_dead(roster, infos)
    dead = [f for f in flags if f["kind"] == "dead"]
    drift = [f for f in flags if f["kind"] == "drift"]
    return {"flags": flags, "dead": dead, "drift": drift}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Monthly get_user_info identity sweep (§9 producer)")
    ap.add_argument("--roster", default=None, help="roster.json path (else resolved via config-dir)")
    ap.add_argument("--out", default=None, help="sweep JSON out path (default: <config>/archive/identity-sweep-<YYYY-MM>.json)")
    ap.add_argument("--token-file", default=None, help=f"file with {TOKEN_VAR}=... (default companion-config secret)")
    ap.add_argument("--feed-yield", action="store_true", help="after the sweep, run run.py --yield --user-info <out> --write-review")
    ap.add_argument("--delay", type=float, default=0.15, help="seconds between calls (politeness)")
    ap.add_argument("--timeout", type=float, default=20.0, help="per-request timeout seconds")
    a = ap.parse_args(argv)

    roster = R.load_roster(a.roster)
    token = load_token(a.token_file)

    print(f"[identity-sweep] {datetime.now(timezone.utc).isoformat()}, sweeping enabled handles")
    infos = sweep(roster, token, a.delay, a.timeout)

    # resolve out path (default next to the archive)
    if a.out:
        out = Path(a.out)
    else:
        arch = R.resolve_roster_path(a.roster).parent / "archive"
        arch.mkdir(parents=True, exist_ok=True)
        out = arch / f"identity-sweep-{datetime.now(timezone.utc):%Y-%m}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(infos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    res = summarize(roster, infos)
    ok = sum(1 for v in infos.values() if isinstance(v, dict) and (v.get("statusesCount") or 0) > 0)
    print(
        f"[identity-sweep] queried={len(infos)} ok={ok} "
        f"DEAD={len(res['dead'])} DRIFT={len(res['drift'])} -> {out}"
    )
    for f in res["flags"]:
        extra = f" -> {f.get('current_handle')}" if f.get("current_handle") else ""
        print(f"    FLAG {f['kind']:5} @{f['handle']}: {f['detail']}{extra}")

    if a.feed_yield:
        runpy = Path(__file__).resolve().parent / "run.py"
        cmd = [sys.executable, str(runpy), "--yield", "--user-info", str(out), "--write-review"]
        print(f"[identity-sweep] feeding yield: {' '.join(cmd)}")
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[identity-sweep] run.py --yield exited rc={rc}", file=sys.stderr)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
