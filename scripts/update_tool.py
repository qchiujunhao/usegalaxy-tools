import argparse
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

import requests
import yaml

DEFAULT_TOOL_SHED_URL = "https://toolshed.g2.bx.psu.edu"
DEFAULT_REQUEST_INTERVAL = 0.25
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_REQUEST_TIMEOUT = 30.0
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class RevisionQueryError(Exception):
    pass


@dataclass
class UpdateStats:
    mode: str
    started_at: float = field(default_factory=time.monotonic)
    lockfiles_scanned: int = 0
    entries_considered: int = 0
    repositories_checked: int = 0
    repositories_current: int = 0
    updates_found: int = 0
    additions_found: int = 0
    additions_written: int = 0
    request_attempts: int = 0
    retries: int = 0
    rate_limits: int = 0
    server_errors: int = 0
    network_errors: int = 0
    invalid_responses: int = 0
    files_written: int = 0
    failures: list = field(default_factory=list)
    duration_seconds: float = 0.0


class RevisionFetcher:
    def __init__(
        self,
        request_interval=DEFAULT_REQUEST_INTERVAL,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        backoff_base=DEFAULT_BACKOFF_BASE,
        request_timeout=DEFAULT_REQUEST_TIMEOUT,
        session=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.request_interval = request_interval
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.request_timeout = request_timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "usegalaxy-tools-update/1.0")
        self.sleep = sleep
        self.clock = clock
        self.last_request_started_at = None

    def get_ordered_installable_revisions(self, tool_shed_url, name, owner, stats):
        tool_shed_url = normalize_tool_shed_url(tool_shed_url)
        endpoint = f"{tool_shed_url.rstrip('/')}/api/repositories/get_ordered_installable_revisions"
        last_error = None

        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_request_slot()
            response = None
            stats.request_attempts += 1

            try:
                response = self.session.get(
                    endpoint,
                    params={"name": name, "owner": owner},
                    timeout=self.request_timeout,
                )
            except requests.RequestException as exc:
                stats.network_errors += 1
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        revisions = response.json()
                    except ValueError as exc:
                        stats.invalid_responses += 1
                        last_error = f"invalid JSON response: {exc}"
                    else:
                        if not isinstance(revisions, list) or not revisions:
                            raise RevisionQueryError("Tool Shed returned no installable revisions")
                        return [str(revision) for revision in revisions]
                elif response.status_code in TRANSIENT_STATUS_CODES:
                    if response.status_code == 429:
                        stats.rate_limits += 1
                    else:
                        stats.server_errors += 1
                    last_error = f"HTTP {response.status_code}"
                else:
                    body = response.text.strip().replace("\n", " ")[:200]
                    raise RevisionQueryError(f"HTTP {response.status_code}: {body}")

            if attempt == self.max_attempts:
                break

            stats.retries += 1
            retry_delay = self._retry_delay(attempt, response)
            logging.warning(
                "Transient Tool Shed error for %s/%s (%s); retrying in %.1fs",
                owner,
                name,
                last_error,
                retry_delay,
            )
            self.sleep(retry_delay)

        raise RevisionQueryError(f"{last_error} after {self.max_attempts} attempts")

    def _wait_for_request_slot(self):
        if self.last_request_started_at is not None:
            delay = self.request_interval - (self.clock() - self.last_request_started_at)
            if delay > 0:
                self.sleep(delay)
        self.last_request_started_at = self.clock()

    def _retry_delay(self, attempt, response):
        retry_after = 0.0
        if response is not None:
            try:
                retry_after = float(response.headers.get("Retry-After", 0))
            except (TypeError, ValueError):
                retry_after = 0.0
        return max(retry_after, self.backoff_base * (2 ** (attempt - 1)))


_default_fetcher = None


