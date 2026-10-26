import hashlib
import bcrypt
from sqlalchemy.orm import Session

from .config import Settings
from .models import AdminUser


def seed_admin(db: Session, settings: Settings) -> None:
    user = db.get(AdminUser, settings.admin_username)
    password = settings.admin_password.get_secret_value().encode("utf-8")
    if user is None:
        password_hash = bcrypt.hashpw(password, bcrypt.gensalt(rounds=12)).decode("ascii")
        db.add(AdminUser(username=settings.admin_username, password_hash=password_hash))
        db.commit()
    elif not verify_password(password.decode("utf-8"), user.password_hash):
        user.password_hash = bcrypt.hashpw(password, bcrypt.gensalt(rounds=12)).decode("ascii")
        db.commit()


def verify_password(password: str, password_hash: str) -> bool:
    password_bytes = password.encode("utf-8")
    try:
        encoded_hash = password_hash.encode("ascii")
        try:
            if bcrypt.checkpw(password_bytes, encoded_hash):
                return True
        except ValueError:
            # Direct bcrypt rejects inputs longer than 72 bytes. Alok's
            # SHA-256 prehash format supports them, so still try that format.
            pass
        digest = hashlib.sha256(password_bytes).digest()
        try:
            return bcrypt.checkpw(digest, encoded_hash)
        except ValueError:
            return False
    except (ValueError, UnicodeEncodeError):
        return False


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def hash_client_password(password: str) -> str:
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode("ascii")
