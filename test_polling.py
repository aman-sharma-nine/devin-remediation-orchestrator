"""Tests for Step 9 polling: Devin session status, PR discovery, and the
polling lifecycle wired into the FastAPI lifespan.

Uses only unittest + httpx.MockTransport, never the network, never real credentials.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

TEST_REPO = "aman-sharma-nine/superset"

_tmp_dir = tempfile.TemporaryDirectory(prefix="orchestrator-polling-test-")
os.environ["DATABASE_PATH"] = str(Path(_tmp_dir.name) / "test.db")
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["GITHUB_REPO"] = TEST_REPO
os.environ["APPROVED_ACTORS"] = "aman-sharma-nine"
os.environ["GITHUB_TOKEN"] = "fake-gh-token"
os.environ["DEVIN_API_KEY"] = "fake-devin-key"
os.environ["DEVIN_ORG_ID"] = "fake-org-id"
os.environ["DEVIN_PLAYBOOK_ID"] = "fake-playbook-id"
os.environ["DEVIN_MAX_ACU_LIMIT"] = "5"

import store  # noqa: E402  (env vars must be set before import)
import flow  # noqa: E402


def tearDownModule():
    _tmp_dir.cleanup()


class DevinGetSessionContractTests(unittest.IsolatedAsyncioTestCase):
    """Verify the Devin v3 GET session request contract."""

    async def test_get_session_url_and_bearer_header(self):
        from devin import get_session

        def handler(request):
            handler.request = request
            return httpx.Response(
                200,
                json={
                    "status": "running",
                    "status_detail": "working",
                    "pull_requests": [],
                    "acus_consumed": 1.5,
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await get_session("key", "org-123", "sess-abc", client=client)

        expected_url = "https://api.devin.ai/v3/organizations/org-123/sessions/sess-abc"
        self.assertEqual(str(handler.request.url), expected_url)

        auth = handler.request.headers.get("Authorization")
        self.assertIsNotNone(auth)
        self.assertTrue(auth.startswith("Bearer "))
        await client.aclose()

    async def test_get_session_parses_fields(self):
        from devin import get_session

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "status": "running",
                        "status_detail": "waiting_for_user",
                        "pull_requests": [
                            {"pr_url": "https://github.com/owner/repo/pull/5", "pr_state": "open"}
                        ],
                        "acus_consumed": 3.25,
                    },
                )
            )
        )
        result = await get_session("key", "org", "sess-1", client=client)

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["status_detail"], "waiting_for_user")
        self.assertEqual(
            result["pull_requests"],
            [{"pr_url": "https://github.com/owner/repo/pull/5", "pr_state": "open"}],
        )
        self.assertEqual(result["acus_consumed"], 3.25)
        await client.aclose()

    async def test_get_session_http_failure_is_safe(self):
        from devin import get_session, DevinError

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(404, json={"error": "not found", "secret": "shh"})
            )
        )
        with self.assertRaises(DevinError) as ctx:
            await get_session("key", "org", "sess-1", client=client)
        self.assertNotIn("shh", str(ctx.exception))
        await client.aclose()

    async def test_get_session_malformed_json_is_safe(self):
        from devin import get_session, DevinError

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"not json"))
        )
        with self.assertRaises(DevinError):
            await get_session("key", "org", "sess-1", client=client)
        await client.aclose()


class GitHubPRHeadShaContractTests(unittest.IsolatedAsyncioTestCase):
    """Verify the GitHub PR head SHA request contract."""

    async def test_pr_head_sha_request_and_parsing(self):
        from gh import get_pull_request_head_sha

        def handler(request):
            handler.request = request
            return httpx.Response(200, json={"head": {"sha": "abc123def456"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sha = await get_pull_request_head_sha(
            "owner/repo", "https://github.com/owner/repo/pull/5", "token", client=client
        )

        self.assertEqual(sha, "abc123def456")
        expected_url = "https://api.github.com/repos/owner/repo/pulls/5"
        self.assertEqual(str(handler.request.url), expected_url)

        auth = handler.request.headers.get("Authorization")
        self.assertIsNotNone(auth)
        self.assertTrue(auth.startswith("Bearer "))
        await client.aclose()

    async def test_pr_head_sha_rejects_wrong_repository(self):
        from gh import get_pull_request_head_sha, GitHubError

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"head": {"sha": "abc123"}}))
        )
        with self.assertRaises(GitHubError):
            await get_pull_request_head_sha(
                "owner/repo", "https://github.com/someone-else/other/pull/5", "token", client=client
            )
        await client.aclose()

    async def test_pr_head_sha_rejects_malformed_url(self):
        from gh import get_pull_request_head_sha, GitHubError

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"head": {"sha": "abc123"}}))
        )
        with self.assertRaises(GitHubError):
            await get_pull_request_head_sha("owner/repo", "not-a-url", "token", client=client)
        await client.aclose()


class PollOnceTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests for flow.poll_once with mocked Devin and GitHub."""

    def setUp(self):
        store.init_db()
        store._conn.execute("DELETE FROM sessions")
        store._conn.commit()

    def tearDown(self):
        store.close_connection()
        store._conn = None

    def _seed_session(self, issue_number=1, session_id="sess-1"):
        store.claim_session(issue_number, "GHSA-demo", "demo-pkg")
        store.complete_dispatch(issue_number, session_id, f"https://devin.ai/sess/{session_id}")

    def _config(self):
        return {
            "devin_api_key": "key",
            "devin_org_id": "org",
            "github_token": "token",
            "github_repo": "owner/repo",
        }

    async def test_working_without_pr_sets_working(self):
        self._seed_session()

        with patch("flow.get_session", new_callable=AsyncMock) as mock_get_session:
            mock_get_session.return_value = {
                "status": "running",
                "status_detail": "working",
                "pull_requests": [],
                "acus_consumed": 0.5,
            }
            await flow.poll_once(self._config())

        row = store.get_session_by_issue(1)
        self.assertEqual(row["outcome"], "working")
        self.assertEqual(row["phase"], "working")
        self.assertEqual(row["acus_consumed"], 0.5)

    async def test_pr_discovery_stores_url_sha_and_sets_awaiting_ci(self):
        self._seed_session()

        with patch("flow.get_session", new_callable=AsyncMock) as mock_get_session, \
                patch("flow.get_pull_request_head_sha", new_callable=AsyncMock) as mock_get_sha:
            mock_get_session.return_value = {
                "status": "running",
                "status_detail": "working",
                "pull_requests": [{"pr_url": "https://github.com/owner/repo/pull/5", "pr_state": "open"}],
                "acus_consumed": 2.0,
            }
            mock_get_sha.return_value = "deadbeef123"

            await flow.poll_once(self._config())

            mock_get_sha.assert_awaited_once_with(
                "owner/repo", "https://github.com/owner/repo/pull/5", "token"
            )

        row = store.get_session_by_issue(1)
        self.assertEqual(row["pr_url"], "https://github.com/owner/repo/pull/5")
        self.assertEqual(row["head_sha"], "deadbeef123")
        self.assertEqual(row["outcome"], "awaiting_ci")
        self.assertEqual(row["phase"], "pr_discovered")

    async def test_second_identical_poll_makes_no_further_calls(self):
        self._seed_session()

        with patch("flow.get_session", new_callable=AsyncMock) as mock_get_session, \
                patch("flow.get_pull_request_head_sha", new_callable=AsyncMock) as mock_get_sha:
            mock_get_session.return_value = {
                "status": "running",
                "status_detail": "working",
                "pull_requests": [{"pr_url": "https://github.com/owner/repo/pull/5", "pr_state": "open"}],
                "acus_consumed": 2.0,
            }
            mock_get_sha.return_value = "deadbeef123"

            await flow.poll_once(self._config())
            await flow.poll_once(self._config())

            # Once outcome=awaiting_ci, list_pollable_sessions excludes the
            # row, so the second poll never calls Devin or GitHub again.
            mock_get_session.assert_awaited_once()
            mock_get_sha.assert_awaited_once()

        row = store.get_session_by_issue(1)
        self.assertEqual(row["pr_url"], "https://github.com/owner/repo/pull/5")
        self.assertEqual(row["head_sha"], "deadbeef123")

    async def test_finished_without_pr_becomes_needs_human_never_verified(self):
        self._seed_session()

        with patch("flow.get_session", new_callable=AsyncMock) as mock_get_session:
            mock_get_session.return_value = {
                "status": "running",
                "status_detail": "finished",
                "pull_requests": [],
                "acus_consumed": 4.0,
            }
            await flow.poll_once(self._config())

        row = store.get_session_by_issue(1)
        self.assertEqual(row["outcome"], "needs_human")
        self.assertEqual(row["phase"], "needs_human")
        self.assertNotEqual(row["outcome"], "verified")

    async def test_waiting_for_user_becomes_blocked(self):
        self._seed_session()

        with patch("flow.get_session", new_callable=AsyncMock) as mock_get_session:
            mock_get_session.return_value = {
                "status": "running",
                "status_detail": "waiting_for_user",
                "pull_requests": [],
                "acus_consumed": 1.0,
            }
            await flow.poll_once(self._config())

        row = store.get_session_by_issue(1)
        self.assertEqual(row["outcome"], "blocked")
        self.assertEqual(row["phase"], "blocked")

    async def test_polling_failure_does_not_corrupt_row(self):
        self._seed_session()
        from devin import DevinError

        with patch("flow.get_session", new_callable=AsyncMock) as mock_get_session:
            mock_get_session.side_effect = DevinError("boom")
            await flow.poll_once(self._config())

        row = store.get_session_by_issue(1)
        self.assertEqual(row["outcome"], "dispatched")
        self.assertEqual(row["phase"], "session_created")
        self.assertIsNone(row["pr_url"])
        self.assertIsNone(row["head_sha"])


