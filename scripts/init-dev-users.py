#!/usr/bin/env python3
"""Crée ou remet à zéro les comptes réservés au développement local."""

import os
import sys

import bcrypt

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from app import repo


DEV_USERS = (
    ("user", "user", False, 100),
    ("admin", "admin", True, 100),
)


def main():
    with repo.locked_state():
        for username, password, is_admin, initial_balance in DEV_USERS:
            account = repo.find_user(username)
            if account is None:
                repo.add_user(
                    username,
                    password,
                    is_admin=is_admin,
                    initial_balance=initial_balance,
                )
                continue

            account.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
            account.is_admin = is_admin
            account.tuxcoin = initial_balance
            repo.update_user(account)

    print("Comptes de développement prêts : user/user et admin/admin")


if __name__ == "__main__":
    main()
