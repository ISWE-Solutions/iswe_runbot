"""Docker operations for preview and build containers. Only the worker imports this."""
import hashlib
import io
import json
import logging
import tarfile
import threading
import time
from functools import lru_cache
from pathlib import Path

import docker
import httpx
from docker.errors import ImageNotFound, NotFound
from docker.types import Mount

from ..config import get_settings
from ..naming import container_name
from .logs import BuildLog

ODOO_PORT = 8069
ODOO_UID = 101  # `odoo` user in the official images
# On images this system builds (previewer-odoo:*), so pruning never touches other stacks' images
IMAGE_LABEL = "previewer.image"
SRC, EXTRA, ENTERPRISE, CONF, DATA = "/mnt/src", "/mnt/extra", "/mnt/enterprise", "/etc/odoo/odoo.conf", "/var/lib/odoo"
_SECOND = 1_000_000_000
# Docker's own probe; filtered out of the runtime log so it does not drown real requests.
HEALTH_PATH = "/web/health"


log_ = logging.getLogger(__name__)

# Lists the core addons shipped in an Odoo image: odoo/addons inside the package, a sibling
# addons/ folder (source layouts), and odoo.addons.__path__ (Odoo 19 has no odoo.__file__).
_CORE_SCRIPT = (
    "import json, os, odoo\n"
    "dirs = []\n"
    "for r in getattr(odoo, '__path__', []):\n"
    "    dirs += [os.path.join(r, 'addons'), os.path.join(os.path.dirname(r), 'addons')]\n"
    "try:\n"
    "    import odoo.addons\n"
    "    dirs += list(odoo.addons.__path__)\n"
    "except Exception:\n"
    "    pass\n"
    "mods = sorted({n for d in dirs if os.path.isdir(d) for n in os.listdir(d)\n"
    "               if os.path.isfile(os.path.join(d, n, '__manifest__.py'))})\n"
    "print(json.dumps(mods))\n"
)


@lru_cache
def client() -> docker.DockerClient:
    return docker.DockerClient(base_url=get_settings().docker_host, version="auto", timeout=120)


def docker_ok() -> bool:
    try:
        return bool(client().ping())
    except Exception:
        return False


def addons_path(n_extra: int, enterprise: bool, with_src: bool = True) -> str:
    """Fixed in-container paths; Odoo adds its core addons itself. A combined preview has
    no repo of its own, so no /mnt/src (Odoo rejects an addons dir without modules)."""
    paths = [ENTERPRISE] if enterprise else []
    paths += [f"{EXTRA}/{i}" for i in range(n_extra)]
    if with_src:
        paths.append(SRC)
    return ",".join(paths)


def render_conf(*, db_name: str, admin_passwd: str, addons: str) -> str:
    c = get_settings()
    opts = {
        "admin_passwd": admin_passwd,
        "db_host": c.postgres_host,
        "db_port": c.postgres_port,
        "db_user": c.odoo_db_user,
        "db_password": c.odoo_db_password,
        "db_name": db_name,
        "dbfilter": f"^{db_name}$",
        "list_db": "False",
        "addons_path": addons,
        "data_dir": DATA,
        "proxy_mode": "True",
        "workers": 0,
        # Odoo 20 listens on 127.0.0.1 by default; previews must be reachable from Traefik/worker
        "http_interface": "0.0.0.0",
    }
    if not addons:
        del opts["addons_path"]  # plain Odoo: only its own core (and Enterprise if set)
    return "[options]\n" + "".join(f"{k} = {v}\n" for k, v in opts.items())


def preview_labels(build_id: int, branch_id: int, host: str) -> dict[str, str]:
    c = get_settings()
    r = f"pv-{build_id}"
    labels = {
        "traefik.enable": "true",
        "traefik.docker.network": c.docker_network,
        f"traefik.http.routers.{r}.rule": f"Host(`{host}`)",
        f"traefik.http.routers.{r}.entrypoints": "websecure" if c.tls_enabled else "web",
        # Newer builds win while old and new briefly share the host during a swap.
        f"traefik.http.routers.{r}.priority": str(10000 + build_id),
        f"traefik.http.routers.{r}.service": r,
        f"traefik.http.services.{r}.loadbalancer.server.port": str(ODOO_PORT),
        # Every request asks the dashboard first (login / public-demo switch, decided per host
        # at request time); the middleware is defined on the app service in docker-compose.yml.
        f"traefik.http.routers.{r}.middlewares": "previewer-auth@docker",
        "previewer.build_id": str(build_id),
        "previewer.branch_id": str(branch_id),
    }
    if c.tls_enabled:
        labels[f"traefik.http.routers.{r}.tls"] = "true"
        labels[f"traefik.http.routers.{r}.tls.certresolver"] = "le"
    return labels


