"""How many previews fit on the Docker host (pure). Each preview may use up to its memory
limit and each running build up to the build limit; Postgres, Traefik, app and worker need
`reserved` on top. Docker's limits are caps, not reservations, so a host that allows more
than it has kills containers at random (Postgres included) when previews get busy.
`overcommit` (System page) lets an admin bet that previews stay below their caps: 2.0 counts
the host as twice its size."""
import re
from dataclasses import dataclass

_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
MEMORY_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([bkmgBKMG]?)$")
MIN_MEMORY = 256 * 1024**2


def parse_memory(value: str) -> int:
    """'1536m' / '2g' / '1.5g' / '1073741824' -> bytes. ValueError when malformed."""
    m = MEMORY_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"not a memory size: {value!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


def fmt_gb(n_bytes: int) -> str:
    return f"{n_bytes / 1024**3:.1f} GB"


@dataclass
class Fit:
    max_previews: int          # previews that fit next to the builds and the reserve (>= 1)
    needed: int                # bytes the current settings could use
    total: int                 # host memory, times the overcommit factor
    connections_needed: int
    max_connections: int | None

    @property
    def memory_ok(self) -> bool:
        return self.needed <= self.total

    @property
    def connections_ok(self) -> bool:
        return self.max_connections is None or self.connections_needed <= self.max_connections


def fit(*, total: int, previews: int, preview_mem: int, builds: int, build_mem: int,
        reserved: int, overcommit: float = 1.0, maxconn: int = 12,
        max_connections: int | None = None) -> Fit:
    total = int(total * max(1.0, overcommit))
    free_for_previews = total - reserved - builds * build_mem
    max_previews = max(1, free_for_previews // preview_mem) if preview_mem > 0 else previews
    if max_connections is not None:
        # every preview and build runs one Odoo with its own pool; keep 20 for the app/worker
        by_conn = max(1, (max_connections - 20) // max(1, maxconn) - builds)
        max_previews = min(max_previews, by_conn)
    return Fit(max_previews=int(max_previews),
               needed=reserved + previews * preview_mem + builds * build_mem,
               total=total, connections_needed=(previews + builds) * maxconn + 20,
               max_connections=max_connections)


def effective_limit(setting: int, total: int | None, *, preview_mem: str, builds: int,
                    build_mem: str, reserved: str, overcommit: float = 1.0, maxconn: int = 12,
                    max_connections: int | None = None) -> int:
    """The running-preview limit actually enforced: the setting, capped to what fits.
    Unknown host memory (no stats yet) leaves the setting as is."""
    if not total:
        return setting
    try:
        f = fit(total=total, previews=setting, preview_mem=parse_memory(preview_mem), builds=builds,
                build_mem=parse_memory(build_mem), reserved=parse_memory(reserved), overcommit=overcommit,
                maxconn=maxconn,
                max_connections=max_connections)
    except ValueError:
        return setting
    return max(1, min(setting, f.max_previews))
