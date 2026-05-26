import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatMemberUpdated, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from starlette.middleware.sessions import SessionMiddleware
from supabase import Client, create_client
import uvicorn


BASE_DIR = Path(__file__).resolve().parent
CTA_MESSAGE = "¿Quieres ver más contenido como este? 🔥"
CTA_BUTTON_TEXT = "QUIERO MÁS CONTENIDO 🔥"
CTA_CALLBACK_DATA = "want_more_content"
CTA_NOTES = "Clicked QUIERO MÁS CONTENIDO button"
CONFIRM_SUBSCRIPTION_MESSAGE = (
    "Bebes, para llevar mejor control, den click aquí para confirmar su suscripción 💕  \n"
    "Es importante dar click, si no, pueden llegar a ser removidos del canal."
)
CONFIRM_SUBSCRIPTION_BUTTON_TEXT = "CONFIRMAR SUSCRIPCIÓN ✅"
CONFIRM_SUBSCRIPTION_CALLBACK_DATA = "confirm_subscription_v1"
CONFIRMATION_CAMPAIGN = "subscription_confirmation_v1"
CONFIRMATION_SOURCE = "confirm_subscription_button"
DATE_FORMAT = "%Y-%m-%d"
APP_TIMEZONE = ZoneInfo("America/Mexico_City")
SCHEMA_MIGRATION_SQL = """
alter table public.telegram_users add column if not exists joined_at timestamptz;
alter table public.telegram_users add column if not exists membership_start_date date;
alter table public.telegram_users add column if not exists payment_status text default 'unpaid';
alter table public.telegram_users add column if not exists pending_payment_file_id text;
alter table public.telegram_users add column if not exists pending_payment_file_type text;
alter table public.telegram_users add column if not exists pending_payment_at timestamptz;
alter table public.telegram_users add column if not exists approved_by_admin_id bigint;
alter table public.telegram_users add column if not exists approved_at timestamptz;
alter table public.telegram_users add column if not exists rejected_at timestamptz;
alter table public.telegram_users add column if not exists needs_new_receipt_at timestamptz;
alter table public.telegram_users add column if not exists last_payment_at timestamptz;
alter table public.telegram_users add column if not exists invite_link text;
alter table public.telegram_users add column if not exists invite_link_created_at timestamptz;
alter table public.telegram_users add column if not exists invite_link_name text;
alter table public.telegram_users add column if not exists invite_link_revoked boolean default false;
alter table public.telegram_users add column if not exists invite_link_used boolean default false;
alter table public.telegram_users add column if not exists revoked_at timestamptz;
alter table public.telegram_users add column if not exists joined_channel_at timestamptz;
alter table public.telegram_users add column if not exists left_channel_at timestamptz;
alter table public.telegram_users add column if not exists last_seen_at timestamptz;
alter table public.telegram_users add column if not exists renewal_notice_7d_sent_at timestamptz;
alter table public.telegram_users add column if not exists renewal_notice_3d_sent_at timestamptz;
alter table public.telegram_users add column if not exists renewal_notice_1d_sent_at timestamptz;
alter table public.telegram_users add column if not exists removed_at timestamptz;
alter table public.telegram_users add column if not exists removal_reason text;
alter table public.telegram_users add column if not exists confirmed_subscription boolean default false;
alter table public.telegram_users add column if not exists confirmed_at timestamptz;
alter table public.telegram_users add column if not exists confirmation_campaign text;
alter table public.telegram_users add column if not exists source text;
alter table public.telegram_users add column if not exists status text;
alter table public.telegram_users add column if not exists notes text;
alter table public.telegram_users add column if not exists expiry_date date;
alter table public.telegram_users alter column payment_status set default 'unpaid';
alter table public.telegram_users alter column confirmed_subscription set default false;
alter table public.telegram_users alter column invite_link_revoked set default false;
alter table public.telegram_users alter column invite_link_used set default false;
update public.telegram_users
set joined_at = coalesce(joined_at, registered_at, now())
where joined_at is null;
update public.telegram_users
set payment_status = coalesce(payment_status, 'unpaid')
where payment_status is null;
update public.telegram_users
set confirmed_subscription = coalesce(confirmed_subscription, false)
where confirmed_subscription is null;
update public.telegram_users
set invite_link_revoked = coalesce(invite_link_revoked, false)
where invite_link_revoked is null;
update public.telegram_users
set invite_link_used = coalesce(invite_link_used, false)
where invite_link_used is null;
update public.telegram_users
set expiry_date = (joined_at + interval '30 days')::date
where expiry_date is null and membership_start_date is null and joined_at is not null;
update public.telegram_users
set expiry_date = membership_start_date + 30
where expiry_date is null and membership_start_date is not null;
create table if not exists public.payment_history (
  id bigserial primary key,
  telegram_id bigint not null,
  username text,
  first_name text,
  admin_id bigint,
  action text default 'approved',
  payment_status text default 'paid',
  receipt_file_id text,
  receipt_file_type text,
  invite_link text,
  membership_start_date date,
  expiry_date date,
  verified boolean default true,
  notes text,
  created_at timestamptz default now()
);
alter table public.payment_history add column if not exists receipt_file_type text;
alter table public.payment_history add column if not exists membership_start_date date;
alter table public.payment_history add column if not exists expiry_date date;
alter table public.payment_history add column if not exists verified boolean default true;
alter table public.payment_history alter column action set default 'approved';
alter table public.payment_history alter column payment_status set default 'paid';
alter table public.payment_history alter column verified set default true;
create index if not exists payment_history_telegram_id_idx
  on public.payment_history (telegram_id);
create index if not exists payment_history_created_at_idx
  on public.payment_history (created_at desc);
create index if not exists payment_history_payment_status_idx
  on public.payment_history (payment_status);
""".strip()

logger = logging.getLogger(__name__)
router = Router()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    supabase_url: str
    supabase_service_role_key: str
    admin_chat_id: int
    content_channel_id: int | str
    admin_user_ids: set[int]
    admin_password: str
    auto_remove_expired: bool
    renewal_notice_days: tuple[int, ...]


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_chat_id(value: str) -> int | str:
    value = value.strip()
    if value.startswith("@"):
        return value
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError("CONTENT_CHANNEL_ID must be a numeric chat ID or @channelusername") from exc


def parse_admin_ids(value: str) -> set[int]:
    admin_ids: set[int] = set()
    for raw_id in value.split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            admin_ids.add(int(raw_id))
        except ValueError as exc:
            raise RuntimeError("ADMIN_USER_IDS must be a comma-separated list of Telegram user IDs") from exc
    if not admin_ids:
        raise RuntimeError("ADMIN_USER_IDS must include at least one Telegram user ID")
    return admin_ids


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_notice_days(value: str | None) -> tuple[int, ...]:
    if not value:
        return (7, 3, 1)
    days: list[int] = []
    for raw_day in value.split(","):
        raw_day = raw_day.strip()
        if not raw_day:
            continue
        days.append(int(raw_day))
    return tuple(days or [7, 3, 1])


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        bot_token=required_env("BOT_TOKEN"),
        supabase_url=required_env("SUPABASE_URL"),
        supabase_service_role_key=required_env("SUPABASE_SERVICE_ROLE_KEY"),
        admin_chat_id=int(required_env("ADMIN_CHAT_ID")),
        content_channel_id=parse_chat_id(required_env("CONTENT_CHANNEL_ID")),
        admin_user_ids=parse_admin_ids(required_env("ADMIN_USER_IDS")),
        admin_password=required_env("ADMIN_PASSWORD"),
        auto_remove_expired=parse_bool(os.getenv("AUTO_REMOVE_EXPIRED"), default=False),
        renewal_notice_days=parse_notice_days(os.getenv("RENEWAL_NOTICE_DAYS")),
    )


def is_admin(message: Message, settings: Settings) -> bool:
    return bool(message.from_user and message.from_user.id in settings.admin_user_ids)


def is_admin_id(user_id: int | None, settings: Settings) -> bool:
    return bool(user_id and user_id in settings.admin_user_ids)


async def reject_non_admin(message: Message) -> None:
    await message.answer("No autorizado.")


def today_iso() -> str:
    return datetime.now(APP_TIMEZONE).date().isoformat()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(value), DATE_FORMAT).date()
        except ValueError:
            return None


def days_remaining(expiry_date: Any) -> int | None:
    parsed = parse_iso_date(expiry_date)
    if not parsed:
        return None
    return (parsed - datetime.now(APP_TIMEZONE).date()).days


def format_local_datetime(value: Any) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(APP_TIMEZONE).strftime("%d/%m/%Y %H:%M")


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def membership_start_for_user(row: dict[str, Any]) -> date:
    membership_start = parse_iso_date(row.get("membership_start_date"))
    if membership_start:
        return membership_start
    joined_at = parse_iso_date(row.get("joined_at"))
    if joined_at:
        return joined_at
    return datetime.now(APP_TIMEZONE).date()


