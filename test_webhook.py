"""Tests for secure GitHub webhook intake (Step 7).

Uses only unittest + httpx (ASGITransport), never the network, never real credentials.
"""

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

TEST_SECRET = "test-webhook-secret"
TEST_REPO = "aman-sharma-nine/superset"
TEST_ACTOR = "aman-sharma-nine"

_tmp_dir = tempfile.TemporaryDirectory(prefix="orchestrator-test-")
os.environ["DATABASE_PATH"] = str(Path(_tmp_dir.name) / "test.db")
os.environ["WEBHOOK_SECRET"] = TEST_SECRET
os.environ["GITHUB_REPO"] = TEST_REPO
os.environ["APPROVED_ACTORS"] = TEST_ACTOR
os.environ["GITHUB_TOKEN"] = "fake-gh-token"
os.environ["DEVIN_API_KEY"] = "fake-devin-key"
os.environ["DEVIN_ORG_ID"] = "fake-org-id"
os.environ["DEVIN_PLAYBOOK_ID"] = "fake-playbook-id"
os.environ["DEVIN_MAX_ACU_LIMIT"] = "5"
os.environ["VERIFY_WORKFLOW_NAME"] = "verify-remediation"

import app as app_module  # noqa: E402  (env vars must be set before import)
import flow  # noqa: E402
import store  # noqa: E402


def tearDownModule():
    _tmp_dir.cleanup()


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _issue_payload(issue_number=1, label_name="devin-remediate", action="labeled",
                    actor=TEST_ACTOR, repo=TEST_REPO):
    payload = {
        "action": action,
        "repository": {"full_name": repo},
        "sender": {"login": actor},
        "label": {"name": label_name},
        "issue": {"number": issue_number},
    }
    return payload


