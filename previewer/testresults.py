"""Module test results from an Odoo build log (`--test-enable`).

Pure functions; the formats come from real Odoo 18/19/20 runs in tests/fixtures/odoo_logs
(*-tests*.log, made from tests/fixtures/test_modules/diag_tests). The worker saves the result
as Build.test_results / Build.test_status.
"""
import re
from dataclasses import asdict, dataclass, field

from .diagnose import Source, _github_url, _locate

_SUMMARY = re.compile(r"odoo\.tests\.result: (\d+) failed, (\d+) error\(s\) of (\d+) tests")
_FAILURE = re.compile(r" odoo\.addons\.(\w+)\.\S*: (FAIL|ERROR): ([\w.]+)\s*$")
_SKIPPED = re.compile(r" odoo\.addons\.(\w+)\.\S*: skipped ([\w.]+) : (.+?)\s*$")
_STARTING = re.compile(r" odoo\.addons\.(\w+)\.\S*: Starting ([\w.]+) \.\.\.")
_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \d+ [A-Z]+ ")
_MNT_FILE = re.compile(r'File "(/mnt/[^"]+)", line (\d+)')
MAX_TRACEBACK_LINES = 40


@dataclass
class TestFailure:
    kind: str            # "fail" (assertion) | "error" (exception)
    test: str            # TestThings.test_fails
    module: str
    message: str         # last line of the traceback, e.g. AssertionError: ...
    file: str | None = None
    line: int | None = None
    source: str | None = None
    url: str | None = None
    traceback: str = ""


@dataclass
class TestReport:
    ran: bool = False     # a result line was found
    total: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    modules: list[str] = field(default_factory=list)
    failures: list[TestFailure] = field(default_factory=list)
    skipped_tests: list[dict] = field(default_factory=list)  # {test, module, reason}

    @property
    def passed(self) -> int:
        return max(0, self.total - self.failed - self.errors - self.skipped)

    @property
    def status(self) -> str | None:
        """"passed" | "failed" | "none" (the modules have no tests) | None (tests did not run)."""
        if not self.ran:
            return None
        if self.total == 0:
            return "none"
        return "failed" if self.failed or self.errors else "passed"

    @property
    def headline(self) -> str:
        if not self.ran:
            return "No tests ran"
        if self.status == "none":
            return "These modules have no tests"
        if self.status == "passed":
            extra = f", {self.skipped} skipped" if self.skipped else ""
            return f"{self.passed} test(s) passed{extra}"
        bad = []
        if self.failed:
            bad.append(f"{self.failed} failed")
        if self.errors:
            bad.append(f"{self.errors} error(s)")
        return f"{' and '.join(bad)} of {self.total} tests"

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(passed=self.passed, status=self.status, headline=self.headline)
        return d


def _traceback(lines: list[str], start: int) -> list[str]:
    """Lines after a FAIL:/ERROR: header up to the next Odoo log line."""
    out = []
    for l in lines[start + 1:]:
        if _LOG_LINE.match(l):
            break
        out.append(l)
    return out


def parse(log: str, mounts: dict[str, Source] | None = None) -> TestReport:
    mounts = mounts or {}
    lines = log.splitlines()
    r = TestReport()
    summaries = [m for m in map(_SUMMARY.search, lines) if m]
    if summaries:
        failed, errors, total = map(int, summaries[-1].groups())
        r.ran, r.failed, r.errors, r.total = True, failed, errors, total
    modules = []
    for i, l in enumerate(lines):
        m = _STARTING.search(l)
        if m and m.group(1) not in modules:
            modules.append(m.group(1))
        m = _SKIPPED.search(l)
        if m:
            r.skipped_tests.append({"module": m.group(1), "test": m.group(2), "reason": m.group(3)})
        m = _FAILURE.search(l)
        if not m:
            continue
        tb = _traceback(lines, i)
        # The exception line and whatever follows it (assertion diffs span several lines)
        exc = max((k for k, t in enumerate(tb) if re.match(r"^[\w.]+(Error|Exception|Failure)\b", t)),
                  default=None)
        message = "\n".join(t.rstrip() for t in tb[exc:] if t.strip())[:2000] if exc is not None else ""
        frames = [f for f in _MNT_FILE.findall("\n".join(tb)) if f"/{m.group(1)}/" in f[0]]
        path, ln = frames[-1] if frames else (None, None)
        _, file, src = _locate(path, mounts) if path else (None, None, None)
        line_no = int(ln) if ln else None
        r.failures.append(TestFailure(
            kind="fail" if m.group(2) == "FAIL" else "error", test=m.group(3), module=m.group(1),
            message=message, file=file, line=line_no, source=src.label if src else None,
            url=_github_url(src, file, line_no), traceback="\n".join(tb[-MAX_TRACEBACK_LINES:])))
    r.modules = modules
    r.skipped = len({(s["module"], s["test"]) for s in r.skipped_tests})
    return r