async def send_long_message(message: Message, text: str) -> None:
    max_length = 3900
    for index in range(0, len(text), max_length):
        await message.answer(text[index : index + max_length])


def format_user(row: dict[str, Any]) -> str:
    telegram_id = row.get("telegram_id", "N/A")
    username = row.get("username")
    first_name = row.get("first_name") or ""
    last_name = row.get("last_name") or ""
    status = row.get("status") or "-"
    expiry = row.get("expiry_date") or "-"
    handle = f"@{username}" if username else "(sin username)"
    full_name = " ".join(part for part in [first_name, last_name] if part).strip() or "(sin nombre)"
    return f"{telegram_id} | {handle} | {full_name} | status: {status} | vence: {expiry}"


def format_user_record(row: dict[str, Any]) -> str:
    if not row:
        return "Usuario no encontrado."
    lines = []
    for key in sorted(row.keys()):
        lines.append(f"{key}: {row.get(key)}")
    return "\n".join(lines)


def format_payment_history_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Sin historial de pagos."
    lines: list[str] = []
    for row in rows:
        created_at = row.get("created_at_display") or format_local_datetime(row.get("created_at"))
        lines.append(
            f"{created_at} | {row.get('action') or '-'} | "
            f"{row.get('payment_status') or '-'} | admin: {row.get('admin_id') or '-'} | "
            f"start: {row.get('membership_start_date') or '-'} | "
            f"expiry: {row.get('expiry_date') or '-'} | "
            f"notes: {row.get('notes') or '-'}"
        )
    return "\n".join(lines)


def get_registered_user(supabase: Client, telegram_id: int) -> dict[str, Any] | None:
    response = (
        supabase.table("telegram_users")
        .select("*")
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def get_user_by_invite_link_name(supabase: Client, invite_link_name: str) -> dict[str, Any] | None:
    response = (
        supabase.table("telegram_users")
        .select("*")
        .eq("invite_link_name", invite_link_name)
        .eq("invite_link_revoked", False)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def insert_payment_history(
    supabase: Client,
    telegram_id: int,
    action: str,
    payment_status: str | None = None,
    admin_id: int | None = None,
    receipt_file_id: str | None = None,
    receipt_file_type: str | None = None,
    invite_link: str | None = None,
    membership_start_date: str | None = None,
    expiry_date: str | None = None,
    verified: bool = True,
    notes: str | None = None,
    username: str | None = None,
    first_name: str | None = None,
) -> None:
    try:
        payload = {
            "telegram_id": telegram_id,
            "username": username,
            "first_name": first_name,
            "payment_status": payment_status,
            "admin_id": admin_id,
            "action": action,
            "receipt_file_id": receipt_file_id,
            "receipt_file_type": receipt_file_type,
            "invite_link": invite_link,
            "membership_start_date": membership_start_date,
            "expiry_date": expiry_date,
            "verified": verified,
            "notes": notes,
        }
        supabase.table("payment_history").insert(payload).execute()
    except Exception:
        logger.warning(
            "Could not insert payment history action=%s telegram_id=%s",
            action,
            telegram_id,
            exc_info=True,
        )


def get_payment_history(supabase: Client, telegram_id: int, limit: int | None = 10) -> list[dict[str, Any]]:
    query = (
        supabase.table("payment_history")
        .select("*")
        .eq("telegram_id", telegram_id)
        .eq("action", "approved")
        .eq("payment_status", "paid")
        .order("created_at", desc=True)
    )
    if limit is not None:
        query = query.limit(limit)
    response = query.execute()
    rows = response.data or []
    for row in rows:
        row["created_at_display"] = format_local_datetime(row.get("created_at"))
        row["receipt_file_url"] = payment_receipt_file_url(row.get("receipt_file_id"))
    return rows


def payment_history_telegram_ids(supabase: Client) -> set[int]:
    response = (
        supabase.table("payment_history")
        .select("telegram_id")
        .eq("action", "approved")
        .eq("payment_status", "paid")
        .limit(5000)
        .execute()
    )
    return {int(row["telegram_id"]) for row in (response.data or []) if row.get("telegram_id") is not None}


def payment_receipt_file_url(file_id: Any) -> str | None:
    if not file_id:
        return None
    return f"/dashboard/payments/file?{urlencode({'file_id': str(file_id)})}"


def list_approved_payments(supabase: Client, search: str = "", limit: int = 200) -> list[dict[str, Any]]:
    response = (
        supabase.table("payment_history")
        .select("*")
        .eq("action", "approved")
        .eq("payment_status", "paid")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = response.data or []
    search_term = search.strip().lower()
    if search_term:
        rows = [
            row
            for row in rows
            if search_term in str(row.get("telegram_id") or "").lower()
            or search_term in str(row.get("username") or "").lower()
        ]
    for row in rows:
        row["created_at_display"] = format_local_datetime(row.get("created_at"))
        row["receipt_file_url"] = payment_receipt_file_url(row.get("receipt_file_id"))
    return rows


def upsert_user_payload(supabase: Client, telegram_id: int, payload: dict[str, Any]) -> None:
    existing = get_registered_user(supabase, telegram_id)
    if existing:
        (
            supabase.table("telegram_users")
            .update(payload)
            .eq("telegram_id", telegram_id)
            .execute()
        )
        return
    payload.setdefault("telegram_id", telegram_id)
    payload.setdefault("registered_at", now_utc_iso())
    payload.setdefault("joined_at", payload["registered_at"])
    supabase.table("telegram_users").insert(payload).execute()


def run_schema_migration(supabase: Client) -> None:
    supabase.rpc("exec_sql", {"sql": SCHEMA_MIGRATION_SQL}).execute()


def list_dashboard_users(
    supabase: Client,
    user_filter: str,
    search: str = "",
    page: int = 1,
    per_page: int = 25,
) -> dict[str, Any]:
    query = supabase.table("telegram_users").select("*")
    if user_filter == "active":
        query = query.eq("status", "active")
    elif user_filter == "pending_payments":
        query = query.eq("payment_status", "pending_review")
    elif user_filter == "paid":
        query = query.eq("payment_status", "paid")
    elif user_filter == "needs_new_receipt":
        query = query.eq("payment_status", "needs_new_receipt")
    elif user_filter == "rejected":
        query = query.eq("payment_status", "rejected")
    elif user_filter == "removed_inactive":
        query = query.eq("status", "inactive")
    elif user_filter == "confirmed":
        query = query.eq("confirmed_subscription", True)
    elif user_filter == "source_confirm_subscription":
        query = query.eq("source", CONFIRMATION_SOURCE)
    elif user_filter == "expiring_7":
        today = today_iso()
        soon = (datetime.now(APP_TIMEZONE).date() + timedelta(days=7)).isoformat()
        query = query.gte("expiry_date", today).lte("expiry_date", soon)
    elif user_filter == "expired":
        query = query.lt("expiry_date", today_iso())
    elif user_filter == "no_expiry":
        query = query.is_("expiry_date", "null")

    response = query.order("registered_at", desc=True).limit(2000).execute()
    rows = response.data or []
    if user_filter == "not_confirmed":
        rows = [row for row in rows if row.get("confirmed_subscription") is not True]
    elif user_filter == "has_payment_history":
        try:
            ids_with_history = payment_history_telegram_ids(supabase)
            rows = [row for row in rows if int(row.get("telegram_id")) in ids_with_history]
        except Exception:
            logger.warning("Could not apply has_payment_history dashboard filter", exc_info=True)
            rows = []
    search_term = search.strip().lower()
    if search_term:
        rows = [
            row
            for row in rows
            if search_term in str(row.get("telegram_id") or "").lower()
            or search_term in str(row.get("username") or "").lower()
            or search_term in str(row.get("first_name") or "").lower()
            or search_term in str(row.get("last_name") or "").lower()
        ]

    for row in rows:
        row["days_remaining"] = days_remaining(row.get("expiry_date"))
        row["joined_at_display"] = format_local_datetime(row.get("joined_at"))
        row["confirmed_at_display"] = format_local_datetime(row.get("confirmed_at"))
        row["joined_channel_at_display"] = format_local_datetime(row.get("joined_channel_at"))
        row["left_channel_at_display"] = format_local_datetime(row.get("left_channel_at"))
        row["membership_start_date_effective"] = membership_start_for_user(row).isoformat()

    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    safe_page = min(max(page, 1), total_pages)
    start = (safe_page - 1) * per_page
    end = start + per_page
    page_rows = rows[start:end]
    for row in page_rows:
        try:
            row["recent_payment_history"] = get_payment_history(supabase, int(row["telegram_id"]), limit=5)
        except Exception:
            logger.warning("Could not load recent payment history telegram_id=%s", row.get("telegram_id"), exc_info=True)
            row["recent_payment_history"] = []
    return {
        "rows": page_rows,
        "total": total,
        "page": safe_page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_previous": safe_page > 1,
        "has_next": safe_page < total_pages,
    }


def renew_user_from_today(supabase: Client, telegram_id: int) -> str:
    expiry = (datetime.now(APP_TIMEZONE).date() + timedelta(days=30)).isoformat()
    (
        supabase.table("telegram_users")
        .update(
            {
                "expiry_date": expiry,
                "status": "active",
                "payment_status": "paid",
                "last_payment_at": now_utc_iso(),
                "notes": "Renewed +30 days from today",
            }
        )
        .eq("telegram_id", telegram_id)
        .execute()
    )
    return expiry


def renew_user_from_current_expiry(supabase: Client, telegram_id: int) -> str:
    user = get_registered_user(supabase, telegram_id)
    if not user:
        raise ValueError("User not found")
    current_expiry = parse_iso_date(user.get("expiry_date"))
    if user.get("status") != "active" or not current_expiry or current_expiry < datetime.now(APP_TIMEZONE).date():
        raise ValueError("User is not active with a future expiry_date")

    expiry = (current_expiry + timedelta(days=30)).isoformat()
    (
        supabase.table("telegram_users")
        .update(
            {
                "expiry_date": expiry,
                "status": "active",
                "payment_status": "paid",
                "last_payment_at": now_utc_iso(),
                "notes": "Renewed +30 days from current expiry_date",
            }
        )
        .eq("telegram_id", telegram_id)
        .execute()
    )
    return expiry


def mark_user_paid(supabase: Client, telegram_id: int) -> None:
    (
        supabase.table("telegram_users")
        .update(
            {
                "status": "active",
                "payment_status": "paid",
                "last_payment_at": now_utc_iso(),
                "notes": "Marked paid from dashboard",
            }
        )
        .eq("telegram_id", telegram_id)
        .execute()
    )


def pending_payment_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Aprobar ✅", callback_data=f"payment:approve:{telegram_id}"),
                InlineKeyboardButton(text="Rechazar ❌", callback_data=f"payment:reject:{telegram_id}"),
            ],
            [
                InlineKeyboardButton(text="Pedir otra captura 🔁", callback_data=f"payment:ask_receipt:{telegram_id}"),
            ],
        ]
    )


