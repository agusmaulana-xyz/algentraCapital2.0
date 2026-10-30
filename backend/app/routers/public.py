import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import PROJECT_ROOT, get_settings
from ..database import get_db
from ..models import (
    ClientUser,
    MasterCopyState,
    MT5MasterAccountState,
    MT5MasterEquityCandle,
    MT5MasterMarketState,
    Trade,
    utc_now,
)
from ..time_utils import WIB, as_utc, wib_iso
from ..registration import LAUNCH_AT, registration_is_open, registration_launch_pending


router = APIRouter(tags=["public"])
MARKET_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
DEFAULT_SITE_ORIGIN = "https://algentracapital.my.id"
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "backend" / "app" / "templates"))


def equity_session_start(now: datetime) -> datetime:
    wib_now = as_utc(now).astimezone(WIB)
    session_date = wib_now.date() if wib_now.hour >= 7 else wib_now.date() - timedelta(days=1)
    return datetime.combine(session_date, time(hour=7), tzinfo=WIB).astimezone(timezone.utc)


def public_site_origin() -> str:
    """Use the configured production domain only when it is a safe HTTPS origin."""
    raw_url = (get_settings().contact_website or "").strip()
    try:
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme == "https"
            and hostname in {"algentracapital.my.id", "www.algentracapital.my.id"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
        ):
            return f"https://{hostname}"
    except ValueError:
        pass
    return DEFAULT_SITE_ORIGIN


def _public_contact_context(db: Session) -> dict[str, object]:
    settings = get_settings()
    whatsapp_digits = re.sub(r"\D", "", settings.contact_whatsapp or "")
    whatsapp = whatsapp_digits if 8 <= len(whatsapp_digits) <= 15 else None
    email = (settings.contact_email or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        email = ""

    social_settings = (
        ("Instagram", settings.contact_instagram, ("instagram.com",), "instagram"),
        ("Grup Telegram", settings.contact_telegram, ("t.me", "telegram.me"), "telegram"),
        ("Admin Telegram", settings.contact_telegram_admin, ("t.me", "telegram.me"), "telegram"),
        ("Facebook", settings.contact_facebook, ("facebook.com", "fb.com"), "facebook"),
        ("LinkedIn", settings.contact_linkedin, ("linkedin.com",), "linkedin"),
        ("X", settings.contact_x, ("x.com", "twitter.com"), "x"),
        ("YouTube", settings.contact_youtube, ("youtube.com", "youtu.be"), "youtube"),
        ("TikTok", settings.contact_tiktok, ("tiktok.com",), "tiktok"),
        ("Website", settings.contact_website, ("algentracapital.my.id",), "website"),
    )
    socials = []
    for label, raw_url, allowed_hosts, icon in social_settings:
        url = (raw_url or "").strip()
        safe_url = None
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").casefold()
        except ValueError:
            parsed = None
            hostname = ""
        if parsed and (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and any(hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts)
        ):
            safe_url = url
        if safe_url:
            path = parsed.path.strip("/")
            if label in {"Instagram", "TikTok", "Admin Telegram"}:
                display = f"@{path.lstrip('@')}"
            elif label == "Website":
                display = parsed.netloc.casefold()
            else:
                display = path
            socials.append({"label": label, "url": safe_url, "icon": icon, "display": display})

    site_origin = public_site_origin()
    organization_id = f"{site_origin}/#organization"
    structured_data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Organization",
                "@id": organization_id,
                "name": "Algentra Capital",
                "url": f"{site_origin}/",
                "logo": {
                    "@type": "ImageObject",
                    "url": f"{site_origin}/brand-logo.png",
                    "width": 512,
                    "height": 512,
                },
                "sameAs": [social["url"] for social in socials if social["label"] != "Website"],
            },
            {
                "@type": "WebSite",
                "@id": f"{site_origin}/#website",
                "url": f"{site_origin}/",
                "name": "Algentra Capital",
                "alternateName": "Algentra Capital Copy Trading",
                "inLanguage": "id-ID",
                "publisher": {"@id": organization_id},
            },
        ],
    }

    return {
        "site_origin": site_origin,
        "structured_data": structured_data,
        "contact_person": (settings.contact_person_name or "").strip(),
        "contact_whatsapp": whatsapp,
        "contact_email": email,
        "contact_socials": socials,
        "registration_open": registration_is_open(db),
        "registration_launch_pending": registration_launch_pending(db),
        "registration_launch_at": LAUNCH_AT.isoformat(),
    }