def mounts(build_dir: Path, filestore: Path, n_extra: int, enterprise_dir: Path | None,
           with_src: bool = True) -> list[Mount]:
    m = [Mount(SRC, str(build_dir / "src"), type="bind", read_only=True)] if with_src else []
    m += [
        Mount(CONF, str(build_dir / "odoo.conf"), type="bind", read_only=True),
        Mount(DATA, str(filestore), type="bind"),
    ]
    m += [Mount(f"{EXTRA}/{i}", str(build_dir / "extra" / str(i)), type="bind", read_only=True)
          for i in range(n_extra)]
    if enterprise_dir:
        m.append(Mount(ENTERPRISE, str(enterprise_dir), type="bind", read_only=True))
    return m


def template_mounts(conf: Path, data: Path, enterprise_dir: Path | None) -> list[Mount]:
    """Mounts for making a database template: config, its own data dir, Enterprise if any."""
    m = [Mount(CONF, str(conf), type="bind", read_only=True), Mount(DATA, str(data), type="bind")]
    if enterprise_dir:
        m.append(Mount(ENTERPRISE, str(enterprise_dir), type="bind", read_only=True))
    return m


def image_id(image: str) -> str:
    """Content id of a local image (changes when the tag is re-pulled), e.g. 'sha256:ab12…'[:19]."""
    return client().images.get(image).id[:19]