async def create_one_use_invite_link(bot: Bot, settings: Settings, telegram_id: int) -> tuple[str, str]:
    timestamp = int(datetime.now(timezone.utc).timestamp())
    name = f"approved-{telegram_id}-{timestamp}"
    invite = await bot.create_chat_invite_link(
        chat_id=settings.content_channel_id,
        name=name,
        member_limit=1,
        expire_date=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return invite.invite_link, name


def has_active_unused_invite(row: dict[str, Any] | None) -> bool:
    if not row or not row.get("invite_link"):
        return False
    if row.get("invite_link_revoked") is True or row.get("invite_link_used") is True:
        return False
    created_at = parse_iso_datetime(row.get("invite_link_created_at"))
    if not created_at:
        return False
    return datetime.now(timezone.utc) - created_at < timedelta(hours=1)


def payment_recently_approved(row: dict[str, Any] | None) -> bool:
    if not row or row.get("payment_status") != "paid":
        return False
    approved_at = parse_iso_datetime(row.get("approved_at"))
    if not approved_at:
        return False
    return datetime.now(timezone.utc) - approved_at < timedelta(hours=1)


async def save_invite_link(
    supabase: Client,
    telegram_id: int,
    invite_link: str,
    invite_link_name: str,
    notes: str = "One-use invite link generated",
) -> None:
    await asyncio.to_thread(
        upsert_user_payload,
        supabase,
        telegram_id,
        {
            "telegram_id": telegram_id,
            "invite_link": invite_link,
            "invite_link_created_at": now_utc_iso(),
            "invite_link_name": invite_link_name,
            "invite_link_revoked": False,
            "invite_link_used": False,
            "notes": notes,
        },
    )


async def revoke_invite_for_user(
    bot: Bot,
    supabase: Client,
    settings: Settings,
    telegram_id: int,
    note: str,
    clear_link: bool = False,
) -> bool:
    user = await asyncio.to_thread(get_registered_user, supabase, telegram_id)
    if not user or not user.get("invite_link") or user.get("invite_link_revoked") is True:
        return False
    revoked_link = user["invite_link"]
    try:
        await bot.revoke_chat_invite_link(settings.content_channel_id, revoked_link)
        logger.info("Revoked previous invite link telegram_id=%s", telegram_id)
    except TelegramBadRequest:
        logger.warning("Could not revoke previous invite link telegram_id=%s", telegram_id, exc_info=True)
    payload: dict[str, Any] = {
        "telegram_id": telegram_id,
        "invite_link_revoked": True,
        "revoked_at": now_utc_iso(),
        "notes": note,
    }
    if clear_link:
        payload.update(
            {
                "invite_link": None,
                "invite_link_created_at": None,
                "invite_link_name": None,
                "invite_link_used": False,
            }
        )
    await asyncio.to_thread(
        upsert_user_payload,
        supabase,
        telegram_id,
        payload,
    )
    return True


async def revoke_existing_invite_link(bot: Bot, supabase: Client, settings: Settings, telegram_id: int) -> None:
    await revoke_invite_for_user(
        bot,
        supabase,
        settings,
        telegram_id,
        "Previous invite link revoked before generating a new one",
    )


async def regenerate_invite_link(bot: Bot, supabase: Client, settings: Settings, telegram_id: int) -> str:
    await revoke_existing_invite_link(bot, supabase, settings, telegram_id)
    invite_link, invite_name = await create_one_use_invite_link(bot, settings, telegram_id)
    await save_invite_link(supabase, telegram_id, invite_link, invite_name, "Invite link regenerated by admin")
    return invite_link


async def create_invite_if_no_active(bot: Bot, supabase: Client, settings: Settings, telegram_id: int) -> str:
    user = await asyncio.to_thread(get_registered_user, supabase, telegram_id)
    if has_active_unused_invite(user):
        raise ValueError("⚠️ Este usuario ya tiene un invite link activo")
    invite_link, invite_name = await create_one_use_invite_link(bot, settings, telegram_id)
    await save_invite_link(supabase, telegram_id, invite_link, invite_name, "Invite link generated by admin")
    return invite_link


async def send_invite_to_user(bot: Bot, telegram_id: int, invite_link: str) -> bool:
    try:
        await bot.send_message(
            telegram_id,
            f"Pago aprobado ✅ Aquí está tu link privado de acceso: {invite_link}\n"
            "Este link es personal y de un solo uso.",
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning("Could not DM invite link to telegram_id=%s", telegram_id, exc_info=True)
        return False


async def approve_payment(
    bot: Bot,
    supabase: Client,
    settings: Settings,
    telegram_id: int,
    admin_id: int,
) -> dict[str, Any]:
    existing_user = await asyncio.to_thread(get_registered_user, supabase, telegram_id)
    if payment_recently_approved(existing_user):
        if has_active_unused_invite(existing_user):
            invite_link = existing_user["invite_link"]
            dm_sent = await send_invite_to_user(bot, telegram_id, invite_link)
            if dm_sent:
                await bot.send_message(settings.admin_chat_id, f"Pago ya aprobado recientemente; reenvié el link existente a {telegram_id}")
            else:
                await bot.send_message(
                    settings.admin_chat_id,
                    "Pago ya aprobado recientemente. No pude reenviar el link; el usuario debe abrir el bot o escribirle primero.",
                )
            logger.warning("Duplicate approval prevented telegram_id=%s active_link_reused=true", telegram_id)
            return {"invite_link": invite_link, "duplicate": True, "reused": True}
        logger.warning("Duplicate approval prevented telegram_id=%s active_link_reused=false", telegram_id)
        raise ValueError("Payment already approved recently; not generating another invite link.")

    today = datetime.now(APP_TIMEZONE).date()
    expiry = today + timedelta(days=30)
    reused_invite = has_active_unused_invite(existing_user)
    if reused_invite:
        invite_link = existing_user["invite_link"]
        invite_name = existing_user.get("invite_link_name") or f"existing-{telegram_id}"
    else:
        invite_link, invite_name = await create_one_use_invite_link(bot, settings, telegram_id)
    approval_payload = {
        "telegram_id": telegram_id,
        "status": "active",
        "payment_status": "paid",
        "approved_by_admin_id": admin_id,
        "approved_at": now_utc_iso(),
        "membership_start_date": today.isoformat(),
        "expiry_date": expiry.isoformat(),
        "last_payment_at": now_utc_iso(),
        "invite_link": invite_link,
        "invite_link_name": invite_name,
        "invite_link_revoked": False,
        "invite_link_used": False,
        "notes": "Payment approved by admin",
    }
    if not reused_invite:
        approval_payload["invite_link_created_at"] = now_utc_iso()
    await asyncio.to_thread(
        upsert_user_payload,
        supabase,
        telegram_id,
        approval_payload,
    )
    await asyncio.to_thread(
        insert_payment_history,
        supabase,
        telegram_id,
        "approved",
        "paid",
        admin_id,
        existing_user.get("pending_payment_file_id") if existing_user else None,
        existing_user.get("pending_payment_file_type") if existing_user else None,
        invite_link,
        today.isoformat(),
        expiry.isoformat(),
        True,
        "Payment approved by admin",
        existing_user.get("username") if existing_user else None,
        existing_user.get("first_name") if existing_user else None,
    )
    dm_sent = await send_invite_to_user(bot, telegram_id, invite_link)
    if dm_sent:
        await bot.send_message(settings.admin_chat_id, f"Pago aprobado y link enviado a {telegram_id}")
    else:
        await bot.send_message(
            settings.admin_chat_id,
            "No pude enviar el link. El usuario debe abrir el bot o escribirle primero.",
        )
    logger.info("Payment approved telegram_id=%s admin_id=%s dm_sent=%s", telegram_id, admin_id, dm_sent)
    return {"invite_link": invite_link, "duplicate": False, "reused": reused_invite}


async def reject_payment(
    bot: Bot,
    supabase: Client,
    settings: Settings,
    telegram_id: int,
    admin_id: int | None = None,
) -> None:
    await asyncio.to_thread(
        upsert_user_payload,
        supabase,
        telegram_id,
        {
            "telegram_id": telegram_id,
            "payment_status": "rejected",
            "rejected_at": now_utc_iso(),
            "notes": "Payment rejected by admin",
        },
    )
    try:
        await bot.send_message(
            telegram_id,
            "Tu comprobante no pudo ser validado. Revisa la información y vuelve a intentarlo.",
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning("Could not DM rejection to telegram_id=%s", telegram_id, exc_info=True)
    await bot.send_message(settings.admin_chat_id, f"Comprobante rechazado para {telegram_id}")
    logger.info("Payment rejected telegram_id=%s", telegram_id)


async def ask_new_receipt(
    bot: Bot,
    supabase: Client,
    settings: Settings,
    telegram_id: int,
    admin_id: int | None = None,
) -> None:
    await asyncio.to_thread(
        upsert_user_payload,
        supabase,
        telegram_id,
        {
            "telegram_id": telegram_id,
            "payment_status": "needs_new_receipt",
            "needs_new_receipt_at": now_utc_iso(),
            "notes": "Admin requested another receipt",
        },
    )
    try:
        await bot.send_message(telegram_id, "Por favor envía otra captura más clara del comprobante.")
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning("Could not DM receipt request to telegram_id=%s", telegram_id, exc_info=True)
    await bot.send_message(settings.admin_chat_id, f"Se pidió otra captura a {telegram_id}")
    logger.info("New receipt requested telegram_id=%s", telegram_id)


async def create_or_send_existing_invite(
    bot: Bot,
    supabase: Client,
    settings: Settings,
    telegram_id: int,
) -> str:
    user = await asyncio.to_thread(get_registered_user, supabase, telegram_id)
    invite_link = user.get("invite_link") if has_active_unused_invite(user) else None
    if not invite_link:
        invite_link, invite_name = await create_one_use_invite_link(bot, settings, telegram_id)
        await save_invite_link(supabase, telegram_id, invite_link, invite_name, "Invite link generated by admin")
    sent = await send_invite_to_user(bot, telegram_id, invite_link)
    if not sent:
        await bot.send_message(
            settings.admin_chat_id,
            "No pude enviar el link. El usuario debe abrir el bot o escribirle primero.",
        )
    logger.info("Invite send attempted telegram_id=%s sent=%s", telegram_id, sent)
    return invite_link


def expired_active_users(supabase: Client) -> list[dict[str, Any]]:
    response = (
        supabase.table("telegram_users")
        .select("*")
        .eq("status", "active")
        .lt("expiry_date", today_iso())
        .execute()
    )
    return response.data or []


async def remove_user_from_channel(
    bot: Bot,
    supabase: Client,
    settings: Settings,
    telegram_id: int,
    reason: str,
) -> None:
    await bot.ban_chat_member(chat_id=settings.content_channel_id, user_id=telegram_id)
    await bot.unban_chat_member(
        chat_id=settings.content_channel_id,
        user_id=telegram_id,
        only_if_banned=True,
    )
    await asyncio.to_thread(
        upsert_user_payload,
        supabase,
        telegram_id,
        {
            "telegram_id": telegram_id,
            "status": "inactive",
            "removed_at": now_utc_iso(),
            "removal_reason": reason,
            "notes": "Removed from channel",
        },
    )
    logger.info("Removed telegram_id=%s reason=%s", telegram_id, reason)


def mark_user_inactive(supabase: Client, telegram_id: int, notes: str = "Marked inactive from dashboard") -> None:
    (
        supabase.table("telegram_users")
        .update({"status": "inactive", "notes": notes})
        .eq("telegram_id", telegram_id)
        .execute()
    )


def set_membership_start_date(supabase: Client, telegram_id: int, start_date: str) -> str:
    parsed = datetime.strptime(start_date, DATE_FORMAT).date()
    expiry = (parsed + timedelta(days=30)).isoformat()
    (
        supabase.table("telegram_users")
        .update(
            {
                "membership_start_date": parsed.isoformat(),
                "expiry_date": expiry,
                "status": "active",
                "notes": "Membership start date set from dashboard",
            }
        )
        .eq("telegram_id", telegram_id)
        .execute()
    )
    return expiry


def update_user_notes(supabase: Client, telegram_id: int, notes: str) -> None:
    (
        supabase.table("telegram_users")
        .update({"notes": notes})
        .eq("telegram_id", telegram_id)
        .execute()
    )


def update_user_invite_link(supabase: Client, telegram_id: int, invite_link: str) -> None:
    (
        supabase.table("telegram_users")
        .update(
            {
                "invite_link": invite_link,
                "invite_link_created_at": now_utc_iso(),
                "notes": "Generated one-use invite link from dashboard",
            }
        )
        .eq("telegram_id", telegram_id)
        .execute()
    )


def set_confirmation_status(supabase: Client, telegram_id: int, confirmed: bool) -> None:
    payload: dict[str, Any] = {
        "confirmed_subscription": confirmed,
        "notes": "Marked confirmed manually" if confirmed else "Marked not confirmed manually",
    }
    if confirmed:
        payload.update(
            {
                "confirmed_at": now_utc_iso(),
                "confirmation_campaign": "manual_dashboard",
                "source": "manual_dashboard",
                "status": "active",
            }
        )
    else:
        payload["confirmed_at"] = None

    (
        supabase.table("telegram_users")
        .update(payload)
        .eq("telegram_id", telegram_id)
        .execute()
    )


def mark_user_removed(supabase: Client, telegram_id: int) -> None:
    (
        supabase.table("telegram_users")
        .update(
            {
                "status": "inactive",
                "removed_at": now_utc_iso(),
                "removal_reason": "dashboard_remove",
                "notes": "Removed from channel from dashboard",
            }
        )
        .eq("telegram_id", telegram_id)
        .execute()
    )


def build_cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CTA_BUTTON_TEXT,
                    callback_data=CTA_CALLBACK_DATA,
                )
            ]
        ]
    )


