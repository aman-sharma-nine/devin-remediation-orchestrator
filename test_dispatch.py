"""Tests for Step 8 dispatch flow: creating Devin sessions and GitHub labels.

Uses only unittest + httpx.MockTransport, never the network, never real credentials.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

_tmp_dir = tempfile.TemporaryDirectory(prefix="orchestrator-dispatch-test-")
os.environ["DATABASE_PATH"] = str(Path(_tmp_dir.name) / "test.db")

import store  # noqa: E402  (env vars must be set before import)


def tearDownModule():
    _tmp_dir.cleanup()


class DevinClientContractTests(unittest.IsolatedAsyncioTestCase):
    """Verify the Devin v3 request contract."""

    async def test_devin_v3_url_is_correct(self):
        from devin import create_session

        def capture_request(request):
            capture_request.last_request = request
            return httpx.Response(
                200,
                json={"session_id": "sess-abc123", "url": "https://devin.ai/sess/abc123"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(capture_request))
        await create_session(
            api_key="key",
            org_id="org-123",
            playbook_id="playbook-abc",
            prompt="test prompt",
            title="test",
            tags=["test"],
            max_acu_limit=5,
            client=client,
        )

        expected_url = "https://api.devin.ai/v3/organizations/org-123/sessions"
        self.assertEqual(str(capture_request.last_request.url), expected_url)
        await client.aclose()

    async def test_devin_bearer_header_exists(self):
        from devin import create_session

        def capture_request(request):
            capture_request.auth_header = request.headers.get("Authorization")
            return httpx.Response(
                200,
                json={"session_id": "sess-abc123", "url": "https://devin.ai/sess/abc123"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(capture_request))
        await create_session(
            api_key="secret-key-xyz",
            org_id="org-123",
            playbook_id="playbook-abc",
            prompt="test prompt",
            title="test",
            tags=["test"],
            max_acu_limit=5,
            client=client,
        )

        self.assertIsNotNone(capture_request.auth_header)
        self.assertTrue(capture_request.auth_header.startswith("Bearer "))
        await client.aclose()

    async def test_devin_request_body_contains_required_fields(self):
        from devin import create_session

        def capture_request(request):
            capture_request.body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"session_id": "sess-abc123", "url": "https://devin.ai/sess/abc123"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(capture_request))
        await create_session(
            api_key="key",
            org_id="org-123",
            playbook_id="playbook-abc",
            prompt="remediate this",
            title="Issue #1",
            tags=["remediation", "issue:1"],
            max_acu_limit=5,
            client=client,
        )

        body = capture_request.body
        self.assertIn("prompt", body)
        self.assertEqual(body["prompt"], "remediate this")
        self.assertIn("title", body)
        self.assertEqual(body["title"], "Issue #1")
        self.assertIn("playbook_id", body)
        self.assertEqual(body["playbook_id"], "playbook-abc")
        self.assertIn("tags", body)
        self.assertEqual(body["tags"], ["remediation", "issue:1"])
        self.assertIn("max_acu_limit", body)
        self.assertEqual(body["max_acu_limit"], 5)
        self.assertNotIn("idempotent", body)
        await client.aclose()

    async def test_devin_response_parsed_correctly(self):
        from devin import create_session

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={"session_id": "sess-xyz789", "url": "https://devin.ai/sess/xyz789"}
                )
            )
        )
        session_id, url = await create_session(
            api_key="key",
            org_id="org-123",
            playbook_id="playbook-abc",
            prompt="test",
            title="test",
            tags=["test"],
            max_acu_limit=5,
            client=client,
        )

        self.assertEqual(session_id, "sess-xyz789")
        self.assertEqual(url, "https://devin.ai/sess/xyz789")
        await client.aclose()


class PromptBuildingTests(unittest.TestCase):
    """Verify the prompt is built from trusted advisory data only."""

    def test_build_prompt_uses_advisory_data(self):
        from flow import build_prompt

        advisory = {
            "advisory_id": "GHSA-test-1234",
            "package": "test-pkg",
            "ecosystem": "npm",
            "current_versions": ["1.0.0"],
            "affected_ranges": ["<1.1.0"],
            "fixed_versions": {"1": "1.1.0"},
            "manifest_path": "package.json",
            "lockfile_path": "package-lock.json",
            "test_command": "npm test",
            "advisory_url": "https://github.com/advisories/GHSA-test",
        }

        prompt = build_prompt(1, "owner/repo", advisory)

        self.assertIn("GHSA-test-1234", prompt)
        self.assertIn("test-pkg", prompt)
        self.assertIn("npm", prompt)
        self.assertIn("package.json", prompt)
        self.assertIn("npm test", prompt)
        self.assertIn("issue #1", prompt)
        self.assertIn("draft PR", prompt)
        self.assertIn("fork", prompt)
        self.assertIn("Fixes #1", prompt)


class StoreOperationsTests(unittest.TestCase):
    """Verify SQLite store operations for dispatch state."""

    def setUp(self):
        store.init_db()
        store._conn.execute("DELETE FROM sessions")
        store._conn.commit()

    def tearDown(self):
        store.close_connection()
        store._conn = None

    def test_claim_session_inserts_row(self):
        result = store.claim_session(1, "GHSA-test", "pkg")
        self.assertTrue(result)

        row = store.get_session_by_issue(1)
        self.assertIsNotNone(row)
        self.assertEqual(row["issue_number"], 1)
        self.assertEqual(row["advisory_id"], "GHSA-test")
        self.assertEqual(row["package"], "pkg")
        self.assertEqual(row["outcome"], "dispatched")
        self.assertEqual(row["phase"], "creating_session")

    def test_claim_session_prevents_duplicates(self):
        result1 = store.claim_session(1, "GHSA-test", "pkg")
        self.assertTrue(result1)

        result2 = store.claim_session(1, "GHSA-test", "pkg")
        self.assertFalse(result2)

    def test_complete_dispatch_updates_row(self):
        store.claim_session(2, "GHSA-test", "pkg")
        store.complete_dispatch(2, "sess-123", "https://devin.ai/sess/123")

        row = store.get_session_by_issue(2)
        self.assertEqual(row["session_id"], "sess-123")
        self.assertEqual(row["session_url"], "https://devin.ai/sess/123")
        self.assertEqual(row["phase"], "session_created")

    def test_fail_dispatch_marks_needs_human(self):
        store.claim_session(3, "GHSA-test", "pkg")
        store.fail_dispatch(3, "API error")

        row = store.get_session_by_issue(3)
        self.assertEqual(row["outcome"], "needs_human")
        self.assertEqual(row["phase"], "dispatch_failed")
        self.assertIn("API error", row["note"])


class DispatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests for flow.dispatch with mocked Devin and GitHub."""

    def setUp(self):
        store.init_db()
        store._conn.execute("DELETE FROM sessions")
        store._conn.commit()

    def tearDown(self):
        store.close_connection()
        store._conn = None

    async def test_successful_dispatch(self):
        from flow import dispatch
        from devin import DevinError

        with patch("flow.create_session", new_callable=AsyncMock) as mock_create:
            with patch("flow.add_issue_label", new_callable=AsyncMock) as mock_add_label:
                mock_create.return_value = ("sess-abc123", "https://devin.ai/sess/abc123")

                advisory = {
                    "advisory_id": "GHSA-demo-1234",
                    "package": "demo-pkg",
                    "ecosystem": "npm",
                    "current_versions": ["1.0.0"],
                    "affected_ranges": ["<2.0.0"],
                    "fixed_versions": {"1": "2.0.0"},
                    "manifest_path": "package.json",
                    "lockfile_path": "package-lock.json",
                    "test_command": "npm test",
                    "advisory_url": "https://github.com/advisories/GHSA-demo",
                }
                config = {
                    "github_repo": "owner/repo",
                    "github_token": "token",
                    "devin_api_key": "key",
                    "devin_org_id": "org",
                    "devin_playbook_id": "pb",
                    "devin_max_acu_limit": 5,
                }

                await dispatch(1, advisory, config)

                mock_create.assert_awaited_once()
                mock_add_label.assert_awaited_once_with(
                    "owner/repo", 1, "devin-in-progress", "token"
                )

                row = store.get_session_by_issue(1)
                self.assertIsNotNone(row)
                self.assertEqual(row["session_id"], "sess-abc123")
                self.assertEqual(row["session_url"], "https://devin.ai/sess/abc123")
                self.assertEqual(row["outcome"], "dispatched")
                self.assertEqual(row["phase"], "session_created")

    async def test_duplicate_dispatch(self):
        from flow import dispatch

        with patch("flow.create_session", new_callable=AsyncMock) as mock_create:
            with patch("flow.add_issue_label", new_callable=AsyncMock) as mock_add_label:
                mock_create.return_value = ("sess-xyz", "https://devin.ai/sess/xyz")

                advisory = {
                    "advisory_id": "GHSA-demo-1234",
                    "package": "demo-pkg",
                    "ecosystem": "npm",
                    "current_versions": ["1.0.0"],
                    "affected_ranges": ["<2.0.0"],
                    "fixed_versions": {"1": "2.0.0"},
                    "manifest_path": "package.json",
                    "lockfile_path": "package-lock.json",
                    "test_command": "npm test",
                    "advisory_url": "https://github.com/advisories/GHSA-demo",
                }
                config = {
                    "github_repo": "owner/repo",
                    "github_token": "token",
                    "devin_api_key": "key",
                    "devin_org_id": "org",
                    "devin_playbook_id": "pb",
                    "devin_max_acu_limit": 5,
                }

                await dispatch(1, advisory, config)
                await dispatch(1, advisory, config)

                mock_create.assert_awaited_once()
                mock_add_label.assert_awaited_once()

                rows = store._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE issue_number = 1"
                ).fetchone()[0]
                self.assertEqual(rows, 1)

    async def test_devin_failure(self):
        from flow import dispatch
        from devin import DevinError

        with patch("flow.create_session", new_callable=AsyncMock) as mock_create:
            with patch("flow.add_issue_label", new_callable=AsyncMock) as mock_add_label:
                mock_create.side_effect = DevinError("API error")

                advisory = {
                    "advisory_id": "GHSA-demo-1234",
                    "package": "demo-pkg",
                    "ecosystem": "npm",
                    "current_versions": ["1.0.0"],
                    "affected_ranges": ["<2.0.0"],
                    "fixed_versions": {"1": "2.0.0"},
                    "manifest_path": "package.json",
                    "lockfile_path": "package-lock.json",
                    "test_command": "npm test",
                    "advisory_url": "https://github.com/advisories/GHSA-demo",
                }
                config = {
                    "github_repo": "owner/repo",
                    "github_token": "token",
                    "devin_api_key": "key",
                    "devin_org_id": "org",
                    "devin_playbook_id": "pb",
                    "devin_max_acu_limit": 5,
                }

                await dispatch(1, advisory, config)

                row = store.get_session_by_issue(1)
                self.assertIsNotNone(row)
                self.assertEqual(row["outcome"], "needs_human")
                self.assertEqual(row["phase"], "dispatch_failed")
                mock_add_label.assert_not_awaited()

                await dispatch(1, advisory, config)
                mock_create.assert_awaited_once()


