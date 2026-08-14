"""Tests for Step 10 verification: workflow_run outcomes drive verified,
protected-file rejection, and a single repair attempt.

Uses only unittest + AsyncMock, never the network, never real credentials.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

TEST_REPO = "aman-sharma-nine/superset"

_tmp_dir = tempfile.TemporaryDirectory(prefix="orchestrator-verify-test-")
os.environ["DATABASE_PATH"] = str(Path(_tmp_dir.name) / "test.db")

import store  # noqa: E402  (env vars must be set before import)
import flow  # noqa: E402


def tearDownModule():
    _tmp_dir.cleanup()


class HandleWorkflowRunTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests for flow.handle_workflow_run with mocked GitHub/Devin."""

    def setUp(self):
        store.init_db()
        store._conn.execute("DELETE FROM sessions")
        store._conn.commit()

    def tearDown(self):
        store.close_connection()
        store._conn = None

    def _seed_session_with_pr(self, issue_number=1, session_id="sess-1", head_sha="a" * 40,
                               pr_url="https://github.com/aman-sharma-nine/superset/pull/5"):
        store.claim_session(issue_number, "GHSA-demo", "demo-pkg")
        store.complete_dispatch(issue_number, session_id, f"https://devin.ai/sess/{session_id}")
        store.record_pr_discovered(issue_number, pr_url, head_sha, 1.0)

    def _config(self):
        return {
            "github_repo": TEST_REPO,
            "github_token": "token",
            "devin_api_key": "key",
            "devin_org_id": "org",
        }

    async def test_success_without_protected_files_marks_verified(self):
        self._seed_session_with_pr()

        with patch("flow.get_pull_request_files", new_callable=AsyncMock) as mock_files, \
                patch("flow.create_session", new_callable=AsyncMock) as mock_create:
            mock_files.return_value = ["superset-frontend/package.json", "superset-frontend/package-lock.json"]

            await flow.handle_workflow_run("a" * 40, "success", "https://github.com/x/actions/runs/1", self._config())

            mock_create.assert_not_called()

        row = store.get_session_by_issue(1)
        self.assertEqual(row["outcome"], "verified")
        self.assertEqual(row["phase"], "verified")
        self.assertEqual(row["check_state"], "green")
        self.assertIsNotNone(row["verified_at"])

    async def test_success_with_protected_files_is_rejected(self):
        self._seed_session_with_pr()

        with patch("flow.get_pull_request_files", new_callable=AsyncMock) as mock_files:
            mock_files.return_value = [
                "superset-frontend/package.json",
                ".github/workflows/verify-remediation.yml",
            ]

            await flow.handle_workflow_run("a" * 40, "success", "https://github.com/x/actions/runs/1", self._config())

        row = store.get_session_by_issue(1)
        self.assertEqual(row["outcome"], "needs_human")
        self.assertEqual(row["phase"], "protected_files_modified")
        self.assertNotEqual(row["outcome"], "verified")

    async def test_one_repair_maximum_then_needs_human(self):
        self._seed_session_with_pr(head_sha="a" * 40)

        with patch("flow.send_message", new_callable=AsyncMock) as mock_send, \
                patch("flow.create_session", new_callable=AsyncMock) as mock_create, \
                patch("flow.get_pull_request_head_sha", new_callable=AsyncMock) as mock_get_sha:
            # First failure: starts the single repair attempt. head_sha is
            # kept (the failed SHA), not cleared, so the poller can detect
            # when Devin pushes a new commit.
            await flow.handle_workflow_run("a" * 40, "failure", "https://github.com/x/actions/runs/1", self._config())

            row = store.get_session_by_issue(1)
            self.assertEqual(row["outcome"], "repairing")
            self.assertEqual(row["repair_attempts"], 1)
            self.assertEqual(row["head_sha"], "a" * 40)
            mock_send.assert_awaited_once()
            sent_message = mock_send.call_args.args[3]
            self.assertIn("https://github.com/aman-sharma-nine/superset/pull/5", sent_message)
            self.assertIn("a" * 40, sent_message)

            # Poll cycle: GitHub still reports the same (failed) SHA -
            # Devin hasn't pushed yet, row must remain repairing untouched.
            mock_get_sha.return_value = "a" * 40
            await flow.poll_once(self._config())

            row = store.get_session_by_issue(1)
            self.assertEqual(row["outcome"], "repairing")
            self.assertEqual(row["phase"], "repairing")
            self.assertEqual(row["head_sha"], "a" * 40)

            # Poll cycle: Devin pushed a new commit - row re-enters awaiting_ci.
            mock_get_sha.return_value = "b" * 40
            await flow.poll_once(self._config())

            row = store.get_session_by_issue(1)
            self.assertEqual(row["outcome"], "awaiting_ci")
            self.assertEqual(row["phase"], "repair_pushed")
            self.assertEqual(row["check_state"], "absent")
            self.assertEqual(row["head_sha"], "b" * 40)

            # Second failure, on the new SHA: must not repair again, must
            # not create a new session, must not send a second message.
            await flow.handle_workflow_run("b" * 40, "failure", "https://github.com/x/actions/runs/2", self._config())

            mock_create.assert_not_called()
            mock_send.assert_awaited_once()  # still only the one call from the first failure

        row = store.get_session_by_issue(1)
        self.assertEqual(row["outcome"], "needs_human")
        self.assertEqual(row["phase"], "repair_failed")
        self.assertEqual(row["repair_attempts"], 1)

    async def test_no_new_devin_session_is_ever_created(self):
        # Two independent sessions: one goes through failure/repair, the
        # other through success. create_session must never be called by
        # handle_workflow_run in either path.
        self._seed_session_with_pr(issue_number=1, session_id="sess-1", head_sha="a" * 40,
                                    pr_url="https://github.com/aman-sharma-nine/superset/pull/5")
        self._seed_session_with_pr(issue_number=2, session_id="sess-2", head_sha="c" * 40,
                                    pr_url="https://github.com/aman-sharma-nine/superset/pull/6")

        with patch("flow.get_pull_request_files", new_callable=AsyncMock) as mock_files, \
                patch("flow.send_message", new_callable=AsyncMock), \
                patch("flow.create_session", new_callable=AsyncMock) as mock_create:
            mock_files.return_value = []

            await flow.handle_workflow_run("a" * 40, "failure", "https://github.com/x/actions/runs/1", self._config())
            await flow.handle_workflow_run("c" * 40, "success", "https://github.com/x/actions/runs/2", self._config())

            mock_create.assert_not_called()

        row1 = store.get_session_by_issue(1)
        row2 = store.get_session_by_issue(2)
        self.assertEqual(row1["outcome"], "repairing")
        self.assertEqual(row2["outcome"], "verified")


if __name__ == "__main__":
    unittest.main()