def build_confirm_subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CONFIRM_SUBSCRIPTION_BUTTON_TEXT,
                    callback_data=CONFIRM_SUBSCRIPTION_CALLBACK_DATA,
                )
            ]
        ]
    )


def upsert_cta_user(supabase: Client, callback_query: CallbackQuery) -> None:
    if not callback_query.from_user:
        raise ValueError("Callback query has no from_user")

    user = callback_query.from_user
    existing = get_registered_user(supabase, user.id)
    joined_at = now_utc_iso()
    membership_start_date = datetime.now(APP_TIMEZONE).date()
    expiry_date = (membership_start_date + timedelta(days=30)).isoformat()
    payload = {
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "status": "active",
        "payment_status": "unpaid",
        "notes": CTA_NOTES,
    }

    if existing:
        if not existing.get("joined_at"):
            payload["joined_at"] = joined_at
        if not existing.get("membership_start_date"):
            payload["membership_start_date"] = membership_start_for_user(existing).isoformat()
        if not existing.get("expiry_date"):
            payload["expiry_date"] = (membership_start_for_user({**existing, **payload}) + timedelta(days=30)).isoformat()
        (
            supabase.table("telegram_users")
            .update(payload)
            .eq("telegram_id", user.id)
            .execute()
        )
        logger.info("Updated CTA user telegram_id=%s", user.id)
        return

    payload["registered_at"] = joined_at
    payload["joined_at"] = joined_at
    payload["membership_start_date"] = membership_start_date.isoformat()
    payload["expiry_date"] = expiry_date
    supabase.table("telegram_users").insert(payload).execute()
    logger.info("Registered CTA user telegram_id=%s", user.id)