class DevinErrorHandlingTests(unittest.TestCase):
    """Verify Devin error handling."""

    def test_devin_error_is_safe_exception(self):
        from devin import DevinError

        exc = DevinError("test message")
        self.assertIsInstance(exc, Exception)
        self.assertIn("test message", str(exc))

    def test_devin_error_validates_inputs(self):
        from devin import create_session, DevinError

        with self.assertRaises(DevinError):
            import asyncio
            asyncio.run(create_session(
                api_key="",
                org_id="org",
                playbook_id="pb",
                prompt="p",
                title="t",
                tags=["t"],
                max_acu_limit=5,
            ))

    def test_github_error_is_safe_exception(self):
        from gh import GitHubError

        exc = GitHubError("test message")
        self.assertIsInstance(exc, Exception)
        self.assertIn("test message", str(exc))


class GitHubLabelContractTests(unittest.IsolatedAsyncioTestCase):
    """Verify the GitHub label request contract."""

    async def test_github_label_url_and_body_are_correct(self):
        from gh import add_issue_label

        def capture_request(request):
            capture_request.url = request.url
            capture_request.body = json.loads(request.content)
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(capture_request))
        await add_issue_label("owner/repo", 42, "devin-in-progress", "token", client=client)

        expected_url = "https://api.github.com/repos/owner/repo/issues/42/labels"
        self.assertEqual(str(capture_request.url), expected_url)
        self.assertEqual(capture_request.body, {"labels": ["devin-in-progress"]})
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
