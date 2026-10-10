import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    AppSetting,
    ClientUser,
    ManagedCopyProfile,
    MasterCopyPosition,
    MasterCopyState,
    MT5Account,
    MT5AccountState,
    MT5HistoryDeal,
    utc_now,
)
from ..mt5_performance import get_mt5_performance
from ..schemas import (
    FollowerAccountReport,
    ManagedCopyActive,
    ManagedCopyLoginCheckResult,
    ManagedCopySetup,
    ManagedCopyWorkerStatus,
    MT5AccountActive,
    MT5AccountUpdate,
)
from ..config import get_settings
from ..security import csrf_token, require_csrf
from ..token_crypto import (
    decrypt_managed_copy_password,
    encrypt_account_token,
    encrypt_managed_copy_password,
)
from ..time_utils import wib_iso


router = APIRouter(prefix="/api/mt5", tags=["mt5"])
MASTER_SNAPSHOT_MAX_AGE = timedelta(seconds=20)
FOLLOWER_ONLINE_MAX_AGE = timedelta(seconds=30)
MANAGED_WORKER_ONLINE_MAX_AGE = timedelta(seconds=30)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _master_source_is_online(db: Session, now: datetime | None = None) -> bool:
    now = now or utc_now()
    state = db.get(MasterCopyState, 1)
    return bool(
        state
        and state.last_snapshot_at
        and now - _aware(state.last_snapshot_at) <= MASTER_SNAPSHOT_MAX_AGE
    )


def _managed_worker_online(db: Session, now: datetime | None = None) -> bool:
    if get_settings().copier_worker_api_key is None:
        return False
    heartbeat = db.get(AppSetting, "managed_copy_worker_seen_at")
    if heartbeat is None:
        return False
    try:
        seen_at = _aware(datetime.fromisoformat(heartbeat.value))
    except (TypeError, ValueError):
        return False
    return (now or utc_now()) - seen_at <= MANAGED_WORKER_ONLINE_MAX_AGE


def _account_payload(
    account: MT5Account,
    master_online: bool = False,
    state: MT5AccountState | None = None,
) -> dict[str, object]:
    now = utc_now()
    follower_online = bool(
        account.active
        and account.last_seen_at
        and now - _aware(account.last_seen_at) <= FOLLOWER_ONLINE_MAX_AGE
    )
    state_online = bool(
        state
        and now - _aware(state.observed_at) <= FOLLOWER_ONLINE_MAX_AGE
    )
    if not account.active:
        connection_status = "disabled"
    elif not follower_online or not state_online:
        connection_status = "offline"
    elif not state.terminal_trade_allowed or not state.expert_trade_allowed:
        connection_status = "trading_disabled"
    elif not master_online:
        connection_status = "waiting_for_source"
    else:
        connection_status = "ready"
    return {
        "id": account.id,
        "label": account.label,
        "server": account.server,
        "login": account.login,
        "plan": account.plan,
        "execution_mode": account.execution_mode,
        "role": "follower",
        "active": account.active,
        "last_seen_at": wib_iso(account.last_seen_at),
        "connection_status": connection_status,
        "balance": state.balance if state else None,
        "equity": state.equity if state else None,
        "floating_profit": state.floating_profit if state else None,
        "currency": state.currency if state else None,
        "trade_mode": state.trade_mode if state else None,
        "state_updated_at": wib_iso(state.observed_at) if state else None,
        "created_at": wib_iso(account.created_at),
    }