def upsert_confirmed_subscription_user(supabase: Client, callback_query: CallbackQuery) -> None:
    if not callback_query.from_user:
        raise ValueError("Callback query has no from_user")

    user = callback_query.from_user
    existing = get_registered_user(supabase, user.id)
    now = now_utc_iso()
    membership_start_date = datetime.now(APP_TIMEZONE).date()
    payload: dict[str, Any] = {
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "status": "active",
        "confirmed_subscription": True,
        "confirmed_at": now,
        "confirmation_campaign": CONFIRMATION_CAMPAIGN,
        "source": CONFIRMATION_SOURCE,
    }

    if existing:
        if not existing.get("joined_at"):
            payload["joined_at"] = now
        if not existing.get("registered_at"):
            payload["registered_at"] = now
        if not existing.get("membership_start_date"):
            payload["membership_start_date"] = membership_start_date.isoformat()
        if not existing.get("expiry_date"):
            payload["expiry_date"] = (membership_start_date + timedelta(days=30)).isoformat()
        (
            supabase.table("telegram_users")
            .update(payload)
            .eq("telegram_id", user.id)
            .execute()
        )
        logger.info("Confirmed subscription for telegram_id=%s", user.id)
        return

    payload["registered_at"] = now
    payload["joined_at"] = now
    payload["membership_start_date"] = membership_start_date.isoformat()
    payload["expiry_date"] = (membership_start_date + timedelta(days=30)).isoformat()
    supabase.table("telegram_users").insert(payload).execute()
    logger.info("Registered confirmed subscription user telegram_id=%s", user.id)


