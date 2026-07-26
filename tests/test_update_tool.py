import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import update_tool  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self.payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeFetcher:
    def __init__(self, revisions=None, error=None):
        self.revisions = revisions
        self.error = error
        self.calls = []

    def get_ordered_installable_revisions(self, tool_shed_url, name, owner, stats):
        self.calls.append((tool_shed_url, owner, name))
        stats.request_attempts += 1
        if self.error:
            raise self.error
        return self.revisions


class MixedFetcher:
    def __init__(self):
        self.calls = []

    def get_ordered_installable_revisions(self, tool_shed_url, name, owner, stats):
        self.calls.append(name)
        stats.request_attempts += 1
        if name == "z_broken":
            raise update_tool.RevisionQueryError("HTTP 429 after 6 attempts")
        return ["old", "latest"]


class UpdateToolTestCase(unittest.TestCase):
    def write_lockfile(self, directory, name, revisions, repository_name="example"):
        filename = Path(directory) / name
        with open(str(filename) + ".lock", "w") as handle:
            yaml.safe_dump(
                {
                    "tools": [
                        {
                            "name": repository_name,
                            "owner": "iuc",
                            "revisions": revisions,
                        }
                    ]
                },
                handle,
            )
        return str(filename)

    def read_revisions(self, filename):
        with open(filename + ".lock") as handle:
            return yaml.safe_load(handle)["tools"][0]["revisions"]

    def test_retries_429_then_succeeds(self):
        session = FakeSession(
            [
                FakeResponse(429, text="rate limited", headers={"Retry-After": "0"}),
                FakeResponse(200, payload=["old", "latest"]),
            ]
        )
        sleeps = []
        fetcher = update_tool.RevisionFetcher(
            request_interval=0,
            max_attempts=3,
            backoff_base=0,
            session=session,
            sleep=sleeps.append,
        )
        stats = update_tool.UpdateStats(mode="test")

        revisions = fetcher.get_ordered_installable_revisions(
            update_tool.DEFAULT_TOOL_SHED_URL,
            "example",
            "iuc",
            stats,
        )

        self.assertEqual(revisions, ["old", "latest"])
        self.assertEqual(stats.request_attempts, 2)
        self.assertEqual(stats.retries, 1)
        self.assertEqual(stats.rate_limits, 1)
        self.assertEqual(sleeps, [0])

    def test_adds_only_latest_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = self.write_lockfile(directory, "tools.yml", ["old"])
            fetcher = FakeFetcher(["old", "intermediate", "latest"])

            stats = update_tool.run_update([filename], fetcher=fetcher)

            self.assertFalse(stats.failures)
            self.assertEqual(self.read_revisions(filename), ["old", "latest"])
            self.assertEqual(stats.updates_found, 1)
            self.assertEqual(stats.additions_written, 1)

    def test_deduplicates_repository_queries_across_lockfiles(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.write_lockfile(directory, "first.yml", ["old"])
            second = self.write_lockfile(directory, "second.yml", ["old"])
            fetcher = FakeFetcher(["old", "latest"])

            stats = update_tool.run_update([first, second], fetcher=fetcher)

            self.assertEqual(len(fetcher.calls), 1)
            self.assertEqual(stats.repositories_checked, 1)
            self.assertEqual(stats.additions_written, 2)
            self.assertEqual(self.read_revisions(first), ["old", "latest"])
            self.assertEqual(self.read_revisions(second), ["old", "latest"])

    def test_unresolved_failure_returns_nonzero_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = self.write_lockfile(directory, "tools.yml", ["old"])
            fetcher = FakeFetcher(error=update_tool.RevisionQueryError("HTTP 429 after 6 attempts"))
            summary_path = Path(directory) / "summary.md"

            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}):
                exit_code = update_tool.main([filename], fetcher=fetcher)

            self.assertEqual(exit_code, 1)
            self.assertEqual(self.read_revisions(filename), ["old"])
            summary = summary_path.read_text()
            self.assertIn("Status:** failed", summary)
            self.assertIn("Unresolved failures: 1", summary)
            self.assertIn("`iuc/example`", summary)

    def test_failure_does_not_write_other_repository_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            current = self.write_lockfile(directory, "current.yml", ["old"], repository_name="a_current")
            broken = self.write_lockfile(directory, "broken.yml", ["old"], repository_name="z_broken")
            fetcher = MixedFetcher()

            stats = update_tool.run_update([current, broken], fetcher=fetcher)

            self.assertEqual(fetcher.calls, ["a_current", "z_broken"])
            self.assertEqual(len(stats.failures), 1)
            self.assertEqual(stats.additions_found, 1)
            self.assertEqual(stats.additions_written, 0)
            self.assertEqual(self.read_revisions(current), ["old"])
            self.assertEqual(self.read_revisions(broken), ["old"])

    def test_retries_network_error(self):
        session = FakeSession(
            [
                requests.ConnectionError("connection failed"),
                FakeResponse(200, payload=["latest"]),
            ]
        )
        fetcher = update_tool.RevisionFetcher(
            request_interval=0,
            max_attempts=2,
            backoff_base=0,
            session=session,
            sleep=lambda _: None,
        )
        stats = update_tool.UpdateStats(mode="test")

        revisions = fetcher.get_ordered_installable_revisions(
            update_tool.DEFAULT_TOOL_SHED_URL,
            "example",
            "iuc",
            stats,
        )

        self.assertEqual(revisions, ["latest"])
        self.assertEqual(stats.network_errors, 1)
        self.assertEqual(stats.retries, 1)

    def test_retries_server_error(self):
        session = FakeSession(
            [
                FakeResponse(503, text="unavailable"),
                FakeResponse(200, payload=["latest"]),
            ]
        )
        fetcher = update_tool.RevisionFetcher(
            request_interval=0,
            max_attempts=2,
            backoff_base=0,
            session=session,
            sleep=lambda _: None,
        )
        stats = update_tool.UpdateStats(mode="test")

        revisions = fetcher.get_ordered_installable_revisions(
            update_tool.DEFAULT_TOOL_SHED_URL,
            "example",
            "iuc",
            stats,
        )

        self.assertEqual(revisions, ["latest"])
        self.assertEqual(stats.server_errors, 1)
        self.assertEqual(stats.retries, 1)

    def test_normalizes_tool_shed_url_without_scheme(self):
        self.assertEqual(
            update_tool.normalize_tool_shed_url("testtoolshed.g2.bx.psu.edu/"),
            "https://testtoolshed.g2.bx.psu.edu",
        )

    def test_writes_github_step_summary(self):
        stats = update_tool.UpdateStats(mode="test")
        stats.lockfiles_scanned = 86
        stats.repositories_checked = 1903
        stats.updates_found = 77
        stats.additions_written = 77
        stats.duration_seconds = 12.5

        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "summary.md"
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}):
                update_tool.emit_summary(stats)

            summary = summary_path.read_text()

        self.assertIn("Lockfiles scanned: 86", summary)
        self.assertIn("Unique repositories checked: 1903", summary)
        self.assertIn("Latest revisions found: 77", summary)
        self.assertIn("Lockfile additions written: 77", summary)
        self.assertIn("Unresolved failures: 0", summary)


if __name__ == "__main__":
    unittest.main()