def _workflow_run_payload(name="verify-remediation", action="completed",
                           conclusion="success", head_sha="a" * 40, repo=TEST_REPO):
    payload = {
        "action": action,
        "repository": {"full_name": repo},
        "sender": {"login": "github-actions[bot]"},
        "workflow_run": {
            "name": name,
            "head_sha": head_sha,
            "conclusion": conclusion,
            "html_url": "https://github.com/" + repo + "/actions/runs/123",
        },
    }
    return payload


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    _counter = 0

    @classmethod
    def _delivery_id(cls):
        cls._counter += 1
        return f"test-delivery-{cls._counter}"

    async def asyncSetUp(self):
        self._dispatch_patcher = patch.object(app_module, "dispatch", new_callable=AsyncMock)
        self.mock_dispatch = self._dispatch_patcher.start()

        self._workflow_run_patcher = patch.object(app_module, "handle_workflow_run", new_callable=AsyncMock)
        self.mock_handle_workflow_run = self._workflow_run_patcher.start()

        self._lifespan_cm = app_module.app.router.lifespan_context(app_module.app)
        await self._lifespan_cm.__aenter__()
        transport = httpx.ASGITransport(app=app_module.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self._lifespan_cm.__aexit__(None, None, None)
        self._dispatch_patcher.stop()
        self._workflow_run_patcher.stop()

    def _row_count(self, delivery_id):
        cur = store._conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        )
        return cur.fetchone()[0]

    async def _post(self, body: bytes, headers: dict):
        return await self.client.post("/webhook/github", content=body, headers=headers)

    async def test_health_returns_200(self):
        r = await self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    async def test_valid_curated_event_is_accepted_and_stored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload()).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json(), {"status": "accepted", "issue_number": 1})
        self.assertEqual(self._row_count(delivery_id), 1)
        self.mock_dispatch.assert_awaited_once()

    async def test_replaying_delivery_id_is_ignored_once_stored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload()).encode()
        headers = {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        }
        r1 = await self._post(body, headers)
        self.assertEqual(r1.status_code, 202)

        r2 = await self._post(body, headers)
        self.assertEqual(r2.status_code, 202)
        self.assertEqual(r2.json(), {"status": "ignored", "reason": "duplicate_delivery"})
        self.assertEqual(self._row_count(delivery_id), 1)

        self.assertFalse(store._conn.in_transaction)
        self.mock_dispatch.assert_awaited_once()

        other_delivery_id = self._delivery_id()
        other_body = json.dumps(_issue_payload()).encode()
        r3 = await self._post(other_body, {
            "X-Hub-Signature-256": _sign(other_body),
            "X-GitHub-Delivery": other_delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r3.status_code, 202)
        self.assertEqual(r3.json(), {"status": "accepted", "issue_number": 1})
        self.assertEqual(self._row_count(other_delivery_id), 1)
        self.assertEqual(self.mock_dispatch.await_count, 2)

    async def test_signed_non_object_json_root_is_rejected_and_not_stored(self):
        delivery_id = self._delivery_id()
        body = json.dumps([1, 2, 3]).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._row_count(delivery_id), 0)

    async def test_missing_signature_is_rejected_and_not_stored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload()).encode()
        r = await self._post(body, {
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self._row_count(delivery_id), 0)

    async def test_invalid_signature_is_rejected_and_not_stored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload()).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self._row_count(delivery_id), 0)

    async def test_signed_malformed_json_is_rejected_and_not_stored(self):
        delivery_id = self._delivery_id()
        body = b"{not valid json"
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._row_count(delivery_id), 0)

    async def test_wrong_repository_is_rejected_and_not_stored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload(repo="someone-else/other-repo")).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._row_count(delivery_id), 0)

    async def test_unapproved_actor_is_rejected_and_not_stored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload(actor="someone-else")).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._row_count(delivery_id), 0)

    async def test_missing_delivery_id_returns_400(self):
        body = json.dumps(_issue_payload()).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 400)

    async def test_unsupported_event_is_ignored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload()).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "ping",
        })
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json(), {"status": "ignored", "reason": "unsupported_event"})
        self.mock_dispatch.assert_not_awaited()

    async def test_unsupported_action_is_ignored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload(action="opened")).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json(), {"status": "ignored", "reason": "unsupported_action"})
        self.mock_dispatch.assert_not_awaited()

    async def test_different_label_is_ignored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload(label_name="bug")).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json(), {"status": "ignored", "reason": "irrelevant_label"})
        self.mock_dispatch.assert_not_awaited()

    async def test_uncurated_issue_is_ignored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_issue_payload(issue_number=999999)).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json(), {"status": "ignored", "reason": "uncurated_issue"})
        self.mock_dispatch.assert_not_awaited()

    async def test_missing_issue_number_returns_400(self):
        delivery_id = self._delivery_id()
        payload = _issue_payload()
        del payload["issue"]["number"]
        body = json.dumps(payload).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 400)

    async def test_boolean_issue_number_returns_400(self):
        delivery_id = self._delivery_id()
        payload = _issue_payload()
        payload["issue"]["number"] = True
        body = json.dumps(payload).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
        })
        self.assertEqual(r.status_code, 400)

    async def test_missing_server_configuration_returns_503(self):
        original = os.environ.pop("WEBHOOK_SECRET")
        try:
            delivery_id = self._delivery_id()
            body = json.dumps(_issue_payload()).encode()
            r = await self._post(body, {
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Delivery": delivery_id,
                "X-GitHub-Event": "issues",
            })
            self.assertEqual(r.status_code, 503)
        finally:
            os.environ["WEBHOOK_SECRET"] = original

    async def test_workflow_run_wrong_name_is_ignored(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_workflow_run_payload(name="some-other-workflow")).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "workflow_run",
        })
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json(), {"status": "ignored", "reason": "irrelevant_workflow"})
        self.mock_handle_workflow_run.assert_not_awaited()

    async def test_workflow_run_matching_and_completed_schedules_verification(self):
        delivery_id = self._delivery_id()
        body = json.dumps(_workflow_run_payload()).encode()
        r = await self._post(body, {
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "workflow_run",
        })
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json(), {"status": "accepted"})
        self.mock_handle_workflow_run.assert_awaited_once_with(
            "a" * 40, "success", "https://github.com/" + TEST_REPO + "/actions/runs/123",
            {
                "github_repo": TEST_REPO,
                "github_token": "fake-gh-token",
                "devin_api_key": "fake-devin-key",
                "devin_org_id": "fake-org-id",
            },
        )
        self.mock_dispatch.assert_not_awaited()


class LifespanRepeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_lifespans_do_not_leak_the_connection(self):
        for _ in range(2):
            cm = app_module.app.router.lifespan_context(app_module.app)
            await cm.__aenter__()
            self.assertIsNotNone(store._conn)
            await cm.__aexit__(None, None, None)
            self.assertIsNone(store._conn)


if __name__ == "__main__":
    unittest.main()
