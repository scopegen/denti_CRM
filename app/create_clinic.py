"""Creates a real clinic and its owner login, until clinics can sign up by themselves.

    python -m app.create_clinic --slug ranco-dental --name "Ranco Dental Clinic" --prefix RANCO \\
        --owner-name "Dr Ravi Kumar" --owner-email owner@rancodental.in

Asks for the owner's password (it is never shown or stored in plain text). The clinic starts on
the Starter pack of the chosen plan, with the starter lists and services, which the owner can
change. Runs as the migration role (MIGRATION_DATABASE_URL).
"""
import argparse
import getpass
import re
import sys

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.clinic_setup import set_up_clinic
from app.config import settings
from app.models import RESERVED_CLINIC_SLUGS, Clinic
from app.security import hash_password

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
PREFIX = re.compile(r"^[A-Z0-9]{2,12}$")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a clinic and its owner login.")
    parser.add_argument("--slug", required=True, help="The clinic's name in the web address, for example ranco-dental")
    parser.add_argument("--name", required=True, help='The clinic\'s full name, for example "Ranco Dental Clinic"')
    parser.add_argument("--prefix", help="Patient ID prefix, for example RANCO gives RANCO-0001. Made from the address if left out")
    parser.add_argument("--plan", choices=["basic", "standard", "pro"], default="standard")
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--owner-not-doctor", action="store_true", help="The owner does not treat patients")
    return parser.parse_args(argv)


def _problems(args: argparse.Namespace) -> list[str]:
    problems = []
    if not SLUG.match(args.slug) or len(args.slug) > 40:
        problems.append("The address name may use small letters, numbers and single hyphens, up to 40 characters.")
    if args.slug in RESERVED_CLINIC_SLUGS:
        problems.append(f"'{args.slug}' is used by Denti itself. Choose another address name.")
    if args.prefix and not PREFIX.match(args.prefix):
        problems.append("The patient ID prefix may use 2 to 12 capital letters or numbers.")
    if not EMAIL.match(args.owner_email):
        problems.append("The owner's email does not look right.")
    if not args.name.strip() or not args.owner_name.strip():
        problems.append("The clinic's name and the owner's name are needed.")
    return problems


def _ask_password() -> str:
    while True:
        first = getpass.getpass("Owner's password (at least 8 characters): ")
        if len(first) < 8:
            print("Too short. Please use at least 8 characters.")
            continue
        if len(first.encode("utf-8")) > 72:
            print("Too long. Please use at most 72 characters.")
            continue
        if getpass.getpass("The same password again: ") != first:
            print("The two passwords are not the same. Please try again.")
            continue
        return first


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    problems = _problems(args)
    if problems:
        print("\n".join(problems))
        return 1

    engine = create_engine(settings.migration_database_url)
    with engine.begin() as connection:
        db = Session(bind=connection)
        if db.scalar(select(func.count()).select_from(Clinic).where(Clinic.slug == args.slug)):
            print(f"A clinic already uses the address '{args.slug}'. Nothing changed.")
            return 1
        password_hash = hash_password(_ask_password())
        clinic = Clinic(
            slug=args.slug, name=args.name.strip(), plan=args.plan, timezone=args.timezone, patient_id_prefix=args.prefix
        )
        owner = {
            "name": args.owner_name.strip(), "email": args.owner_email.strip().lower(), "role": "owner",
            "treats_patients": not args.owner_not_doctor,
        }
        set_up_clinic(connection, db, clinic, [owner], password_hash)

    print(f"Created {args.name.strip()} on the {args.plan.title()} plan.")
    print(f"Sign in at {settings.app_base_url.rstrip('/')}/{args.slug} as {owner['email']}.")
    print("The owner adds everyone else in Settings, Staff.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
