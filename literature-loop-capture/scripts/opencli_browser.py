#!/usr/bin/env python3
"""Small OpenCLI browser command helpers for the literature capture scripts."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

PUBLISHER_HOME_URL = "https://www.sciencedirect.com/search"


def ensure_opencli() -> None:
    if not shutil.which("opencli"):
        raise RuntimeError("opencli_not_found; install/configure OpenCLI before browser discovery or capture")


def run_opencli_command(parts: list[str], *, timeout: int = 90) -> str:
    ensure_opencli()
    profile = os.environ.get("OPENCLI_PROFILE", "").strip()
    command = ["opencli"]
    if profile:
        command.extend(["--profile", profile])
    command.extend(parts)
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"opencli_command_failed:{detail[:500]}")
    return completed.stdout.strip()


def doctor(timeout: int = 60) -> dict[str, Any]:
    try:
        output = run_opencli_command(["doctor"], timeout=timeout)
    except Exception as exc:
        return {"status": "missing_or_failed", "error": str(exc)[:500]}
    return {"status": "ok", "output": output[:1000]}


def page_state(session: str, *, timeout: int = 30) -> str:
    return run_opencli_command(["browser", session, "state"], timeout=timeout)


def _state_url(state: str) -> str:
    for line in state.splitlines():
        if line.startswith("URL: "):
            return line[5:].strip()
        if line.startswith("url: "):
            return line[5:].strip()
    return ""


def _target_reached(target_url: str, current_url: str) -> bool:
    if not current_url:
        return False
    if target_url.startswith("about:"):
        return current_url.startswith(target_url)
    target = urlparse(target_url)
    current = urlparse(current_url)
    if target.netloc and current.netloc:
        return target.netloc == current.netloc
    return current_url != "about:blank"


def open_url(session: str, url: str, *, timeout: int = 90) -> None:
    run_opencli_command(["browser", session, "open", url], timeout=timeout)
    deadline = time.monotonic() + min(timeout, 12)
    last_url = ""
    while time.monotonic() < deadline:
        state = page_state(session, timeout=min(15, timeout))
        last_url = _state_url(state)
        if _target_reached(url, last_url):
            return
        time.sleep(0.5)
    raise RuntimeError(f"opencli_navigation_not_ready:{last_url or 'unknown'}")


def open_url_allow_redirect(session: str, url: str, *, timeout: int = 90) -> str:
    """Open a URL and return the reached URL without requiring target-domain match."""
    run_opencli_command(["browser", session, "open", url], timeout=timeout)
    deadline = time.monotonic() + min(timeout, 12)
    last_url = ""
    while time.monotonic() < deadline:
        state = page_state(session, timeout=min(15, timeout))
        last_url = _state_url(state)
        if last_url and last_url not in {"about:blank"} and not last_url.startswith(("chrome://newtab", "edge://newtab")):
            return last_url
        time.sleep(0.5)
    return last_url


def wait_time(session: str, seconds: int, *, timeout: int | None = None) -> None:
    if seconds <= 0:
        return
    run_opencli_command(["browser", session, "wait", "time", str(seconds)], timeout=timeout or max(10, seconds + 10))


def scroll_down(session: str, *, timeout: int = 30) -> None:
    run_opencli_command(["browser", session, "scroll", "down"], timeout=timeout)


def ms_to_wait_seconds(milliseconds: int | float | str | None) -> int:
    try:
        value = float(milliseconds or 0)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return max(1, int(math.ceil(value / 1000)))


def settle_article_page(
    session: str,
    *,
    initial_wait_ms: int = 5000,
    scroll_rounds: int = 0,
    scroll_wait_ms: int = 1000,
    final_wait_ms: int = 0,
) -> None:
    """Wait and scroll after opening an article page before extracting DOM."""
    wait_time(session, ms_to_wait_seconds(initial_wait_ms))
    for _ in range(max(0, int(scroll_rounds))):
        try:
            scroll_down(session, timeout=30)
        except Exception:
            break
        wait_time(session, ms_to_wait_seconds(scroll_wait_ms))
    wait_time(session, ms_to_wait_seconds(final_wait_ms))


def eval_script(session: str, script: str, *, timeout: int = 90) -> str:
    return run_opencli_command(["browser", session, "eval", script], timeout=timeout)


def eval_json(session: str, script: str, *, timeout: int = 90) -> Any:
    raw = eval_script(session, script, timeout=timeout)
    try:
        return json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"opencli_invalid_json:{raw[:500]}") from exc


def page_snapshot(session: str, *, timeout: int = 60) -> dict[str, Any]:
    script = r"""(() => JSON.stringify({
      url: location.href,
      title: document.title || "",
      text: (document.body && document.body.innerText || "").slice(0, 5000),
      anchors: document.querySelectorAll("a[href]").length
    }))()"""
    data = eval_json(session, script, timeout=timeout)
    if not isinstance(data, dict):
        return {"url": "", "title": "", "text": "", "anchors": 0}
    return data


def publisher_home_problem(snapshot: dict[str, Any], *, expected_hosts: set[str] | None = None) -> str:
    current_url = str(snapshot.get("url") or "").strip()
    title = str(snapshot.get("title") or "").strip()
    text = str(snapshot.get("text") or "").strip()
    haystack = f"{current_url} {title} {text}".lower()
    if current_url in {"", "about:blank"} or current_url.startswith(("chrome://newtab", "edge://newtab")):
        return "publisher_blank_page"
    auth_terms = [
        "authentication",
        "institutional login",
        "log in",
        "login",
        "sign in",
        "session expired",
        "shibboleth",
        "access through your institution",
    ]
    robot_terms = ["captcha", "robot", "unusual traffic", "verify you are human", "access denied"]
    if any(term in haystack for term in robot_terms):
        return "publisher_robot_blocked"
    if any(term in haystack for term in auth_terms):
        return "publisher_auth_required"
    if expected_hosts:
        host = urlparse(current_url).netloc.lower()
        if host and not any(host == expected or host.endswith("." + expected) for expected in expected_hosts):
            return f"publisher_unexpected_home_url:{current_url[:220]}"
    return ""


def preflight_publisher_home(
    session: str,
    *,
    home_url: str = PUBLISHER_HOME_URL,
    expected_hosts: set[str] | None = None,
    wait_ms: int = 3000,
    timeout: int = 90,
) -> tuple[dict[str, Any], str]:
    reached_url = open_url_allow_redirect(session, home_url, timeout=timeout)
    wait_time(session, ms_to_wait_seconds(wait_ms))
    snapshot = page_snapshot(session, timeout=min(timeout, 60))
    if reached_url and not str(snapshot.get("url") or "").strip():
        snapshot["url"] = reached_url
    return snapshot, publisher_home_problem(snapshot, expected_hosts=expected_hosts)


def close(session: str, *, timeout: int = 15) -> None:
    run_opencli_command(["browser", session, "close"], timeout=timeout)
