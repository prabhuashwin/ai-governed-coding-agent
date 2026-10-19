"""Regression tests for the sixth external review (19 October 2026, main @ db227b2): six findings."""

from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import closing

import pytest

from gca.models import Restrictions
from gca.publish import github_slug, parse_remote
from gca.retention import prune
from gca.workspace import KILL_DRAIN_SECONDS, run_until_stopped


# ---------------------------------------------------------------- 1 (P1) a child that outlives its parent
def test_stop_kills_a_child_whose_parent_already_exited(tmp_path):
    """The command spawns a child and exits at once; the child keeps writing and holds the output pipes. A stop
    must still end the child (the group/job outlives its leader) and return within the drain bound."""
    marker = tmp_path / "writes.txt"
    child = tmp_path / "child.py"
    child.write_text(f"import time\nwhile True:\n    open({str(marker)!r}, 'a').write('x')\n    time.sleep(0.05)\n")
    parent = [sys.executable, "-c", f"import subprocess, sys\nsubprocess.Popen([sys.executable, {str(child)!r}])\n"]
    stop_at = time.monotonic() + 1.0
    t0 = time.monotonic()
    res = run_until_stopped(parent, cwd=str(tmp_path), env=None, timeout=60,
                            stop_check=lambda: time.monotonic() > stop_at)
    elapsed = time.monotonic() - t0
    assert res.interrupted and elapsed < 1.0 + KILL_DRAIN_SECONDS + 3, elapsed
    size = marker.stat().st_size
    time.sleep(0.5)
    assert marker.stat().st_size == size, "the orphaned child kept writing after the stop"


# ---------------------------------------------------------------- 2 (P2) bounded pipe shutdown (Windows reader threads)
def test_stop_returns_within_the_drain_bound_even_if_pipes_stay_open(tmp_path):
    """A process that escapes the group/job (its own session) and holds the pipes must not hold the worker
    beyond the drain bound — on any platform, including Windows where closing a pipe under a reader blocks."""
    holder = tmp_path / "holder.py"
    if sys.platform == "win32":
        flags = "creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB"
    else:
        flags = "start_new_session=True"
    holder.write_text("import time\ntime.sleep(20)\n")
    parent = [sys.executable, "-c", f"import subprocess, sys\n"
                                    f"try:\n    subprocess.Popen([sys.executable, {str(holder)!r}], {flags})\n"
                                    f"except OSError:\n    subprocess.Popen([sys.executable, {str(holder)!r}])\n"
                                    "import time; time.sleep(30)"]
    stop_at = time.monotonic() + 0.8
    t0 = time.monotonic()
    res = run_until_stopped(parent, cwd=str(tmp_path), env=None, timeout=60, stop_check=lambda: time.monotonic() > stop_at)
    assert res.interrupted
    assert time.monotonic() - t0 < 0.8 + KILL_DRAIN_SECONDS + 4


# ---------------------------------------------------------------- 3 (P2) prune on a pre-migration database
def test_prune_works_on_a_database_without_first_received_at(config):
    db = config.db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as c, c:
        c.execute("CREATE TABLE inbound_deliveries (id TEXT PRIMARY KEY, received_at TEXT NOT NULL, "
                  "status INTEGER NOT NULL, response TEXT NOT NULL)")  # the 0.1.0 shape, never migrated
        c.execute("INSERT INTO inbound_deliveries VALUES ('old', '2020-01-01T00:00:00+00:00', 200, '{}')")
        c.execute("INSERT INTO inbound_deliveries VALUES ('new', '2999-01-01T00:00:00+00:00', 200, '{}')")
    assert prune(config, dry_run=True)["inbound_deliveries"] == 1
    assert prune(config)["inbound_deliveries"] == 1
    with closing(sqlite3.connect(db)) as c:
        assert [r[0] for r in c.execute("SELECT id FROM inbound_deliveries")] == ["new"]


# ---------------------------------------------------------------- 4 (P2) remote parsing compares real hostnames
@pytest.mark.parametrize("url,host,slug", [
    ("https://ghe.corp.example:8443/org/repo.git", "ghe.corp.example", "org/repo"),  # explicit port
    ("ssh://git@ghe.corp.example:2222/org/repo", "ghe.corp.example", "org/repo"),
    ("https://user:tok@github.com/org/repo.git", None, "org/repo"),  # credentials in the URL
    ("https://evil.example/github.com/org/repo", None, None),  # hostname text inside another host's path
    ("https://evil.example/ghe.corp.example/org/repo", "ghe.corp.example", None),
    ("https://github.com.evil.example/org/repo", None, None),  # prefix match is not a match
    ("https://github.com/org/repo/extra", None, None),  # not owner/repo
    ("git@github.com:org/repo.git", None, "org/repo"),
    ("C:\\\\repos\\\\remote.git", None, None),  # a Windows path is not host:path
    ("/srv/git/bare.git", None, None),
])
def test_slug_uses_the_real_hostname(url, host, slug):
    assert github_slug(url, host) == slug


def test_parse_remote_shapes():
    assert parse_remote("https://ghe.corp.example:8443/org/repo.git") == ("ghe.corp.example", "org/repo")
    assert parse_remote("git@ghe.corp.example:org/repo") == ("ghe.corp.example", "org/repo")
    assert parse_remote("https://ghe.corp.example/org") == ("ghe.corp.example", None)
    assert parse_remote("not a url") == (None, None)


# ---------------------------------------------------------------- 6 (P3) very large integer budgets
@pytest.mark.parametrize("huge", [10**309, 10**400])
def test_huge_integer_budgets_are_rejected_not_500(huge, work_item, config):
    from gca.models import Budget
    from gca.queue import JobQueue
    r = Restrictions.model_validate({"budget": {"max_turns": huge}})  # finite, so validation passes...
    q = JobQueue(config.db_path)
    job = q.submit(work_item)
    with pytest.raises(ValueError, match="would raise"):  # ...and the narrowing rule rejects it cleanly
        q.approve(job.id, "lead", restrictions=r)
    assert q.get(job.id).status.value == "submitted"
    assert Budget.model_validate({"max_turns": 5}).max_turns == 5
    q.close()
