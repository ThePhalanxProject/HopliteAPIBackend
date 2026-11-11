from datetime import datetime, timedelta, timezone
from hashlib import scrypt
from secrets import compare_digest, token_bytes, token_urlsafe

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from consumption import _to_utc
from models import PilotLogin, PilotSession, PilotUser
from pilot import bearer, get_db, token_digest

router = APIRouter(prefix="/pilot/auth", tags=["pilot"])


class LoginRequest(BaseModel):
    email: str = Field(max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=8, max_length=128)


class EnrollmentRequest(LoginRequest):
    display_name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    invitation_token: str = Field(min_length=20, max_length=128)


class LoginResult(BaseModel):
    id: str
    email: str
    display_name: str
    access_token: str


def password_digest(password, salt):
    return scrypt(password.encode(), salt=salt, n=2**15, r=8, p=3,
                  maxmem=64 * 1024 * 1024, dklen=32).hex()


def create_session(db, account, now):
    token = token_urlsafe(32)
    db.add(PilotSession(
        token_hash=token_digest(token),
        user_id=account.user_id,
        expires_at=now + timedelta(days=7),
    ))
    return LoginResult(id=str(account.user_id), email=account.email,
                       display_name=account.display_name, access_token=token)


@router.post("/enroll", response_model=LoginResult)
def enroll(data: EnrollmentRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(PilotUser).where(
        PilotUser.token_hash == token_digest(data.invitation_token)
    ).with_for_update())
    if user is None or db.get(PilotLogin, user.id) is not None:
        raise HTTPException(403, "Invalid or already used pilot invitation")
    salt = token_bytes(16)
    account = PilotLogin(
        user_id=user.id, email=data.email.lower(),
        display_name=data.display_name.strip(),
        password_hash=f"{salt.hex()}:{password_digest(data.password, salt)}",
    )
    db.add(account)
    result = create_session(db, account, datetime.now(timezone.utc))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This email is already registered") from None
    return result


@router.post("/login", response_model=LoginResult)
def login(data: LoginRequest, db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    account = db.scalar(select(PilotLogin).where(PilotLogin.email == data.email.lower()).with_for_update())
    if account is None:
        password_digest(data.password, b"\0" * 16)
        raise HTTPException(401, "Invalid email or password")
    if account.locked_until and _to_utc(account.locked_until) > now:
        raise HTTPException(429, "Too many attempts. Try again later.")
    salt, digest = account.password_hash.split(":", 1)
    if not compare_digest(password_digest(data.password, bytes.fromhex(salt)), digest):
        account.failed_attempts += 1
        if account.failed_attempts >= 5:
            account.locked_until = now + timedelta(minutes=15)
            account.failed_attempts = 0
        db.commit()
        raise HTTPException(401, "Invalid email or password")
    account.failed_attempts = 0
    account.locked_until = None
    db.execute(delete(PilotSession).where(
        PilotSession.user_id == account.user_id, PilotSession.expires_at <= now
    ))
    result = create_session(db, account, now)
    db.commit()
    return result


@router.post("/logout", status_code=204)
def logout(credentials=Depends(bearer), db: Session = Depends(get_db)):
    if credentials is not None:
        db.execute(delete(PilotSession).where(
            PilotSession.token_hash == token_digest(credentials.credentials)
        ))
        db.commit()
