import os, json, bcrypt, logging, requests
from dataclasses import dataclass, asdict
from typing import List, Optional
from cryptography.fernet import Fernet
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from threading import Thread
# ------------------- Config -------------------
APP_DIR = os.path.abspath(os.path.dirname(__file__))
KEY_FILE = os.path.join(APP_DIR, "key.key")
USERS_FILE = os.path.join(APP_DIR, "users.enc")
PRODUCTS_FILE = os.path.join(APP_DIR, "products.json")
CURRENCY = "TuxCoin"



class Repo:
    def __init__(self):
        self.key = self.generate_key()
        self.users: List[User] = self.load_users()
        self.products: List[Product] = self.load_products()
    def obligatory_check(self, method=""):
        pass
        #self.users = self.load_users()
        #self.products = self.load_products()
        #print("method =", method, self.users)
    def save_users_2(self, users: List[User]):
        self.obligatory_check("save_users_2")
        fernet = Fernet(self.key)
        raw = [
            {
                "username": u.username,
                "password_hash_hex": u.password_hash.hex(),
                "tuxcoin": u.tuxcoin,
                "is_admin": u.is_admin,
            }
            for u in users
        ]
        token = fernet.encrypt(json.dumps(raw).encode())
        with open(USERS_FILE, "wb") as f:
            f.write(token)
    def update_products(self):
        self.obligatory_check("update_products")
        raw = [asdict(p) for p in self.products]
        with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    def delete_product(self, product: Product):
        self.obligatory_check("delete_product")
        self.products.remove(product)
        self.update_products()
    def generate_key(self) -> bytes:
        if not os.path.exists(KEY_FILE):
            key = Fernet.generate_key()
            with open(KEY_FILE, "wb") as f:
                f.write(key)
        else:
            with open(KEY_FILE, "rb") as f:
                key = f.read()
        return key
    def add_user(self, username: str, password: str, is_admin: bool=False, initial_balance: float=0) -> bool:
        self.obligatory_check("add_user")
        if self.find_user(username):
            return False
        pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        self.users.append(
            User(
                username=username,
                password_hash=pwd_hash,
                tuxcoin=round_tuxcoin(initial_balance),
                is_admin=is_admin,
            )
        )
        self.save_users()
        return True
    def update_user(self, user: User):
        self.obligatory_check("update_user")
        self.save_users()
    def delete_user(self, user: User):
        self.obligatory_check("delete_user")
        self.users.remove(user)
        self.save_users()
    def load_users(self) -> List[User]:
        if not os.path.exists(USERS_FILE):
            admin = User("admin", bcrypt.hashpw("admin".encode(), bcrypt.gensalt()), 100, True)
            self.save_users([admin])
            return [admin]
        with open(USERS_FILE, "rb") as f:
            token = f.read()
        fernet = Fernet(self.key)
        data = fernet.decrypt(token).decode()
        raw = json.loads(data)
        return [
            User(
                u["username"],
                bytes.fromhex(u["password_hash_hex"]),
                round_tuxcoin(float(u["tuxcoin"])),
                bool(u["is_admin"]),
            )
            for u in raw
        ]
    def save_users(self, users: Optional[List[User]] = None):
        self.obligatory_check("save_users")
        if users is None:
            users = self.users
        fernet = Fernet(self.key)
        raw = [
            {
                "username": u.username,
                "password_hash_hex": u.password_hash.hex(),
                "tuxcoin": u.tuxcoin,
                "is_admin": u.is_admin,
            }
            for u in users
        ]
        token = fernet.encrypt(json.dumps(raw, ensure_ascii=False, indent=2).encode())
        with open(USERS_FILE, "wb") as f:
            f.write(token)
    def load_products(self) -> List[Product]:
        if not os.path.exists(PRODUCTS_FILE):
            sample = [
                {"name": "Barre chocolatée", "price": 3, "quantity": 20, "image_url": ""},
                {"name": "Chips", "price": 4, "quantity": 15, "image_url": ""},
                {"name": "Soda 33cl", "price": 5, "quantity": 24, "image_url": ""},
            ]
            with open(PRODUCTS_FILE, "w") as f:
                json.dump(sample, f, indent=2)
        with open(PRODUCTS_FILE, "r") as f:
            raw = json.load(f)
        return [Product(**p) for p in raw]
    def save_products(self):
        self.obligatory_check("save_products")
        raw = [asdict(p) for p in self.products]
        with open(PRODUCTS_FILE, "w") as f:
            json.dump(raw, f, indent=2)
    def find_user(self, username: str) -> Optional[User]:
        self.obligatory_check("find_user")
        for u in self.users:
            if u.username.lower() == username.lower():
                return u
        return None


repo = Repo()

users = repo.load_users()

print(users)
