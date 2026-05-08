#!/usr/bin/env python3
"""End-to-end smoke test for the chat sessions feature.

Walks the new session API surface a real student sees:

1. Log in as a student.
2. Confirm an empty chat list (or dump what's there).
3. Create a session, send a message, assert the response is shaped right
   and includes ``session_id``.
4. Reload history and assert at least the user + assistant turns landed.
5. Try to exceed the cap by creating five sessions; expect HTTP 409.
6. Delete a session and confirm the audit collection shrinks.

Run after starting uvicorn:
  BE/venv/bin/python BE/test/test_chat_sessions_flow.py
"""

from __future__ import annotations

import os
import sys

import requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
STUDENT_EMAIL = os.environ.get("STUDENT_EMAIL", "student1@email.com")
STUDENT_PASSWORD = os.environ.get("STUDENT_PASSWORD", "123456")


def fail(msg: str) -> None:
    print(f"\n❌ {msg}", flush=True)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"  ✓ {msg}", flush=True)


def login() -> str:
    res = requests.post(
        f"{BASE}/api/iam/login",
        json={"email": STUDENT_EMAIL, "password": STUDENT_PASSWORD},
        timeout=30,
    )
    if res.status_code != 200:
        fail(f"login failed: {res.status_code} {res.text[:200]}")
    return res.json()["access_token"]


def main() -> int:
    print(f"BASE={BASE}  STUDENT={STUDENT_EMAIL}")
    token = login()
    headers = {"Authorization": f"Bearer {token}"}
    ok("logged in as student")

    # 1. List sessions — start from a clean baseline by purging existing.
    res = requests.get(f"{BASE}/api/chat/sessions", headers=headers, timeout=30)
    if res.status_code != 200:
        fail(f"list sessions failed: {res.status_code} {res.text}")
    body = res.json()
    print(f"  initial sessions: {len(body['sessions'])} (max={body['max_sessions']})")
    for s in body["sessions"]:
        d = requests.delete(
            f"{BASE}/api/chat/sessions/{s['session_id']}",
            headers=headers,
            timeout=30,
        )
        if d.status_code not in (204, 404):
            fail(f"cleanup delete failed: {d.status_code} {d.text}")
    ok("cleared pre-existing sessions")

    # 2. Create one session and send a message through it.
    res = requests.post(
        f"{BASE}/api/chat/sessions",
        headers=headers,
        json={"title": "smoke-test-1"},
        timeout=30,
    )
    if res.status_code != 201:
        fail(f"create session failed: {res.status_code} {res.text}")
    session_id = res.json()["session_id"]
    ok(f"created session {session_id[:8]}…")

    res = requests.post(
        f"{BASE}/api/chat/learning-path",
        headers=headers,
        json={
            "session_id": session_id,
            "messages": [
                {"role": "user", "content": "Hello assistant, am I doing okay?"}
            ],
        },
        timeout=120,
    )
    if res.status_code != 200:
        fail(f"chat call failed: {res.status_code} {res.text[:500]}")
    chat_body = res.json()
    if chat_body.get("session_id") != session_id:
        fail(f"chat response session_id mismatch: {chat_body.get('session_id')}")
    if not chat_body.get("reply"):
        fail("chat response had empty reply")
    ok(f"chat replied via {chat_body['provider']} ({len(chat_body['reply'])} chars)")
    if chat_body.get("tool_results"):
        ok(f"tool_results: {len(chat_body['tool_results'])} call(s)")

    # 3. Replay history; expect at least one user turn + one assistant
    #    turn to have landed in chat_events.
    res = requests.get(
        f"{BASE}/api/chat/sessions/{session_id}/messages",
        headers=headers,
        timeout=30,
    )
    if res.status_code != 200:
        fail(f"history fetch failed: {res.status_code} {res.text}")
    msgs = res.json()["messages"]
    roles = [m["role"] for m in msgs]
    if "user" not in roles or "assistant" not in roles:
        fail(f"history missing roles: {roles}")
    ok(f"history persisted: {len(msgs)} turns ({roles})")

    # 4. Confirm session metadata reflects the messages.
    res = requests.get(f"{BASE}/api/chat/sessions", headers=headers, timeout=30)
    matched = next(
        (s for s in res.json()["sessions"] if s["session_id"] == session_id),
        None,
    )
    if not matched:
        fail("session disappeared from list")
    if matched["message_count"] < 2:
        fail(f"message_count not bumped: {matched['message_count']}")
    ok(f"session metadata bumped: count={matched['message_count']}")

    # 5. Cap test: we already have 1 session, create 4 more, expect the
    #    6th attempt to 409.
    extra_ids: list[str] = []
    for i in range(4):
        r = requests.post(
            f"{BASE}/api/chat/sessions",
            headers=headers,
            json={"title": f"smoke-fill-{i}"},
            timeout=30,
        )
        if r.status_code != 201:
            fail(f"fill session {i} failed: {r.status_code} {r.text}")
        extra_ids.append(r.json()["session_id"])
    res = requests.post(
        f"{BASE}/api/chat/sessions",
        headers=headers,
        json={"title": "should-fail"},
        timeout=30,
    )
    if res.status_code != 409:
        fail(f"expected 409 at cap, got {res.status_code} {res.text}")
    ok("cap enforced: 6th create returned 409")

    # 6. Delete one session and confirm the slot frees up.
    target = extra_ids[0]
    res = requests.delete(
        f"{BASE}/api/chat/sessions/{target}",
        headers=headers,
        timeout=30,
    )
    if res.status_code != 204:
        fail(f"delete failed: {res.status_code} {res.text}")
    res = requests.post(
        f"{BASE}/api/chat/sessions",
        headers=headers,
        json={"title": "after-delete"},
        timeout=30,
    )
    if res.status_code != 201:
        fail(f"post-delete create failed: {res.status_code} {res.text}")
    new_after_delete = res.json()["session_id"]
    ok(f"slot recovered after delete (new={new_after_delete[:8]}…)")

    # 7. Cleanup remaining test sessions to leave the account tidy.
    for sid in (
        [session_id, new_after_delete] + extra_ids[1:]
    ):
        requests.delete(
            f"{BASE}/api/chat/sessions/{sid}",
            headers=headers,
            timeout=30,
        )

    print("\n✅ chat-sessions smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