def public_performance_payload(db: Session, *, include_equity_curve: bool = True) -> dict[str, object]:
    registered_clients = db.execute(select(func.count(ClientUser.id))).scalar_one()
    master_account = db.get(MT5MasterAccountState, 1)
    master_copy_state = db.get(MasterCopyState, 1)
    market_state = db.get(MT5MasterMarketState, 1)
    now = utc_now()
    master_online = bool(
        master_account
        and master_copy_state
        and master_copy_state.last_snapshot_at
        and now - as_utc(master_copy_state.last_snapshot_at) <= timedelta(seconds=20)
        and now - as_utc(master_account.observed_at) <= timedelta(seconds=30)
    )
    market_quotes: list[dict[str, object]] = []
    if market_state is not None:
        try:
            decoded_quotes = json.loads(market_state.quotes_json)
            if isinstance(decoded_quotes, list):
                for quote in decoded_quotes:
                    if not isinstance(quote, dict):
                        continue
                    try:
                        symbol = quote["symbol"]
                        bid = float(quote["bid"])
                        ask = float(quote["ask"])
                        if (
                            not isinstance(symbol, str)
                            or not MARKET_SYMBOL_PATTERN.fullmatch(symbol)
                            or not math.isfinite(bid)
                            or not math.isfinite(ask)
                            or bid <= 0
                            or ask < bid
                        ):
                            continue
                        tick_time = datetime.fromtimestamp(int(quote["time_msc"]) / 1000, timezone.utc)
                        market_quotes.append({
                            "symbol": symbol,
                            "bid": bid,
                            "ask": ask,
                            "updated_at": wib_iso(tick_time),
                        })
                    except (KeyError, TypeError, ValueError, OverflowError, OSError):
                        continue
        except (TypeError, json.JSONDecodeError):
            market_quotes = []
    market_quotes_online = bool(
        master_online
        and market_state
        and now - as_utc(market_state.observed_at) <= timedelta(seconds=30)
    )
    equity_curve: list[dict[str, object]] = []
    equity_candles: list[MT5MasterEquityCandle] = []
    if include_equity_curve:
        session_start = equity_session_start(now)
        latest_equity_account_key = db.execute(
            select(MT5MasterEquityCandle.account_key)
            .order_by(MT5MasterEquityCandle.observed_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        equity_candles = list(db.execute(
            select(MT5MasterEquityCandle)
            .where(
                MT5MasterEquityCandle.account_key == latest_equity_account_key,
                MT5MasterEquityCandle.minute_start >= session_start,
            )
            .order_by(MT5MasterEquityCandle.minute_start.asc())
        ).scalars()) if latest_equity_account_key else []
        equity_curve = [{
            "hour": wib_iso(candle.minute_start),
            "equity": round(candle.close_equity, 2),
            "currency": candle.currency,
        } for candle in equity_candles]
    payload: dict[str, object] = {
        "master_account": {
            "available": master_account is not None,
            "online": master_online,
            "account_key": latest_equity_account_key if include_equity_curve else None,
            "balance": master_account.balance if master_account else None,
            "equity": master_account.equity if master_account else None,
            "floating_profit": master_account.floating_profit if master_account else None,
            "currency": master_account.currency if master_account else None,
            "trade_mode": master_account.trade_mode if master_account else None,
            "updated_at": wib_iso(master_account.observed_at) if master_account else None,
        },
        "market_quotes": market_quotes,
        "market_quotes_online": market_quotes_online,
        "market_quotes_reported_at": wib_iso(market_state.observed_at) if market_state else None,
        "registered_clients": registered_clients,
    }
    if include_equity_curve:
        current_minute = now.replace(second=0, microsecond=0)
        payload["equity_curve"] = equity_curve
        payload["equity_candles"] = [{
            "minute": wib_iso(candle.minute_start),
            "open": round(candle.open_equity, 2),
            "high": round(candle.high_equity, 2),
            "low": round(candle.low_equity, 2),
            "close": round(candle.close_equity, 2),
            "currency": candle.currency,
            "closed": as_utc(candle.minute_start) < current_minute,
            "observed_at": wib_iso(candle.observed_at),
        } for candle in equity_candles]
    return payload


@router.get("/api/public/performance")
def public_performance(db: Session = Depends(get_db)) -> dict[str, object]:
    return public_performance_payload(db)


@router.get("/api/public/performance/pnl-calendar")
def public_pnl_calendar(
    month: str | None = Query(default=None, min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$"),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    now_wib = utc_now().astimezone(WIB)
    business_date = now_wib.date() if now_wib.hour >= 7 else now_wib.date() - timedelta(days=1)
    selected_month = month or business_date.strftime("%Y-%m")
    try:
        month_start_date = datetime.strptime(selected_month, "%Y-%m").date().replace(day=1)
        if not 1970 <= month_start_date.year <= 9998:
            raise ValueError("Tahun di luar jangkauan")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Bulan harus berformat YYYY-MM") from exc

    next_month_date = (
        date(month_start_date.year + 1, 1, 1)
        if month_start_date.month == 12
        else date(month_start_date.year, month_start_date.month + 1, 1)
    )
    # Satu hari PnL dimulai pukul 07.00 WIB dan berakhir tepat sebelum 07.00 WIB berikutnya.
    start_utc = datetime.combine(month_start_date, time(hour=7), tzinfo=WIB).astimezone(timezone.utc)
    end_utc = datetime.combine(next_month_date, time(hour=7), tzinfo=WIB).astimezone(timezone.utc)
    closed_trades = db.execute(
        select(Trade.closed_at, Trade.profit)
        .where(
            Trade.closed_at.is_not(None),
            Trade.closed_at >= start_utc,
            Trade.closed_at < end_utc,
            or_(Trade.result.is_(None), Trade.result != "EXPIRED"),
        )
        .order_by(Trade.closed_at.asc(), Trade.id.asc())
    ).all()

    days: dict[str, dict[str, float | int]] = {}
    total_pnl = 0.0
    latest_close: datetime | None = None
    for closed_at, raw_profit in closed_trades:
        if closed_at is None:
            continue
        closed_at_wib = as_utc(closed_at).astimezone(WIB)
        pnl_date = (closed_at_wib - timedelta(hours=7)).date().isoformat()
        profit = float(raw_profit or 0.0)
        day = days.setdefault(pnl_date, {"pnl": 0.0, "closed_trades": 0})
        day["pnl"] = float(day["pnl"]) + profit
        day["closed_trades"] = int(day["closed_trades"]) + 1
        total_pnl += profit
        if latest_close is None or as_utc(closed_at) > as_utc(latest_close):
            latest_close = closed_at

    master_account = db.get(MT5MasterAccountState, 1)
    return {
        "month": month_start_date.strftime("%Y-%m"),
        "business_date": business_date.isoformat(),
        "currency": master_account.currency if master_account else None,
        "total_pnl": round(total_pnl, 2),
        "closed_trades": len(closed_trades),
        "updated_at": wib_iso(latest_close),
        "days": [
            {
                "date": day,
                "pnl": round(float(values["pnl"]), 2),
                "closed_trades": int(values["closed_trades"]),
            }
            for day, values in sorted(days.items())
        ],
    }


@router.get("/api/public/registration-status")
def public_registration_status(db: Session = Depends(get_db)) -> dict[str, bool]:
    return {"registration_open": registration_is_open(db)}


@router.get("/robots.txt", include_in_schema=False)
def robots_txt() -> Response:
    content = f"User-agent: *\nAllow: /\nSitemap: {public_site_origin()}/sitemap.xml\n"
    return Response(content, media_type="text/plain", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml(db: Session = Depends(get_db)) -> Response:
    site_origin = public_site_origin()
    registration_urls = (
        f"\n  <url><loc>{site_origin}/register</loc></url>"
        if registration_is_open(db)
        else ""
    )
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url>
    <loc>{site_origin}/</loc>
    <image:image>
      <image:loc>{site_origin}/share/algentra-capital.png</image:loc>
      <image:title>Algentra Capital — Copy Trading XAUUSD</image:title>
    </image:image>
  </url>
  {registration_urls}
</urlset>'''
    return Response(content, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/share/algentra-capital.png", include_in_schema=False)
def social_share_image() -> FileResponse:
    image_path = PROJECT_ROOT / "backend" / "app" / "static" / "algentra-share-1200x630.png"
    return FileResponse(
        image_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=3600"},
    )


@router.get("/brand-logo.png", include_in_schema=False)
def brand_logo_image() -> FileResponse:
    image_path = PROJECT_ROOT / "backend" / "app" / "static" / "algentra-brand-512.png"
    return FileResponse(
        image_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=3600"},
    )


@router.get("/favicon.svg", include_in_schema=False)
def favicon_image() -> FileResponse:
    image_path = PROJECT_ROOT / "backend" / "app" / "static" / "algentra-favicon.svg"
    return FileResponse(
        image_path,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=3600"},
    )


@router.get("/musik.mp3", include_in_schema=False)
def site_music() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "musik.mp3", media_type="audio/mpeg")


@router.get("/", response_class=HTMLResponse)
def public_home(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="public.html", context=_public_contact_context(db))


@router.get("/performance", response_class=HTMLResponse)
def public_performance_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="public.html", context=_public_contact_context(db))
