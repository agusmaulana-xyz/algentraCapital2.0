from fastapi import APIRouter, Response

from ..whatsapp_otp import whatsapp_otp_manager


router = APIRouter(prefix="/api/whatsapp-otp", tags=["whatsapp-otp"])


@router.get("/status")
def whatsapp_status(response: Response) -> dict[str, str | bool | None]:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return whatsapp_otp_manager.status()


@router.post("/connect")
def connect_whatsapp(response: Response) -> dict[str, str | bool | None]:
    whatsapp_otp_manager.connect()
    response.headers["Cache-Control"] = "no-store"
    return whatsapp_otp_manager.status()


@router.post("/new-qr")
def create_new_qr(response: Response) -> dict[str, str | bool | None]:
    whatsapp_otp_manager.reset_session()
    response.headers["Cache-Control"] = "no-store"
    return whatsapp_otp_manager.status()


@router.post("/disconnect")
def disconnect_whatsapp(response: Response) -> dict[str, str | bool | None]:
    whatsapp_otp_manager.disconnect()
    response.headers["Cache-Control"] = "no-store"
    return whatsapp_otp_manager.status()
