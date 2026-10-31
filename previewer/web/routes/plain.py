"""Plain Odoo previews ("Try Odoo"): a version, edition and demo-data choice with no custom
code. A plain preview is a Project(kind="plain") with no parts; it shares the combined-preview
machinery (one COMBO_BRANCH row, host <slug>.<domain>, the worker's `sync` computes its id and
queues the first build) and is managed on the /combos/<id> pages.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...models import COMBO_BRANCH, Branch, OdooVersion, Project, User
from ...naming import preview_host, slugify
from ...security import random_secret
from .. import accounts, selection
from ..deps import current_user, flash, render
from .projects import _parse_modules

router = APIRouter()

# (technical name, label, Enterprise only). Odoo installs each app's dependencies itself.
APPS = [
    ("crm", "CRM", False),
    ("sale_management", "Sales", False),
    ("account", "Invoicing", False),
    ("account_accountant", "Accounting", True),
    ("stock", "Inventory", False),
    ("purchase", "Purchase", False),
    ("mrp", "Manufacturing", False),
    ("project", "Project", False),
    ("hr", "Employees", False),
    ("hr_holidays", "Time Off", False),
    ("hr_expense", "Expenses", False),
    ("website", "Website", False),
    ("point_of_sale", "Point of Sale", False),
    ("helpdesk", "Helpdesk", True),
    ("knowledge", "Knowledge", True),
    ("industry_fsm", "Field Service", True),
    ("planning", "Planning", True),
    ("documents", "Documents", True),
    ("sign", "Sign", True),
    ("hr_payroll", "Payroll", True),
]
APP_NAMES = {t for t, _, _ in APPS}
APP_LABELS = {t: label for t, label, _ in APPS}
EE_APPS = {t for t, _, ee in APPS if ee}


def enabled_versions(s: Session) -> list[str]:
    return list(s.scalars(select(OdooVersion.version).where(OdooVersion.enabled.is_(True))
                          .order_by(OdooVersion.version)))


def enterprise_statuses(s: Session, versions: list[str]) -> dict[str, dict]:
    """Per version: is its Enterprise source readable (cached ~5 min in accounts)."""
    return {v: accounts.enterprise_status(s, v) for v in versions}


def split_modules(modules: str) -> tuple[list[str], str]:
    """A stored modules list -> (apps from the grid, the rest as free text)."""
    mods = [m.strip() for m in (modules or "").split(",") if m.strip()]
    return [m for m in mods if m in APP_NAMES], ", ".join(m for m in mods if m not in APP_NAMES)


def modules_from_form(form, edition: str, errors: dict) -> str:
    apps = [a for a in form.getlist("apps") if a in APP_NAMES]
    if edition != "enterprise":
        ee = [APP_LABELS[a] for a in apps if a in EE_APPS]
        if ee:
            errors["apps"] = f"{', '.join(ee)} {'is an' if len(ee) == 1 else 'are'} Enterprise app" \
                             f"{'' if len(ee) == 1 else 's'}: choose Enterprise or untick {'it' if len(ee) == 1 else 'them'}"
    other = _parse_modules(form.get("other_modules") or "", errors, "other_modules")
    mods = list(dict.fromkeys(apps + [m for m in other.split(",") if m]))
    return ",".join(mods)


def describe(p: Project) -> str:
    """e.g. 'Odoo 19.0 · Enterprise · demo · CRM, Sales' for rows and cards."""
    mods = p.module_set.modules if p.module_set else p.modules_to_install
    names = [APP_LABELS.get(m, m) for m in (mods or "").split(",") if m]
    bits = [f"Odoo {p.default_odoo_version}", p.edition.capitalize(),
            "demo" if p.install_demo_data else "no demo", ", ".join(names) or "base only"]
    return " · ".join(bits)


def auto_name(version: str, edition: str, demo: bool) -> str:
    major = version.split(".")[0]
    return f"Odoo {major} {'EE' if edition == 'enterprise' else 'CE'}{' + demo' if demo else ''}"


def _free_slug(s: Session, base: str) -> str:
    domain = get_settings().domain
    slug, n = base, 2
    while (s.scalars(select(Project).where(Project.slug == slug)).first()
           or s.scalars(select(Branch).where(
               Branch.host == preview_host(slug, COMBO_BRANCH, domain, combo=True))).first()):
        slug, n = f"{base}-{n}"[:40], n + 1
    return slug


def validate(s: Session, form) -> tuple[dict, dict]:
    errors: dict = {}
    versions = enabled_versions(s)
    version = (form.get("default_odoo_version") or "").strip()
    if version not in versions:
        errors["default_odoo_version"] = "Choose an enabled Odoo version"
    edition = (form.get("edition") or "community").strip()
    if edition not in ("community", "enterprise"):
        errors["edition"] = "Choose Community or Enterprise"
    elif edition == "enterprise" and version in versions:
        st = accounts.enterprise_status(s, version)
        if st["ok"] is not True:
            errors["edition"] = f"Enterprise is not available for Odoo {version}: {st['message']}"
    demo = form.get("install_demo_data") == "with"
    modules = modules_from_form(form, edition, errors)
    name = (form.get("name") or "").strip() or auto_name(version or "?", edition, demo)
    typed_slug = (form.get("slug") or "").strip()
    slug = slugify(typed_slug or name)[:40].strip("-")
    if not slug:
        errors["slug"] = "Required"
    elif typed_slug:
        host = preview_host(slug, COMBO_BRANCH, get_settings().domain, combo=True)
        if s.scalars(select(Project).where(Project.slug == slug)).first():
            errors["slug"] = "Another project or preview uses this slug"
        elif s.scalars(select(Branch).where(Branch.host == host)).first():
            errors["slug"] = f"The address {host} is already used"
    else:
        slug = _free_slug(s, slug)
    data = {"name": name, "slug": slug, "default_odoo_version": version, "edition": edition,
            "install_demo_data": demo, "modules_to_install": modules}
    return data, errors


def form_ctx(s: Session, values: dict, errors: dict) -> dict:
    versions = enabled_versions(s)
    apps, other = split_modules(values.get("modules_to_install", ""))
    return {"values": values, "errors": errors, "versions": versions,
            "enterprise": enterprise_statuses(s, versions), "apps": APPS,
            "chosen_apps": values.get("apps", apps), "other_modules": values.get("other_modules", other)}


@router.get("/try")
def try_odoo(request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    versions = enabled_versions(s)
    values = {"default_odoo_version": versions[-1] if versions else "", "edition": "community",
              "install_demo_data": False, "modules_to_install": "", "name": "", "slug": ""}
    return render(request, "try_odoo.html", form_ctx(s, values, {}))


@router.post("/try")
async def create_plain(request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    data, errors = validate(s, form)
    if errors:
        values = {**data, "name": form.get("name") or "", "slug": form.get("slug") or "",
                  "apps": form.getlist("apps"), "other_modules": form.get("other_modules") or ""}
        return render(request, "try_odoo.html", form_ctx(s, values, errors), status_code=400)
    p = Project(kind="plain", repo_url="", webhook_secret=random_secret(32), **data)
    s.add(p)
    s.flush()
    s.add(Branch(project_id=p.id, name=COMBO_BRANCH, slug=COMBO_BRANCH, enabled=True,
                 dependency_branches={},
                 host=preview_host(p.slug, COMBO_BRANCH, get_settings().domain, combo=True)))
    s.flush()
    # No commit id yet: the worker's sync gives it one and queues the first build.
    selection.refresh_combo(s, p.id)
    s.commit()
    flash(request, f"{p.name} is being built: {describe(p)}. It opens at "
                   f"{preview_host(p.slug, COMBO_BRANCH, get_settings().domain, combo=True)} when ready.",
          "success")
    return RedirectResponse(f"/combos/{p.id}", status_code=303)
