"""Why did a build fail? Reads an Odoo build log and returns findings a person can act on:
the kind of problem (XML, view, data, Python, dependency...), the file and line in the repo
(with a GitHub link), Odoo's own message and a hint.

Pure functions; the patterns were taken from real Odoo 18 and 19 failure logs
(tests/fixtures/odoo_logs). The worker saves the result as Build.diagnosis.
"""
import re
from dataclasses import asdict, dataclass, field


@dataclass
class Source:
    """Where an in-container addons folder comes from."""
    label: str                 # "payroll_extension @ v18" / "Odoo Enterprise"
    repo_url: str = ""         # for GitHub links; blank = no link
    ref: str = ""              # commit sha or branch
    root_module: str = ""      # set when the repo root IS this module (exported into a subfolder)


@dataclass
class Finding:
    kind: str
    title: str
    message: str
    hint: str
    module: str | None = None
    file: str | None = None    # path inside its repo, e.g. "views/hr_views.xml"
    line: int | None = None
    source: str | None = None  # Source.label
    url: str | None = None     # GitHub link to the file and line
    excerpt: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def where(self) -> str:
        if not self.file:
            return ""
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"{loc} ({self.source})" if self.source else loc


@dataclass
class Signals:
    """Facts the worker knows that are not in Odoo's output."""
    timed_out: bool = False
    timeout_minutes: int = 0
    oom_killed: bool = False
    memory_limit: str = ""
    health_failed: bool = False
    exit_code: int | None = None


_MNT_FILE = re.compile(r'File "(/mnt/[^"]+)", line (\d+)')
_WHILE_PARSING = re.compile(r"while parsing (/mnt/\S+?):(\d+)")


def _locate(path: str, mounts: dict[str, Source]) -> tuple[str | None, str | None, Source | None]:
    """/mnt/extra/0/bank_zm/views/x.xml -> (module, path in repo, source)."""
    for prefix in sorted(mounts, key=len, reverse=True):
        if path.startswith(prefix.rstrip("/") + "/"):
            rel = path[len(prefix.rstrip("/")) + 1:]
            src = mounts[prefix]
            module = rel.split("/", 1)[0]
            in_repo = rel.split("/", 1)[1] if src.root_module and module == src.root_module and "/" in rel else rel
            return module, in_repo, src
    parts = path.split("/")
    return (parts[3] if len(parts) > 3 else None), "/".join(parts[3:]) or None, None


def _github_url(src: Source | None, file: str | None, line: int | None) -> str | None:
    if not src or not src.repo_url or not file or "github.com" not in src.repo_url:
        return None
    base = src.repo_url.rstrip("/").removesuffix(".git")
    return f"{base}/blob/{src.ref or 'HEAD'}/{file}" + (f"#L{line}" if line else "")


def _finding(kind, title, message, hint, mounts, path=None, line=None, excerpt="", module=None):
    mod, file, src = _locate(path, mounts) if path else (None, None, None)
    return Finding(kind=kind, title=title, message=message.strip(), hint=hint, module=module or mod,
                   file=file, line=int(line) if line else None, source=src.label if src else None,
                   url=_github_url(src, file, int(line) if line else None), excerpt=excerpt.strip())


def _last_mnt_file(text: str) -> tuple[str | None, str | None]:
    hits = _MNT_FILE.findall(text)
    return hits[-1] if hits else (None, None)


def _excerpt(lines: list[str], i: int, before: int = 2, after: int = 6) -> str:
    keep = [l for l in lines[max(0, i - before): i + after] if not l.lstrip().startswith("File \"/usr")]
    return "\n".join(keep)


