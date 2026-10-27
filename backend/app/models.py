from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_created_at", "created_at"),
        Index("ux_signals_content_hash", "content_hash", unique=True),
        Index("ix_signals_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    parsed_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class EAExecution(Base):
    __tablename__ = "ea_executions"
    __table_args__ = (UniqueConstraint("signal_id", "leg", name="ux_ea_executions_signal_leg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), nullable=False)
    leg: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    ticket: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    exec_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ux_trades_ticket", "ticket", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    ticket: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[str | None] = mapped_column(Text, nullable=True)
    exec_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScalperTrade(Base):
    __tablename__ = "scalper_trades"
    __table_args__ = (
        UniqueConstraint("ticket", name="ux_scalper_trades_ticket"),
        Index("ix_scalper_trades_opened_at", "opened_at"),
        Index("ix_scalper_trades_magic_closed", "magic", "closed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket: Mapped[str] = mapped_column(String(128), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(16), nullable=False)
    terminal_login: Mapped[str | None] = mapped_column(String(32), nullable=True)
    terminal_server: Mapped[str | None] = mapped_column(String(128), nullable=True)
    magic: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    lots: Mapped[float] = mapped_column(Float, nullable=False)
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScalperEAStatus(Base):
    __tablename__ = "scalper_ea_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_login: Mapped[str | None] = mapped_column(String(32), nullable=True)
    terminal_server: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    magic: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ChatGroup(Base):
    __tablename__ = "groups"

    chat_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)


class SystemLog(Base):
    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO", nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AppSetting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class AdminUser(Base):
    __tablename__ = "admin_users"

    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ClientUser(Base):
    __tablename__ = "client_users"
    __table_args__ = (Index("ux_client_users_email", "email", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_user", "user_type", "user_id", "revoked_at"),
        Index("ix_auth_sessions_expiry", "expires_at"),
    )

    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_type: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailVerification(Base):
    __tablename__ = "email_verifications"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resend_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    send_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    send_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PasswordResetCode(Base):
    __tablename__ = "password_reset_codes"
    __table_args__ = (Index("ix_password_reset_last_activity", "last_activity_at"),)

    email_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resend_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    send_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    send_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class MT5Account(Base):
    __tablename__ = "mt5_accounts"
    __table_args__ = (
        Index("ux_mt5_accounts_token_hash", "token_hash", unique=True),
        Index("ix_mt5_accounts_owner_role", "owner_id", "role"),
        UniqueConstraint("owner_id", "server", "login", name="ux_mt5_owner_server_login"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("client_users.id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    server: Mapped[str] = mapped_column(String(128), nullable=False)
    login: Mapped[str] = mapped_column(String(32), nullable=False)
    plan: Mapped[str] = mapped_column(String(16), default="ZERO", nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), default="EA", nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ManagedCopyProfile(Base):
    """Encrypted broker access and bounded copy settings for server-managed followers."""

    __tablename__ = "managed_copy_profiles"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("mt5_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    password_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    prepayment_check_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    follower_symbol: Mapped[str] = mapped_column(String(64), default="XAUUSD", nullable=False)
    mode_lot: Mapped[str] = mapped_column(String(16), default="rasio", nullable=False)
    ratio_lot: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    lot_tetap: Mapped[float] = mapped_column(Float, default=0.01, nullable=False)
    # Zero means no application-level limit for managed server copy trading.
    max_lot_per_order: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_lot_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    login_status: Mapped[str] = mapped_column(String(24), default="UNCHECKED", nullable=False)
    login_message: Mapped[str | None] = mapped_column(String(240), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PAUSED", nullable=False)
    status_message: Mapped[str | None] = mapped_column(String(240), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    __table_args__ = (
        Index("ix_payment_orders_owner_created", "owner_id", "created_at"),
        Index("ix_payment_orders_status_created", "status", "created_at"),
        UniqueConstraint("account_id", name="ux_payment_orders_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("client_users.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    server: Mapped[str] = mapped_column(String(128), nullable=False)
    login: Mapped[str] = mapped_column(String(32), nullable=False)
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), default="EA", nullable=False)
    prepayment_check_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    total_idr: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_provider: Mapped[str] = mapped_column(String(16), nullable=False, default="MANUAL")
    airpay_external_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    airpay_transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    airpay_image_tid: Mapped[str | None] = mapped_column(String(150), nullable=True)
    airpay_merchant_uid: Mapped[str | None] = mapped_column(String(150), nullable=True)
    airpay_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    airpay_total_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    airpay_unique_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    airpay_fee_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    airpay_net_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    airpay_mdr_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    airpay_expiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    airpay_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_message: Mapped[str | None] = mapped_column(String(240), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDING_PAYMENT", nullable=False)
    proof_filename: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("mt5_accounts.id", ondelete="SET NULL"), nullable=True)
    admin_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class PendingMT5Verification(Base):
    """Short-lived, encrypted broker credentials verified before checkout."""

    __tablename__ = "pending_mt5_verifications"
    __table_args__ = (
        Index("ix_pending_mt5_verifications_owner_created", "owner_id", "created_at"),
        Index("ix_pending_mt5_verifications_status_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("client_users.id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    server: Mapped[str] = mapped_column(String(128), nullable=False)
    login: Mapped[str] = mapped_column(String(32), nullable=False)
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    password_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="CHECKING", nullable=False)
    message: Mapped[str | None] = mapped_column(String(240), nullable=True)
    session_connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    floating_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    account_trade_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    terminal_trade_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expert_trade_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    session_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CSConversation(Base):
    __tablename__ = "cs_conversations"
    __table_args__ = (
        CheckConstraint(
            "(client_user_id IS NOT NULL AND public_visitor_hash IS NULL) OR "
            "(client_user_id IS NULL AND public_visitor_hash IS NOT NULL)",
            name="ck_cs_conversation_identity",
        ),
        Index("ix_cs_conversations_client_expiry", "client_user_id", "expires_at"),
        Index("ix_cs_conversations_visitor_expiry", "public_visitor_hash", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_user_id: Mapped[int | None] = mapped_column(ForeignKey("client_users.id", ondelete="CASCADE"), nullable=True)
    public_visitor_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CSChatMessage(Base):
    __tablename__ = "cs_chat_messages"
    __table_args__ = (Index("ix_cs_chat_messages_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("cs_conversations.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class MT5AccountState(Base):
    __tablename__ = "mt5_account_state"

    account_id: Mapped[int] = mapped_column(ForeignKey("mt5_accounts.id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    floating_profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    margin: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    free_margin: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    allow_live_trading: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    account_trade_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    python_api_trade_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    terminal_trade_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expert_trade_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    copy_error: Mapped[str | None] = mapped_column(String(240), nullable=True)
    open_position_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    history_cursor: Mapped[str] = mapped_column(String(64), nullable=False, default="0")
    history_cursor_msc: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class MT5HistoryDeal(Base):
    __tablename__ = "mt5_history_deals"
    __table_args__ = (
        UniqueConstraint("account_id", "deal_ticket", name="ux_mt5_history_account_ticket"),
        Index("ix_mt5_history_account_position", "account_id", "position_id"),
        Index("ix_mt5_history_account_time", "account_id", "deal_time_msc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mt5_accounts.id", ondelete="CASCADE"), nullable=False)
    deal_ticket: Mapped[str] = mapped_column(String(64), nullable=False)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deal_time_msc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    entry: Mapped[str] = mapped_column(String(12), nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    profit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    swap: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class MasterCopyState(Base):
    __tablename__ = "mt5_master_copy_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MT5MasterAccountState(Base):
    __tablename__ = "mt5_master_account_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    floating_profit: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class MT5MasterMarketState(Base):
    __tablename__ = "mt5_master_market_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    quotes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class MT5MasterEquitySample(Base):
    __tablename__ = "mt5_master_equity_samples"
    __table_args__ = (
        UniqueConstraint("sample_hour", name="ux_mt5_master_equity_hour"),
        Index("ix_mt5_master_equity_hour", "sample_hour"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sample_hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class MT5MasterEquityMinuteSample(Base):
    __tablename__ = "mt5_master_equity_minute_samples"
    __table_args__ = (
        UniqueConstraint("sample_minute", name="ux_mt5_master_equity_minute"),
        Index("ix_mt5_master_equity_minute", "sample_minute"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sample_minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class MT5MasterEquityCandle(Base):
    __tablename__ = "mt5_master_equity_candles"
    __table_args__ = (
        UniqueConstraint("account_key", "minute_start", name="ux_mt5_master_equity_candle_account_minute"),
        Index("ix_mt5_master_equity_candle_account_minute", "account_key", "minute_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_key: Mapped[str] = mapped_column(String(192), nullable=False)
    minute_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open_equity: Mapped[float] = mapped_column(Float, nullable=False)
    high_equity: Mapped[float] = mapped_column(Float, nullable=False)
    low_equity: Mapped[float] = mapped_column(Float, nullable=False)
    close_equity: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class MasterCopyPosition(Base):
    __tablename__ = "mt5_master_copy_positions"
    __table_args__ = (
        UniqueConstraint("source_ticket", name="ux_mt5_master_copy_source_ticket"),
        Index("ix_mt5_master_copy_open", "is_open"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_ticket: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    lots: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class EAStatus(Base):
    __tablename__ = "ea_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
