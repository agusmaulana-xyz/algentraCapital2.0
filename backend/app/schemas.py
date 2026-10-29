from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class ClientLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=65536)
    remember_me: bool = False


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=65536)
    phone: str = Field(min_length=7, max_length=24)


class ResendCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=65536)


class VerifyEmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    channel: Literal["email", "whatsapp"] = "email"


class PasswordResetConfirmRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")
    password: str = Field(min_length=8, max_length=65536)
    channel: Literal["email", "whatsapp"] = "email"


class MT5AccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    server: str = Field(min_length=1, max_length=128)
    login: str = Field(min_length=1, max_length=32)
    plan: Literal["ZERO", "PRO", "EXPERT"]
    execution_mode: Literal["EA", "MANAGED"] = "EA"
    role: Literal["follower"] = "follower"


class MT5AccountUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    server: str = Field(min_length=1, max_length=128)
    login: str = Field(min_length=1, max_length=32)


class MT5AccountActive(BaseModel):
    active: bool


class ManagedCopySetup(BaseModel):
    broker_password: str = Field(default="", max_length=128)
    follower_symbol: str = Field(default="XAUUSD", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    mode_lot: Literal["tetap", "rasio"] = "rasio"
    ratio_lot: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    lot_tetap: float = Field(default=0.01, gt=0, allow_inf_nan=False)
    max_lot_per_order: float = Field(default=0, ge=0, allow_inf_nan=False)
    max_lot_total: float = Field(default=0, ge=0, allow_inf_nan=False)
    max_open_positions: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_lot_caps(self) -> "ManagedCopySetup":
        if self.max_lot_per_order > 0 and self.max_lot_total > 0 and self.max_lot_per_order > self.max_lot_total:
            raise ValueError("Batas lot per order tidak boleh melebihi batas total lot terbuka")
        return self


class ManagedCopyActive(BaseModel):
    active: bool
    acknowledge_live_risk: bool = False


class ManagedCopyWorkerStatus(BaseModel):
    status: Literal["STARTING", "STANDBY", "RUNNING", "ERROR", "STOPPED"]
    message: str | None = Field(default=None, max_length=240)


class ManagedCopyLoginCheckResult(BaseModel):
    valid: bool
    message: str = Field(min_length=1, max_length=240)


class PrepaymentMT5SessionReport(BaseModel):
    connected: bool
    message: str = Field(min_length=1, max_length=240)
    balance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    equity: float | None = Field(default=None, allow_inf_nan=False)
    floating_profit: float | None = Field(default=None, allow_inf_nan=False)
    currency: str | None = Field(default=None, min_length=3, max_length=16, pattern=r"^[A-Za-z0-9]+$")
    account_trade_allowed: bool | None = None
    terminal_trade_allowed: bool | None = None
    expert_trade_allowed: bool | None = None


class PrepaymentMT5CheckRequest(BaseModel):
    check_id: str | None = Field(default=None, min_length=24, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1, max_length=80)
    server: str = Field(min_length=1, max_length=128)
    login: str = Field(min_length=1, max_length=32, pattern=r"^\d+$")
    plan: Literal["ZERO", "PRO", "EXPERT"]
    execution_mode: Literal["EA", "MANAGED"]
    broker_password: str | None = Field(default=None, max_length=128)


class CSChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class MT5HistoryDealInput(BaseModel):
    deal_ticket: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")
    position_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")
    time_msc: int = Field(ge=1, le=4102444800000)
    symbol: str = Field(min_length=1, max_length=64)
    action: Literal["BUY", "SELL"]
    entry: Literal["IN", "OUT", "INOUT", "OUT_BY"]
    volume: float = Field(gt=0, le=1000, allow_inf_nan=False)
    price: float = Field(gt=0, allow_inf_nan=False)
    profit: float = Field(allow_inf_nan=False)
    commission: float = Field(allow_inf_nan=False)
    swap: float = Field(allow_inf_nan=False)
    fee: float = Field(allow_inf_nan=False)


class FollowerAccountReport(BaseModel):
    balance: float = Field(ge=0, allow_inf_nan=False)
    equity: float = Field(allow_inf_nan=False)
    floating_profit: float = Field(allow_inf_nan=False)
    margin: float = Field(ge=0, allow_inf_nan=False)
    free_margin: float = Field(allow_inf_nan=False)
    currency: str = Field(min_length=3, max_length=16, pattern=r"^[A-Za-z0-9]+$")
    trade_mode: Literal["real", "demo", "contest"]
    allow_live_trading: bool
    # Managed accounts trade through the external Python API, whose permission
    # is separate from the terminal Algo Trading switch used by EAs.
    account_trade_allowed: bool | None = None
    python_api_trade_allowed: bool | None = None
    terminal_trade_allowed: bool
    expert_trade_allowed: bool
    copy_error: str | None = Field(default=None, max_length=240)
    open_position_ids: list[str] = Field(max_length=500)
    deals: list[MT5HistoryDealInput] = Field(max_length=100)


class CopyPositionInput(BaseModel):
    ticket: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=64)
    action: Literal["BUY", "SELL"]
    lots: float = Field(gt=0, le=1000, allow_inf_nan=False)
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    sl: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    tp: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class MT5MasterMarketQuoteInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")
    bid: float = Field(gt=0, allow_inf_nan=False)
    ask: float = Field(gt=0, allow_inf_nan=False)
    time_msc: int = Field(ge=1, le=4102444800000)


class CopySnapshotInput(BaseModel):
    terminal_login: str = Field(min_length=1, max_length=32)
    terminal_server: str = Field(min_length=1, max_length=128)
    positions: list[CopyPositionInput] = Field(max_length=500)
    balance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    equity: float | None = Field(default=None, allow_inf_nan=False)
    floating_profit: float | None = Field(default=None, allow_inf_nan=False)
    currency: str | None = Field(default=None, min_length=3, max_length=16, pattern=r"^[A-Za-z0-9]+$")
    trade_mode: Literal["real", "demo", "contest"] | None = None
    market_quotes: list[MT5MasterMarketQuoteInput] | None = Field(default=None, max_length=100)


class SignalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    group_id: str | None
    group_name: str | None
    message_id: str | None
    sender_id: str | None
    sender_name: str | None
    raw_text: str
    parsed_json: str | None
    normalized_text: str | None
    confidence: float | None
    status: str
    created_at: datetime
    ticket: str | None = None
    profit: float | None = None


class LogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    level: str
    source: str
    message: str
    created_at: datetime


class SettingsUpdate(BaseModel):
    values: dict[str, Any]