class StorePollingHelperTests(unittest.TestCase):
    """Verify SQLite polling helpers directly, including no-op behavior."""

    def setUp(self):
        store.init_db()
        store._conn.execute("DELETE FROM sessions")
        store._conn.commit()

    def tearDown(self):
        store.close_connection()
        store._conn = None

    def test_list_pollable_sessions_excludes_terminal_outcomes(self):
        store.claim_session(1, "GHSA-a", "pkg-a")
        store.complete_dispatch(1, "sess-1", "https://devin.ai/sess/1")
        store.claim_session(2, "GHSA-b", "pkg-b")
        # issue 2 has no session_id yet - should be excluded

        rows = store.list_pollable_sessions()
        issue_numbers = [r["issue_number"] for r in rows]
        self.assertIn(1, issue_numbers)
        self.assertNotIn(2, issue_numbers)

    def test_update_polling_state_is_a_noop_when_unchanged(self):
        store.claim_session(3, "GHSA-c", "pkg-c")
        store.complete_dispatch(3, "sess-3", "https://devin.ai/sess/3")

        first = store.update_polling_state(3, "working", "working", 1.0)
        second = store.update_polling_state(3, "working", "working", 1.0)

        self.assertTrue(first)
        self.assertFalse(second)

    def test_record_pr_discovered_is_a_noop_when_unchanged(self):
        store.claim_session(4, "GHSA-d", "pkg-d")
        store.complete_dispatch(4, "sess-4", "https://devin.ai/sess/4")

        first = store.record_pr_discovered(4, "https://github.com/o/r/pull/1", "sha1", 1.0)
        second = store.record_pr_discovered(4, "https://github.com/o/r/pull/1", "sha1", 1.0)

        self.assertTrue(first)
        self.assertFalse(second)


class LifespanPollingTests(unittest.IsolatedAsyncioTestCase):
    """Verify the polling task starts and cancels cleanly with the app lifespan."""

    async def test_lifespan_starts_and_cancels_polling_task_cleanly(self):
        import app as app_module

        async def _hang(config):
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

        with patch("app.poll_loop", new_callable=AsyncMock) as mock_poll_loop:
            mock_poll_loop.side_effect = _hang

            cm = app_module.app.router.lifespan_context(app_module.app)
            await cm.__aenter__()

            self.assertIsNotNone(store._conn)
            mock_poll_loop.assert_called_once()

            await cm.__aexit__(None, None, None)

            self.assertIsNone(store._conn)


if __name__ == "__main__":
    unittest.main()