def diagnose(log: str, mounts: dict[str, Source] | None = None,
             signals: Signals | None = None) -> list[Finding]:
    """Most useful finding first. Empty when nothing recognisable was found."""
    mounts = mounts or {}
    sig = signals or Signals()
    lines = log.splitlines()
    found: list[Finding] = []

    def add(f: Finding):
        if not any(x.kind == f.kind and x.message == f.message for x in found):
            found.append(f)

    # Things only the worker knows
    if sig.oom_killed or sig.exit_code == 137:
        add(Finding("out_of_memory", "Ran out of memory",
                    f"Odoo was stopped by the memory limit{f' ({sig.memory_limit})' if sig.memory_limit else ''}.",
                    "Installing many modules (or demo data) needs more memory: raise the build memory "
                    "limit on the System page, or install fewer modules."))
    if sig.timed_out:
        add(Finding("timeout", "Took too long",
                    f"The install did not finish within {sig.timeout_minutes or '?'} minutes.",
                    "Raise the build time limit on the System page, or check the log for a step that hangs."))

    # XML syntax (lxml)
    for i, l in enumerate(lines):
        m = re.search(r"XMLSyntaxError: (.+)", l)
        if m:
            path, ln = _last_mnt_file("\n".join(lines[max(0, i - 15): i + 1]))
            add(_finding("xml_syntax", "XML syntax error", m.group(1),
                         "The file is not valid XML: every tag must be closed and properly nested, and "
                         "& < > in text must be escaped (&amp; &lt; &gt;).", mounts, path, ln,
                         _excerpt(lines, i, 1, 1)))

    # View validation errors (wrong field / attribute in a view)
    for i, l in enumerate(lines):
        if l.startswith("Error while validating view"):
            block = lines[i + 1: i + 40]
            end = next((k for k, b in enumerate(block) if b.startswith("View error context")), len(block))
            body = [b for b in block[:end] if b.strip()]
            arch = next((b for b in body if b.lstrip().startswith("<")), "")
            msg = "\n".join(b for b in body if b is not arch) or "Invalid view"
            ctx = "\n".join(block[end: end + 12])
            path = re.search(r"'file': '([^']+)'", ctx)
            xmlid = re.search(r"'xmlid': '([^']+)'", ctx)
            model = re.search(r"'view\.model': '([^']+)'", ctx)
            parse = _WHILE_PARSING.search("\n".join(lines[max(0, i - 3): i + 1]))
            fpath = parse.group(1) if parse else (path.group(1) if path else None)
            fline = parse.group(2) if parse else None
            fm = re.search(r'Field "(\w+)" does not exist in model "([\w.]+)"', msg)
            hint = (f"The view uses field {fm.group(1)}, which model {fm.group(2)} does not have: check "
                    "the spelling, or add the module that defines that field to 'depends'."
                    if fm else "Odoo rejected this view: check the field names and attributes it uses "
                               "against the model.")
            title = f"Invalid view {xmlid.group(1)}" if xmlid else "Invalid view"
            if model:
                title += f" ({model.group(1)})"
            add(_finding("view", title, msg, hint, mounts, fpath, fline, arch))

    # Data files (records): find the cause just before each ParseError
    for i, l in enumerate(lines):
        m = _WHILE_PARSING.search(l)
        if not m or "ParseError" not in l or any(f.kind == "view" for f in found):
            continue
        cause_text = "\n".join(lines[max(0, i - 40): i])
        ext = re.findall(r"External ID not found in the system: (\S+)", cause_text)
        bad = re.findall(r"Invalid field '(\w+)' (?:on model|in) '([\w.]+)'", cause_text)  # 18 | 19 wording
        snippet = "\n".join(lines[i + 1: i + 4])
        if ext:
            add(_finding("external_id", "Reference to a record that does not exist",
                         f"External ID not found: {ext[-1]}",
                         f"A ref= or eval ref() points at {ext[-1]}, which does not exist (yet): check the "
                         "module prefix and name, add its module to 'depends', or load the file that "
                         "defines it earlier in the manifest's 'data' list.", mounts, m.group(1), m.group(2), snippet))
        elif bad:
            add(_finding("xml_data", "Unknown field in a data record",
                         f"Invalid field '{bad[-1][0]}' on model '{bad[-1][1]}'",
                         f"The record sets field {bad[-1][0]}, which {bad[-1][1]} does not have: check the "
                         "spelling or the module that adds it.", mounts, m.group(1), m.group(2), snippet))
        else:
            cause = next((c for c in reversed(lines[max(0, i - 40): i])
                          if re.match(r"^[\w.]+(Error|Exception|Warning)\b.*:", c)), "")
            add(_finding("xml_data", "Error loading a data file", cause or l,
                         "Odoo could not load this record; the message above says why.", mounts,
                         m.group(1), m.group(2), snippet))

    # Access rights CSV
    for i, l in enumerate(lines):
        m = re.search(r"file (\S+\.csv) could not be processed:", l)
        if m:
            detail = "\n".join(x for x in lines[i + 1: i + 4] if x.strip() and "INFO" not in x)
            ext = re.search(r"external id '([^']+)' in field '(\w+)'", detail)
            hint = (f"A row refers to {ext.group(1)}, which does not exist: for model_id use "
                    "model_<model_name_with_underscores> of a model this module or its dependencies "
                    "define." if ext else "Check the CSV header and the ids it refers to.")
            module = m.group(1).split("/", 1)[0]
            add(Finding("access_csv", "Invalid access rights file", detail or l, hint, module=module,
                        file=m.group(1).split("/", 1)[1] if "/" in m.group(1) else m.group(1)))

    # A data file loads records of a model that is not in the registry (Odoo 20 wording:
    # a bare KeyError: 'model.name' raised from convert_csv_import / convert_xml_import)
    for i, l in enumerate(lines):
        m = re.match(r"^KeyError: '([a-z_]+(?:\.[a-z_0-9]+)+)'$", l)
        if not m or not any("convert_" in x for x in lines[max(0, i - 30): i]):
            continue
        loading = [x for x in lines[:i] if re.search(r"odoo\.modules\.loading: loading \S+\.(csv|xml)", x)]
        rel = re.search(r"loading (\S+\.(?:csv|xml))", loading[-1]).group(1) if loading else None
        module, file = (rel.split("/", 1) + [None])[:2] if rel else (None, None)
        add(Finding("access_csv" if file and file.endswith("ir.model.access.csv") else "xml_data",
                    "File loads records of an unknown model",
                    f"{rel or 'A data file'} loads records of model {m.group(1)}, which is not available.",
                    f"Add the module that defines {m.group(1)} to 'depends' in the manifest, or check "
                    "the model name.", module=module, file=file))

    # Python: syntax, imports, packages, model definitions
    for i, l in enumerate(lines):
        m = re.match(r"^(SyntaxError|IndentationError|TabError): (.+)", l)
        if m:
            path, ln = _last_mnt_file("\n".join(lines[max(0, i - 12): i + 1]))
            add(_finding("python_syntax", "Python syntax error", f"{m.group(1)}: {m.group(2)}",
                         "Fix the Python syntax at this line (missing ':' / bracket / indentation).",
                         mounts, path, ln, _excerpt(lines, i, 3, 1)))
        m = re.match(r"^(ModuleNotFoundError|ImportError): (.+)", l)
        if m:
            path, ln = _last_mnt_file("\n".join(lines[max(0, i - 30): i + 1]))
            pkg = re.search(r"No module named '([\w.]+)'", m.group(2))
            hint = (f"The Python package {pkg.group(1).split('.')[0]} is not installed: add it to "
                    "requirements.txt at the repo root (the previewer installs it) and list it under "
                    "'external_dependencies' in the manifest." if pkg else "Check the import.")
            add(_finding("python_import", "Python import failed", f"{m.group(1)}: {m.group(2)}", hint,
                         mounts, path, ln))
        m = re.search(r'Unable to install module "(\w+)" because an external dependency is not met: (.+)', l)
        if m:
            pkg = re.search(r"External dependency (\S+) not installed", m.group(2))
            name = pkg.group(1) if pkg else m.group(2).split()[0]
            add(Finding("python_package", "Missing Python package", m.group(2),
                        f"Add {name} to requirements.txt at the repo root; the previewer builds an image "
                        "with it.", module=m.group(1)))
        m = re.search(r'install module "(\w+)" that depends on module "(\w+)"', l)
        if m:
            add(Finding("missing_dependency", "Missing module dependency",
                        f"{m.group(1)} depends on {m.group(2)}, which is not available.",
                        f"Add the repo that provides {m.group(2)} as a dependency (or use Enterprise if it "
                        "is an Enterprise module), or remove it from 'depends'.", module=m.group(1)))
        m = re.match(r"^AssertionError: Field ([\w.]+) with unknown comodel_name '([\w.]+)'", l)
        if m:
            add(Finding("model_definition", "Field points to an unknown model", l.split(": ", 1)[1],
                        f"{m.group(1)} relates to {m.group(2)}, which does not exist: fix the model name, "
                        "or add the module that defines it to 'depends'.",
                        module=None))

    # Database
    for l in lines:
        m = re.search(r"(?:psycopg2\.errors\.\w+|^ERROR): (.+)", l)
        if m and ("out of shared memory" in l):
            add(Finding("database", "Database ran out of lock memory", m.group(1),
                        "Raise max_locks_per_transaction on the Postgres server, or run fewer builds at once."))
        elif m and re.search(r"violates (not-null|unique|foreign key|check) constraint", l):
            add(Finding("database", "Database constraint violated", m.group(1),
                        "A record broke a database rule (missing required value, duplicate, or a link to "
                        "a missing record); the error around it in the log says which record."))

    loopback = re.search(r"HTTP service running on (127\.0\.0\.1|localhost):(\d+)", log)
    if sig.health_failed and loopback:
        add(Finding("health", "Odoo only listens inside its container",
                    f"Odoo started but listens on {loopback.group(1)}:{loopback.group(2)}, so the "
                    "proxy cannot reach it.",
                    "Set http_interface = 0.0.0.0 in the Odoo configuration (Odoo 20 defaults to "
                    "127.0.0.1); the previewer's own config does this."))
    if sig.health_failed and not found:
        add(Finding("health", "Preview did not start",
                    "Odoo installed, but the preview did not answer /web/login in time.",
                    "Look at the end of the log for an error raised while the server started."))

    if not found:
        last_tb = max((k for k, l in enumerate(lines) if l.startswith("Traceback")), default=None)
        if last_tb is not None:
            exc = next((l for l in lines[last_tb + 1:] if re.match(r"^[\w.]+(Error|Exception)\b", l)), None)
            if exc:
                path, ln = _last_mnt_file("\n".join(lines[last_tb:]))
                add(_finding("other", "Odoo error", exc, "See the log around this error.", mounts, path, ln))

    order = ["out_of_memory", "timeout", "xml_syntax", "view", "external_id", "xml_data", "access_csv",
             "python_syntax", "python_import", "python_package", "missing_dependency",
             "model_definition", "database", "health", "other"]
    found.sort(key=lambda f: order.index(f.kind))
    return found[:5]


def headline(findings: list[Finding]) -> str:
    """One line for error_summary / board cards."""
    if not findings:
        return ""
    f = findings[0]
    where = f" in {f.where}" if f.where else (f" ({f.module})" if f.module else "")
    return f"{f.title}{where}: {f.message.splitlines()[0]}"