@router.message(Command("send_poll"))
async def send_poll(message: Message, settings: Settings) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return

    try:
        await message.bot.send_message(
            chat_id=settings.content_channel_id,
            text=CTA_MESSAGE,
            reply_markup=build_cta_keyboard(),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.exception("Could not send CTA message")
        await message.answer(f"No pude enviar el mensaje: {exc}")
        return

    await message.answer("Mensaje enviado al canal.")


@router.message(Command("send_confirm_subscription"))
async def send_confirm_subscription(message: Message, settings: Settings) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return

    try:
        await message.bot.send_message(
            chat_id=settings.content_channel_id,
            text=CONFIRM_SUBSCRIPTION_MESSAGE,
            reply_markup=build_confirm_subscription_keyboard(),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.exception("Could not send subscription confirmation message")
        await message.answer(f"No pude enviar el mensaje de confirmación: {exc}")
        return

    await message.answer("Mensaje de confirmación enviado al canal.")


@router.message(F.chat.type == "private", (F.photo | F.document))
async def receive_payment_receipt(message: Message, settings: Settings, supabase: Client) -> None:
    if message.from_user and message.from_user.id in settings.admin_user_ids:
        return
    if not message.from_user:
        return

    now = now_utc_iso()
    file_type = "photo" if message.photo else "document"
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    user = message.from_user
    existing_user = await asyncio.to_thread(get_registered_user, supabase, user.id)
    payload = {
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "payment_status": "pending_review",
        "pending_payment_file_id": file_id,
        "pending_payment_file_type": file_type,
        "pending_payment_at": now,
        "source": "payment_receipt_private_bot",
        "last_seen_at": now,
        "notes": "Payment receipt submitted privately",
    }
    await asyncio.to_thread(upsert_user_payload, supabase, user.id, payload)
    if existing_user and existing_user.get("payment_status") == "pending_review":
        await message.answer(
            "Tu comprobante anterior fue actualizado ✅\n"
            "Estamos validando tu pago y pronto recibirás tu acceso."
        )
        logger.info("Updated existing pending payment receipt telegram_id=%s", user.id)
        return

    await message.answer("Comprobante recibido ✅ Lo revisaremos manualmente.")

    username = f"@{user.username}" if user.username else "-"
    admin_text = (
        "Nuevo comprobante pendiente\n"
        f"telegram_id: {user.id}\n"
        f"username: {username}\n"
        f"first_name: {user.first_name or '-'}\n"
        f"last_name: {user.last_name or '-'}\n"
        f"pending_payment_at: {now}"
    )
    try:
        await message.bot.copy_message(
            chat_id=settings.admin_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await message.bot.send_message(
            settings.admin_chat_id,
            admin_text,
            reply_markup=pending_payment_keyboard(user.id),
        )
        logger.info("Payment receipt submitted telegram_id=%s", user.id)
    except Exception:
        logger.exception("Could not notify admin about payment receipt telegram_id=%s", user.id)


@router.message(Command("users"))
async def users(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return

    try:
        count_response = (
            supabase.table("telegram_users")
            .select("*", count="exact")
            .execute()
        )
        latest_response = (
            supabase.table("telegram_users")
            .select("*")
            .order("registered_at", desc=True)
            .limit(10)
            .execute()
        )
    except Exception as exc:
        logger.exception("Could not fetch users")
        await message.answer(f"No pude consultar usuarios: {exc}")
        return

    total = count_response.count or 0
    latest = latest_response.data or []
    lines = [f"Usuarios registrados: {total}", "", "Últimos 10:"]
    lines.extend(format_user(row) for row in latest)
    if not latest:
        lines.append("Sin usuarios registrados todavía.")
    await send_long_message(message, "\n".join(lines))


def command_telegram_id(message: Message) -> int | None:
    parts = (message.text or "").split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


@router.message(Command("unconfirmed"))
async def unconfirmed(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return

    try:
        response = (
            supabase.table("telegram_users")
            .select("*")
            .order("registered_at", desc=True)
            .limit(500)
            .execute()
        )
    except Exception as exc:
        logger.exception("Could not fetch unconfirmed users")
        await message.answer(f"No pude consultar no confirmados: {exc}")
        return

    rows = [row for row in (response.data or []) if row.get("confirmed_subscription") is not True]
    lines = [f"Usuarios sin confirmación: {len(rows)}"]
    lines.extend(format_user(row) for row in rows[:50])
    if len(rows) > 50:
        lines.append(f"...y {len(rows) - 50} más.")
    if not rows:
        lines.append("Todos los usuarios están confirmados.")
    await send_long_message(message, "\n".join(lines))


@router.message(Command("pending_payments"))
async def pending_payments(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    try:
        response = (
            supabase.table("telegram_users")
            .select("*")
            .eq("payment_status", "pending_review")
            .order("pending_payment_at", desc=True)
            .limit(100)
            .execute()
        )
    except Exception as exc:
        logger.exception("Could not fetch pending payments")
        await message.answer(f"No pude consultar pagos pendientes: {exc}")
        return
    rows = response.data or []
    lines = [f"Pagos pendientes: {len(rows)}"]
    lines.extend(format_user(row) for row in rows)
    if not rows:
        lines.append("No hay pagos pendientes.")
    await send_long_message(message, "\n".join(lines))


@router.message(Command("user"))
async def user_record(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    telegram_id = command_telegram_id(message)
    if telegram_id is None:
        await message.answer("Uso: /user <telegram_id>")
        return
    row = await asyncio.to_thread(get_registered_user, supabase, telegram_id)
    await send_long_message(message, format_user_record(row or {}))


@router.message(Command("payment_history"))
async def payment_history_command(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    telegram_id = command_telegram_id(message)
    if telegram_id is None:
        await message.answer("Uso: /payment_history <telegram_id>")
        return
    try:
        rows = await asyncio.to_thread(get_payment_history, supabase, telegram_id, 10)
    except Exception as exc:
        logger.exception("Could not fetch payment history telegram_id=%s", telegram_id)
        await message.answer(f"No pude consultar historial de pagos: {exc}")
        return
    await send_long_message(message, f"Historial de pagos para {telegram_id}:\n{format_payment_history_rows(rows)}")


@router.message(Command("send_invite"))
async def send_invite(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    telegram_id = command_telegram_id(message)
    if telegram_id is None:
        await message.answer("Uso: /send_invite <telegram_id>")
        return
    try:
        await create_or_send_existing_invite(message.bot, supabase, settings, telegram_id)
        await message.answer(f"Link enviado o guardado para {telegram_id}.")
    except Exception as exc:
        logger.exception("Could not send invite telegram_id=%s", telegram_id)
        await message.answer(f"No pude enviar/generar link: {exc}")


@router.message(Command("revoke_invite"))
async def revoke_invite(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    telegram_id = command_telegram_id(message)
    if telegram_id is None:
        await message.answer("Uso: /revoke_invite <telegram_id>")
        return

    revoked = await revoke_invite_for_user(
        message.bot,
        supabase,
        settings,
        telegram_id,
        "Invite link revoked by admin",
    )
    if not revoked:
        await message.answer("No invite link found.")
        return
    await message.answer("Invite link revoked.")


@router.message(Command("revoke_user"))
async def revoke_user(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    telegram_id = command_telegram_id(message)
    if telegram_id is None:
        await message.answer("Uso: /revoke_user <telegram_id>")
        return

    revoked = await revoke_invite_for_user(
        message.bot,
        supabase,
        settings,
        telegram_id,
        "Latest invite link revoked for user by admin",
        clear_link=True,
    )
    if not revoked:
        await message.answer("No invite link found.")
        return
    await message.answer(f"✅ Último link revocado para usuario {telegram_id}")


@router.message(Command("revoke_link"))
async def revoke_link(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await message.answer("Uso: /revoke_link <invite_link_name>")
        return
    invite_link_name = parts[1].strip()
    user = await asyncio.to_thread(get_user_by_invite_link_name, supabase, invite_link_name)
    invite_link = user.get("invite_link") if user else None
    if not user or not invite_link:
        await message.answer("No invite link found.")
        return

    try:
        await message.bot.revoke_chat_invite_link(
            chat_id=settings.content_channel_id,
            invite_link=invite_link,
        )
        await asyncio.to_thread(
            upsert_user_payload,
            supabase,
            int(user["telegram_id"]),
            {
                "telegram_id": int(user["telegram_id"]),
                "invite_link_revoked": True,
                "revoked_at": now_utc_iso(),
                "notes": "Invite link revoked by admin",
            },
        )
    except Exception as exc:
        logger.exception("Could not revoke invite by name invite_link_name=%s", invite_link_name)
        await message.answer(f"No pude revocar el link: {exc}")
        return

    await message.answer("✅ Link revocado correctamente")


@router.message(Command("approve"))
async def approve_command(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    telegram_id = command_telegram_id(message)
    if telegram_id is None or not message.from_user:
        await message.answer("Uso: /approve <telegram_id>")
        return
    try:
        result = await approve_payment(message.bot, supabase, settings, telegram_id, message.from_user.id)
        if result.get("duplicate"):
            await message.answer(f"Pago ya aprobado recientemente; reenvié link existente para {telegram_id}.")
        else:
            await message.answer(f"Pago aprobado para {telegram_id}.")
    except Exception as exc:
        logger.exception("Could not approve telegram_id=%s", telegram_id)
        await message.answer(f"No pude aprobar: {exc}")


@router.message(Command("reject"))
async def reject_command(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    telegram_id = command_telegram_id(message)
    if telegram_id is None:
        await message.answer("Uso: /reject <telegram_id>")
        return
    await reject_payment(message.bot, supabase, settings, telegram_id, message.from_user.id if message.from_user else None)
    await message.answer(f"Pago rechazado para {telegram_id}.")


@router.message(Command("ask_receipt"))
async def ask_receipt_command(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    telegram_id = command_telegram_id(message)
    if telegram_id is None:
        await message.answer("Uso: /ask_receipt <telegram_id>")
        return
    await ask_new_receipt(message.bot, supabase, settings, telegram_id, message.from_user.id if message.from_user else None)
    await message.answer(f"Se pidió otra captura a {telegram_id}.")


@router.message(Command("set_expiry"))
async def set_expiry(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Uso: /set_expiry <telegram_id> <YYYY-MM-DD>")
        return

    try:
        telegram_id = int(parts[1])
        expiry = datetime.strptime(parts[2], DATE_FORMAT).date().isoformat()
    except ValueError:
        await message.answer("Formato inválido. Usa: /set_expiry <telegram_id> <YYYY-MM-DD>")
        return

    try:
        existing = get_registered_user(supabase, telegram_id)
        if not existing:
            await message.answer("Usuario no encontrado en telegram_users.")
            return
        (
            supabase.table("telegram_users")
            .update({"expiry_date": expiry})
            .eq("telegram_id", telegram_id)
            .execute()
        )
    except Exception as exc:
        logger.exception("Could not set expiry for telegram_id=%s", telegram_id)
        await message.answer(f"No pude actualizar la fecha: {exc}")
        return

    await message.answer(f"Vencimiento actualizado: {telegram_id} -> {expiry}")


@router.message(Command("expired"))
async def expired(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return

    try:
        rows = await asyncio.to_thread(expired_active_users, supabase)
    except Exception as exc:
        logger.exception("Could not fetch expired users")
        await message.answer(f"No pude consultar expirados: {exc}")
        return

    lines = [f"Usuarios activos expirados al {today_iso()}: {len(rows)}"]
    lines.extend(format_user(row) for row in rows)
    if not rows:
        lines.append("No hay usuarios activos expirados.")
    await send_long_message(message, "\n".join(lines))


@router.message(Command("remove_expired_preview"))
async def remove_expired_preview(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    rows = await asyncio.to_thread(expired_active_users, supabase)
    lines = [f"Usuarios que serían removidos: {len(rows)}"]
    lines.extend(format_user(row) for row in rows)
    if not rows:
        lines.append("No hay usuarios para remover.")
    await send_long_message(message, "\n".join(lines))


@router.message(Command("remove_expired_confirm"))
async def remove_expired_confirm(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return
    rows = await asyncio.to_thread(expired_active_users, supabase)
    removed = 0
    errors: list[str] = []
    for row in rows:
        telegram_id = int(row["telegram_id"])
        try:
            await remove_user_from_channel(message.bot, supabase, settings, telegram_id, "expired_manual_confirm")
            removed += 1
        except Exception as exc:
            logger.exception("Could not remove expired telegram_id=%s", telegram_id)
            errors.append(f"{telegram_id}: {exc}")
    text = f"Removidos: {removed}/{len(rows)}"
    if errors:
        text += "\nErrores:\n" + "\n".join(errors[:10])
    await send_long_message(message, text)


@router.message(Command("sync_schema"))
async def sync_schema(message: Message, settings: Settings, supabase: Client) -> None:
    if not is_admin(message, settings):
        await reject_non_admin(message)
        return

    try:
        await asyncio.to_thread(run_schema_migration, supabase)
        await message.answer("Schema sincronizado correctamente.")
    except Exception as exc:
        logger.exception("Could not sync schema")
        await send_long_message(
            message,
            "No pude ejecutar la migración automáticamente. "
            "Crea un RPC seguro llamado exec_sql o ejecuta este SQL en Supabase:\n\n"
            f"```sql\n{SCHEMA_MIGRATION_SQL}\n```\n\n"
            f"Error: {exc}",
        )


@router.callback_query(F.data == CTA_CALLBACK_DATA)
async def want_more_content(callback_query: CallbackQuery, supabase: Client) -> None:
    try:
        upsert_cta_user(supabase, callback_query)
        await callback_query.answer("Listo 🔥")
    except Exception:
        user_id = callback_query.from_user.id if callback_query.from_user else "unknown"
        logger.exception("Could not process CTA callback for user_id=%s", user_id)
        await callback_query.answer("Intenta de nuevo.", show_alert=False)


@router.callback_query(F.data == CONFIRM_SUBSCRIPTION_CALLBACK_DATA)
async def confirm_subscription(callback_query: CallbackQuery, supabase: Client) -> None:
    try:
        upsert_confirmed_subscription_user(supabase, callback_query)
        await callback_query.answer("Suscripción confirmada ✅")
    except Exception:
        user_id = callback_query.from_user.id if callback_query.from_user else "unknown"
        logger.exception("Could not process subscription confirmation for user_id=%s", user_id)
        await callback_query.answer("Intenta de nuevo.", show_alert=False)


@router.callback_query(F.data.startswith("payment:"))
async def payment_admin_callback(callback_query: CallbackQuery, settings: Settings, supabase: Client) -> None:
    if not is_admin_id(callback_query.from_user.id if callback_query.from_user else None, settings):
        await callback_query.answer("No autorizado.", show_alert=True)
        return
    parts = (callback_query.data or "").split(":")
    if len(parts) != 3:
        await callback_query.answer("Acción inválida.", show_alert=True)
        return
    action = parts[1]
    try:
        telegram_id = int(parts[2])
    except ValueError:
        await callback_query.answer("Usuario inválido.", show_alert=True)
        return

    try:
        if action == "approve":
            result = await approve_payment(callback_query.bot, supabase, settings, telegram_id, callback_query.from_user.id)
            if result.get("duplicate"):
                await callback_query.answer("Ya estaba aprobado; reenvié el link existente.", show_alert=True)
            else:
                await callback_query.answer("Aprobado ✅")
        elif action == "reject":
            await reject_payment(callback_query.bot, supabase, settings, telegram_id, callback_query.from_user.id)
            await callback_query.answer("Rechazado ❌")
        elif action == "ask_receipt":
            await ask_new_receipt(callback_query.bot, supabase, settings, telegram_id, callback_query.from_user.id)
            await callback_query.answer("Solicitud enviada 🔁")
        else:
            await callback_query.answer("Acción inválida.", show_alert=True)
            return
    except Exception as exc:
        logger.exception("Payment admin action failed action=%s telegram_id=%s", action, telegram_id)
        await callback_query.answer(f"Error: {exc}", show_alert=True)


@router.chat_member()
async def track_channel_membership(update: ChatMemberUpdated, settings: Settings, supabase: Client) -> None:
    channel_matches = update.chat.id == settings.content_channel_id
    if isinstance(settings.content_channel_id, str):
        channel_matches = update.chat.username and f"@{update.chat.username}" == settings.content_channel_id
    if not channel_matches:
        return

    user = update.new_chat_member.user
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    now = now_utc_iso()
    active_statuses = {"member", "administrator", "creator"}
    left_statuses = {"left", "kicked"}
    payload = {
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "last_seen_at": now,
    }
    if new_status in active_statuses and old_status not in active_statuses:
        payload.update(
            {
                "joined_channel_at": now,
                "status": "active",
                "source": "channel_join",
                "invite_link_used": True,
            }
        )
    elif new_status in left_statuses:
        existing = await asyncio.to_thread(get_registered_user, supabase, user.id)
        current = bool(existing and existing.get("payment_status") == "paid" and days_remaining(existing.get("expiry_date")) is not None and days_remaining(existing.get("expiry_date")) >= 0)
        payload.update(
            {
                "left_channel_at": now,
                "notes": "Left channel or removed",
            }
        )
        if not current:
            payload["status"] = "inactive"
    else:
        return
    await asyncio.to_thread(upsert_user_payload, supabase, user.id, payload)
    logger.info("Tracked channel membership telegram_id=%s old=%s new=%s", user.id, old_status, new_status)


@router.my_chat_member()
async def track_bot_channel_membership(update: ChatMemberUpdated, settings: Settings) -> None:
    channel_matches = update.chat.id == settings.content_channel_id
    if isinstance(settings.content_channel_id, str):
        channel_matches = update.chat.username and f"@{update.chat.username}" == settings.content_channel_id
    if channel_matches:
        logger.info(
            "Bot membership changed in content channel old=%s new=%s",
            update.old_chat_member.status,
            update.new_chat_member.status,
        )


@router.error()
async def handle_error(event: ErrorEvent) -> None:
    logger.exception("Unhandled update error: %s", event.exception)


async def notify_expiring_today(bot: Bot, supabase: Client, settings: Settings) -> None:
    today = datetime.now(APP_TIMEZONE).date()
    try:
        sections: list[str] = []
        for notice_day in settings.renewal_notice_days:
            target = (today + timedelta(days=notice_day)).isoformat()
            column = f"renewal_notice_{notice_day}d_sent_at"
            response = (
                supabase.table("telegram_users")
                .select("*")
                .eq("status", "active")
                .eq("expiry_date", target)
                .is_(column, "null")
                .execute()
            )
            rows = response.data or []
            if rows:
                sections.append(f"Expiran en {notice_day} días ({target}): {len(rows)}")
                sections.extend(format_user(row) for row in rows)
                ids = [row["telegram_id"] for row in rows]
                (
                    supabase.table("telegram_users")
                    .update({column: now_utc_iso()})
                    .in_("telegram_id", ids)
                    .execute()
                )

        expired_rows = await asyncio.to_thread(expired_active_users, supabase)
        sections.append(f"Usuarios activos expirados: {len(expired_rows)}")
        sections.extend(format_user(row) for row in expired_rows)

        if settings.auto_remove_expired and expired_rows:
            removed = 0
            for row in expired_rows:
                try:
                    await remove_user_from_channel(
                        bot,
                        supabase,
                        settings,
                        int(row["telegram_id"]),
                        "expired_auto_remove",
                    )
                    removed += 1
                except Exception:
                    logger.exception("Auto remove failed telegram_id=%s", row.get("telegram_id"))
            sections.append(f"AUTO_REMOVE_EXPIRED=true, removidos: {removed}/{len(expired_rows)}")
        elif expired_rows:
            sections.append("AUTO_REMOVE_EXPIRED=false, no se removió a nadie.")

        text = "\n".join(sections) if sections else f"Sin avisos de renovación para {today.isoformat()}."
        await bot.send_message(settings.admin_chat_id, text[:3900])
        logger.info("Sent daily renewal notification")
    except Exception:
        logger.exception("Could not send daily renewal notification")


async def run_telegram_bot(bot: Bot, supabase: Client, settings: Settings) -> None:
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=APP_TIMEZONE)
    scheduler.add_job(
        notify_expiring_today,
        CronTrigger(hour=9, minute=0, timezone=APP_TIMEZONE),
        args=[bot, supabase, settings],
        id="daily_expiry_notification",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()

    logger.info("Starting Telegram bot polling")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            settings=settings,
            supabase=supabase,
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def dashboard_redirect(
    user_filter: str = "all",
    search: str = "",
    page: int = 1,
    message: str | None = None,
    error: str | None = None,
    invite_link: str | None = None,
) -> RedirectResponse:
    params: dict[str, Any] = {"filter": user_filter, "page": page}
    if search:
        params["search"] = search
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    if invite_link:
        params["invite_link"] = invite_link
    return RedirectResponse(url=f"/dashboard?{urlencode(params)}", status_code=303)


def create_web_app(settings: Settings, supabase: Client, bot: Bot) -> FastAPI:
    templates = Jinja2Templates(directory="templates")
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key=f"{settings.bot_token}:{settings.admin_password}",
        same_site="lax",
        https_only=bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT_NAME")),
    )

    def is_logged_in(request: Request) -> bool:
        return bool(request.session.get("admin_authenticated"))

    @app.get("/health", response_model=None)
    async def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse, response_model=None)
    async def root(request: Request):
        if is_logged_in(request):
            return RedirectResponse(url="/dashboard", status_code=303)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse, response_model=None)
    async def login_page(request: Request):
        if is_logged_in(request):
            return RedirectResponse(url="/dashboard", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})

    @app.post("/login", response_model=None)
    async def login(request: Request, password: str = Form(...)):
        if secrets.compare_digest(password, settings.admin_password):
            request.session["admin_authenticated"] = True
            return RedirectResponse(url="/dashboard?message=Login%20successful", status_code=303)

        logger.warning("Failed dashboard login")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Invalid password"},
            status_code=401,
        )

    @app.post("/logout", response_model=None)
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/dashboard", response_class=HTMLResponse, response_model=None)
    async def dashboard(
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
        message: str | None = None,
        error: str | None = None,
        invite_link: str | None = None,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)

        safe_filter = (
            filter
            if filter
            in {
                "all",
                "active",
                "pending_payments",
                "paid",
                "needs_new_receipt",
                "rejected",
                "removed_inactive",
                "confirmed",
                "not_confirmed",
                "source_confirm_subscription",
                "expiring_7",
                "expired",
                "no_expiry",
                "has_payment_history",
            }
            else "all"
        )
        try:
            page_data = await asyncio.to_thread(list_dashboard_users, supabase, safe_filter, search, page)
        except Exception as exc:
            logger.exception("Could not load dashboard users")
            page_data = {
                "rows": [],
                "total": 0,
                "page": 1,
                "per_page": 25,
                "total_pages": 1,
                "has_previous": False,
                "has_next": False,
            }
            error = f"Could not load users: {exc}"

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "users": page_data["rows"],
                "active_filter": safe_filter,
                "search": search,
                "page": page_data["page"],
                "per_page": page_data["per_page"],
                "total_users": page_data["total"],
                "total_pages": page_data["total_pages"],
                "has_previous": page_data["has_previous"],
                "has_next": page_data["has_next"],
                "message": message,
                "error": error,
                "invite_link": invite_link,
                "today": today_iso(),
            },
        )

    @app.post("/dashboard/users/{telegram_id}/renew/today", response_model=None)
    async def dashboard_renew_today(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            expiry = await asyncio.to_thread(renew_user_from_today, supabase, telegram_id)
            return dashboard_redirect(
                filter,
                search,
                page,
                message=f"Renewed from today. New expiry: {expiry} for {telegram_id}.",
            )
        except Exception as exc:
            logger.exception("Could not renew from today for telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not renew user: {exc}")

    @app.post("/dashboard/users/{telegram_id}/renew/current-expiry", response_model=None)
    async def dashboard_renew_current_expiry(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            expiry = await asyncio.to_thread(renew_user_from_current_expiry, supabase, telegram_id)
            return dashboard_redirect(
                filter,
                search,
                page,
                message=f"Renewed from current expiry_date. New expiry: {expiry} for {telegram_id}.",
            )
        except Exception as exc:
            logger.exception("Could not renew from current expiry for telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not renew from current expiry_date: {exc}")

    @app.post("/dashboard/users/{telegram_id}/paid", response_model=None)
    async def dashboard_mark_paid(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            await asyncio.to_thread(mark_user_paid, supabase, telegram_id)
            return dashboard_redirect(filter, search, page, message=f"User {telegram_id} marked paid.")
        except Exception as exc:
            logger.exception("Could not mark paid telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not mark paid: {exc}")

    @app.post("/dashboard/users/{telegram_id}/membership-start", response_model=None)
    async def dashboard_set_membership_start(
        telegram_id: int,
        request: Request,
        membership_start_date: str = Form(...),
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            expiry = await asyncio.to_thread(
                set_membership_start_date,
                supabase,
                telegram_id,
                membership_start_date,
            )
            return dashboard_redirect(
                filter,
                search,
                page,
                message=f"Membership start updated. New expiry: {expiry} for {telegram_id}.",
            )
        except Exception as exc:
            logger.exception("Could not set membership start for telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not set membership start date: {exc}")

    @app.post("/dashboard/users/{telegram_id}/approve-payment", response_model=None)
    async def dashboard_approve_payment(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            result = await approve_payment(bot, supabase, settings, telegram_id, next(iter(settings.admin_user_ids)))
            if result.get("duplicate"):
                return dashboard_redirect(filter, search, page, message=f"Payment was already approved recently; existing link resent for {telegram_id}.")
            return dashboard_redirect(filter, search, page, message=f"Payment approved for {telegram_id}.")
        except Exception as exc:
            logger.exception("Dashboard approve failed telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not approve payment: {exc}")

    @app.post("/dashboard/users/{telegram_id}/reject-payment", response_model=None)
    async def dashboard_reject_payment(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        await reject_payment(bot, supabase, settings, telegram_id, next(iter(settings.admin_user_ids)))
        return dashboard_redirect(filter, search, page, message=f"Payment rejected for {telegram_id}.")

    @app.post("/dashboard/users/{telegram_id}/ask-receipt", response_model=None)
    async def dashboard_ask_receipt(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        await ask_new_receipt(bot, supabase, settings, telegram_id, next(iter(settings.admin_user_ids)))
        return dashboard_redirect(filter, search, page, message=f"Requested another receipt from {telegram_id}.")

    @app.post("/dashboard/users/{telegram_id}/confirmed", response_model=None)
    async def dashboard_mark_confirmed(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            await asyncio.to_thread(set_confirmation_status, supabase, telegram_id, True)
            return dashboard_redirect(filter, search, page, message=f"User {telegram_id} marked confirmed.")
        except Exception as exc:
            logger.exception("Could not mark confirmed telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not mark confirmed: {exc}")

    @app.post("/dashboard/users/{telegram_id}/not-confirmed", response_model=None)
    async def dashboard_mark_not_confirmed(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            await asyncio.to_thread(set_confirmation_status, supabase, telegram_id, False)
            return dashboard_redirect(filter, search, page, message=f"User {telegram_id} marked not confirmed.")
        except Exception as exc:
            logger.exception("Could not mark not confirmed telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not mark not confirmed: {exc}")

    @app.post("/dashboard/users/{telegram_id}/inactive", response_model=None)
    async def dashboard_mark_inactive(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            await asyncio.to_thread(mark_user_inactive, supabase, telegram_id)
            return dashboard_redirect(filter, search, page, message=f"User {telegram_id} marked inactive.")
        except Exception as exc:
            logger.exception("Could not mark inactive telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not mark inactive: {exc}")

    @app.post("/dashboard/users/{telegram_id}/invite", response_model=None)
    async def dashboard_invite(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            invite_link = await create_invite_if_no_active(bot, supabase, settings, telegram_id)
            return dashboard_redirect(
                filter,
                search,
                page,
                message=f"One-use invite link generated for {telegram_id}.",
                invite_link=invite_link,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.exception("Could not create invite link for telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not create invite link: {exc}")
        except Exception as exc:
            logger.exception("Unexpected invite link error for telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=str(exc))

    @app.post("/dashboard/users/{telegram_id}/revoke-current-invite", response_model=None)
    async def dashboard_revoke_current_invite(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            revoked = await revoke_invite_for_user(
                bot,
                supabase,
                settings,
                telegram_id,
                "Invite link revoked from dashboard",
                clear_link=True,
            )
            if not revoked:
                return dashboard_redirect(filter, search, page, error="No invite link found.")
            return dashboard_redirect(filter, search, page, message=f"Invite link revoked for {telegram_id}.")
        except Exception as exc:
            logger.exception("Dashboard revoke invite failed telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not revoke invite link: {exc}")

    @app.post("/dashboard/users/{telegram_id}/send-existing-invite", response_model=None)
    async def dashboard_send_existing_invite(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            await create_or_send_existing_invite(bot, supabase, settings, telegram_id)
            return dashboard_redirect(filter, search, page, message=f"Invite send attempted for {telegram_id}.")
        except Exception as exc:
            logger.exception("Dashboard send invite failed telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not send invite: {exc}")

    @app.get("/dashboard/users/{telegram_id}/history", response_class=HTMLResponse, response_model=None)
    async def dashboard_payment_history(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            user = await asyncio.to_thread(get_registered_user, supabase, telegram_id)
            rows = await asyncio.to_thread(get_payment_history, supabase, telegram_id, None)
            return templates.TemplateResponse(
                request,
                "payment_history.html",
                {
                    "request": request,
                    "user": user or {"telegram_id": telegram_id},
                    "history": rows,
                    "active_filter": filter,
                    "search": search,
                    "page": page,
                },
            )
        except Exception as exc:
            logger.exception("Could not load payment history telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not load payment history: {exc}")

    @app.get("/dashboard/payments", response_class=HTMLResponse, response_model=None)
    async def dashboard_payments(
        request: Request,
        search: str = "",
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            rows = await asyncio.to_thread(list_approved_payments, supabase, search)
            return templates.TemplateResponse(
                request,
                "payment_history.html",
                {
                    "request": request,
                    "payments_page": True,
                    "history": rows,
                    "search": search,
                    "active_filter": "all",
                    "page": 1,
                },
            )
        except Exception as exc:
            logger.exception("Could not load approved payments")
            return dashboard_redirect("all", error=f"Could not load approved payments: {exc}")

    @app.get("/dashboard/payments/file", response_model=None)
    async def dashboard_payment_file(
        request: Request,
        file_id: str,
    ):
        if not is_logged_in(request):
            return Response(status_code=403)
        try:
            telegram_file = await bot.get_file(file_id)
            if not telegram_file.file_path:
                return Response(status_code=404)
            buffer = BytesIO()
            await bot.download_file(telegram_file.file_path, destination=buffer)
            path = telegram_file.file_path.lower()
            media_type = "image/jpeg"
            if path.endswith(".png"):
                media_type = "image/png"
            elif path.endswith(".webp"):
                media_type = "image/webp"
            elif path.endswith(".pdf"):
                media_type = "application/pdf"
            return Response(content=buffer.getvalue(), media_type=media_type)
        except Exception:
            logger.warning("Could not proxy Telegram payment receipt file", exc_info=True)
            return Response(status_code=404)

    @app.get("/dashboard/users/{telegram_id}/remove", response_class=HTMLResponse, response_model=None)
    async def dashboard_remove_confirm(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            user = await asyncio.to_thread(get_registered_user, supabase, telegram_id)
            if not user:
                return dashboard_redirect(filter, search, page, error=f"User {telegram_id} not found.")
            return templates.TemplateResponse(
                request,
                "confirm_remove.html",
                {
                    "request": request,
                    "user": user,
                    "active_filter": filter,
                    "search": search,
                    "page": page,
                },
            )
        except Exception as exc:
            logger.exception("Could not load remove confirmation for telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not load confirmation: {exc}")

    @app.post("/dashboard/users/{telegram_id}/remove/confirm", response_model=None)
    async def dashboard_remove_confirmed(
        telegram_id: int,
        request: Request,
        filter: str = "all",
        search: str = "",
        page: int = 1,
    ):
        if not is_logged_in(request):
            return RedirectResponse(url="/login", status_code=303)
        try:
            await bot.ban_chat_member(chat_id=settings.content_channel_id, user_id=telegram_id)
            await bot.unban_chat_member(
                chat_id=settings.content_channel_id,
                user_id=telegram_id,
                only_if_banned=True,
            )
            await asyncio.to_thread(mark_user_removed, supabase, telegram_id)
            return dashboard_redirect(filter, search, page, message=f"User {telegram_id} removed from channel.")
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.exception("Could not remove telegram_id=%s from channel", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not remove user from channel: {exc}")
        except Exception as exc:
            logger.exception("Unexpected remove error for telegram_id=%s", telegram_id)
            return dashboard_redirect(filter, search, page, error=f"Could not remove user from channel: {exc}")

    return app


async def run_web_server(app: FastAPI) -> None:
    PORT = int(os.getenv("PORT", "8080"))
    logger.info(f"Starting web dashboard on port {PORT}")
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_startup_migration(supabase: Client) -> None:
    try:
        await asyncio.to_thread(run_schema_migration, supabase)
        logger.info("Schema migration completed")
    except Exception:
        logger.warning("Schema migration skipped or failed; use /sync_schema or run README SQL", exc_info=True)


async def main() -> None:
    configure_logging()
    settings = load_settings()
    supabase = create_client(settings.supabase_url, settings.supabase_service_role_key)
    bot = Bot(settings.bot_token)
    app = create_web_app(settings, supabase, bot)

    asyncio.create_task(run_startup_migration(supabase), name="schema-migration")
    bot_task = asyncio.create_task(run_telegram_bot(bot, supabase, settings), name="telegram-bot")
    web_task = asyncio.create_task(run_web_server(app), name="web-server")
    try:
        await asyncio.gather(bot_task, web_task)
    finally:
        for task in (bot_task, web_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(bot_task, web_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