def get_default_fetcher():
    global _default_fetcher
    if _default_fetcher is None:
        _default_fetcher = RevisionFetcher(
            request_interval=float(os.environ.get("TOOL_SHED_REQUEST_INTERVAL", DEFAULT_REQUEST_INTERVAL)),
            max_attempts=int(os.environ.get("TOOL_SHED_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
            backoff_base=float(os.environ.get("TOOL_SHED_BACKOFF_BASE", DEFAULT_BACKOFF_BASE)),
            request_timeout=float(os.environ.get("TOOL_SHED_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)),
        )
    return _default_fetcher


def should_update(tool, owner=None, name=None, without=False):
    if without:
        return not tool.get("revisions")
    if owner and tool["owner"] not in owner:
        return False
    if name and tool["name"] != name:
        return False
    return True


def normalize_tool_shed_url(tool_shed_url):
    if tool_shed_url.lower().startswith(("http://", "https://")):
        return tool_shed_url.rstrip("/")
    return ("https://" + tool_shed_url).rstrip("/")


def run_update(filenames, owner=None, name=None, without=False, fetcher=None, stats=None):
    mode = "missing revisions only" if without else "all matching repositories"
    stats = stats or UpdateStats(mode=mode)
    fetcher = fetcher or get_default_fetcher()
    lockfiles = {}
    targets = defaultdict(list)

    for filename in filenames:
        lock_filename = filename + ".lock"
        with open(lock_filename) as handle:
            locked = yaml.safe_load(handle)
        lockfiles[filename] = locked
        stats.lockfiles_scanned += 1

        for tool in locked["tools"]:
            logging.debug("Examining %s/%s", tool["owner"], tool["name"])
            if not should_update(tool, owner=owner, name=name, without=without):
                continue

            stats.entries_considered += 1
            configured_tool_shed_url = tool.get("tool_shed_url", DEFAULT_TOOL_SHED_URL)
            tool_shed_url = normalize_tool_shed_url(configured_tool_shed_url)
            if tool_shed_url != DEFAULT_TOOL_SHED_URL:
                logging.warning(
                    "Non-default Tool Shed URL for %s/%s: %s",
                    tool["owner"],
                    tool["name"],
                    configured_tool_shed_url,
                )
            key = (tool_shed_url, str(tool["owner"]), str(tool["name"]))
            targets[key].append((filename, tool))

    touched_files = set()
    for (tool_shed_url, repository_owner, repository_name), entries in sorted(targets.items()):
        stats.repositories_checked += 1
        logging.info("Fetching updates for %s/%s", repository_owner, repository_name)
        try:
            revisions = fetcher.get_ordered_installable_revisions(
                tool_shed_url,
                repository_name,
                repository_owner,
                stats,
            )
        except RevisionQueryError as exc:
            logging.error("Unable to check %s/%s: %s", repository_owner, repository_name, exc)
            stats.failures.append(
                {
                    "owner": repository_owner,
                    "name": repository_name,
                    "error": str(exc),
                }
            )
            break

        latest_revision = revisions[-1]
        updated_entries = 0
        for filename, tool in entries:
            if latest_revision in tool.get("revisions", []):
                continue
            tool.setdefault("revisions", []).append(latest_revision)
            touched_files.add(filename)
            updated_entries += 1

        if updated_entries:
            logging.info(
                "Found newer revision of %s/%s (%s)",
                repository_owner,
                repository_name,
                latest_revision,
            )
            stats.updates_found += 1
            stats.additions_found += updated_entries
        else:
            stats.repositories_current += 1

    if stats.failures:
        logging.error("Not writing lockfiles because %d repositories could not be checked", len(stats.failures))
        return stats

    for filename in sorted(touched_files):
        with open(filename + ".lock", "w") as handle:
            yaml.dump(lockfiles[filename], handle, default_flow_style=False)

    stats.files_written = len(touched_files)
    stats.additions_written = stats.additions_found
    return stats


def update_file(fn, owner=None, name=None, without=False, fetcher=None):
    stats = run_update([fn], owner=owner, name=name, without=without, fetcher=fetcher)
    if stats.failures:
        raise RevisionQueryError(f"{len(stats.failures)} repositories could not be checked")
    return stats


def summary_markdown(stats):
    status = "failed" if stats.failures else "completed"
    lines = [
        f"## Tool Shed update summary ({stats.mode})",
        "",
        f"**Status:** {status}",
        "",
        f"- Lockfiles scanned: {stats.lockfiles_scanned}",
        f"- Repository entries considered: {stats.entries_considered}",
        f"- Unique repositories checked: {stats.repositories_checked}",
        f"- Repositories already current: {stats.repositories_current}",
        f"- Latest revisions found: {stats.updates_found}",
        f"- Lockfile additions written: {stats.additions_written}",
        f"- Files written: {stats.files_written}",
        f"- Request attempts: {stats.request_attempts}",
        f"- Transient retries: {stats.retries}",
        f"- HTTP 429 responses: {stats.rate_limits}",
        f"- HTTP 5xx responses: {stats.server_errors}",
        f"- Network errors: {stats.network_errors}",
        f"- Invalid responses: {stats.invalid_responses}",
        f"- Unresolved failures: {len(stats.failures)}",
        f"- Duration: {stats.duration_seconds:.1f}s",
    ]

    if stats.failures:
        lines.extend(["", "### Unresolved repositories", ""])
        for failure in stats.failures[:50]:
            error = failure["error"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"- `{failure['owner']}/{failure['name']}`: {error}")
        if len(stats.failures) > 50:
            lines.append(f"- …and {len(stats.failures) - 50} more")

    return "\n".join(lines) + "\n"


def emit_summary(stats):
    summary = summary_markdown(stats)
    logging.info("\n%s", summary)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write(summary)
            handle.write("\n")


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("fn", nargs="+", help="Tool YAML files")
    parser.add_argument(
        "--owner",
        action="append",
        help="Repository owner to filter on. Can be specified multiple times.",
    )
    parser.add_argument("--name", help="Repository name to filter on")
    parser.add_argument(
        "--without",
        action="store_true",
        help="Ignore owner/name and add the latest revision only to entries without revisions.",
    )
    parser.add_argument(
        "--log",
        choices=("critical", "error", "warning", "info", "debug"),
        default="info",
    )
    return parser


def main(argv=None, fetcher=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log.upper()))
    stats = UpdateStats(mode="missing revisions only" if args.without else "all matching repositories")

    try:
        run_update(
            args.fn,
            owner=args.owner,
            name=args.name,
            without=args.without,
            fetcher=fetcher,
            stats=stats,
        )
    except Exception as exc:
        logging.exception("Tool update failed unexpectedly")
        stats.failures.append(
            {
                "owner": "internal",
                "name": "update_tool",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        stats.duration_seconds = time.monotonic() - stats.started_at
        emit_summary(stats)

    return 1 if stats.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
