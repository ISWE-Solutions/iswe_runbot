"""Admin commands: python -m previewer.cli <command> ..."""
import argparse
import getpass
import sys

from sqlalchemy import select

from .db import session_scope
from .config import get_settings
from .models import GithubCredential, OdooVersion, User
from .security import encrypt, hash_password

# Seeded once; the Odoo versions admin page manages them afterwards.
# Demo flags verified against `odoo --help` in each image.
DEFAULT_VERSIONS = [
    # version, image, demo on, demo off, enabled
    ("18.0", "odoo:18", "", "--without-demo=all", True),
    ("19.0", "odoo:19", "--with-demo", "", True),
    ("20.0", "odoo:20", "--with-demo", "", True),
]


def seed_versions() -> None:
    with session_scope() as s:
        for version, image, on, off, enabled in DEFAULT_VERSIONS:
            if s.get(OdooVersion, version) is None:
                s.add(OdooVersion(version=version, docker_image=image, demo_flag_on=on,
                                  demo_flag_off=off, enabled=enabled))


def seed_default_credential() -> None:
    """First start: turn GITHUB_TOKEN / GITHUB_USERNAME from .env into the default account."""
    c = get_settings()
    if not c.github_token:
        return
    with session_scope() as s:
        if s.scalars(select(GithubCredential)).first() is None:
            s.add(GithubCredential(name="Default", username=c.github_username,
                                   token_enc=encrypt(c.github_token), is_default=True))


def create_user(email: str, name: str, password: str) -> None:
    with session_scope() as s:
        user = s.scalars(select(User).where(User.email == email.lower())).first()
        if user:
            user.password_hash = hash_password(password)
            user.active = True
            print(f"Updated {email}")
        else:
            s.add(User(email=email.lower(), name=name, password_hash=hash_password(password)))
            print(f"Created {email}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="previewer.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="Insert default Odoo versions if missing")
    u = sub.add_parser("create-user", help="Create or reset a dashboard user")
    u.add_argument("--email", required=True)
    u.add_argument("--name", default="")
    u.add_argument("--password", help="Prompted when omitted")
    args = p.parse_args(argv)

    if args.cmd == "seed":
        seed_versions()
        seed_default_credential()
    elif args.cmd == "create-user":
        password = args.password or getpass.getpass("Password: ")
        if len(password) < 8:
            print("Password must be at least 8 characters", file=sys.stderr)
            return 1
        create_user(args.email, args.name, password)
    return 0


if __name__ == "__main__":
    sys.exit(main())
