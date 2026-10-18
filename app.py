"""Application web MONIX.

Ce fichier contient volontairement toute la logique de l'application :
les utilisateurs, les produits, les achats, le frigo et les pages Flask.
Les données restent dans des fichiers à côté de ce fichier Python.
"""

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort
import os, json, bcrypt, logging, requests, time, tempfile, math, fcntl, base64, uuid, secrets, hmac
from dataclasses import dataclass, asdict
from typing import List, Optional
from cryptography.fernet import Fernet
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from threading import Thread, RLock
from contextlib import contextmanager
from collections import Counter, defaultdict
from werkzeug.utils import secure_filename
from urllib.parse import urlparse

# ------------------- CONFIGURATION ET FICHIERS -------------------
# APP_DIR est le dossier où se trouve ce fichier. Tous les chemins ci-dessous
# sont construits à partir de lui, pour fonctionner quel que soit le terminal.
APP_DIR = os.path.abspath(os.path.dirname(__file__))
# MONIX_DATA_DIR sert aux tests : en fonctionnement normal il n'est pas défini
# et les fichiers restent dans le dossier de l'application (APP_DIR).
DATA_DIR = os.environ.get("MONIX_DATA_DIR", APP_DIR)
KEY_FILE = os.path.join(DATA_DIR, "key.key")
USERS_FILE = os.path.join(DATA_DIR, "users.enc")
PRODUCTS_FILE = os.path.join(DATA_DIR, "products.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "static", "uploads")
LOCK_FILE = os.path.join(DATA_DIR, ".monix-data.lock")
PENDING_TRANSACTION_FILE = os.path.join(DATA_DIR, ".monix-pending-transaction.json")
SESSION_KEY_FILE = os.path.join(DATA_DIR, ".flask-secret")
CURRENCY = "TuxCoin"

def load_session_key():
    """Charge FLASK_SECRET ou crée une clé locale privée et persistante."""
    configured = os.environ.get("FLASK_SECRET")
    if configured:
        return configured
    try:
        with open(SESSION_KEY_FILE, "r", encoding="ascii") as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass

    generated = secrets.token_hex(32)
    try:
        descriptor = os.open(SESSION_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as f:
            f.write(generated)
        return generated
    except FileExistsError:
        # Un autre processus a créé la clé entre la lecture et O_EXCL.
        with open(SESSION_KEY_FILE, "r", encoding="ascii") as f:
            return f.read().strip()

SECRET_KEY = load_session_key()
global OPEN_UNTIL
OPEN_UNTIL = time.time()
FRIDGE_LOCK = RLock()
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ------------------- JOURNAUX (LOGS) -------------------
# app.log est lisible par un humain. events.jsonl est lisible par le tableau
# de bord : une ligne JSON correspond à un achat ou à une ouverture du frigo.
LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
EVENTS_FILE = os.path.join(LOG_DIR, "events.jsonl")
EVENTS_LOCK_FILE = os.path.join(LOG_DIR, ".events.lock")

LOG_FILE = os.path.join(LOG_DIR, "app.log")

file_handler = TimedRotatingFileHandler(
    LOG_FILE, when="midnight", interval=1, backupCount=30, encoding="utf-8"
)
file_handler.suffix = "%Y%m%d_%H%M%S.log"

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

logger = logging.getLogger("app")
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

def log_action(user: str, action: str, details: str = ""):
    """Ajoute une ligne simple dans app.log pour faciliter le dépannage."""
    logger.info(f"user={user} | action={action} | {details}")


def record_event(event_type: str, user: str, **details):
    """Historique structuré, indépendant des formats produits/utilisateurs."""
    event = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "type": event_type,
        "user": user,
        **details,
    }
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    # O_APPEND + flock évitent le mélange de deux lignes avec plusieurs workers.
    with open(EVENTS_LOCK_FILE, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_events():
    """Lit l'historique JSONL et renvoie les événements du plus récent au plus ancien."""
    if not os.path.exists(EVENTS_FILE):
        return []
    events = []
    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
                if isinstance(event, dict) and "timestamp" in event and "type" in event:
                    events.append(event)
            except json.JSONDecodeError:
                logger.warning("Ligne d'historique invalide ignorée")
    return list(reversed(events))


def product_category(name: str) -> str:
    """Catégorisation indicative sans ajouter de champ aux produits existants."""
    upper_name = name.upper()
    if any(word in upper_name for word in ("MONSTER", "COCA", "OASIS", "LIPTON", "CAPRI", "CACOLAC", "SODA")):
        return "Boissons"
    if any(word in upper_name for word in ("SNICKERS", "MARS", "TWIX", "LION", "KITKAT", "KINDER", "KNOPPERS", "CHOC", "OREO")):
        return "Snacks sucrés"
    if "CHIPS" in upper_name:
        return "Snacks salés"
    return "Autres"


# ------------------- PETITES FONCTIONS DE VALIDATION -------------------
def round_tuxcoin(value: float) -> float:
    """
    Arrondit le montant à la valeur la plus proche de 0.0, 0.25, 0.5 ou 0.75
    """
    integer_part = int(value)
    decimal_part = value - integer_part
    allowed_decimals = [0.0, 0.25, 0.5, 0.75]
    closest = min(allowed_decimals, key=lambda x: abs(decimal_part - x))
    return integer_part + closest


def parse_quantity(value) -> int:
    """Accepte les anciens stocks ecrits comme 5.0, jamais les fractions."""
    quantity = float(value)
    if not math.isfinite(quantity) or quantity < 0 or not quantity.is_integer():
        raise ValueError
    return int(quantity)


def save_product_image(upload):
    """Enregistre une image locale sûre et renvoie son URL publique."""
    if not upload or not upload.filename:
        return None
    filename = secure_filename(upload.filename)
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not filename or extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Image invalide : PNG, JPG, GIF ou WEBP uniquement.")
    # L'extension seule est contournable (ex. un programme renommé en .jpg).
    # On vérifie aussi la signature binaire au début du fichier envoyé.
    signature = upload.read(16)
    upload.seek(0)
    is_image = (
        signature.startswith(b"\x89PNG\r\n\x1a\n") or
        signature.startswith(b"\xff\xd8\xff") or
        signature.startswith((b"GIF87a", b"GIF89a")) or
        signature.startswith(b"RIFF") and signature[8:12] == b"WEBP"
    )
    if not is_image:
        raise ValueError("Le fichier envoyé ne ressemble pas à une image valide.")
    saved_name = f"{uuid.uuid4().hex}.{extension}"
    upload.save(os.path.join(UPLOAD_DIR, saved_name))
    return f"/static/uploads/{saved_name}"


def validate_image_url(value: str) -> str:
    """N'accepte que les URL web ou les images locales créées par l'application."""
    if not value:
        return ""
    if value.startswith("/static/uploads/"):
        return value
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    raise ValueError("Le lien d'image doit commencer par http:// ou https://.")


def openFridge(opened_by: str, source: str):
    """Demande l'ouverture au boîtier du frigo et enregistre le résultat.

    Cette fonction est lancée dans un thread après la réponse web : ainsi,
    un appel réseau lent ne bloque pas la page de l'utilisateur.
    """
    global OPEN_UNTIL
    with FRIDGE_LOCK:
        if (time.time() - OPEN_UNTIL) > 15:
            OPEN_UNTIL = time.time()
            try:
                requests.get("http://monix-esp.glz.ovh:51284/56623625630532256256hgfhgfjffhrerytrurytreuurytrfjvhfdjfbgfhjcnxcvbcncbhjfghfjiytrezughfjdfhgfjzaziazeieod406", timeout=5)
                record_event("fridge_open", opened_by, source=source, status="opened")
            except requests.RequestException:
                logger.exception("Impossible d'ouvrir le frigo")
                record_event("fridge_open", opened_by, source=source, status="error")
            finally:
                OPEN_UNTIL = time.time()
        else:
            record_event("fridge_open", opened_by, source=source, status="cooldown")
            print(f"[{time.ctime()}] Derniere ouverture il y a : {time.time() - OPEN_UNTIL} sec(s) tu forces fréro ! manges ton cooldown", flush=True)

# ------------------- MODÈLES DE DONNÉES -------------------
# @dataclass évite d'écrire le code répétitif d'un petit objet Python.
@dataclass
class User:
    """Un compte : le mot de passe est stocké sous forme de hash, jamais en clair."""
    username: str
    password_hash: bytes
    tuxcoin: float
    is_admin: bool

@dataclass
class Product:
    """Un article vendu. is_promo est simplement la case Promo de l'administration."""
    name: str
    price: float
    quantity: int
    image_url: str
    purchase_cost: float = 0
    is_promo: bool = False


# ------------------- ACCÈS AUX DONNÉES (REPOSITORY) -------------------
class Repo:
    """Centralise lecture/écriture des fichiers utilisateurs et produits.

    Toutes les modifications passent ici. Cela évite que deux achats réalisés
    presque en même temps écrasent le stock ou le solde l'un de l'autre.
    """
    def __init__(self):
        # RLock couvre les threads Flask. flock couvre aussi plusieurs processus
        # Gunicorn/Docker qui partageraient le même volume.
        self._thread_lock = RLock()
        self.key = self.generate_key()
        # Le verrou fichier est pris dès le démarrage avant la première lecture.
        with open(LOCK_FILE, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self.recover_pending_transaction()
                self.users: List[User] = self.load_users()
                self.products: List[Product] = self.load_products()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def obligatory_check(self, method=""):
        """Ancien point d'extension conservé pour compatibilité ; ne fait rien."""
        pass
        #self.users = self.load_users()
        #self.products = self.load_products()
        #print("method =", method, self.users)

    @staticmethod
    def _atomic_write(path: str, data, binary: bool = False):
        """Ecrit un fichier complet puis le remplace d'un coup.

        Ainsi une coupure pendant l'ecriture ne laisse jamais products.json ou
        users.enc tronque/corrompu.
        """
        # On écrit d'abord un fichier temporaire, puis os.replace le remplace.
        # Le lecteur voit donc toujours soit l'ancien, soit le nouveau fichier.
        mode = "wb" if binary else "w"
        kwargs = {} if binary else {"encoding": "utf-8"}
        # Le fichier temporaire doit être dans le même dossier que la cible ;
        # cela garantit que os.replace reste une opération atomique.
        fd, temporary_path = tempfile.mkstemp(prefix=".monix-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, mode, **kwargs) as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    @contextmanager
    def locked_state(self):
        """Recharge l'etat courant et le verrouille le temps d'une mutation."""
        with self._thread_lock:
            with open(LOCK_FILE, "a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    # Si le programme s'était arrêté au milieu d'un achat,
                    # on finit l'achat avant de laisser quelqu'un modifier les données.
                    self.recover_pending_transaction()
                    # Evite qu'un worker conserve une ancienne copie en memoire.
                    self.users = self.load_users()
                    self.products = self.load_products()
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def refresh(self):
        """Lit l'etat le plus recent, notamment s'il y a plusieurs workers."""
        with self.locked_state():
            pass

    def recover_pending_transaction(self):
        """Termine un achat interrompu entre les deux fichiers de donnees."""
        if not os.path.exists(PENDING_TRANSACTION_FILE):
            return
        with open(PENDING_TRANSACTION_FILE, "r", encoding="utf-8") as f:
            pending = json.load(f)
        self._atomic_write(PRODUCTS_FILE, pending["products"])
        self._atomic_write(USERS_FILE, base64.b64decode(pending["users_token"]), binary=True)
        os.unlink(PENDING_TRANSACTION_FILE)

    def save_purchase(self):
        """Commit recuperable d'un achat sur les deux stockages existants."""
        # asdict transforme un Product Python en dictionnaire JSON sauvegardable.
        products = json.dumps([asdict(p) for p in self.products], ensure_ascii=False, indent=2)
        raw_users = [
            {"username": u.username, "password_hash_hex": u.password_hash.hex(),
             "tuxcoin": u.tuxcoin, "is_admin": u.is_admin}
            for u in self.users
        ]
        token = Fernet(self.key).encrypt(json.dumps(raw_users, ensure_ascii=False, indent=2).encode())
        journal = json.dumps({
            "products": products,
            "users_token": base64.b64encode(token).decode("ascii"),
        })
        # Le journal est ecrit avant les deux fichiers. Au redemarrage, il
        # permet de finir exactement le meme achat au lieu de perdre stock ou solde.
        self._atomic_write(PENDING_TRANSACTION_FILE, journal)
        self._atomic_write(PRODUCTS_FILE, products)
        self._atomic_write(USERS_FILE, token, binary=True)
        os.unlink(PENDING_TRANSACTION_FILE)

    def update_products(self):
        """Sauvegarde la liste actuelle de produits dans products.json."""
        self.obligatory_check("update_products")
        raw = [asdict(p) for p in self.products]
        self._atomic_write(PRODUCTS_FILE, json.dumps(raw, ensure_ascii=False, indent=2))

    def delete_product(self, product: Product):
        self.obligatory_check("delete_product")
        self.products.remove(product)
        self.update_products()

    def generate_key(self) -> bytes:
        """Crée une clé de chiffrement une seule fois, puis la relit ensuite."""
        try:
            # O_EXCL garantit que deux démarrages simultanés ne peuvent pas
            # créer deux clés différentes. 0o600 = propriétaire uniquement.
            descriptor = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            key = Fernet.generate_key()
            with os.fdopen(descriptor, "wb") as f:
                f.write(key)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            with open(KEY_FILE, "rb") as f:
                key = f.read()
        # Corrige aussi les permissions d'une clé créée par une ancienne version.
        os.chmod(KEY_FILE, 0o600)
        return key

    def add_user(self, username: str, password: str, is_admin: bool=False, initial_balance: float=0) -> bool:
        """Ajoute un utilisateur et chiffre immédiatement la nouvelle liste."""
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
        """Déchiffre users.enc et recrée les objets User en mémoire."""

        if not os.path.exists(USERS_FILE):
            # Premier démarrage uniquement : création du compte administrateur initial.
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
        """Transforme les utilisateurs en JSON puis chiffre ce JSON dans users.enc."""
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
        self._atomic_write(USERS_FILE, token, binary=True)

    def load_products(self) -> List[Product]:
        """Charge products.json et complète automatiquement les anciens produits."""
        if not os.path.exists(PRODUCTS_FILE):
            sample = [
                {"name": "Barre chocolatée", "price": 3, "quantity": 20, "image_url": ""},
                {"name": "Chips", "price": 4, "quantity": 15, "image_url": ""},
                {"name": "Soda 33cl", "price": 5, "quantity": 24, "image_url": ""},
            ]
            self._atomic_write(PRODUCTS_FILE, json.dumps(sample, ensure_ascii=False, indent=2))
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        migrated = False
        # Une ancienne version peut ne pas avoir les champs récents. Les ajouter
        # ici permet de démarrer sans demander à l'administrateur une migration manuelle.
        for item in raw:
            # Migration automatique des anciens products.json : le nouveau
            # champ ne change ni les autres valeurs ni leur structure.
            if "purchase_cost" not in item:
                item["purchase_cost"] = 0
                migrated = True
            if "is_promo" not in item:
                item["is_promo"] = False
                migrated = True
        products = []
        for p in raw:
            product = Product(**p)
            # Les produits sont vendus a l'unite : une valeur fractionnaire,
            # negative ou non-finie ne doit pas pouvoir passer en caisse.
            if (not isinstance(product.quantity, (int, float)) or
                    not math.isfinite(product.quantity) or product.quantity < 0 or
                    not float(product.quantity).is_integer()):
                raise ValueError(f"Quantite invalide pour le produit '{product.name}'")
            if not isinstance(product.price, (int, float)) or not math.isfinite(product.price) or product.price < 0:
                raise ValueError(f"Prix invalide pour le produit '{product.name}'")
            if (not isinstance(product.purchase_cost, (int, float)) or
                    not math.isfinite(product.purchase_cost) or product.purchase_cost < 0):
                raise ValueError(f"Coût d'achat invalide pour le produit '{product.name}'")
            if not isinstance(product.is_promo, bool):
                raise ValueError(f"Promotion invalide pour le produit '{product.name}'")
            products.append(product)
        if migrated:
            self._atomic_write(PRODUCTS_FILE, json.dumps(raw, ensure_ascii=False, indent=2))
            logger.info("Migration products.json : purchase_cost=0 ajouté aux produits existants")
        return products

    def save_products(self):
        """Alias explicite de sauvegarde utilisé après une modification simple."""
        self.obligatory_check("save_products")
        raw = [asdict(p) for p in self.products]
        self._atomic_write(PRODUCTS_FILE, json.dumps(raw, ensure_ascii=False, indent=2))

    def find_user(self, username: str) -> Optional[User]:
        """Cherche un utilisateur sans tenir compte des majuscules/minuscules."""
        self.obligatory_check("find_user")
        for u in self.users:
            if u.username.lower() == username.lower():
                return u
        return None


repo = Repo()
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,       # JavaScript ne peut pas lire le cookie de session.
    SESSION_COOKIE_SAMESITE="Lax",      # Limite l'envoi du cookie depuis un autre site.
    SESSION_COOKIE_SECURE=os.environ.get("MONIX_HTTPS") == "1",  # À activer derrière HTTPS.
)


def get_csrf_token():
    """Crée un jeton secret lié à la session et le réutilise pour ses formulaires."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.context_processor
def inject_csrf_token():
    """Rend csrf_token() disponible dans tous les templates HTML."""
    return {"csrf_token": get_csrf_token}


@app.before_request
def reject_cross_site_writes():
    """Bloque toute requête POST qui n'est pas venue d'un formulaire MONIX."""
    if request.method != "POST":
        return
    sent_token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    expected_token = session.get("csrf_token", "")
    if not sent_token or not expected_token or not hmac.compare_digest(sent_token, expected_token):
        abort(400, description="Jeton de sécurité invalide. Actualisez la page et réessayez.")


@app.after_request
def add_security_headers(response):
    """Ajoute des en-têtes qui réduisent plusieurs attaques côté navigateur."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# ------------------- Auth Decorators -------------------
def login_required(f):
    """Décorateur : refuse une page si la personne n'est pas connectée.

    Un décorateur est une fonction qui ajoute une règle autour d'une autre
    fonction. Ici, il redirige simplement vers /login si la session est vide.
    """
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    """Décorateur : laisse passer uniquement un compte avec is_admin=True."""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Relit le fichier avant le contrôle : un droit admin retiré est effectif
        # immédiatement, même si plusieurs processus Flask sont lancés.
        repo.refresh()
        u = repo.find_user(session.get("username", ""))
        if not u or not u.is_admin:
            flash("Accès réservé aux administrateurs","danger")
            return redirect(url_for("main"))
        return f(*args, **kwargs)
    return wrapper


# ------------------- Routes -------------------
@app.route("/", methods=["GET"])
@login_required
def main():
    """Affiche le catalogue public des produits qui ont encore du stock."""
    repo.refresh()
    u = repo.find_user(session["username"])
    # On garde l'index original avec le produit : c'est lui que /buy utilise.
    # Sans cela, masquer une rupture décalerait les indices des autres produits.
    available_products = [(index, product) for index, product in enumerate(repo.products) if product.quantity > 0]
    return render_template("main.html", user=u, products=available_products, currency=CURRENCY)


@app.route("/buy/<int:pid>", methods=["POST"])
@login_required
def buy(pid):
    """Vérifie puis effectue un achat : stock -1, solde -prix, historique + frigo."""
    expected_name = request.form.get("expected_name", "")
    # Toute la vérification et la modification se font dans le même verrou.
    # Ainsi, deux personnes ne peuvent pas acheter le dernier exemplaire ensemble.
    with repo.locked_state():
        user = repo.find_user(session["username"])
        if pid < 0 or pid >= len(repo.products) or not user:
            flash("Produit ou utilisateur introuvable", "danger")
            return redirect(url_for("main"))

        prod = repo.products[pid]
        # L'index est historique : apres une suppression, il peut pointer sur
        # une autre ligne. Dans ce cas on refuse au lieu de vendre le mauvais.
        if prod.name != expected_name:
            flash("Le catalogue a changé : vérifiez le produit puis réessayez.", "warning")
            return redirect(url_for("main"))
        if prod.quantity < 1:
            flash("Produit en rupture de stock", "danger")
            return redirect(url_for("main"))
        if user.tuxcoin < prod.price:
            flash(f"Solde insuffisant ({user.tuxcoin} {CURRENCY})", "danger")
            return redirect(url_for("main"))

        # Les contrôles ont réussi : la vente est maintenant validée.
        prod.quantity -= 1
        user.tuxcoin = round_tuxcoin(user.tuxcoin - prod.price)
        # Commit recuperable : aucun achat ne peut laisser un seul des deux
        # fichiers a jour apres un arret brutal.
        repo.save_purchase()
        # L'historique garde le coût du jour, même si l'admin le change plus tard.
        record_event(
            "purchase", user.username, product=prod.name,
            category=product_category(prod.name), price=prod.price,
            purchase_cost=prod.purchase_cost,
            profit=prod.price - prod.purchase_cost,
            balance_after=user.tuxcoin, quantity_after=prod.quantity,
        )
    flash(f"Achat de '{prod.name}' pour {prod.price} {CURRENCY}", "success")
    log_action(user.username, "ACHAT", f"Produit={prod.name}, Prix={prod.price}, NouveauSolde={user.tuxcoin}")

    Thread(target=openFridge, args=(user.username, "purchase")).start()

    log_action(user.username, "OUVERTURE_FRIGO", "Frigo ouvert après achat")

    flash("Frigo ouvert, servez-vous !", "info")
    return redirect(url_for("main"))


@app.route("/login", methods=["GET","POST"])
def login():
    """Affiche le formulaire de connexion ou enregistre la session après vérification."""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        repo.refresh()
        user = repo.find_user(username)
        if not user or not bcrypt.checkpw(password.encode(), user.password_hash):
            log_action(username, "ECHEC_CONNEXION", "Mot de passe incorrect")
            flash("Utilisateur ou mot de passe incorrect","danger")
            return redirect(url_for("login"))
        # Efface toute ancienne session avant d'ouvrir la nouvelle (anti fixation).
        session.clear()
        session["username"] = user.username
        log_action(user.username, "CONNEXION", "Connexion réussie")
        return redirect(url_for("main"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    """Supprime seulement la session du navigateur ; le compte reste dans users.enc."""
    log_action(session.get("username"), "DECONNEXION", "Déconnexion réussie")
    session.pop("username", None)
    return redirect(url_for("login"))


@app.route("/fbiOpenUp", methods=["POST"])
@login_required
def fbiOpenUp():
    """Permet à tout membre connecté de demander l'ouverture manuelle du frigo."""
    username = session["username"]
    log_action(username, "OUVERTURE_DU_FRIGO_FBI_OPEN_UP", "Ouverture manuelle du frigo")
    Thread(target=openFridge, args=(username, "manual")).start()
    flash("FBI OPEN UP", "info")
    return redirect(url_for("main"))

# ------------------- Admin Users -------------------
@app.route("/admin/data", methods=["GET"])
@login_required
@admin_required
def admin_data():
    """Construit les statistiques du tableau de bord à partir de events.jsonl."""
    events = load_events()
    purchases = [event for event in events if event.get("type") == "purchase"]
    openings = [event for event in events if event.get("type") == "fridge_open"]
    category_totals = Counter(event.get("category", "Autres") for event in purchases)
    product_totals = Counter(event.get("product", "Produit inconnu") for event in purchases)
    buyer_totals = Counter(event.get("user", "Inconnu") for event in purchases)
    revenue_by_product = defaultdict(float)
    profit_by_product = defaultdict(float)
    sales_by_weekday = Counter()
    profit_by_weekday = defaultdict(float)
    sales_by_month = Counter()
    profit_by_month = defaultdict(float)
    weekday_labels = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    # Chaque achat alimente plusieurs regroupements : produit, jour et mois.
    for event in purchases:
        product = event.get("product", "Produit inconnu")
        price = float(event.get("price", 0))
        profit = float(event.get("profit", price - float(event.get("purchase_cost", 0))))
        revenue_by_product[product] += price
        profit_by_product[product] += profit
        try:
            occurred_at = datetime.fromisoformat(event["timestamp"])
            weekday = weekday_labels[occurred_at.weekday()]
            month = occurred_at.strftime("%Y-%m")
            sales_by_weekday[weekday] += 1
            profit_by_weekday[weekday] += profit
            sales_by_month[month] += 1
            profit_by_month[month] += profit
        except (KeyError, TypeError, ValueError):
            logger.warning("Date d'achat invalide ignorée dans les statistiques")
    return render_template(
        "admin_data.html", currency=CURRENCY, purchases=purchases,
        openings=openings, recent_events=events[:100],
        category_totals=category_totals.most_common(),
        product_totals=product_totals.most_common(),
        buyer_totals=buyer_totals.most_common(),
        product_financials=sorted(
            [(product, revenue_by_product[product], profit_by_product[product]) for product in revenue_by_product],
            key=lambda item: item[2], reverse=True,
        ),
        weekday_financials=[(day, sales_by_weekday[day], profit_by_weekday[day]) for day in weekday_labels if sales_by_weekday[day]],
        month_financials=[(month, sales_by_month[month], profit_by_month[month]) for month in sorted(sales_by_month, reverse=True)],
        total_revenue=sum(float(event.get("price", 0)) for event in purchases),
        total_profit=sum(float(event.get("profit", float(event.get("price", 0)) - float(event.get("purchase_cost", 0)))) for event in purchases),
    )

@app.route("/admin/users", methods=["GET"])
@login_required
@admin_required
def admin_users():
    """Affiche la liste des comptes. Cette page est réservée aux admins."""
    repo.refresh()
    return render_template(
        "admin_users.html",
        users=repo.users,
        currency=CURRENCY,
        current_user=session.get("username")
    )

@app.route("/admin/users/add", methods=["POST"])
@login_required
@admin_required
def admin_add_user():
    """Valide puis ajoute un compte depuis le formulaire administrateur."""
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password")
    is_admin = request.form.get("is_admin") == "1"
    try:
        initial_balance = float(request.form.get("initial_balance") or 0)
        if not username or not password or not math.isfinite(initial_balance) or initial_balance < 0:
            raise ValueError
    except (TypeError, ValueError):
        flash("Pseudo, mot de passe et solde valide requis.", "danger")
        return redirect(url_for("admin_users"))
    with repo.locked_state():
        if repo.find_user(username):
            flash("Ce pseudo existe déjà.", "danger")
            return redirect(url_for("admin_users"))
        repo.add_user(username, password, is_admin=is_admin, initial_balance=initial_balance)
    log_action(session["username"], "ADMIN_AJOUT_USER", f"Ajout de {username}, admin={is_admin}, balance={initial_balance}")
    flash(f"Utilisateur {username} ajouté.", "success")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<username>", methods=["POST"])
@login_required
@admin_required
def admin_update_user(username):
    """Exécute l'action demandée pour un utilisateur : mot de passe, solde, rôle ou suppression."""
    action = request.form.get("action")
    with repo.locked_state():
        u = repo.find_user(username)
        if not u:
            flash("Utilisateur introuvable.", "danger")
            return redirect(url_for("admin_users"))
        if action == "update_password":
            new_pass = request.form.get("password")
            if new_pass:
                u.password_hash = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt())
                repo.update_user(u)
                log_action(session["username"], "ADMIN_UPDATE_PASSWORD", f"Utilisateur={u.username}")
                flash(f"Mot de passe de {u.username} modifié.", "success")
            else:
                flash("Mot de passe vide.", "warning")

        elif action == "update_balance":
            try:
                new_balance = float(request.form.get("tuxcoin"))
                if not math.isfinite(new_balance) or new_balance < 0:
                    raise ValueError
                u.tuxcoin = round_tuxcoin(new_balance)
                repo.update_user(u)
                log_action(session["username"], "ADMIN_UPDATE_BALANCE", f"Utilisateur={u.username}, NouveauSolde={u.tuxcoin}")
                flash(f"Solde de {u.username} modifié.", "success")
            except (TypeError, ValueError):
                flash("Solde invalide.", "danger")

        elif action == "update_admin":
            u.is_admin = request.form.get("is_admin") == "1"
            repo.update_user(u)
            log_action(session["username"], "ADMIN_UPDATE_ROLE", f"Utilisateur={u.username}, Admin={u.is_admin}")
            flash(f"Rôle admin de {u.username} mis à jour.", "success")

        elif action == "delete_user":
            if u.username == session.get("username"):
                flash("Vous ne pouvez pas supprimer votre propre compte.", "warning")
            else:
                repo.users.remove(u)
                repo.update_user(u)
                log_action(session["username"], "ADMIN_DELETE_USER", f"Suppression de {u.username}")
                flash(f"Utilisateur {u.username} supprimé.", "success")

    return redirect(url_for("admin_users"))


# ------------------- Admin Products -------------------
@app.route("/admin/products", methods=["GET"])
@login_required
@admin_required
def admin_products():
    """Affiche tous les produits, même ceux qui ne sont plus vendables (stock à zéro)."""
    repo.refresh()
    return render_template(
        "admin_products.html",
        products=repo.products,
        currency=CURRENCY
    )

@app.route("/admin/products/add", methods=["POST"])
@login_required
@admin_required
def admin_add_product():
    """Crée un produit depuis le formulaire, avec URL ou image envoyée par l'admin."""
    # request.form contient les champs texte/nombre ; request.files contient le fichier image.
    try:
        name = (request.form.get("name") or "").strip()
        price = float(request.form.get("price"))
        purchase_cost = float(request.form.get("purchase_cost") or 0)
        quantity = parse_quantity(request.form.get("quantity"))
        image_url = validate_image_url((request.form.get("image_url") or "").strip())
        is_promo = request.form.get("is_promo") == "1"
        if (not name or not math.isfinite(price) or price < 0 or
                not math.isfinite(purchase_cost) or purchase_cost < 0 or quantity < 0):
            raise ValueError
    except (TypeError, ValueError):
        flash("Nom, prix et quantité valides requis.", "danger")
        return redirect(url_for("admin_products"))
    try:
        uploaded_image = save_product_image(request.files.get("image_file"))
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(url_for("admin_products"))
    with repo.locked_state():
        repo.products.append(Product(name=name, price=price, quantity=quantity,
                                     image_url=uploaded_image or image_url,
                                     purchase_cost=purchase_cost, is_promo=is_promo))
        repo.update_products()
    log_action(session["username"], "ADMIN_AJOUT_PRODUIT", f"Produit={name}, Prix={price}, Cout={purchase_cost}, Qte={quantity}")
    flash(f"Produit {name} ajouté.", "success")
    return redirect(url_for("admin_products"))

@app.route("/admin/products/<int:index>", methods=["POST"])
@login_required
@admin_required
def admin_update_product(index):
    """Modifie, réapprovisionne ou supprime le produit à la position indiquée."""
    action = request.form.get("action")
    with repo.locked_state():
        if index < 0 or index >= len(repo.products):
            flash("Produit introuvable.", "danger")
            return redirect(url_for("admin_products"))
        p = repo.products[index]
        # Protection contre une page admin restée ouverte pendant qu'un autre
        # administrateur a supprimé ou déplacé une ligne du catalogue.
        if p.name != request.form.get("expected_name", ""):
            flash("Le catalogue a changé : actualisez la page avant de modifier un produit.", "warning")
            return redirect(url_for("admin_products"))
        if action == "edit_product":
            try:
                name = (request.form.get("name") or "").strip()
                price = float(request.form.get("price"))
                purchase_cost = float(request.form.get("purchase_cost") or 0)
                if (not name or not math.isfinite(price) or price < 0 or
                        not math.isfinite(purchase_cost) or purchase_cost < 0):
                    raise ValueError
                p.name = name
                p.price = price
                p.purchase_cost = purchase_cost
                p.is_promo = request.form.get("is_promo") == "1"
                uploaded_image = save_product_image(request.files.get("image_file"))
                p.image_url = uploaded_image or validate_image_url((request.form.get("image_url") or "").strip())
                repo.update_products()
                log_action(session["username"], "ADMIN_EDIT_PRODUIT", f"Produit={p.name}, Prix={p.price}")
                flash(f"Produit {p.name} modifié.", "success")
            except (TypeError, ValueError):
                flash("Nom ou prix invalide.", "danger")

        elif action == "update_quantity":
            try:
                quantity = parse_quantity(request.form.get("quantity"))
                if quantity < 0:
                    raise ValueError
                p.quantity = quantity
                repo.update_products()
                log_action(session["username"], "ADMIN_UPDATE_QTE", f"Produit={p.name}, Qte={p.quantity}")
                flash(f"Quantité de {p.name} mise à jour.", "success")
            except (TypeError, ValueError):
                flash("La quantité doit être un entier positif ou nul.", "danger")

        elif action == "delete_product":
            repo.products.pop(index)
            repo.update_products()
            log_action(session["username"], "ADMIN_DELETE_PRODUIT", f"Produit={p.name}")
            flash(f"Produit {p.name} supprimé.", "success")

    return redirect(url_for("admin_products"))


# ------------------- Don -------------------
@app.route("/donate", methods=["POST"])
@login_required
def donate():
    """Retire un don du solde et renvoie du JSON au JavaScript de la page."""
    try:
        amount = float(request.form.get("amount", 0))
    except ValueError:
        return jsonify({"error": "Montant invalide."})

    if not math.isfinite(amount) or amount <= 0:
        return jsonify({"error": "Veuillez entrer un montant positif."})

    try:
        with repo.locked_state():
            user = repo.find_user(session["username"])
            if not user or user.tuxcoin < amount:
                balance = user.tuxcoin if user else 0
                return jsonify({"error": f"Solde insuffisant : {balance} < {amount}"})
            user.tuxcoin = round_tuxcoin(user.tuxcoin - amount)
            repo.update_user(user)
        log_action(user.username, "DON", f"Montant={amount}, NouveauSolde={user.tuxcoin}")
        return jsonify({"new_balance": user.tuxcoin})
    except Exception as e:
        return jsonify({"error": f"Erreur serveur: {str(e)}"})


# ------------------- Run -------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
