"""Slug, host-label and database-name rules from SPEC.md "Routing design"."""
import hashlib
import re

MAX_LABEL = 63


def slugify(value: str) -> str:
    """Lowercase; anything outside a-z0-9 becomes '-'; collapse and trim hyphens."""
    s = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return s.strip("-")


def _short_hash(value: str, n: int = 6) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:n]


def _fit(value: str, limit: int, sep: str) -> str:
    """Truncate to `limit`, appending a 6-char hash of the full value when cut."""
    if len(value) <= limit:
        return value
    h = _short_hash(value)
    return value[: limit - len(h) - 1].rstrip(sep) + sep + h


def host_label(project_slug: str, branch_slug: str) -> str:
    return _fit(f"{project_slug}-{branch_slug}", MAX_LABEL, "-")


def preview_host(project_slug: str, branch_slug: str, domain: str, combo: bool = False) -> str:
    """<project>-<branch>.<domain>; a combined preview is just <slug>.<domain>."""
    label = _fit(project_slug, MAX_LABEL, "-") if combo else host_label(project_slug, branch_slug)
    return f"{label}.{domain}"


def db_name(project_slug: str, branch_slug: str, sha: str, build_id: int) -> str:
    """<project>_<branch>_<short-sha>_b<build-id>; the build id keeps rebuilds of the
    same commit from clashing with the database still serving the live preview."""
    raw = f"{project_slug}_{branch_slug}_{sha[:7]}"
    raw = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")
    suffix = f"_b{build_id}"
    return _fit(raw, MAX_LABEL - len(suffix), "_") + suffix


def container_name(build_id: int) -> str:
    return f"pv-{build_id}"