def _owned_account(db: Session, owner_id: int, account_id: int) -> MT5Account:
    account = db.execute(
        select(MT5Account).where(MT5Account.id == account_id, MT5Account.owner_id == owner_id)
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Akun MT5 tidak ditemukan")
    return account


def _clear_account_history(db: Session, account_id: int) -> None:
    db.execute(delete(MT5HistoryDeal).where(MT5HistoryDeal.account_id == account_id))
    db.execute(delete(MT5AccountState).where(MT5AccountState.account_id == account_id))


def require_client_id(request: Request, db: Session = Depends(get_db)) -> int:
    user_id = request.session.get("client_user_id")
    if not isinstance(user_id, int):
        raise HTTPException(status_code=401, detail="Login pengguna diperlukan")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_csrf(request)
    else:
        csrf_token(request)
    user = db.get(ClientUser, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Login pengguna diperlukan")
    return user.id


def _account_from_token(
    x_account_token: str | None = Header(default=None, alias="X-Account-Token"),
    x_mt5_login: str | None = Header(default=None, alias="X-MT5-Login"),
    x_mt5_server: str | None = Header(default=None, alias="X-MT5-Server"),
    db: Session = Depends(get_db),
) -> MT5Account:
    if not x_account_token or len(x_account_token) < 32:
        raise HTTPException(status_code=401, detail="Token akun tidak valid")
    account = db.execute(
        select(MT5Account).where(MT5Account.token_hash == _token_hash(x_account_token))
    ).scalar_one_or_none()
    if account is None or not account.active:
        raise HTTPException(status_code=401, detail="Token akun tidak valid atau dinonaktifkan")
    if account.execution_mode == "MANAGED":
        raise HTTPException(status_code=403, detail="Akun ini dikelola worker copier dan tidak memakai EA token")
    if (x_mt5_login or "").strip() != account.login or (x_mt5_server or "").strip().casefold() != account.server.casefold():
        raise HTTPException(status_code=403, detail="Server atau nomor login terminal tidak sesuai dengan akun terdaftar")
    account.last_seen_at = utc_now()
    return account


@router.get("/accounts")
def list_accounts(owner_id: int = Depends(require_client_id), db: Session = Depends(get_db)) -> list[dict[str, object]]:
    # The owner id is taken from the signed client session, never from query input.
    master_online = _master_source_is_online(db)
    rows = db.execute(
        select(MT5Account).where(MT5Account.owner_id == owner_id).order_by(MT5Account.id.asc())
    ).scalars()
    payloads = []
    for row in rows:
        payload = _account_payload(row, master_online, db.get(MT5AccountState, row.id))
        profile = db.get(ManagedCopyProfile, row.id) if row.execution_mode == "MANAGED" else None
        payload["managed_copy"] = _managed_copy_payload(profile)
        payloads.append(payload)
    return payloads


def _managed_copy_payload(profile: ManagedCopyProfile | None) -> dict[str, object]:
    if profile is None:
        return {"configured": False, "active": False, "status": "SETUP_REQUIRED"}
    status = profile.status
    if profile.active:
        active_reference = profile.last_seen_at or profile.updated_at
        if utc_now() - _aware(active_reference) > FOLLOWER_ONLINE_MAX_AGE:
            status = "OFFLINE"
    return {
        "configured": True,
        "login_status": profile.login_status,
        "login_message": profile.login_message,
        "follower_symbol": profile.follower_symbol,
        "active": profile.active,
        "status": status,
        "status_message": profile.status_message,
        "last_seen_at": wib_iso(profile.last_seen_at),
        "mode_lot": profile.mode_lot,
        "ratio_lot": profile.ratio_lot,
        "lot_tetap": profile.lot_tetap,
        "max_lot_per_order": profile.max_lot_per_order,
        "max_lot_total": profile.max_lot_total,
        "max_open_positions": profile.max_open_positions,
    }


@router.put("/accounts/{account_id}/managed-copy")
def configure_managed_copy(
    account_id: int,
    payload: ManagedCopySetup,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    account = _owned_account(db, owner_id, account_id)
    if account.execution_mode != "MANAGED":
        raise HTTPException(status_code=409, detail="Akun ini dibuat untuk pemasangan EA dari MT5")
    profile = db.get(ManagedCopyProfile, account.id)
    if profile is not None and profile.active:
        raise HTTPException(status_code=409, detail="Jeda copy trading sebelum mengubah pengaturan akun")
    values = payload.model_dump(exclude={"broker_password"})
    if profile is None:
        profile = ManagedCopyProfile(
            account_id=account.id,
            password_ciphertext=encrypt_managed_copy_password(payload.broker_password),
            **values,
        )
        db.add(profile)
    else:
        profile.password_ciphertext = encrypt_managed_copy_password(payload.broker_password)
        for key, value in values.items():
            setattr(profile, key, value)
        profile.status = "PAUSED"
        profile.status_message = None
    profile.login_status = "CHECKING"
    profile.login_message = "Memeriksa login dan kesiapan terminal MT5."
    profile.active = False
    profile.updated_at = utc_now()
    db.commit()
    return _managed_copy_payload(profile)


@router.put("/accounts/{account_id}/managed-copy/active")
def set_managed_copy_active(
    account_id: int,
    payload: ManagedCopyActive,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    account = _owned_account(db, owner_id, account_id)
    profile = db.get(ManagedCopyProfile, account.id)
    if account.execution_mode != "MANAGED":
        raise HTTPException(status_code=409, detail="Akun ini tidak memakai layanan managed copy")
    if profile is None:
        raise HTTPException(status_code=409, detail="Simpan pengaturan dan kredensial broker terlebih dahulu")
    if payload.active and profile.login_status != "VALID":
        raise HTTPException(status_code=409, detail="Login broker belum lolos pemeriksaan. Simpan kredensial untuk memeriksa login terlebih dahulu.")
    if payload.active and not _managed_worker_online(db):
        raise HTTPException(status_code=409, detail="Worker copier sedang offline. Coba lagi setelah layanan worker tersambung.")
    if payload.active and not payload.acknowledge_live_risk:
        raise HTTPException(status_code=422, detail="Konfirmasi risiko diperlukan untuk mulai menyalin")
    profile.active = payload.active
    profile.status = "STARTING" if payload.active else "PAUSED"
    profile.status_message = None
    if not payload.active:
        profile.last_seen_at = None
    profile.updated_at = utc_now()
    db.commit()
    return _managed_copy_payload(profile)


def _require_copier_worker(x_copier_worker_key: str | None) -> None:
    configured = get_settings().copier_worker_api_key
    if configured is None:
        raise HTTPException(status_code=503, detail="Worker copier belum dikonfigurasi")
    if not x_copier_worker_key or not hmac.compare_digest(
        x_copier_worker_key, configured.get_secret_value()
    ):
        raise HTTPException(status_code=401, detail="Worker tidak terautentikasi")


@router.get("/copier/jobs")
def list_managed_copy_jobs(
    response: Response,
    x_copier_worker_key: str | None = Header(default=None, alias="X-Copier-Worker-Key"),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_copier_worker(x_copier_worker_key)
    response.headers["Cache-Control"] = "no-store"
    db.merge(AppSetting(key="managed_copy_worker_seen_at", value=utc_now().isoformat()))
    rows = db.execute(
        select(ManagedCopyProfile, MT5Account)
        .join(MT5Account, MT5Account.id == ManagedCopyProfile.account_id)
        .where(ManagedCopyProfile.active.is_(True), MT5Account.execution_mode == "MANAGED")
        .order_by(MT5Account.id.asc())
    ).all()
    jobs = []
    for profile, account in rows:
        try:
            password = decrypt_managed_copy_password(profile.password_ciphertext)
        except Exception as exc:
            profile.status = "ERROR"
            profile.status_message = "Kredensial tersimpan tidak dapat dibuka; simpan ulang password broker."
            logger.exception("Gagal membuka kredensial managed copy untuk akun %s", account.id)
            continue
        jobs.append({
            "account_id": account.id,
            "label": account.label,
            "login": account.login,
            "server": account.server,
            "broker_password": password,
            "follower_symbol": profile.follower_symbol,
            "mode_lot": profile.mode_lot,
            "ratio_lot": profile.ratio_lot,
            "lot_tetap": profile.lot_tetap,
            "max_lot_per_order": profile.max_lot_per_order,
            "max_lot_total": profile.max_lot_total,
            "max_open_positions": profile.max_open_positions,
        })
    login_checks = []
    check_rows = db.execute(
        select(ManagedCopyProfile, MT5Account)
        .join(MT5Account, MT5Account.id == ManagedCopyProfile.account_id)
        .where(
            ManagedCopyProfile.login_status == "CHECKING",
            MT5Account.execution_mode == "MANAGED",
        )
        .order_by(MT5Account.id.asc())
    ).all()
    for profile, account in check_rows:
        try:
            password = decrypt_managed_copy_password(profile.password_ciphertext)
        except Exception:
            profile.login_status = "ERROR"
            profile.login_message = "Kredensial tidak dapat dibuka. Masukkan ulang password broker."
            logger.exception("Gagal membuka kredensial untuk pemeriksaan login akun %s", account.id)
            continue
        login_checks.append({
            "account_id": account.id,
            "login": account.login,
            "server": account.server,
            "broker_password": password,
        })
    managed_ids = list(db.execute(
        select(MT5Account.id).where(MT5Account.execution_mode == "MANAGED")
    ).scalars())
    db.commit()
    return {"jobs": jobs, "login_checks": login_checks, "managed_account_ids": managed_ids}


@router.post("/copier/jobs/{account_id}/login-check")
def report_managed_copy_login_check(
    account_id: int,
    payload: ManagedCopyLoginCheckResult,
    x_copier_worker_key: str | None = Header(default=None, alias="X-Copier-Worker-Key"),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    _require_copier_worker(x_copier_worker_key)
    profile = db.get(ManagedCopyProfile, account_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profil managed copy tidak ditemukan")
    if profile.login_status != "CHECKING":
        return {"status": profile.login_status}
    profile.login_status = "VALID" if payload.valid else "ERROR"
    profile.login_message = payload.message
    db.commit()
    return {"status": profile.login_status}


@router.get("/copier/status")
def managed_copy_service_status(
    response: Response,
    _owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    heartbeat = db.get(AppSetting, "managed_copy_worker_seen_at")
    last_seen = None
    if heartbeat is not None:
        try:
            last_seen = _aware(datetime.fromisoformat(heartbeat.value))
        except (TypeError, ValueError):
            last_seen = None
    configured = get_settings().copier_worker_api_key is not None
    available = bool(
        configured and last_seen is not None
        and utc_now() - last_seen <= MANAGED_WORKER_ONLINE_MAX_AGE
    )
    return {
        "configured": configured,
        "available": available,
        "status": "ONLINE" if available else "OFFLINE",
        "last_seen_at": wib_iso(last_seen),
    }


@router.post("/copier/jobs/{account_id}/status")
def report_managed_copy_worker_status(
    account_id: int,
    payload: ManagedCopyWorkerStatus,
    x_copier_worker_key: str | None = Header(default=None, alias="X-Copier-Worker-Key"),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    _require_copier_worker(x_copier_worker_key)
    profile = db.get(ManagedCopyProfile, account_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Job copy tidak ditemukan")
    profile.status = "PAUSED" if payload.status == "STOPPED" and not profile.active else payload.status
    profile.status_message = None if profile.status == "PAUSED" else payload.message
    if payload.status == "RUNNING":
        profile.login_status = "VALID"
        profile.login_message = "Login broker berhasil dan terminal siap untuk copy trading."
    profile.last_seen_at = utc_now()
    db.commit()
    return {"status": "ok"}


@router.get("/performance")
def client_mt5_performance(
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    account_ids = list(db.execute(
        select(MT5Account.id).where(MT5Account.owner_id == owner_id)
    ).scalars())
    return get_mt5_performance(db, account_ids)


@router.post("/accounts")
def create_account(_owner_id: int = Depends(require_client_id)) -> dict[str, object]:
    # Client accounts are provisioned only by the approved payment review flow.
    raise HTTPException(
        status_code=409,
        detail="Akun dibuat setelah admin menyetujui pembayaran. Buat pesanan melalui alur pembayaran.",
    )


@router.post("/accounts/{account_id}/rotate-token")
def rotate_token(account_id: int, owner_id: int = Depends(require_client_id), db: Session = Depends(get_db)) -> dict[str, str]:
    account = _owned_account(db, owner_id, account_id)
    if account.execution_mode == "MANAGED":
        raise HTTPException(status_code=409, detail="Akun managed copy tidak memakai token EA")
    token = secrets.token_urlsafe(32)
    account.token_hash = _token_hash(token)
    account.token_ciphertext = encrypt_account_token(token)
    account.last_seen_at = None
    db.commit()
    return {"token": token}


@router.put("/accounts/{account_id}/active")
def set_active(account_id: int, payload: MT5AccountActive, owner_id: int = Depends(require_client_id), db: Session = Depends(get_db)) -> dict[str, object]:
    account = _owned_account(db, owner_id, account_id)
    if account.execution_mode == "MANAGED":
        raise HTTPException(status_code=409, detail="Kelola status managed copy dari kontrol copy trading")
    if account.active != payload.active:
        account.last_seen_at = None
    account.active = payload.active
    db.commit()
    return _account_payload(account, _master_source_is_online(db), db.get(MT5AccountState, account.id))


@router.put("/accounts/{account_id}")
def update_account(
    account_id: int,
    payload: MT5AccountUpdate,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    account = _owned_account(db, owner_id, account_id)
    if account.execution_mode == "MANAGED":
        profile = db.get(ManagedCopyProfile, account.id)
        if profile is not None and profile.active:
            raise HTTPException(status_code=409, detail="Jeda copy trading sebelum mengubah detail akun")
    label, server, login = payload.label.strip(), payload.server.strip(), payload.login.strip()
    if not label or not server or not login:
        raise HTTPException(status_code=422, detail="Nama, server, dan nomor login wajib diisi")
    if account.server.casefold() != server.casefold() or account.login != login:
        account.last_seen_at = None
        _clear_account_history(db, account.id)
    account.label, account.server, account.login = label, server, login
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Server dan nomor akun tersebut sudah terdaftar") from exc
    return _account_payload(account, _master_source_is_online(db), db.get(MT5AccountState, account.id))


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: int,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    account = _owned_account(db, owner_id, account_id)
    profile = db.get(ManagedCopyProfile, account.id) if account.execution_mode == "MANAGED" else None
    if profile is not None and profile.active:
        raise HTTPException(status_code=409, detail="Jeda copy trading sebelum menghapus akun")
    _clear_account_history(db, account_id)
    db.delete(account)
    db.commit()
    return {"status": "deleted", "id": account_id}


@router.post("/follower/report")
def report_follower_account(
    payload: FollowerAccountReport,
    account: MT5Account = Depends(_account_from_token),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return _store_follower_account_report(account, payload, db)


def _store_follower_account_report(
    account: MT5Account,
    payload: FollowerAccountReport,
    db: Session,
) -> dict[str, object]:
    now = utc_now()
    state = db.get(MT5AccountState, account.id)
    if state is None:
        state = MT5AccountState(account_id=account.id)
        db.add(state)

    state.balance = payload.balance
    state.equity = payload.equity
    state.floating_profit = payload.floating_profit
    state.margin = payload.margin
    state.free_margin = payload.free_margin
    state.currency = payload.currency.upper()
    state.trade_mode = payload.trade_mode
    state.allow_live_trading = payload.allow_live_trading
    state.terminal_trade_allowed = payload.terminal_trade_allowed
    state.expert_trade_allowed = payload.expert_trade_allowed
    state.open_position_ids_json = json.dumps(sorted(set(payload.open_position_ids)))
    state.observed_at = now

    tickets = [deal.deal_ticket for deal in payload.deals]
    existing = {}
    if tickets:
        existing = {
            row.deal_ticket: row
            for row in db.execute(select(MT5HistoryDeal).where(
                MT5HistoryDeal.account_id == account.id,
                MT5HistoryDeal.deal_ticket.in_(tickets),
            )).scalars()
        }
    cursor = int(state.history_cursor or "0")
    cursor_msc = int(state.history_cursor_msc or 0)
    for deal in payload.deals:
        ticket_value = int(deal.deal_ticket)
        cursor = max(cursor, ticket_value)
        cursor_msc = max(cursor_msc, deal.time_msc)
        row = existing.get(deal.deal_ticket)
        if row is None:
            row = MT5HistoryDeal(account_id=account.id, deal_ticket=deal.deal_ticket)
            db.add(row)
            existing[deal.deal_ticket] = row
        row.position_id = deal.position_id
        row.deal_time_msc = deal.time_msc
        row.deal_time = datetime.fromtimestamp(deal.time_msc / 1000, timezone.utc).replace(tzinfo=None)
        row.symbol = deal.symbol
        row.action = deal.action
        row.entry = deal.entry
        row.volume = deal.volume
        row.price = deal.price
        row.profit = deal.profit
        row.commission = deal.commission
        row.swap = deal.swap
        row.fee = deal.fee
    state.history_cursor = str(cursor)
    state.history_cursor_msc = cursor_msc
    account.last_seen_at = now
    db.commit()
    return {
        "status": "ok",
        "history_cursor": state.history_cursor,
        "history_cursor_msc": state.history_cursor_msc,
        "received_deals": len(payload.deals),
        "observed_at": wib_iso(now),
    }


@router.post("/copier/jobs/{account_id}/report")
def report_managed_copy_account(
    account_id: int,
    payload: FollowerAccountReport,
    x_copier_worker_key: str | None = Header(default=None, alias="X-Copier-Worker-Key"),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_copier_worker(x_copier_worker_key)
    profile = db.get(ManagedCopyProfile, account_id)
    account = db.get(MT5Account, account_id)
    if profile is None or account is None or account.execution_mode != "MANAGED":
        raise HTTPException(status_code=404, detail="Akun managed copy tidak ditemukan")
    if not profile.active:
        raise HTTPException(status_code=409, detail="Copy trading akun ini sedang dijeda")
    return _store_follower_account_report(account, payload, db)


@router.get("/follower/positions")
def follower_positions(
    account: MT5Account = Depends(_account_from_token),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    now = utc_now()
    fresh = _master_source_is_online(db, now)
    positions: list[dict[str, object]] = []
    if fresh:
        rows = db.execute(select(MasterCopyPosition).where(
            MasterCopyPosition.is_open.is_(True),
        ).order_by(MasterCopyPosition.id.asc())).scalars()
        positions = [{
            "ticket": row.source_ticket,
            "symbol": row.symbol,
            "action": row.action,
            "lots": row.lots,
            "entry_price": row.entry_price,
            "sl": row.sl,
            "tp": row.tp,
        } for row in rows]
    db.commit()
    account_state = db.get(MT5AccountState, account.id)
    return {
        "active": fresh,
        "master_label": "EA utama Algentra" if fresh else None,
        "positions": positions,
        "history_cursor": account_state.history_cursor if account_state else "0",
        "history_cursor_msc": account_state.history_cursor_msc if account_state else 0,
        "server_time": wib_iso(now),
    }


def _aware(value):
    return value.replace(tzinfo=utc_now().tzinfo) if value.tzinfo is None else value
