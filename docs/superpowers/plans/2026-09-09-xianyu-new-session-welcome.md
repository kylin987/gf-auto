# Xianyu New Session Welcome Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent welcome messages for Xianyu conversations that existed before the current session-open signal.

**Architecture:** Add one freshness predicate to `XianyuLive` and apply it at both normalization boundaries that can produce `session_opened`. Keep the existing gateway payload and persistent deduplication unchanged.

**Tech Stack:** Python 3, asyncio, unittest

**Spec:** `docs/superpowers/specs/2026-09-09-xianyu-new-session-welcome-design.md`

## Global Constraints

- Only conversations created within 120 seconds may produce `session_opened`.
- Missing or invalid creation timestamps must fail closed.
- Existing gateway payloads and deduplication keys must not change.
- Do not include unrelated working-tree files.

---

### Task 1: Filter historical Xianyu conversations

**Files:**
- Modify: `goofish_live.py`
- Test: `tests/test_session_open_event.py`

**Interfaces:**
- Consumes: session creation timestamps from `sessionInfo.createTime` and `singleChatConversation.createAt`
- Produces: `XianyuLive._is_new_session(create_time, now_ms=None) -> bool`

- [x] **Step 1: Change the existing historical-session tests to expect rejection**

Update the formal session-arouse and typing-candidate tests so a conversation created five minutes ago returns `None`.

- [x] **Step 2: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.test_session_open_event -v`

Expected: the historical-session expectations fail against the current implementation.

- [x] **Step 3: Implement the shared freshness predicate**

Add a 120-second class constant and a helper that normalizes second timestamps, rejects missing timestamps, rejects timestamps over 120 seconds old, and rejects timestamps more than 30 seconds in the future. Call it from `_simplify_session_opened` and `_new_conversation_payload`.

- [x] **Step 4: Run focused and full tests**

Run: `python3 -m unittest tests.test_session_open_event -v`

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass.

- [x] **Step 5: Review the diff**

Run: `git diff --check` and confirm only the two implementation files and the approved design artifacts changed.