def ensure_image(base_image: str, version: str, requirements: Path | None, log: BuildLog) -> str:
    """odoo:<version> as is, or a cached image with the repo's requirements.txt installed."""
    dc = client()
    try:
        dc.images.get(base_image)
    except ImageNotFound:
        log.step(f"Pulling {base_image}")
        dc.images.pull(base_image)
    if requirements is None:
        return base_image
    content = requirements.read_bytes()
    tag = f"previewer-odoo:{version}-{hashlib.sha256(base_image.encode() + content).hexdigest()[:12]}"
    try:
        dc.images.get(tag)
        log.step(f"Reusing image {tag}")
        return tag
    except ImageNotFound:
        pass
    log.step(f"Building image {tag} with requirements.txt")
    dockerfile = (
        f"FROM {base_image}\nUSER root\nCOPY requirements.txt /tmp/requirements.txt\n"
        "RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt"
        " || pip3 install --no-cache-dir -r /tmp/requirements.txt\nUSER odoo\n"
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in (("Dockerfile", dockerfile), ("requirements.txt", content)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    for chunk in dc.api.build(fileobj=buf, custom_context=True, tag=tag, rm=True, decode=True,
                              labels={IMAGE_LABEL: "1"}):
        if "stream" in chunk:
            log.write(chunk["stream"])
        if "error" in chunk:
            raise RuntimeError(f"Image build failed: {chunk['error']}")
    return tag


def core_modules(image: str) -> set[str] | None:
    """Odoo core module names in the image, cached per image id under DATA_DIR/cache.
    None when they cannot be listed (the install plan then treats unknown names as core)."""
    try:
        image_id = client().images.get(image).id.split(":")[-1][:16]
    except Exception:  # noqa: BLE001 — Docker unavailable or image missing
        return None
    cache = get_settings().data_dir / "cache" / f"core-{image_id}.json"
    if cache.exists():
        try:
            return set(json.loads(cache.read_text()))
        except ValueError:
            pass
    try:
        out = client().containers.run(image, entrypoint=["python3", "-c", _CORE_SCRIPT],
                                      remove=True, network_disabled=True, mem_limit="512m")
        mods = json.loads(out.decode("utf-8", errors="replace").strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        log_.exception("could not list core modules of %s", image)
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(mods))
    return set(mods)


def run_init(*, image: str, build_id: int, args: list[str], mounts_: list[Mount], mem_limit: str,
             timeout_s: int, log: BuildLog, outcome: dict | None = None, name: str | None = None) -> int:
    """One-off `odoo ... --stop-after-init`; streams output to the log. Returns exit code.
    `outcome`, if given, receives {"exit_code", "oom", "timed_out"} for failure diagnosis."""
    c = get_settings()
    dc = client()
    name = name or f"pv-{build_id}-init"
    remove_container(name)
    ctr = dc.containers.create(image, command=["odoo", *args], name=name, mounts=mounts_,
                               network=c.docker_network, mem_limit=mem_limit,
                               labels={"previewer.build_id": str(build_id), "previewer.role": "init"})
    try:
        ctr.start()
        t = threading.Thread(target=log.stream, args=(ctr.logs(stream=True, follow=True),), daemon=True)
        t.start()
        timed_out = False
        try:
            rc = ctr.wait(timeout=timeout_s)["StatusCode"]
        except Exception:
            ctr.kill()
            log.step(f"Build time limit reached ({timeout_s // 60} min), container killed")
            rc, timed_out = -1, True
        t.join(timeout=10)
        if outcome is not None:
            state = container_state(ctr)
            outcome.update(exit_code=rc, timed_out=timed_out,
                           oom=bool(state.get("oom")) or (rc == 137 and not timed_out))
        return rc
    finally:
        remove_container(name)


def run_preview(*, image: str, build_id: int, branch_id: int, host: str, db_name: str,
                mounts_: list[Mount], mem_limit: str, cpus: float):
    c = get_settings()
    name = container_name(build_id)
    remove_container(name)
    health = (
        "import urllib.request; "
        f"urllib.request.urlopen('http://localhost:{ODOO_PORT}{HEALTH_PATH}', timeout=5)"
    )
    return client().containers.run(
        image,
        command=["odoo", "--database", db_name, "--db-filter", f"^{db_name}$", "--no-database-list",
                 "--workers", "0", "--proxy-mode"],
        name=name,
        detach=True,
        mounts=mounts_,
        network=c.docker_network,
        mem_limit=mem_limit,
        nano_cpus=int(cpus * _SECOND),
        restart_policy={"Name": "unless-stopped"},
        labels=preview_labels(build_id, branch_id, host),
        healthcheck={"test": ["CMD", "python3", "-c", health], "interval": 5 * _SECOND,
                     "timeout": 6 * _SECOND, "retries": 3, "start_period": 120 * _SECOND},
    )


def container_state(ctr_or_name) -> dict:
    """{"oom": bool, "exit_code": int | None, "status": str} of a container, {} if gone."""
    try:
        ctr = client().containers.get(ctr_or_name) if isinstance(ctr_or_name, str) else ctr_or_name
        ctr.reload()
    except (NotFound, docker.errors.APIError):
        return {}
    st = ctr.attrs.get("State", {})
    return {"oom": bool(st.get("OOMKilled")), "exit_code": st.get("ExitCode"), "status": ctr.status}


def wait_healthy(name: str, timeout_s: int) -> str | None:
    """Poll /web/login until 200 and Docker reports healthy (Traefik routes only healthy
    containers). Returns None on success, else the reason."""
    deadline = time.monotonic() + timeout_s
    http_ok = False
    reason = "timed out"
    while time.monotonic() < deadline:
        try:
            ctr = client().containers.get(name)
        except NotFound:
            return "container disappeared"
        if ctr.status in ("exited", "dead"):
            return f"container exited (code {ctr.attrs['State'].get('ExitCode')})"
        if not http_ok:
            try:
                r = httpx.get(f"http://{name}:{ODOO_PORT}/web/login", timeout=5)
                http_ok = r.status_code == 200
                reason = f"/web/login returned {r.status_code}"
            except httpx.HTTPError as e:
                reason = f"/web/login unreachable ({type(e).__name__})"
        if http_ok and ctr.attrs["State"].get("Health", {}).get("Status") == "healthy":
            return None
        time.sleep(2)
    return f"health check {reason} after {timeout_s}s"


def get_container(name_or_id: str):
    try:
        return client().containers.get(name_or_id)
    except NotFound:
        return None


def stop_container(name_or_id: str) -> None:
    ctr = get_container(name_or_id)
    if ctr and ctr.status == "running":
        ctr.stop(timeout=20)


def remove_container(name_or_id: str) -> None:
    ctr = get_container(name_or_id)
    if ctr:
        ctr.remove(force=True)


def pull(image: str) -> None:
    client().images.pull(image)


def follow_logs(name: str, log: BuildLog, from_start: bool = False) -> None:
    """Append runtime output until the container stops (run in a daemon thread)."""
    ctr = get_container(name)
    if ctr is None:
        return
    kwargs = {} if from_start else {"since": int(time.time())}
    probe = f"GET {HEALTH_PATH} ".encode()
    try:
        log.stream(line for line in _lines(ctr.logs(stream=True, follow=True, **kwargs))
                   if probe not in line)
    except Exception:
        pass


def _lines(chunks):
    """Re-split a docker log stream on newlines (chunks may hold partial or several lines)."""
    buf = b""
    for chunk in chunks:
        buf += chunk
        *complete, buf = buf.split(b"\n")
        for line in complete:
            yield line + b"\n"
    if buf:
        yield buf


def preview_containers() -> list:
    """Every preview/init container this system created (any state)."""
    return client().containers.list(all=True, filters={"label": "previewer.build_id"})


def mounted_sources(containers) -> set[str]:
    """Host paths bind-mounted into the given containers."""
    return {m.get("Source", "") for c in containers for m in c.attrs.get("Mounts", [])}
