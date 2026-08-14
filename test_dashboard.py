"""Tests for Step 11: the read-only demo dashboard.

Uses only unittest + httpx (ASGITransport), never the network, never the
live orchestrator.db - an isolated temporary SQLite database is used with
an unconditional (not setdefault) test database path.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

TEST_REPO = "aman-sharma-nine/superset"

_tmp_dir = tempfile.TemporaryDirectory(prefix="orchestrator-dashboard-test-")
os.environ["DATABASE_PATH"] = str(Path(_tmp_dir.name) / "test.db")
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["GITHUB_REPO"] = TEST_REPO
os.environ["APPROVED_ACTORS"] = "aman-sharma-nine"
os.environ["GITHUB_TOKEN"] = "fake-gh-token"
os.environ["DEVIN_API_KEY"] = "fake-devin-key"
os.environ["DEVIN_ORG_ID"] = "fake-org-id"
os.environ["DEVIN_PLAYBOOK_ID"] = "fake-playbook-id"
os.environ["DEVIN_MAX_ACU_LIMIT"] = "5"
os.environ["VERIFY_WORKFLOW_NAME"] = "verify-remediation"

import app as app_module  # noqa: E402  (env vars must be set before import)
import store  # noqa: E402


def tearDownModule():
    _tmp_dir.cleanup()


async def _hang_forever(config):
    """Stand-in for the real poll loop: never calls Devin/GitHub, just
    waits until the lifespan cancels it on shutdown."""
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Dashboard tests seed rows directly via store.*, which the real
        # background poller would otherwise treat as pollable and try to
        # reach Devin/GitHub for with fake credentials. Replace the poll
        # loop with a harmless stand-in so no network call is ever made.
        self._poll_patcher = patch.object(app_module, "poll_loop", new_callable=AsyncMock)
        self.mock_poll_loop = self._poll_patcher.start()
        self.mock_poll_loop.side_effect = _hang_forever

        self._lifespan_cm = app_module.app.router.lifespan_context(app_module.app)
        await self._lifespan_cm.__aenter__()
        store._conn.execute("DELETE FROM sessions")
        store._conn.execute("DELETE FROM deliveries")
        store._conn.commit()

        transport = httpx.ASGITransport(app=app_module.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self._lifespan_cm.__aexit__(None, None, None)
        self._poll_patcher.stop()

    def _seed_dispatched(self, issue_number, advisory_id="GHSA-demo", package="demo-pkg",
                          session_id="sess-1"):
        store.claim_session(issue_number, advisory_id, package)
        store.complete_dispatch(issue_number, session_id, f"https://app.devin.ai/sessions/{session_id}")

    def _seed_verified(self, issue_number, session_id="sess-verified",
                        pr_url="https://github.com/aman-sharma-nine/superset/pull/5"):
        self._seed_dispatched(issue_number, session_id=session_id)
        store.record_pr_discovered(issue_number, pr_url, "a" * 40, 1.0)
        store.mark_verified(issue_number)

    async def test_empty_database_returns_zero_totals_and_no_sessions(self):
        r = await self.client.get("/api/metrics")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["totals"], {"dispatched": 0, "prs_opened": 0, "ci_verified": 0})
        self.assertEqual(body["sessions"], [])

    async def test_seeded_rows_produce_correct_totals(self):
        self._seed_dispatched(1, session_id="sess-1")  # dispatched only
        self._seed_dispatched(2, session_id="sess-2")
        store.record_pr_discovered(2, "https://github.com/aman-sharma-nine/superset/pull/2", "b" * 40, 1.0)
        self._seed_verified(3, session_id="sess-3", pr_url="https://github.com/aman-sharma-nine/superset/pull/3")

        r = await self.client.get("/api/metrics")
        self.assertEqual(r.status_code, 200)
        totals = r.json()["totals"]
        self.assertEqual(totals["dispatched"], 3)
        self.assertEqual(totals["prs_opened"], 2)
        self.assertEqual(totals["ci_verified"], 1)

    async def test_metrics_returns_safe_fields_and_issue_url(self):
        self._seed_verified(1, session_id="sess-abc")

        r = await self.client.get("/api/metrics")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["sessions"]), 1)
        session = body["sessions"][0]

        expected_keys = {
            "issue_number", "issue_url", "session_id", "session_url", "pr_url",
            "outcome", "phase", "check_state", "repair_attempts",
            "dispatched_at", "verified_at",
        }
        self.assertEqual(set(session.keys()), expected_keys)

        self.assertEqual(session["issue_number"], 1)
        self.assertEqual(session["issue_url"], f"https://github.com/{TEST_REPO}/issues/1")
        self.assertEqual(session["session_id"], "sess-abc")
        self.assertEqual(session["session_url"], "https://app.devin.ai/sessions/sess-abc")

        # No credentials, config, delivery data, or internal notes leak.
        forbidden = {"note", "advisory_id", "package", "acus_consumed", "head_sha"}
        self.assertTrue(forbidden.isdisjoint(session.keys()))
        for value in session.values():
            if isinstance(value, str):
                self.assertNotIn("fake-devin-key", value)
                self.assertNotIn("fake-gh-token", value)

    async def test_dashboard_html_contains_title_and_column_headings(self):
        r = await self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        html = r.text

        self.assertIn("Devin Remediation Orchestrator", html)
        for heading in ("Issue", "Devin session", "Pull request", "Verification", "Repair attempts"):
            self.assertIn(heading, html)

    async def test_verified_session_exposes_links_and_repair_count_to_ui(self):
        self._seed_verified(1, session_id="sess-verified",
                             pr_url="https://github.com/aman-sharma-nine/superset/pull/5")

        r = await self.client.get("/api/metrics")
        session = r.json()["sessions"][0]

        self.assertEqual(session["outcome"], "verified")
        self.assertEqual(session["check_state"], "green")
        self.assertEqual(session["repair_attempts"], 0)
        self.assertIsNotNone(session["verified_at"])
        self.assertEqual(session["session_url"], "https://app.devin.ai/sessions/sess-verified")
        self.assertEqual(session["pr_url"], "https://github.com/aman-sharma-nine/superset/pull/5")
        self.assertEqual(session["issue_url"], f"https://github.com/{TEST_REPO}/issues/1")


if __name__ == "__main__":
    unittest.main()
