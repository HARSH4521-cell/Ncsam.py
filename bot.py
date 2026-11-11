from contextlib import asynccontextmanager
from datetime import datetime
import asyncio
import io
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Optional

import aiosqlite
from dotenv import load_dotenv
from telegram import Bot, ChatPermissions, ReactionTypeEmoji, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

try:
    from telethon import TelegramClient
    from telethon.errors import ChatAdminRequiredError, FloodWaitError, RPCError
    from telethon.tl.functions.channels import CreateChannelRequest
except ImportError:
    TelegramClient = None
    ChatAdminRequiredError = FloodWaitError = RPCError = None
    CreateChannelRequest = None

load_dotenv()

# Define this before module-level rate limiters are instantiated.
MESSAGE_SEND_INTERVAL_SECONDS = max(
    0.01,
    float(os.getenv("MESSAGE_SEND_INTERVAL_SECONDS", "0.01")),
)

OWNER_IDS = {
8744730118
}
OWNER_IDS.update(
    {
        int(item.strip())
        for item in os.getenv("OWNER_IDS", "").split(",")
        if item.strip().isdigit()
    }
)

BOT_OWNER_PHOTO_CACHE: dict[int, str] = {}

BOT_OWNER_IDS: dict[int, int] = {}

raw_bot_owner_mapping = os.getenv("BOT_OWNER_IDS", "").strip()
if raw_bot_owner_mapping:
    for entry in raw_bot_owner_mapping.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        bot_raw, owner_raw = entry.split(":", 1)
        try:
            bot_id = int(bot_raw.strip())
            owner_id = int(owner_raw.strip())
        except ValueError:
            continue
        BOT_OWNER_IDS[bot_id] = owner_id


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def get_owner_id_for_bot(bot_id: Optional[int]) -> Optional[int]:
    """Resolve the configured owner ID for the current bot instance without using an arbitrary owner."""
    if bot_id is None:
        return None

    if bot_id in BOT_OWNER_IDS:
        return BOT_OWNER_IDS[bot_id]

    if not OWNER_IDS:
        return None

    return min(OWNER_IDS)


def get_uptime_text() -> str:
    elapsed_seconds = max(0, int(time.monotonic() - APP_START_TIME))
    hours, remainder = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


async def resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[object]:
    if not update.message:
        return None

    reply = update.message.reply_to_message
    if reply and getattr(reply, "from_user", None) is not None:
        return reply.from_user

    entities = getattr(update.message, "entities", None) or []
    if entities:
        for entity in entities:
            if entity.type == "text_mention" and getattr(entity, "user", None) is not None:
                return entity.user
            if entity.type == "mention":
                username = update.message.text[entity.offset + 1:entity.offset + entity.length]
                if username:
                    try:
                        return await context.bot.get_chat(username)
                    except Exception:
                        continue

    text = (update.message.text or "").strip()
    if text:
        parts = text.split(None, 1)
        if len(parts) > 1:
            candidate = parts[1].strip()
            if candidate.startswith("@"):
                username = candidate[1:].split()[0]
                if username:
                    try:
                        return await context.bot.get_chat(username)
                    except Exception:
                        pass

    return None


def _senpai_command_name(update: Update) -> str:
    text = getattr(getattr(update, "message", None), "text", "") or "command"
    return text.strip().split(None, 1)[0].split("@", 1)[0] if text.strip() else ".command"


def _senpai_target_name(update: Update) -> Optional[str]:
    text = getattr(getattr(update, "message", None), "text", "") or ""
    command_parts = text.strip().split()
    if len(command_parts) > 1 and command_parts[1].startswith("@"):
        return command_parts[1].split()[0]
    reply = getattr(getattr(update, "message", None), "reply_to_message", None)
    user = getattr(reply, "from_user", None)
    if user is None:
        return None
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    return getattr(user, "full_name", None) or getattr(user, "first_name", None)


def senpai_status(
    command: str,
    status: str,
    action: Optional[str] = None,
    target: Optional[str] = None,
    reason: Optional[str] = None,
    detail: Optional[str] = None,
) -> str:
    """Format a compact, consistent Senpai status response."""
    headings = {
        "running": ("✦ 𝐒ᴛᴀʀᴛᴇᴅ ✦", "🟡 𝐑ᴜɴɴɪɴɢ"),
        "active": ("✦ 𝐀ᴄᴛɪᴠᴇ ✦", "🟢 𝐀ᴄᴛɪᴠᴇ"),
        "stopped": ("✦ 𝐒ᴛᴏᴘᴘᴇᴅ ✦", "⚪ 𝐒ᴛᴏᴘᴘᴇᴅ"),
        "error": ("✦ 𝐄ʀʀᴏʀ ✦", "🔴 𝐅ᴀɪʟᴇᴅ"),
        "success": ("✦ 𝐒ᴜᴄᴄᴇss ✦", "🟢 𝐃ᴏɴᴇ"),
    }
    heading, status_line = headings.get(status, headings["success"])
    lines = [
        "╰┈➤ 🪻 Harsh",
        "",
        f"        {heading}",
        "",
        f"   ♡ 𝐂ᴏᴍᴍᴀɴᴅ ─ {command}",
    ]
    if target:
        lines.append(f"   ♡ 𝐓ᴀʀɢᴇᴛ ─ {target}")
    if action:
        lines.append(f"   ♡ 𝐀ᴄᴛɪᴏɴ ─ {action}")
    lines.append(f"   ♡ 𝐒ᴛᴀᴛᴜs ─ {status_line}")
    if reason:
        lines.append(f"   ♡ 𝐑ᴇᴀsᴏɴ ─ {reason}")
    if detail:
        lines.append(f"   ♡ 𝐃ᴇᴛᴀɪʟ ─ {detail}")
    lines.extend(["", "        𓆩⚡𓆪"])
    return "\n".join(lines)


def _senpai_response_status(text: str) -> tuple[str, Optional[str]]:
    normalized = text.strip().lower()
    error_markers = (
        "❌", "⚠️", "failed", "unable", "invalid", "cannot", "usage:",
        "no active", "no saved", "not found", "not currently",
    )
    if normalized.startswith(error_markers) or any(marker in normalized for marker in error_markers[1:]):
        return "error", text.replace("❌", "").replace("⚠️", "").strip()[:180]
    if any(marker in normalized for marker in ("stopped", "stop", "cancelled")):
        return "stopped", None
    if any(marker in normalized for marker in ("active", "started", "running")):
        return "active", None
    return "success", None


async def safe_edit_text(message: object, text: str, **kwargs: object) -> object:
    """Edit a Telegram message while ignoring harmless no-op edits."""
    if getattr(message, "text", None) == text:
        return message
    try:
        return await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return message
        raise


async def senpai_reply(update: Update, text: object, *args: object, **kwargs: object) -> object:
    """Send an existing response through the global Senpai theme."""
    raw_reply = update.message.reply_text
    if not isinstance(text, str):
        return await raw_reply(text, *args, **kwargs)

    command = _senpai_command_name(update)
    if command in {"/start", "/help", "/menu", "/menu1", "/menu2", "/menu3", "/menu4", "/menu5"}:
        return await raw_reply(text, *args, **kwargs)

    status, reason = _senpai_response_status(text)
    target = _senpai_target_name(update)
    final_text = senpai_status(
        command=command,
        status=status,
        action="𝐂ᴏᴍᴘʟᴇᴛᴇ" if status == "success" else None,
        target=target,
        reason=reason,
        detail=text[:1000] if status != "error" else None,
    )

    return await raw_reply(final_text, *args, **kwargs)


async def resolve_owner_display_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    bot_id = getattr(context.bot, "id", None)
    owner_id = get_owner_id_for_bot(bot_id)
    if owner_id is None:
        return "Owner"
    try:
        owner_chat = await context.bot.get_chat(owner_id)
        if getattr(owner_chat, "first_name", None):
            return owner_chat.first_name
        if getattr(owner_chat, "username", None):
            return f"@{owner_chat.username}"
    except Exception:
        pass
    return "Owner"


async def add_bot_to_group(bot, chat_id: int, bot_id: int) -> tuple[bool, str]:
    try:
        await bot.add_chat_member(chat_id=chat_id, user_id=bot_id)
        return True, "added"
    except BadRequest as exc:
        message = str(exc).lower()
        if "already" in message or "participant" in message or "member" in message:
            return True, "already_present"
        if "not found" in message or "user not found" in message:
            return False, "not_found"
        return False, "error"
    except Forbidden:
        return False, "forbidden"
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS)
        try:
            await bot.add_chat_member(chat_id=chat_id, user_id=bot_id)
            return True, "added"
        except Exception:
            return False, "error"
    except Exception:
        return False, "error"


def build_grantable_admin_permissions(current_member: object) -> dict[str, bool]:
    permissions = {
        "can_change_info": False,
        "can_post_messages": False,
        "can_edit_messages": False,
        "can_delete_messages": False,
        "can_invite_users": False,
        "can_restrict_members": False,
        "can_pin_messages": False,
        "can_promote_members": False,
        "can_manage_topics": False,
        "can_manage_video_chats": False,
        "can_manage_chat": False,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_manage_direct_messages": False,
        "can_manage_tags": False,
    }
    if current_member is None:
        return permissions

    for key in permissions:
        value = getattr(current_member, key, None)
        if isinstance(value, bool):
            permissions[key] = value
    return permissions


async def promote_bot_to_admin(bot, chat_id: int, user_id: int, permissions: Optional[dict[str, bool]] = None) -> bool:
    effective_permissions = permissions or {
        "can_change_info": True,
        "can_post_messages": True,
        "can_edit_messages": True,
        "can_delete_messages": True,
        "can_invite_users": True,
        "can_restrict_members": True,
        "can_pin_messages": True,
        "can_promote_members": True,
        "can_manage_topics": True,
        "can_manage_video_chats": True,
        "can_manage_chat": True,
        "can_post_stories": True,
        "can_edit_stories": True,
        "can_delete_stories": True,
        "can_manage_direct_messages": True,
        "can_manage_tags": True,
    }
    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            can_change_info=effective_permissions.get("can_change_info", False),
            can_post_messages=effective_permissions.get("can_post_messages", False),
            can_edit_messages=effective_permissions.get("can_edit_messages", False),
            can_delete_messages=effective_permissions.get("can_delete_messages", False),
            can_invite_users=effective_permissions.get("can_invite_users", False),
            can_restrict_members=effective_permissions.get("can_restrict_members", False),
            can_pin_messages=effective_permissions.get("can_pin_messages", False),
            can_promote_members=effective_permissions.get("can_promote_members", False),
            can_manage_topics=effective_permissions.get("can_manage_topics", False),
            can_manage_video_chats=effective_permissions.get("can_manage_video_chats", False),
            can_manage_chat=effective_permissions.get("can_manage_chat", False),
            can_post_stories=effective_permissions.get("can_post_stories", False),
            can_edit_stories=effective_permissions.get("can_edit_stories", False),
            can_delete_stories=effective_permissions.get("can_delete_stories", False),
            can_manage_direct_messages=effective_permissions.get("can_manage_direct_messages", False),
            can_manage_tags=effective_permissions.get("can_manage_tags", False),
        )
        return True
    except (BadRequest, Forbidden, TelegramError):
        return False
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS)
        try:
            await bot.promote_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                can_change_info=effective_permissions.get("can_change_info", False),
                can_post_messages=effective_permissions.get("can_post_messages", False),
                can_edit_messages=effective_permissions.get("can_edit_messages", False),
                can_delete_messages=effective_permissions.get("can_delete_messages", False),
                can_invite_users=effective_permissions.get("can_invite_users", False),
                can_restrict_members=effective_permissions.get("can_restrict_members", False),
                can_pin_messages=effective_permissions.get("can_pin_messages", False),
                can_promote_members=effective_permissions.get("can_promote_members", False),
                can_manage_topics=effective_permissions.get("can_manage_topics", False),
                can_manage_video_chats=effective_permissions.get("can_manage_video_chats", False),
                can_manage_chat=effective_permissions.get("can_manage_chat", False),
                can_post_stories=effective_permissions.get("can_post_stories", False),
                can_edit_stories=effective_permissions.get("can_edit_stories", False),
                can_delete_stories=effective_permissions.get("can_delete_stories", False),
                can_manage_direct_messages=effective_permissions.get("can_manage_direct_messages", False),
                can_manage_tags=effective_permissions.get("can_manage_tags", False),
            )
            return True
        except Exception:
            return False
    except Exception:
        return False


# Persistent per-bot cooldown state.  This is intentionally a safety/rate-limit
# mechanism: Telegram RetryAfter values are always respected; cooldowns never bypass
# Telegram flood control.
# Single-file build: the default cooldown JSON is embedded here.
# A sidecar cooldowns.json is still used automatically when available so cooldowns
# survive restarts without requiring a second file to be distributed manually.
EMBEDDED_COOLDOWNS_JSON = r'''{}'''
COOLDOWN_FILE = os.getenv("COOLDOWN_FILE", "cooldowns.json")
BOT_COOLDOWNS: dict[int, float] = {}

def load_cooldowns() -> None:
    global BOT_COOLDOWNS
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raw = json.loads(EMBEDDED_COOLDOWNS_JSON)
        BOT_COOLDOWNS = {int(k): float(v) for k, v in raw.items()}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        try:
            raw = json.loads(EMBEDDED_COOLDOWNS_JSON)
            BOT_COOLDOWNS = {int(k): float(v) for k, v in raw.items()}
        except (ValueError, TypeError, json.JSONDecodeError):
            BOT_COOLDOWNS = {}

def save_cooldowns() -> None:
    tmp = f"{COOLDOWN_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in BOT_COOLDOWNS.items()}, f)
        os.replace(tmp, COOLDOWN_FILE)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

def is_bot_ready(bot_idx: int) -> bool:
    return time.time() >= BOT_COOLDOWNS.get(bot_idx, 0.0)

def set_bot_cooldown(bot_idx: int, seconds: float) -> None:
    # Keep the persistent user-requested cooldown range.  Longer Telegram
    # RetryAfter waits are handled separately by the calling task and are never shortened.
    BOT_COOLDOWNS[bot_idx] = time.time() + max(0.01, min(float(seconds), 5.0))
    save_cooldowns()

def clear_expired_cooldowns() -> None:
    now = time.time()
    expired = [k for k, v in BOT_COOLDOWNS.items() if v <= now]
    if expired:
        for k in expired:
            BOT_COOLDOWNS.pop(k, None)
        save_cooldowns()

load_cooldowns()
clear_expired_cooldowns()
BOT_INDEX_BY_ID: dict[int, int] = {}

def get_bot_index(bot_id: int) -> int:
    # Bot index is stable for configured bots; bot id is a safe fallback until startup registration.
    return BOT_INDEX_BY_ID.get(bot_id, bot_id)


BOT_TOKENS = [    "8936069827:AAHsVAxDsOzrCFn2E5kgUaFPVG9ckiKLpr4",
    "8834366685:AAGArZ93OnXiHsv_nZh-q7Sn93bQqcYbTgw",
    "8701810393:AAGONOBXL_Nf4d31KeS9N-Tz1wRvjTbP0q4",
    "8834995128:AAESlP9ze8El62byKhiEExSnstbDUo-GvM0",
    "8803112805:AAFTCHqt9DafT6WDRj-PzCfFRBFHl5XN4Sw",
    "8872733838:AAGcG8tYnmQYhi9e3uDmY3r-PxQm1BLB5gQ",
    "8837560214:AAEjz7cYSspioe19sj6ef6RLrOjjXgQUqPo",
    "8600896496:AAEFUD83AKoG2WIMuelv-IeaDZ5XDpmp_GU",
    "8754768892:AAEwDz58sm5u642N9I_zHtjAqM-yhgapkds",
    "8612639845:AAH8SNQq4tQqeg9fScpz5d0Vt8Y_1e40ZiM",
#idher bot token daal gawar randike ladke 20+ bot token support krta hai
]

fallback_token = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKENS and fallback_token and fallback_token != "your_telegram_bot_token_here":
    BOT_TOKENS = [fallback_token]

BOT_TOKENS = list(dict.fromkeys(BOT_TOKENS))

PROMOTE_BOT_USERNAMES = [
#apne 10 bot add kar idher @ laga ke make lode
]

APP_START_TIME = time.monotonic()
ACTIVE_BOT_APPLICATIONS: list[object] = []
REACTING_CHATS: dict[tuple[int, int], str] = {}
NORMAL_COMMAND_CLAIMS: set[tuple[int, int, str]] = set()

DATABASE_PATH = os.getenv("DATABASE_PATH", "nc_state.db")
ROTATION_INTERVAL_SECONDS = max(0.01, float(os.getenv("ROTATION_INTERVAL_SECONDS", "0.01")))
TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS = float(os.getenv("TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS", "10.0"))
CRTGC_API_ID_RAW = os.getenv("API_ID", "").strip()
CRTGC_API_HASH = os.getenv("API_HASH", "").strip()
CRTGC_USER_SESSION = os.getenv("USER_SESSION", "senpai_crtgc.session").strip() or "senpai_crtgc.session"
CRTGC_API_ID: Optional[int]
try:
    CRTGC_API_ID = int(CRTGC_API_ID_RAW)
except ValueError:
    CRTGC_API_ID = None

CRTGC_CLIENT = None
CRTGC_CLIENT_LOCK = asyncio.Lock()
CRTGC_CLIENT_READY = False

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("telegram_nc_bot")
logger.setLevel(logging.INFO)

# Background tasks retry transient Telegram/network failures indefinitely with
# capped exponential backoff. Expected runtime failures are kept inside the
# background loop so a task does not die silently.
BACKGROUND_RETRY_BASE_SECONDS = float(os.getenv("BACKGROUND_RETRY_BASE_SECONDS", "0.01"))
BACKGROUND_RETRY_MAX_SECONDS = max(BACKGROUND_RETRY_BASE_SECONDS, float(os.getenv("BACKGROUND_RETRY_MAX_SECONDS", "30.0")))


def _background_retry_delay(attempt: int) -> float:
    """Return a bounded exponential backoff delay for a transient failure."""
    attempt = max(1, attempt)
    return min(
        BACKGROUND_RETRY_MAX_SECONDS,
        BACKGROUND_RETRY_BASE_SECONDS * (2 ** min(attempt - 1, 10)),
    )




def _install_task_restart_supervisor(
    task: asyncio.Task,
    registry: dict,
    key,
    restart_factory,
    label: str,
) -> None:
    """Restart a long-lived background task only when it dies from an unexpected exception.

    Explicit cancellation and normal returns are never restarted. The callback itself
    is intentionally quiet so transient runtime failures do not pollute the terminal.
    """
    def _done(completed: asyncio.Task) -> None:
        if completed.cancelled():
            return
        try:
            exc = completed.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return

        # The runner's finally block normally removes itself. If it crashed before
        # cleanup, remove the stale entry before creating the replacement.
        if registry.get(key) is completed:
            registry.pop(key, None)

        async def _restart() -> None:
            await asyncio.sleep(0.01)
            try:
                new_task = asyncio.create_task(restart_factory())
                registry[key] = new_task
                _install_task_restart_supervisor(new_task, registry, key, restart_factory, label)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed restart will be retried by the next explicit start/resume.
                pass

        try:
            asyncio.create_task(_restart())
        except RuntimeError:
            pass

    task.add_done_callback(_done)

class _BotAdaptiveRateLimiter:
    """Telegram-compliant per-bot pacing that backs off after transient pressure.

    It does not bypass flood control. A RetryAfter is still honored in full; this
    limiter only makes subsequent background requests less bursty and gradually
    returns toward the configured base interval after successful sends.
    """

    def __init__(self, base_interval: float = 0.01) -> None:
        self.base_interval = max(0.01, float(base_interval))
        self._next_allowed: dict[int, float] = {}
        self._factor: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, bot_id: int) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            factor = self._factor.get(bot_id, 1.0)
            allowed = self._next_allowed.get(bot_id, now)
            delay = max(0.0, allowed - now)
            self._next_allowed[bot_id] = max(now, allowed) + self.base_interval * factor
        if delay:
            await asyncio.sleep(delay)

    async def success(self, bot_id: int) -> None:
        async with self._lock:
            current = self._factor.get(bot_id, 1.0)
            self._factor[bot_id] = max(1.0, current * 0.90)

    async def pressure(self, bot_id: int, retry_after: float = 0.0) -> None:
        async with self._lock:
            current = self._factor.get(bot_id, 1.0)
            self._factor[bot_id] = min(4.0, max(0.015, current * 1.5))
            loop = asyncio.get_running_loop()
            extra = min(max(float(retry_after), 0.0), 0.1)
            self._next_allowed[bot_id] = max(
                self._next_allowed.get(bot_id, loop.time()),
                loop.time() + extra,
            )


class _BotChatRateLimiter:
    """Serialize background Telegram API calls per bot/chat to avoid rate-limit storms."""

    def __init__(self, min_interval: float = 0.01) -> None:
        self.min_interval = max(0.1, float(min_interval))
        self._next_allowed: dict[tuple[int, int], float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, bot_id: int, chat_id: int) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            key = (bot_id, chat_id)
            allowed = self._next_allowed.get(key, now)
            delay = max(0.0, allowed - now)
            self._next_allowed[key] = max(now, allowed) + self.min_interval
        if delay:
            await asyncio.sleep(delay)


BACKGROUND_CHAT_MIN_INTERVAL_SECONDS = max(
    0.01,
    float(os.getenv("BACKGROUND_CHAT_MIN_INTERVAL_SECONDS", "0.01")),
)
BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS = max(
    15.0,
    float(os.getenv("BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS", "30")),
)
bot_chat_rate_limiter = _BotChatRateLimiter(BACKGROUND_CHAT_MIN_INTERVAL_SECONDS)
bot_adaptive_rate_limiter = _BotAdaptiveRateLimiter(MESSAGE_SEND_INTERVAL_SECONDS)


def configure_terminal_logging() -> None:
    """Reduce noisy routine polling/request logs while keeping warnings and errors visible."""
    # Force UTF-8 for Windows/VS Code terminal output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext._application").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext._job_queue").setLevel(logging.WARNING)
    logging.getLogger("telegram.request").setLevel(logging.WARNING)
    logging.getLogger("telegram.vendor.ptb_urllib3").setLevel(logging.WARNING)


def print_startup_banner() -> None:
    print("╭━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╮")
    print("┃     ⚕️Harsh 𝐊ᴇɴɢ⚕️")
    print("┃         𝐁ᴏᴛ 𝐒ᴛᴀʀᴛ    ")
    print("╰━━━━━━ ✦ ⚡ ✦ ━━━━━━━╯")


def print_ready_banner() -> None:
    print("\n    ❤︎ 𝐁ᴏᴛ 𝐈s 𝐑𝐞ᴀᴅʏ 𝐓ᴏ 𝐔sᴇ ❤︎")

NC_CUSTOM_NAMES = [
    "senpai",  # NC1
    "godnc",        # NC2
    "randomnc",        # NC3
    "kengnc",        # NC4
    "hellnc",        # NC5
    "senpaigod",        # NC6
    "invisiblenc",        # NC7
    "fucknc",        # NC8
    "animalnc",        # NC9
    "chodunc",        # NC10
    "bluenc",               #11
    "randnc",                   #12
    "maxxnc",                   #13
    "angrync",                   #14
    "dilnc",                   #15
    "godnc2",                   #16
    "bignc",                   #17
    "targetnc",                   #18
    "emonc",                   #19
    "cstmnc",                   #20
]


def _build_nc_command_lookup() -> dict[str, int]:
    return {
        name.strip().lower(): idx + 1
        for idx, name in enumerate(NC_CUSTOM_NAMES)
        if name and name.strip()
    }


def get_nc_display_label(profile_id: int) -> str:
    if 1 <= profile_id <= len(NC_CUSTOM_NAMES):
        name = NC_CUSTOM_NAMES[profile_id - 1]
    else:
        name = ""
    return name.strip() if isinstance(name, str) and name.strip() else f"NC{profile_id}"


def get_nc_command_alias(profile_id: int) -> str:
    if 1 <= profile_id <= len(NC_CUSTOM_NAMES):
        name = NC_CUSTOM_NAMES[profile_id - 1]
    else:
        name = ""
    return name.strip().lower() if isinstance(name, str) and name.strip() else f"nc{profile_id}"


def get_slider_command_alias(profile_id: int) -> str:
    if 1 <= profile_id <= len(SLIDER_CUSTOM_NAMES):
        name = SLIDER_CUSTOM_NAMES[profile_id - 1]
    else:
        name = ""
    return name.strip().lower() if isinstance(name, str) and name.strip() else f"slide{profile_id}"


NC_CUSTOM_COMMAND_LOOKUP = _build_nc_command_lookup()
NC_CUSTOM_COMMAND_LIST = list(NC_CUSTOM_COMMAND_LOOKUP.keys())

NC_NAMES: dict[int, list[str]] = {
    1: [
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗖𝗛𝗨𝗗 𝗟𝗘",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗖𝗛𝗨𝗗𝗗𝗞𝗔𝗗",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗥𝗔𝗡𝗗𝗔𝗟",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗥𝗔𝗗𝗡𝗜 𝗣𝗨𝗧𝗔𝗥",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗞𝗔𝗠𝗝𝗢𝗥",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗟𝗔𝗡𝗚𝗗𝗔",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗝𝗔𝗠𝗔𝗗𝗔𝗥",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗛𝗜𝗝𝗗𝗔",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗔𝗣𝗔𝗛𝗜𝗝",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗕𝗜𝗧𝗖𝗛",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗦𝗟𝗔𝗩𝗘",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗠𝗢𝗧𝗛𝗘𝗥𝗗𝗨𝗖𝗞𝗘𝗥",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗧𝗠𝗞𝗕",
        "{target} 𝐒ɪᴛ 𝐃ᴏɢɢʏ 𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙🌈⃤𒈙 𝗠𝗗𝗖",
    ],
    2: [
        "{target} 匚卄ㄩᗪ ☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️☮️",
        "{target} ㄥㄩ几ᗪ ㄥ乇 ✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️✡️",
        "{target} ㄒ爪Ҝ乃 ☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️☸️",
        "{target} 匚卄ㄩᗪ 卩ᘜㄥ 🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️🕉️",
        "{target} 千卂爪 乃ㄖㄚ ☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️☪️",
        "{target} Ҝㄩㄒ|ㄚ卂 ✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️✝️",
        "{target} 乃卄ㄖ丂ᗪ| ♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓♓",
        "{target} ᘜ卂几ᗪㄩ ♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏♏",
        "{target} 匚卄ㄩㄒ|ㄚ卂 ♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈♈",
        "{target} 匚卄卂爪卂尺 ♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋♋",
        "{target} 丂ㄥ卂ᐯ乇 ㄥㄩ几ᗪ ㄥ乇 ♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌♌",
        "{target} 匚卄ㄩㄒ卂ᗪ 🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐🛐",
        "{target} 尺卂几ᗪ| 丂ㄖ几 ☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️☯️",
        "{target} 乃|卄卂尺| ☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️☦️",
    ],
    3: [
        "{target} 𝓇𝒶𝓃𝒹𝒾 𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙✨𒈙𒈙✨𒈙✨𒈙✨𒈙✨",
        "{target} 𝒢𝒜𝒴 𒈙🎀𒈙🎀𒈙🎀🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀𒈙🎀",
        "{target} 𝐻𝐼𝒥𝒟𝒜 𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙 𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨𒈙🧨",
        "{target} 𝒦𝒜𝐿𝐼 𝒢𝒜𝒩𝒟 𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙🎁𒈙",
        "{target} 𝒢𝒰𝐿𝒜𝐵𝐼 𝒞𝐻𝒰𝒯 𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️𒈙🎗️",
        "{target} 𝐵𝒰𝑅 𝒞𝐻𝒜𝒯𝒯𝒜 𒈙🧵𒈙🧵𒈙🧵𒈙??𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵𒈙🧵",
        "{target} 𝒦𝒰𝒯𝒯𝐸 𝒦𝒜 𝐿𝒰𝒩𝒟 𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉𒈙🎉",
        "{target} 𝒢𝒜𝒩𝒟𝑀𝑅𝒜 𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊 𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊𒈙🎊",
        "{target} 𝒞𝐻𝒰𝒯𝒜𝒟 𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐𒈙🎐",
        "{target} 𝒞𝐻𝒰𝒯 𝒦𝐼 𝒞𝐻𝒜𝒫𝒜𝐿𝐿 𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧𒈙🧧",
        "{target} 𝐿𝒰𝒩𝒟 𝒯𝐸 𝒱𝒜𝒥 𒈙??𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍𒈙🎍",
        "{target} 𝒞𝐻𝒰𝒟𝒟𝒦𝒜𝒟 𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃𒈙🎃",
        "{target} 𝒟𝒪𝒢𝒢𝒴 𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠𒈙🎠",
        "{target} 𝒮𝐿𝒜𝒱𝐸 𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭𒈙🎭",
    ],
    4: [
        "{target} 𝚌𝚑𝚞𝚍 𝚋𝚑𝚜𝚘𝚍𝚎 ¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸¥🐸",
        "{target} 𝚐𝚞𝚕𝚊𝚖𝚒 𝚔𝚊𝚛 ¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲¥💲",
        "{target} 𝚃𝙼𝙺𝙱 ¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥¥🐦‍🔥",
        "{target} 𝚃𝙼𝙺𝙲 ¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼¥🪼",
        "{target} 𝚁𝙰𝙽𝙳𝙸𝙺𝙴 ¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖¥🤖",
        "{target} 𝙲𝙷𝚄𝚃𝙰𝙳 ¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️¥🕷️",
        "{target} 𝙻𝙰𝙽𝙶𝙳𝙰 ¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️¥🕸️",
        "{target} 𝙷𝙰𝙲𝙻𝙴 𝙻𝙳𝙺𝙴 ¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏¥🫏",
        "{target} 𝙶𝙰𝚁𝙴𝙴𝙱 𝙱𝙲𝙷𝙴 ¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬¥🧬",
        "{target} 𝙻𝚄𝙽𝙳 𝙲𝙷𝚄𝚂 𝙱𝙰𝙳𝙰 𝙷𝙾 ¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚¥🦚",
        "{target} 𝙱𝙾𝙺𝙰𝙲𝙷𝙾𝙳𝙰 ¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗¥🦗",
        "{target} 𝚁𝙰𝙽𝙳𝙰𝙻 ¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞¥🐞",
    ],
    5: [
        "{target} 𝖳𝖤𝖱𝖨 𝖬𝖠𝖠 𝖲𝖤𝖷 𝖶𝖮𝖪𝖤𝖱 😀 ───────────── 😎😀 ───────────── 😎😀 ───────────── 😎😀 ───────────── 😎😀 ───────────── 😎😀 ───────────── 😎😀 ───────────── 😎😀 ───────────── 😎",
        "{target} TERI BHN SEX WOKER 😂 ───────────── 🤣😂 ───────────── 🤣😂 ───────────── 🤣😂 ───────────── 🤣😂 ───────────── 🤣😂 ───────────── 🤣😂 ───────────── 🤣😂 ───────────── 🤣",
        "{target} 𝖳𝖤𝖱𝖨 𝖡𝖧𝖭 𝖪𝖴𝖣𝖤 𝖶𝖠𝖫𝖨 🥶 ───────────── 🔥🥶 ───────────── 🔥🥶 ───────────── 🔥🥶 ───────────── 🔥🥶 ───────────── 🔥🥶 ───────────── 🔥🥶 ───────────── 🔥🥶 ───────────── 🔥",
        "{target} 𝖳𝖤𝖱𝖨 𝖬𝖠𝖠 𝖪𝖴𝖣𝖤 𝖶𝖠𝖫𝖨 👑 ───────────── 🥷👑 ───────────── 🥷👑 ───────────── 🥷👑 ───────────── 🥷👑 ───────────── 🥷👑 ───────────── 🥷👑 ───────────── 🥷👑 ───────────── 🥷",
        "{target} 𝖳𝖤𝖱𝖨 𝖬𝖠𝖠 𝖱𝖠𝖭𝖣𝖨 🌙 ───────────── ☀️🌙 ───────────── ☀️🌙 ───────────── ☀️🌙 ───────────── ☀️🌙 ───────────── ☀️🌙 ───────────── ☀️🌙 ───────────── ☀️🌙 ───────────── ☀️",
        "{target} 𝖳𝖤𝖱𝖨 𝖬𝖠𝖠 𝖠𝖧𝖠𝖧𝖨𝖩 ❤️ ───────────── 🖤❤️ ───────────── 🖤❤️ ───────────── 🖤❤️ ───────────── 🖤❤️ ───────────── 🖤❤️ ───────────── 🖤❤️ ───────────── 🖤❤️ ───────────── 🖤",
        "{target} 𝖳𝖤𝖱𝖨 𝖬𝖠𝖠 𝖵𝖨𝖪𝖫𝖠𝖭𝖦 💜 ───────────── 💙💜 ───────────── 💙💜 ───────────── 💙💜 ───────────── 💙💜 ───────────── 💙💜 ───────────── 💙💜 ───────────── 💙💜 ───────────── 💙",
        "{target} 𝖳𝖤𝖱𝖨 𝖡𝖠𝖠𝖯 𝖵𝖨𝖪𝖫𝖠𝖭𝖦 💚 ───────────── 💛💚 ───────────── 💛💚 ───────────── 💛💚 ───────────── 💛💚 ───────────── 💛💚 ───────────── 💛💚 ───────────── 💛💚 ───────────── 💛",
        "{target} 𝖳𝖤𝖱𝖨 𝖬𝖠𝖲𝖨 𝖵𝖨𝖪𝖫𝖠𝖭𝖦 🩷 ───────────── 🩵🩷 ───────────── 🩵🩷 ───────────── 🩵🩷 ───────────── 🩵🩷 ───────────── 🩵🩷 ───────────── 🩵🩷 ───────────── 🩵🩷 ───────────── 🩵",
        "{target} 𝖳𝖤𝖱𝖨 𝖬𝖠𝖠 𝖡𝖧𝖭 𝖢𝖧𝖮𝖣𝖴 🐍 ───────────── 🦂🐍 ───────────── 🦂🐍 ───────────── 🦂🐍 ───────────── 🦂🐍 ───────────── 🦂🐍 ───────────── 🦂🐍 ───────────── 🦂🐍 ───────────── 🦂",
        "{target} 𝖳𝖤𝖱𝖠 𝖡𝖠𝖠𝖯 𝖦𝖠𝖸 🐺 ───────────── 🦁🐺 ───────────── 🦁🐺 ───────────── 🦁🐺 ───────────── 🦁🐺 ───────────── 🦁🐺 ───────────── 🦁🐺 ───────────── 🦁🐺 ───────────── 🦁",
        "{target} 𝖢𝖧𝖠𝖫 𝖱𝖠𝖭𝖣𝖨𝖪𝖤 🦅 ───────────── 🐉🦅 ───────────── 🐉🦅 ───────────── 🐉🦅 ───────────── 🐉🦅 ───────────── 🐉🦅 ───────────── 🐉🦅 ───────────── 🐉🦅 ───────────── 🐉",
        "{target} 𝖢𝖧𝖠𝖫 𝖳𝖱𝖨 𝖬𝖠𝖪𝖠 𝖡𝖧𝖮𝖲𝖣𝖠 🌹 ───────────── 🌸🌹 ───────────── 🌸🌹 ───────────── 🌸🌹 ───────────── 🌸🌹 ───────────── 🌸🌹 ───────────── 🌸🌹 ───────────── 🌸🌹 ───────────── 🌸",
        "{target} 𝖱𝖠𝖭𝖣𝖠𝖫 𝖪𝖤 𝖡𝖠𝖢𝖧𝖤 🍀 ───────────── 🌻🍀 ───────────── 🌻🍀 ───────────── 🌻🍀 ───────────── 🌻🍀 ───────────── 🌻🍀 ───────────── 🌻🍀 ───────────── 🌻🍀 ───────────── 🌻",
        "{target} 𝖠𝖱𝖤 𝖳𝖬𝖪𝖡𝖥𝖩 🚀 ───────────── 🛸🚀 ───────────── 🛸🚀 ───────────── 🛸🚀 ───────────── 🛸🚀 ───────────── 🛸🚀 ───────────── 🛸🚀 ───────────── 🛸🚀 ───────────── 🛸🚀 ───────────── 🛸",
        "{target} 𝖳𝖤𝖱𝖤 𝖬𝖠 𝖢𝖧𝖮𝖣𝖱 𝖲𝖠𝖭𝖣 🎯 ───────────── 🏆🎯 ───────────── 🏆🎯 ───────────── 🏆🎯 ───────────── 🏆🎯 ───────────── 🏆🎯 ───────────── 🏆🎯 ───────────── 🏆🎯 ───────────── 🏆",
        "{target} 𝖳𝖬𝖪𝖡 𝖢𝖧𝖮𝖣𝖴𝖦𝖠 𝖱𝖤 💎 ───────────── 👑💎 ───────────── 👑💎 ───────────── 👑💎 ───────────── 👑💎 ───────────── 👑💎 ───────────── 👑💎 ───────────── 👑💎 ───────────── 👑",
        "{target} 𝖠𝖱𝖤 𝖳𝖬𝖪𝖡 𝖬𝖱𝖴 🧿 ───────────── 🔮🧿 ───────────── 🔮🧿 ───────────── 🔮🧿 ───────────── 🔮🧿 ───────────── 🔮🧿 ───────────── 🔮🧿 ───────────── 🔮🧿 ───────────── 🔮",
        "{target} 𝖢𝖧𝖴𝖣𝖫𝖤 𝖫𝖠𝖭𝖦𝖣𝖤 ✨ ───────────── 💫✨ ───────────── 💫✨ ───────────── 💫✨ ───────────── 💫✨ ───────────── 💫✨ ───────────── 💫✨ ───────────── 💫✨ ───────────── 💫",
    ],
    6: [
        "{target} (𓃵)- ​🇰​​🇺​​🇹​​🇹​​🇮​ ​🇰​​🇪 ​𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆𒅒𒈔𒅒𒇫𒄆",
        "{target} (𖤐)- ​🇹​​🇲​​🇰​​🇨 🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔*:･🪼⋆.ೃ࿔​",
        "{target} (⛧)- ​🇱​​🇦​​🇳​​🇬​​🇩​​🇪​ ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ִֶָ🪽་༘࿐ִֶָ. ..𓂃 ࣪ ",
        "{target} (⸸)- 🇲​​🇰​​🇱 ඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞඞ",
        "{target} (⛧⃝)- ʀᴀɴᴅ ˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱??⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚⊱🪷⊰˚˚",
        "{target} (𓀐𓂸)- ​🇨​​🇭​​🇺​​🇩​​🇱​​🇪 𓂃˖˳·˖ ִֶָ ⋆🌷͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ𓂃˖˳·˖ ִֶָ ⋆🌷͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ𓂃˖˳·˖ ִֶָ ⋆🌷͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ𓂃˖˳·˖ ִֶָ ⋆🌷͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ𓂃˖˳·˖ ִֶָ ⋆🌷͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ𓂃˖˳·˖ ִֶָ ⋆🌷͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ𓂃˖˳·˖ ִֶָ ⋆🌷͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ𓂃˖˳·˖ ִֶָ ⋆🌷͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ𓂃˖˳·𓂃 ִֶָ",
        "{target} (🜏)- ​🇷​​🇦​​🇳​​🇩​​🇮​​🇰​​🇪 ⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋??𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚⋆˙𖠋𖠋𖠋𖠋𖠋⋆˚",
        "{target} (´ཀ`)- ​🇹​​🇧​​🇰​​🇧​ 🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*ੈ✩‧₊˚🎐🫧𓇼𓏲*", 
        "{target} (𓄃)- ​🇭​​🇮​​🇯​​🇩​​🇪 🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️⫘🎗️",
    ],

    7: [
        "{target} लंड चूस ~🩵 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~🩷 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~💚 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~🤍 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~🖤 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~❤️‍🔥 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~❤️‍🩹 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~💗 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~💝 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~💟 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
        "{target} लंड चूस ~💌 ██████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████",
       
    ],
    8: [
        "{target} ᶠᶸᶜᵏᵧₒᵤ! 🎀💄👙🐽🐖🍬🧞‍♀️👚🩷👛🦩🩰🎟️🤙🐶🐼☑️🎀💄👙🐽🐖🍬🧞‍♀️👚🩷👛🦩🩰🎟️🤙🐶🐼☑️🎀💄👙🐽🐖🍬🧞‍♀️👚🩷👛🦩🩰🎟️🤙🐶🐼☑️☑️",
        "{target} ᶠᶸᶜᵏᵧₒᵤ! 👷🏼‍♂️✴️🟠🟧🔶📙🏵️🉑🈸🥻🈚️🈺🚼🈶🆚☣️🩳👷🏼‍♂️✴️🟠🟧🔶📙🏵️🉑🈸🥻🈚️🈺🚼🈶🆚☣️🩳👷🏼‍♂️✴️🟠🟧🔶📙🏵️🉑🈸🥻🈚️🈺🚼🈶🆚☣️🩳🩳",
        "{target} ᶠᶸᶜᵏᵧₒᵤ! 🥥🐑🐈💟🐹🐹🥤🍄🐇🐧🎬🧑🏻‍🦳👩🏻‍🦳👨🏻‍🦳🦴⚪️🐁🥥🐑🐈💟🐹🐹🥤🍄🐇🐧🎬🧑🏻‍🦳👩🏻‍🦳👨🏻‍🦳🦴⚪️🐁🥥🐑🐈💟🐹🐹🥤🍄🐇🐧🎬🧑🏻‍🦳👩🏻‍🦳👨🏻‍🦳🦴⚪️🐁💟🐹",
        "{target} ᶠᶸᶜᵏᵧₒᵤ! ♓️♌️♑️♉️♑️🟪🟣🪻🆔🚺⛎🪁🪯🧕🏽♍️⚛️♓️♌️♑️♉️♑️🟪🟣🪻🆔🚺⛎🪁🪯🧕🏽♍️⚛️♓️♌️♑️♉️♑️🟪🟣🪻🆔🚺⛎🪁🪯🧕🏽♍️⚛️♓️♌️♑️🧕🏽♍️⚛️",
        "{target} ᶠᶸᶜᵏᵧₒᵤ! 🥑🌱🐢🦖🐛🍬📚🌳🍉🧤🥝🦎🥗🥦👗🥒🍏🥑🌱🐢🦖🐛🍬📚🌳🍉🧤🥝🦎🥗🥦👗🥒🍏🥑🌱🐢🦖🐛🍬📚🌳🍉🧤🥝🦎🥗🥦👗🥒🍏",
        "{target} ᶠᶸᶜᵏᵧₒᵤ! 🍉🧣🧧👠🥤🥩⁉️🍄♦️✂️🎸🆘🧱🦜🍅🍝⛳️🍉🧣🧧👠🥤🥩⁉️🍄♦️✂️🎸🆘🧱🦜🍅🍝⛳️🍉🧣🧧👠🥤🥩⁉️🍄♦️✂️🎸🆘🧱🦜🍅🍝⛳️⛳️ ",
        "{target} ᶠᶸᶜᵏᵧₒᵤ! 🔹🌀🧞‍♂️🧵🔷🎽📘🟦🪣🖲️🩵🛃🛅🛂🛋️🪁🛗🔹🌀🧞‍♂️🧵🔷🎽📘🟦🪣🖲️🩵🛃🛅🛂🛋️🪁🛗🔹🌀🧞‍♂️🧵🔷🎽📘🟦🪣🖲️🩵🛃🛅🛂🛋️🪁🛗🛗",
    ],
    9: [
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍🦍",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧🦧",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎🐎",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄🦄",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼🐼",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒🦒",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘🐘",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏🦏",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡🦡",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇🐇",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇🦇",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫🐫",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬🦬",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖🐖",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰🐰",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁🐁",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛🐦‍⬛",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥🐦‍🔥",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆🦆",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜🦜",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩🦩",
        "{target} 𝕋𝕄𝕂ℂ 𝕄𝔼 🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚🦚",
    ],
    10: [
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙??⃤𒈙🔥⃤𒈙🔥⃤𒈙𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙🔥⃤𒈙",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙??⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤𒈙💀⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤𒈙⚡⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤𒈙😈⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤𒈙👑⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤𒈙💎⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤𒈙🌙⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤𒈙☠️⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤𒈙🦋⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤𒈙🌹⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤𒈙🐉⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤𒈙👿⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤𒈙🩸⃤",
        "{target} 𝒢𝒰𝐿𝒜𝑀𝐼 𝒦𝒜𝑅 𒈙🔥⃤𒈙🌪️⃤𒈙🌊⃤𒈙❄️⃤𒈙☄️⃤𒈙🌟⃤𒈙✨⃤𒈙🌈⃤𒈙💫⃤𒈙🖤⃤𒈙🤍⃤𒈙💜⃤𒈙💙⃤𒈙❤️⃤𒈙💚⃤𒈙🧡⃤𒈙💛⃤𒈙🤎⃤𒈙🩵⃤𒈙🩷⃤𒈙🩶⃤𒈙🔥⃤𒈙🌪️⃤𒈙🌊⃤𒈙❄️⃤𒈙☄️⃤𒈙🌟⃤𒈙✨⃤𒈙🌈⃤𒈙💫⃤𒈙🖤⃤𒈙🤍⃤𒈙💜⃤𒈙💙⃤𒈙❤️⃤𒈙💚⃤𒈙🧡⃤𒈙💛⃤𒈙🤎⃤𒈙🩵⃤𒈙🩷⃤𒈙🩶⃤𒈙🔥⃤𒈙🌪️⃤𒈙🌊⃤𒈙❄️⃤𒈙☄️⃤𒈙🌟⃤𒈙✨⃤𒈙🌈⃤𒈙💫⃤𒈙🖤⃤𒈙🤍⃤𒈙💜⃤𒈙💙⃤𒈙❤️⃤𒈙💚⃤𒈙🧡⃤𒈙💛⃤𒈙🤎⃤𒈙🩵⃤𒈙🩷⃤𒈙🩶⃤𒈙🔥⃤𒈙🌪️⃤𒈙🌊⃤𒈙❄️⃤𒈙☄️⃤𒈙🌟⃤𒈙✨⃤𒈙🌈⃤𒈙💫⃤𒈙🖤⃤𒈙🤍⃤𒈙💜⃤𒈙💙⃤𒈙❤️⃤𒈙💚⃤𒈙🧡⃤𒈙💛⃤𒈙🤎⃤𒈙🩵⃤𒈙🩷⃤",
        
    ],
    11: [
        "{target} {target} मेरा लंड चूसो ⌁ ➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️⃤➡️",
        "{target} {target} मेरा लंड चूसो ⌁ ⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤⬅️⃤",
        "{target} {target} मेरा लंड चूसो ⌁️ ⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⃤⬇️⬇️⃤⬇️",
        "{target} {target} मेरा लंड चूसो ⌁ ↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️⃤↗️↗️⃤↗️⃤↗️",
        "{target} {target} मेरा लंड चूसो ⌁ ↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️⃤↘️↘️⃤↘️⃤↘️",
        "{target} {target} मेरा लंड चूसो ⌁ ↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️⃤↙️↙️⃤↙️⃤↙️",
        "{target} {target} मेरा लंड चूसो ⌁ ↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️⃤↖️↖️⃤↖️⃤↖️",
        "{target} {target} मेरा लंड चूसो ⌁ ↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️↕️⃤↕️⃤↕️",
        "{target} {target} मेरा लंड चूसो ⌁ ↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️⃤↕️↕️⃤↕️⃤↕️",
        "{target} {target} मेरा लंड चूसो ⌁ 🔄️⃤🔄️⃤🔄️⃤??️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️⃤🔄️🔄️⃤🔄️",
        "{target} {target} मेरा लंड चूसो ⌁ ↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️⃤↪️↪️⃤↪️⃤↪️",
        "{target} {target} मेरा लंड चूसो ⌁ ↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️⃤↩️↩️⃤↩️",
        "{target} {target} मेरा लंड चूसो ⌁ ⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⃤⤴️⤴️⃤⤴️",
        "{target} {target} मेरा लंड चूसो ⌁ ⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⃤⤵️⤵️⃤⤵️⃤⤵️",
        "{target} {target} मेरा लंड चूसो ⌁ ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️⃤ℹ️ℹ️⃤ℹ️⃤ℹ️",
        "{target} {target} मेरा लंड चूसो ⌁ 🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮⃤🚮🚮⃤🚮⃤🚮",
    ],
    12: [
        "{target} ʟᴀɴᴅ ᴄʜᴏᴏꜱ ɴᴏʀᴍɪᴇ 🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆˚˖𓍢🎀༘⋆˚˖𓍢ִ໋🌷͙֒✧˚.🎀༘⋆",
        "{target} ɢᴀʀᴇᴇʙ ʀᴀɴᴅɪ ᴋᴇ ʟᴀᴅᴋᴇ ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙🫐⃟.꩜‹—ꉂ🗯˙",
        "{target} ʜɪᴊᴅᴇ ᴋᴇ ʟᴀᴅᴋᴇ ʀɴᴅ  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪  ࣪ ",
        "{target} ᴄʜᴜᴅ ᴋᴇ ᴅᴀꜰᴀɴ ɴᴏʀᴍɪᴇ ႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷͙֒ᰔᩚႋ🌷",
        "{target} ᴛᴇʀɪ ᴍᴀ ᴄʜᴜᴅɪ ɢᴀʀᴇᴇʙ 🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧🌊⋆｡ 𖦹°.🐚",
        "{target} ʀᴀɴᴅɪ ᴋᴇ ʙᴀᴄᴄʜᴇ ʟᴀɴᴅ ʟᴇ 𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°.𖥔 ݁ ˖ִ🛸༄˖°˖°.",
        "{target} ᴛᴇʀɪ ᴍᴀ ᴄʜᴜᴅ ᴋᴇ ʙʜᴀɢɪ ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ ᗢ₊˚✧ ﾟ˚ʚ",
    ],
    13: [
        "{target} 𝓢𝓔𝓝𝓟𝓐𝓘 𝓚𝓞 𝓑𝓐𝓐𝓟 𝓑𝓐𝓝𝓐 😀😁😂🤣😃😄😅😆😉😊😋😎😍😘🥰😗😀😁😂🤣😃😄😅😆😉😊😋😎😍😘🥰😗😀😁😂🤣😃😄😅😆😉😊😋😎😍😘🥰😗😀😁😂🤣😃😄😅😆😉😊😋😎😍😘🥰😗😀😁😂🤣😗",
        "{target} 𝓝𝓘𝓖𝓖𝓐 𝓢𝓞𝓝 😙😚☺️🙂🤗🤩🤔🤨😐😑😶🙄😏😣😥😮🤐😙😚☺️🙂🤗🤩🤔🤨😐😑😶🙄😏😣😥😮🤐😙😚☺️🙂🤗🤩🤔🤨😐😑😶🙄😏😣😥😮🤐😙😚☺️🙂🤗🤩🤔🤨😐😑😶🙄😏😣😥😮🤐😙😚☺️🙂🤗🤩",
        "{target} 𝓡𝓐𝓝𝓓𝓘𝓚𝓔 𝓛𝓐𝓓𝓚𝓔 🤥🤫🤭🧐🤓☻😈👿🤡👹👺💀☠👻👽👾🤖🤥🤫🤭🧐🤓☻😈👿🤡👹👺💀☠👻👽👾🤖🤥🤫🤭🧐🤓☻😈👿🤡👹👺💀☠👻👽👾🤖🤥🤫🤭🧐🤓☻😈👿🤡👹👺💀☠👻👽👾🤖🤥🤫🤭🧐🤓☻😈👿",
        "{target} 𝓟𝓐𝓘𝓓 𝓟𝓐𝓚𝓐𝓓 𝓖𝓤𝓛𝓐𝓜 💩😺😸😹😻😼😽🙀😿😾🙈🙉🙊👶🧒👦👧💩😺😸😹😻😼😽🙀😿😾🙈🙉🙊👶🧒👦👧💩😺😸😹😻😼😽🙀😿😾🙈🙉🙊👶🧒👦👧💩😺😸😹😻😼😽🙀😿😾🙈🙉🙊👶🧒👦👧💩😺👶",
        "{target} 𝓐𝓡𝓔 𝓣𝓜𝓚𝓑 🏄‍♂️🏄‍♀️🚣‍♂️🚣‍♀️🏊‍♂️🏊‍♀️⛹️‍♂️⛹️‍♀️🏋️‍♂️🏋️‍♀️🚴‍♂️🚴‍♀️🚵‍♂️🚵‍♀️🏎🏍🤸‍♂️🏄‍♂️🏄‍♀️🚣‍♂️🚣‍♀️🏊‍♂️🏊‍♀️⛹️‍♂️⛹️‍♀️🏋️‍♂️🏋️‍♀️🚴‍♂️🚴‍♀️🚵‍♂️🚵‍♀️🏎🏍🤸‍♂️🏄‍♂️🏄‍♀️🚣‍♂️🚣‍♀️🏊‍♂️🏊‍♀️⛹️‍♂️⛹️‍♀️🏋️‍♂️🏋️‍♀️🚴‍♂️🚴‍♀️🚵‍♂️🚵‍♀️🏎🏍🤸‍♂️🏄‍♂️🏄‍♀️🚣‍♂️🚣‍♀️🏊‍♂️🏊‍♀️⛹️‍♂️⛹️‍♀️🏋️‍♂️🏋️‍♀️🚴‍♂️🚴‍♀️🚵‍♂️🚵‍♀️🏎🏍🤸‍♂️🏄‍♂️🏄‍♀️🚣‍♂️🚣‍♀️🏊‍♂️🏊‍♀️⛹️‍♂️⛹️‍♀️🚣‍♂️🚣‍♀️",
        "{target} 𝓟𝓨 𝓢𝓘𝓚𝓐𝓗𝓤 🦴🦷👅👄🦰🦱🦳🦲💋💘💝💖💓💞💕💌❣💔🦴🦷👅👄🦰🦱🦳🦲💋💘💝💖💓💞💕💌❣💔🦴🦷👅👄🦰🦱🦳🦲💋💘💝💖💓💞💕💌❣💔🦴🦷👅👄🦰🦱🦳🦲💋💘💝💖💓💞💕💌❣💔🦴🦷👅👄💔🦴",
        "{target} 𝓒𝓗𝓤𝓓 𝓛𝓘𝓨𝓐 𝓣𝓤 ❤🧡💛💚💙💜🖤💟💤💢💣💥💦💨💫💬🗨🗯❤🧡💛💚💙💜🖤💟💤💢💣💥💦💨💫💬🗨🗯❤🧡💛💚💙💜🖤💟💤💢💣💥💦💨💫💬🗨🗯❤🧡💛💚💙💜🖤💟💤💢💣💥💦💨💫💬🗨🗯❤🧡💛💚🗯❤",
        "{target} 𝓑𝓐𝓤𝓝𝓔 𝓣𝓜𝓡 🦆🦢🦉🦚🦜🐸🐊🐢🦎🐍🐲🐉🦕🦖🐳🐋🐬🦆🦢🦉🦚🦜🐸🐊🐢🦎🐍🐲🐉🦕🦖🐳🐋🐬🦆🦢🦉🦚🦜🐸🐊🐢🦎🐍🐲🐉🦕🦖🐳🐋🐬🦆🦢🦉🦚🦜🐸🐊🐢🦎🐍🐲🐉🦕🦖🐳🐋🐬🦆🦢🦉🦚🦜🐬",
    ],
    14: [
        "{target} ♥T♥M♥R♥ 💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢💢",
        "{target} ♥T♥M♥K♥B♥ ❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗❗",
        "{target} ♥T♥M♥K♥C♥ ❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓❓",
        "{target} ♥T♥B♥K♥C♥ ⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕⭕",
        "{target} ♥T♥B♥K♥B♥ 🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴",
        "{target} ♥M♥K♥L♥ 🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥",
        "{target} ♥B♥K♥L♥ 🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧🧧",
    ],
    15: [
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻқⲥ---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢 𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢---𝄞🧡𝄢",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻқⲥ---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢---𝄞💛𝄢",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻқ--- 𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢---𝄞💚𝄢",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻқ--- 𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢---𝄞💋𝄢",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻ--- 𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢---𝄞❣️𝄢",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻ--- 𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢-𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢---𝄞💟𝄢",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻ--- 𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢---𝄞💝𝄢-",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻ--- 𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢---𝄞💕𝄢-",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻ--- 💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢---𝄞💖𝄢",
        "{target} 𝐒ʟᴀᴠᴇ रैंडी τϻ--- 𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢---𝄞❤️‍🔥𝄢",
    ],
    16: [
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ ❄️",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ 🔱",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ ⚜️",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ 〽️",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ 🚸",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ 🌪️",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ 🪻",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ ⚕️",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ 💤",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ ❔",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ 💠",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ ♻️",
        "< {target} > ",
        "< {target} >꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅꧅ ᥴʜꪊᴅꫝɪ ᛕꫀ 𝑩ꫝᴋʀᴇ 💯",
        "< {target} > ",
        
    ],
    17: [
        """{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ ☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞
⚞☀️⚟⚞☀️⚟⚞""",

        """{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞
⚞🌸⚟⚞🌸⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞
⚞🐉⚟⚞🐉⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞
⚞💎⚟⚞💎⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞
⚞🦋⚟⚞🦋⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞
⚞🌪️⚟⚞🌪️⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞
⚞🌊⚟⚞🌊⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞
⚞🦂⚟⚞🦂⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞
⚞🦁⚟⚞🦁⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞
⚞👻⚟⚞👻⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞
⚞🖤⚟⚞🖤⚟⚞""",

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ ✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞
⚞✨⚟⚞✨⚟⚞ """,

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞
⚞🩷⚟⚞🩷⚟⚞"""

"""{target} 𝐅ꫀꫀ𝗟 𝐘ꪮꪊ𝗥 𝐂ꫝᴜ𝗗ᴀɪ 🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞
⚞🌟⚟⚞🌟⚟⚞""",


    ],
    18: [
        "{target}  𝙆𝙐𝘾𝙃 𝙉𝙃𝙄 𝙎𝙐𝙉𝙐𝙉𝙂𝘼 𝙏𝙀𝙍𝙄 𝙈𝘼 𝙆𝘼 𝘽𝙃𝙊𝙎𝘿𝘼 🤣🤣🤣🤣🤣🤣🤣🤣🤣🤣🤣😍😚😗😗😗😗😭😭😭😭🤤🤤🤤😋😛😛😌😬😬😬😬🤪🤪🤪🤪🤪 ",
        "{target}  𝙏𝙀𝙍𝙔 𝙈𝘼𝘼 𝙆𝙀 𝘽𝙃𝙊𝙎𝘿𝙀 𝙈𝙀 𝙃𝙀𝙇𝙄𝘾𝙊𝙋𝙏𝙀𝙍 🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁🚁",
        "{target}  𝙎𝙃𝙐𝙏𝙐𝙋 𝙈𝙊𝙏𝙄 𝙈𝘼𝘼 𝙆𝙀 𝙇𝘼𝘿𝘾𝙀 🙀🙀🙀🙀🙀🙀😾😾😾😾😾🙈🙈🙈🙈🙈🙉🙉🙉🌟🌟🌟💙💙💘💘💘💘💘👅👏🏿👏🏿👏🏿👏🏿👏🏿",
        "{target}  𝙏𝙀𝙍𝙔 𝘽𝙃𝙀𝙉 𝙍𝘼𝙉𝘿𝙔 𝙌𝙐𝙀𝙀𝙉 🍆🍆🍆🍆🍆🍆🍆🍆🍆🍆🍆🍆🍆🍆🍆🍆🫐🫐🫐🫐🫐🫐🫐🫐🫐🫐🫐🫐🫐🫐🫐🫐🥕🥕🥕🥕🥕🥕🥕🥕🥕",
        "{target}  𝙊𝙔𝙀𝙀 𝘽𝙄𝙃𝘼𝙍𝙄 𝙈𝘼𝙅𝘿𝙐𝙍 𝙆𝘼𝘼𝙈 𝙋𝙀 𝙅𝘼𝘼 🤮🤢🤮🤢🤮🤢🤢😌🤮😌🤮🤮🤮🤮🤮🤮🤮🤮🤮🤮🤮🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢🤢",
        "{target}  𝙏𝙀𝙍𝙀 𝘽𝘼𝘼𝙋 𝙆𝙀 𝙆𝘼𝘽𝘼𝙍 𝙋𝙀 𝙅𝘼𝙆𝙀 𝙈𝙊𝙊𝙏 𝘿𝙐𝙉𝙂𝘼 𝘽𝙃𝙊𝙎𝘿𝙄𝙒𝘼𝙇𝙀🤭🤭🤭🤭🤭🤭💀💀💀💀👻👻👻👻👿👿⛄⛄⛄☃️☃️👿👿👿👿👿👿👿🙈🙈🙈🙈🙈🙈💥💥💥💥💥💢💢💢⚡⚡⚡⚡⚡⚡⚡✨😼✨😼😼😼😼",
        
    ],
    19: [
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬𐙚🍬",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐✨𖤐",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍💫𖤍",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 ✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔🫟✔",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿𝕏🍿",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 ⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦⸙🐦",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 ⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔🐍⚔",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾.👅ִֶָ࣪☾",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 ☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋☦🎋",
        "{target} #Harsh(𝗢𝗙𝗙) 𐌊Ꝋ अब्बू 𝓑𝓪𝓷𝓪 ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.??ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀ᥫ᭡.🎀",
    ],
    20: [
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
        "{target} ",
    ],
}

DECORATIONS = [
    "₊‧⁺˖⚜️˖⁺‧₊",
    "⊹₊˚✧🪽✧˚₊⊹",
    "₊˚⊹🪐⊹˚₊",
    "⋆｡°✩🌙✩°｡⋆",
    "₊˚ෆ🦋ෆ˚₊",
    "❁𓂃✧𓂂❁",
    "⋆⸜🌸⸝⋆",
    "⁺✧˚🌟˚✧⁺",
    "⊹⟢💫⟣⊹",
    "₊⋆🖤⋆₊",
    "✧･ﾟ: *✧･ﾟ:*",
    "＊:･ﾟ✧:･ﾟ＊",
    "⭒⋆⭒",
    "┊⋆🕊⋆┊",
    "⁺˚ˑ☽ˑ˚⁺",
    "⋆˚✦🌺✦˚⋆",
    "◌𓈒𓐍𓈒◌",
    "⫷⫸✨⫷⫸",
    "❀۪۪━━━━",
    "⋆｡˚⋆🕯⋆˚｡⋆",
    "₊˚✶✨✶˚₊",
    "⁺⊹🧿⊹⁺",
    "𖧷⋆𖤐⋆𖧷",
    "˚₊✧ʚ🧚‍♀️ɞ✧₊˚",
    "⭒∘🦢∘⭒",
    "⋆⟢🐉⟣⋆",
    "₊˚✸🧩✸˚₊",
    "✦⸙🌿⸙✦",
    "⟡｡･:*:･ﾟ’✧",
]


MSG_CUSTOM_NAMES = [
    "senpaispam",  # MGS1
    "frndspam",  # MGS2
    "baapspam",  # MGS3
    "h8rchod",  # MGS4
    "kengspam",  # MGS5
    "heartspam",  # MGS6
    "lund",  # MGS7
    "hindispam",  # MGS8
    "jodspam",  # MGS9
    "multispam",  # MGS10
]


def _build_msg_command_lookup() -> dict[str, int]:
    return {
        name.strip().lower(): idx + 1
        for idx, name in enumerate(MSG_CUSTOM_NAMES)
        if name and name.strip()
    }


MSG_CUSTOM_COMMAND_LOOKUP = _build_msg_command_lookup()
MSG_CUSTOM_COMMAND_LIST = list(MSG_CUSTOM_COMMAND_LOOKUP.keys())

SLIDER_CUSTOM_NAMES = [
    "godslide1",
    "godslide2",
    "godslide3",
]


def _build_slider_command_lookup() -> dict[str, int]:
    return {
        name.strip().lower(): idx + 1
        for idx, name in enumerate(SLIDER_CUSTOM_NAMES)
        if name and name.strip()
    }


SLIDER_CUSTOM_COMMAND_LOOKUP = _build_slider_command_lookup()
SLIDER_CUSTOM_COMMAND_LIST = list(SLIDER_CUSTOM_COMMAND_LOOKUP.keys())

SLIDER_MESSAGES = {
    1: [
        "🌸🌺🌹 {target} ᴛᴇʀɪ ᴍᴀ ᴋɪ ᴄʜᴜᴛ 🌸🌺🌹 ꜰʟᴏᴡᴇʀ ꜱᴘᴀᴍ",
        "🌷🌼💐 {target} ᴛᴇʀɪ ʙᴇʜᴇɴ ᴋɪ ᴄʜᴜᴛ 🌷🌼💐",
        "🪷🌻🌺 {target} ʀᴀɴᴅɪ ᴋᴇ ʙᴀᴄᴄʜᴇ 🪷🌻🌺 ɢᴀʀᴇᴇʙ",
        "🌹🥀🌺 {target} ᴛᴇʀɪ ᴍᴀ ᴋɪ ᴄʜᴜᴅᴀɪ 🌹🥀🌺 ʜɪᴊᴅᴇ",
        "💮🏵️🌸 {target} ʟᴀɴᴅ ᴄʜᴏᴏꜱ ɴᴏʀᴍɪᴇ 💮🏵️🌸",
        "🌼🌻🌷 {target} ᴄʜᴜᴅ ᴋᴇ ᴅᴀꜰᴀɴ 🌼🌻🌷 ɢᴀʀᴇᴇʙ",
        "🪻🌸💐 {target} ᴛᴇʀɪ ᴍᴀ ʀᴀɴᴅɪ ʜᴀɪ 🪻🌸💐",
        "🌺🌹🌷 {target} ʙʜᴀɢ ᴍᴀᴛ ʀᴀɴᴅɪ ᴋᴇ ʙᴀᴄᴄʜᴇ 🌺🌹🌷",
       
    ],
    2: [
        "{target} [𝙍𝘼𝙉𝘿𝙄] ₰ ────(🤍) ᴛᴇʀɪ ᴍᴀ ᴄʜᴜᴅɪ",
        "{target} [𝙍𝘼𝙉𝘿𝙄] ₪ ────(🤍) ʀᴀɴᴅɪ ᴋᴇ ʙᴀᴄᴄʜᴇ",
        "{target} [𝙍𝘼𝙉𝘿𝙄] ₦ ────(🤍) ɢᴀʀᴇᴇʙ ʜɪᴊᴅᴇ",
        "{target} [𝙍𝘼𝙉𝘿𝙄] ₤ ────(🤍) ʟᴀɴᴅ ʟᴇ ɴᴏʀᴍɪᴇ",
        "{target} [𝙍𝘼𝙉𝘿𝙄] ✯ ────(🤍) ᴄʜᴜᴅ ᴋᴇ ᴅᴀꜰᴀɴ",
        "{target} [𝙍𝘼𝙉𝘿𝙄] ₫ ────(🤍) ᴛᴇʀɪ ᴍᴀ ʀᴀɴᴅɪ",
        "{target} [𝙍𝘼𝙉𝘿𝙄] ₴ ────(🤍) ʙʜᴀɢ ᴍᴀᴛ ɢᴀʀᴇᴇʙ",
        "{target} [𝙍𝘼𝙉𝘿𝙄] Ұ ────(🤍) ʜɪᴊᴅᴇ ᴋɪ ᴀᴜʟᴀᴅ",
    ],
    3: [
        "{target} RNDִֶָ𓂃 ˖ ִֶָ👑་༘࿐ ᴄᴏᴠᴇʀ ᴋᴀʀ",
        "{target} TMKBִֶָ𓂃 ˖ ִֶָ👑་༘࿐ ʙʜᴀɢ ᴍᴀᴛ",
        "{target} CHUDִֶָ𓂃 ˖ ִֶָ👑་༘࿐ ʀᴀɴᴅɪ ᴋᴇ ʙᴀᴄᴄʜᴇ",
        "{target} GAREEBִֶָ𓂃 ˖ ִֶָ👑་༘࿐ ᴛᴇʀɪ ᴍᴀ ᴄʜᴜᴅɪ",
        "{target} HIJDEִֶָ𓂃 ˖ ִֶָ👑་༘࿐ ʟᴀɴᴅ ʟᴇ",
        "{target} TERI MA RNDIִֶָ𓂃 ˖ ִֶָ👑་༘࿐",
        "{target} CVR KRִֶָ𓂃 ˖ ִֶָ👑་༘࿐ ʀᴀɴᴅɪ",
        "{target} KAMZORִֶָ𓂃 ˖ ִֶָ👑་༘࿐ ɢᴀʀᴇᴇʙ",
    ],
}

SLIDER_REPLIES: dict[int, str] = {i: (SLIDER_MESSAGES.get(i, [f"{{target}} — Slider Reply {i}"])[0]) for i in range(1, max(SLIDER_MESSAGES.keys())+1)}

SLIDER_INTERVAL_SECONDS = float(os.getenv("SLIDER_INTERVAL_SECONDS", "0.1"))

MESSAGE_GROUPS: dict[int, list[str]] = {
    1: [
        "{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨 \n{target} Tєяι Mαкι 🎗️ Cнυ∂αι 🎃🧨",
        "{target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n {target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n target} oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤 \n",
        "{target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n {target} 𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶 \n",
        "{target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \nī {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅??𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n {target} 𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥 \n",
        "{target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ {target} 𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ sꪶꪖꪜꫀ ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝗟𝗡𝗗 𝗖𝗛𝗢𝗢𝗦 -/- ⋆˚🤍 ݁˖⭑ \n ",
        "{target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲 \n {target} 𝐂ʜᴀᴍᴀʀ 𝐑ᴀɴᴅɪ 💲💲💲💲💲",
    ],
    2: [
    "{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙎𝙀𝙉𝙋𝘼𝙄 👑",
    "{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝐒ʜɪ𝐕ᴀ𝐌 👑",
    "{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝙀𝙇𝙀𝘾𝙏𝙍𝙊👑",
    "{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝒜𝒯𝐼𝐹 👑",
    "{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓢𝓗𝓐𝓓𝓦𝓞 👑",
    "{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝑹𝑰𝑵𝑲𝑼 👑",
    "{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑\n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑 \n{target} 𝐒ᴀᴠᴅʜᴀɴ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴏᴅɴᴇ 𝐀ᴀʏᴀ 𝓐𝓝𝓢𝓗 👑",
    

    ],
    3: [
        "💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n💲┗━━━━━━━━━━{target}━━━━━━━━━━━━┛💲RANDI\n\n\n\n",
        "💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n💎┗━━━━━━━━━{target}━━━━━━━━━━━━┛💎 CHUD\n\n\n\n",
        "😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n😈┗━━━━━━━━━{target}━━━━━━━━━━━━┛😈 LUND LE\n\n\n\n",
        "🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n🐉┗━━━━━━━━━{target}━━━━━━━━━━━━┛🐉 CHUD RAND\n\n\n\n",
        "🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n🦅┗━━━━━━━━━{target}━━━━━━━━━━━━┛🦅 BUR CHATTA\n\n\n\n",
        "🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n🖤┗━━━━━━━━━{target}━━━━━━━━━━━━┛🖤 CHUD CHAMAR\n\n\n\n",
        "💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n💜┗━━━━━━━━━{target}━━━━━━━━━━━━┛💜 MOTI RAND KE BCHE\n\n\n\n",
        "💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n💙┗━━━━━━━━━{target}━━━━━━━━━━━━┛💙 HIJDE KA BACCHA\n\n\n\n",
        "💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n💚┗━━━━━━━━━{target}━━━━━━━━━━━━┛💚 RANDI PUTAR\n\n\n\n",
        "🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n🩵┗━━━━━━━━━{target}━━━━━━━━━━━━┛🩵 TERI MAA CHDI RE\n\n\n\n",
        "🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n🧡┗━━━━━━━━━{target}━━━━━━━━━━━━┛🧡 LANGDI MAA KE BACCHE\n\n\n\n",
        "❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n❤️┗━━━━━━━━━{target}━━━━━━━━━━━━┛❤️ CHAL BHSODI \n\n\n\n",
        "☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n☠️┗━━━━━━━━━{target}━━━━━━━━━━━━┛☠️ CHDAI NHI RUKEGI\n\n\n\n",
        "🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n🚀┗━━━━━━━━━{target}━━━━━━━━━━━━┛🚀 LUND TE VAJ\n\n\n\n",
        "🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n🏆┗━━━━━━━━━{target}━━━━━━━━━━━━┛🏆 TERI MA SATRRANGI RAND\n\n\n\n",
    ],
    4: [
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴜᴅ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴᴅɪ ᴄʜᴜᴅɪ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ ??༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜᴀᴄʟɪ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴅᴋᴀᴅ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʀᴀɴɪ ʀᴀɴᴅɪ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ʜɪᴊᴅᴇ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴋᴜᴛɪʏᴀ ᴋᴀ ʙʜᴏꜱᴅᴀ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱᴏɴ ᴏꜰ ʙɪᴛᴄʜ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴅᴏɢɢʏ ᴋᴀ ʟᴜɴᴅ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ꜱɪᴛ ᴅᴏᴡɴ ᴛᴏᴍᴍʏ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴄʜᴀʟ ᴍᴀ ᴄʜᴜᴅɴᴀ ᴀᴘɴɪ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪ ᴍᴀᴀ ᴄʜᴜᴅᴏ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ʙʜᴀɢ ᴍᴀᴅᴀʀᴄʜᴏᴅ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴛᴇʀɪᴍᴀᴀ ᴄʜᴜᴅɪ ɢᴀᴍᴇ ᴏᴠᴇʀ\n\n\n\n\n\n",
        "꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n꧁⎝ 𓆩༺{target}༻𓆪 ⎠꧂\n ᴜᴛʜ ᴍᴀᴅᴀʀᴄʜᴏᴅ ᴍᴀʀᴀ ᴋʏᴜ\n\n\n\n\n\n",
    ],
    5: [
        "{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ ⋆⁺₊⋆ ☾ ⋆⁺₊⋆ ☁️︎😂😂\n\n\n\n\n",
        "{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n{target} 𝐊ɪ 𝐌ᴀᴀ 𝐓ᴏ 𝐊ᴀʟɪ 𝐑ᴀɴᴅ 𝐇ᴀɪ 😑🖕🏿🖕🏿\n\n\n\n\n",
        "{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕??🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n{target} 𝐊ᴀ 𝐁ᴀᴀᴘ 𝐒ᴀʙᴊɪ 𝐖ᴀʟᴀ 🤪⭕🤪⭕🤍🤪\n\n",
        "{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n{target} 𝐓ᴇʀɪ ᴛᴏ 𝐌ᴀᴀ 😂🚺 𝐂ʜᴜᴅɪ 𝐍ᴀᴄʜᴏ\n\n\n\n",
        "{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n{target} 𝐓ʀɪᴍᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐌ᴇ 𝐋ᴀᴀᴛ 𝐌ᴀʀᴜ 😹🦵🏻\n\n\n\n",
        "{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n{target} 𝐄ᴋ 𝐋ᴀᴀᴛ 𝐌ᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐁ʜᴏsᴅᴇ 𝐒ᴇ 𝐊ʜᴏᴏɴ 𝐍ɪᴋᴀʟ 𝐃ᴜɢᴀ 🩸🩴🩸🩴🩸🩴🩸\n\n\n\n",
        "{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ ??ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐆ᴜʟᴀᴍ 𝐇ᴀɪ 𝐁ᴇᴛᴇ 😹🐥😹🐥😹🐥\n\n\n",
        "{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴀ 𝐆ᴀʟᴀ 𝐊ᴀᴀᴛ 𝐃ᴜɢᴀ 😑🚩\n\n\n\n",
        "{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐂ʜᴜᴅɪ 𝐉ᴀʏᴇɢɪ 😎🖕🏿🖕🏿 𝐌ᴜᴊʜᴇ 𝐇ᴀʀᴀɴᴇ 𝐌ᴇ\n\n\n\n",
        "{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ ??ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n{target} 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 𝐂ʜᴜᴅɪ 😑🚩😑 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐅ɪʀsᴇ 𝐂ʜᴜᴅɪ\n\n\n\n",
        "{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n{target} 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐁ʜɴ 𝐁ᴀʜᴏᴛ 𝐂ʜᴏᴅ 𝐋ɪʏᴀ 𝐀ʙ 𝐓ᴇʀɪ 𝐁ᴜᴀ 𝐂ʜᴜᴅᴇʜɪ 😈\n\n\n\n\n",
    ],
    6: [
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💟\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🩷\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ??\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁??𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 🖤\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💙\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💜\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🔥\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(??𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 ❤️‍🩹\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💗\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💌\n\n\n\n\n\n\n\n\n\n",
        "𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n𓆩{target}𓆪 𝒫𝒾𝓁𝓁𝑒 #Harsh(𝗢𝗙𝗙) 𝐵𝒶𝒶𝓅 𝒦𝑒 𝒮𝒶𝓂𝓃𝑒 𝒮𝓅𝒶𝓂 𝒦𝓇𝑒𝑔𝒶 💝\n\n\n\n\n\n\n\n\n\n",

          ],
    7: [
        "{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤣\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😂\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😅\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😉\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😋\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🤩\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😍\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😑\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😭\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↔️\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🙂‍↕️\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🥴\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😏\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 🫣\n\n\n\n\n\n",
        "{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n{target} मेरा लंड चूसो ⌁ 😞\n\n\n\n\n\n",
    ],
    8: [
        "𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n𝐀ᴀᴊ 𝐌𝐀ɪ 𝐉ᴀᴀ𝐊ᴇ ꜱ𝐂𝐇ᴏᴏ𝐋 ❤️‍🩹 {target} ᴛ𝐄𝐑ɪ 𝐌ᴀ ᴋ𝐈 𝐂ʜᴜ𝐓 𝐌ᴀᴀ𝐑 ᴋ𝐄 ᴊᴀ𝐔𝐍ɢ𝐀 𝐁𝐇ᴜʟ💋\n\n\n",
        "तेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\nतेरी माँ रंडी है {target} गरीब की औलाद 🤣🤙\n\n\n",
        "{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n{target} की माँ चुद के भाग गई 😂🤙 रंडी की औलाद\n\n\n",
        "भाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\nभाग मत रंडी के बच्चे {target} कवर कर 👞💩\n\n\n",
        "{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n{target} तेरी माँ को ऐसा चोदूँगा gang रैप वाली फील आएगी 🤣🤙\n\n\n",
    ],
    9: [
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 ??𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️❤️‍🔥༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💛༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈?? 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 ??𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅??𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💚༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💙༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁??𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🤍༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n {target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🖤༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️🩶༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁??𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈?? 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💗༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💖༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴??𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💝༻꧂\n\n",
        "{target}  𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n{target} 𝐾𝐼 मां 𝐾𝐸 𝐶𝐻𝑈𝐷 𝑆𝐸 रक्षाबंधन 𝐾𝐼 राखी 𝐵𝐴𝐴𝑁𝐷𝐻 के उसकी 𝐶𝐻𝑈𝐷 𝐾𝐼 रखवाली  𝐾𝐴𝑅𝑈𝑁𝐺𝐴----------------------⚔️💞༻꧂\n\n",
    ],
    10: [
        "{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n{target} ═════════════════════════════════════════════════════════════════════════════════  SENAPI----𝐔𝐍𝐁𝐄𝐀𝐓𝐀𝐁𝐋𝐄 》》》✴.·´¯`·.·★  🎀हवाबाजी करोगे ?? तुम्हारी माँ चोद डालूंगा🙏👅🎀  ★·.·`¯´·.✴ 𝐉𝐀𝐊𝐄 𝐊𝐀𝐇𝐈 𝐀𝐔𝐑 𝐌𝐀𝐀 𝐂𝐇𝐔𝐃𝐖𝐀 𝐆𝐀𝐑𝐈𝐁 ⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ ─── ⋆⋅ ☾⋅⋆ ───⋆｡˚ ☁︎ ˚｡⋆｡˚☽˚｡⋆ \n\n\n",
        "{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚??𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n{target} ______________________________________________________________\n𝗡𝗔 𝗛𝗔𝗧𝗛𝗜 𝗦𝗘 𝗡𝗔𝗔 𝗚𝗛𝗢𝗗𝗘 𝗦𝗘 HATERS 𝗞𝗜 𝗠𝗔𝗔 𝗖𝗛𝗨𝗗𝗘𝗚𝗜 𝗦𝗛𝗜𝗥𝗙 𝗗𝗥𝗔𝗞𝗘 𝗞𝗘 𝗟𝗢𝗗𝗘 𝗦𝗘 ??_______________\n\n",
        "{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?{target} ______________________________________________________________\nHATERS  ᴋᴀʙ ᴛᴀᴋ ᴀᴘɴᴀ ᴄʜᴜᴛ ʙᴀᴄʜᴀʏᴇɢᴀ?",
        "{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______{target} ____________________________________________________________________________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ_______",
        " _______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ_______________________________\nHATERS  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ ⁿᵃ ???? ʰᵉ ʰᵉ ʰᵉ____________________\n_______________________________\n{target}  ᵗᵉʳⁱ ᵐᵃ ʳᵃⁿᵈⁱ ᵒᵏ",
        "{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌??𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇??𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍{target} 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃 𝐓𝐔𝐌𝐇𝐀𝐑𝐈 𝐌𝐀𝐀𝐊𝐈 𝐆𝐀𝐍𝐃 𝐌𝐄 𝐇𝐀𝐓𝐇𝐈 𝐊𝐀𝐀 𝐋𝐀𝐍𝐃 𝐅𝐀𝐒𝐀𝐀 𝐃𝐔𝐍𝐆𝐀\\n🥰🤤😍🤤😍🥰🤤😍🤤🥰😍",
        "< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n< {target} > रैंडी 🦋\n",
        "↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n↗️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↙️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↕️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↖️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗  ↔️\", \"{target} 𝗚𝗨𝗟𝗔𝗠 𝗞𝗨𝗧𝗧𝗜𝗬𝗔 𝗖𝗛𝗨𝗗\n\n\n",
        "{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n{target} 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🪲🤖!\",\n\" {target}♪\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ!\",\n\" {target}ᴥ︎︎︎\n\n 𝙏𝙀𝙍𝙄 𝙈𝘼𝘼 चुददकड़ हाँ🌳!\",\n\" {target}🂱\n\n\n",
        "₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n₍🌑₎ Mᴀᴋɪ Cʜᴜᴛ KAʟɪ {target} Tᴇʀɪ\n\n\n₍🌒₎Bᴇʜᴇɴ Bʜᴀɴɢɪ________________________________________{target} Tᴇʀɪ\n\n\n₍🌔₎ Bᴀᴀᴘ Cʜᴀᴍᴀʀ {target} Tᴇʀᴀ\n\n\n₍🌔₎ Kᴜᴛɪᴀ »»-----------► {target} Kᴀᴍᴢᴏʀ\n\n\n₍🌕₎Tʜᴜɴᴅᴇʀ Usᴇʀ_/__/{target} Sᴘᴀᴍᴍᴇʀ Bᴀɴᴇɢᴀ\n\n\n",
        "{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n{target} 𓊈 👾 𓊉 #𝔻𝕆𝔾L#ℙ𝔸𝔻𝕀#ℙℝ𝔸𝕋𝕀𝕂#𝔻𝕆𝔾𝔾𝕐#𝔽𝕆ℝ𝕍𝔼ℝ𝕏#ℙℝ𝔼𝕄  #ₜₑᵣᵢ#ₘₐₐ#CₕₒDₖₑ#Gₐₜₐᵣ#Bₐₙₐ#DᵢYₐ\n\n\n\n",
        "{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎??︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n{target} 🆂︎🆄︎🅹︎🅰︎🅻︎ 🆃︎🅴︎🆁︎🅸︎ 🅼︎🅰︎ 🅲︎🅾︎🅳︎🆄︎.𖥔 ݁ ˖🛸── .✦.𖥔 ݁ ˖🛸── .✦\n\n\n\n\n\n\n\n\n\n\n",
        "⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n⟨#{target}⟩  ✩‧₊𝐋ᴜɴᴅ 𝐊ʜᴀ ˖ ᡣ𐭩 ⊹🪼\",\n",
        "{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n{target} #𝗥ɴ𝗗ɪ𝗞ᴇ #𝗟ᴀ𝗥ᴄ𝗘 #ɪ𝗗ʜ𝗔ʀ #𝗔ᴀ #𝗧ᴇ𝗥ɪ #𝗠ᴏ𝗠ᴍ𝗬 #ᴋ𝗢 #s𝗣ᴍ𝗠ᴇ𝗥 #ʙ𝗔ɴ𝗔ᴜ #𝗠ᴄ 𓆩🤍𓆪\\n  \\n \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n  \\n\n\n\n\n",
        "{target} ɢᴜʟᴀᴀᴍᴏ ɢᴀɴᴅ ᴋᴀ sᴀʀᴀ ᴊᴏʀ ʟɢᴀ ʟᴏ sɴᴀᴘᴘʏ ᴀʙʙᴜ ᴋᴏ ᴄᴠʀ ɴᴀ ᴋʀ ᴘᴀᴏɢᴇ🔫🍑 ྀ࿐ ˊˎ-𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ  𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ  𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ  𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ  𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ  𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ  𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ  𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ 𒁂 𒁂 ʚɞ𒁂 𒁂 𒁂 𒁂  ",
        "{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n{target}  RiS ɴᴇᴡ ɢᴇɴ ᴘɪʟʟᴀ ⃖‹💜›⃗\n\n\n\n\n\n\n",
    ],
}

MESSAGES = {message_id: group[0] for message_id, group in MESSAGE_GROUPS.items() if group}

_SUDO_DB_LOCK = asyncio.Lock()

@asynccontextmanager
async def get_db():
    """Async context manager for SQLite database connection."""
    async with aiosqlite.connect(DATABASE_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("PRAGMA busy_timeout = 30000"):
            pass
        yield db

async def init_db() -> None:
    """Initialize SQLite database tables and migrate legacy schema if needed."""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nc_states (
                bot_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                nc_profile INTEGER NOT NULL,
                current_index INTEGER DEFAULT 0,
                decoration_index INTEGER DEFAULT 0,
                is_running INTEGER DEFAULT 0,
                target_name TEXT DEFAULT 'User',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bot_id, chat_id, nc_profile)
            )
        """)
        await db.commit()

        async with db.execute("PRAGMA table_info('nc_states')") as cursor:
            columns = [row["name"] for row in await cursor.fetchall()]

        if "decoration_index" not in columns:
            await db.execute(
                "ALTER TABLE nc_states ADD COLUMN decoration_index INTEGER DEFAULT 0"
            )
            await db.commit()

        if "bot_id" not in columns:
            logger.info("Legacy database schema detected; migrating nc_states to include bot_id.")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS nc_states_new (
                    bot_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    nc_profile INTEGER NOT NULL,
                    current_index INTEGER DEFAULT 0,
                    decoration_index INTEGER DEFAULT 0,
                    is_running INTEGER DEFAULT 0,
                    target_name TEXT DEFAULT 'User',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (bot_id, chat_id, nc_profile)
                )
            """)
            await db.execute(
                "INSERT OR REPLACE INTO nc_states_new (bot_id, chat_id, nc_profile, current_index, decoration_index, is_running, target_name, updated_at)"
                " SELECT 0, chat_id, nc_profile, current_index, 0, is_running, target_name, updated_at FROM nc_states"
            )
            await db.execute("DROP TABLE nc_states")
            await db.execute("ALTER TABLE nc_states_new RENAME TO nc_states")
            await db.commit()
            logger.info("Legacy database migration completed.")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sudo_users (
                bot_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (bot_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_profile_photos (
                bot_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                file_id TEXT NOT NULL,
                PRIMARY KEY (bot_id, chat_id, file_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS help_profile_photos (
                bot_id BIGINT NOT NULL PRIMARY KEY,
                file_id TEXT NOT NULL
            )
        """)
        await db.commit()

async def migrate_legacy_state_to_bot(bot_id: int, chat_id: int, nc_profile: int) -> None:
    """Move legacy rows with bot_id=0 to the current bot id."""
    async with get_db() as db:
        await db.execute(
            "UPDATE nc_states SET bot_id = ? WHERE bot_id = 0 AND chat_id = ? AND nc_profile = ?",
            (bot_id, chat_id, nc_profile)
        )
        await db.commit()

async def get_nc_state(bot_id: int, chat_id: int, nc_profile: int) -> Optional[dict]:
    """Retrieve NC state for a specific bot, chat and profile."""
    async with get_db() as db:
        async with db.execute(
            "SELECT bot_id, chat_id, nc_profile, current_index, decoration_index, is_running, target_name FROM nc_states WHERE bot_id IN (?, 0) AND chat_id = ? AND nc_profile = ? ORDER BY bot_id DESC LIMIT 1",
            (bot_id, chat_id, nc_profile)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                state = {
                    "bot_id": row["bot_id"],
                    "chat_id": row["chat_id"],
                    "nc_profile": row["nc_profile"],
                    "current_index": row["current_index"],
                    "decoration_index": row["decoration_index"],
                    "is_running": bool(row["is_running"]),
                    "target_name": row["target_name"]
                }
                logger.debug("Loaded NC state for bot=%s chat=%s profile=%s target=%r", bot_id, chat_id, nc_profile, state["target_name"])
                return state
            return None

async def save_nc_state(bot_id: int, chat_id: int, nc_profile: int, current_index: int, decoration_index: int, is_running: bool, target_name: str) -> None:
    """Insert or update NC state for a specific bot, chat and profile."""
    async with get_db() as db:
        await db.execute("""
            INSERT INTO nc_states (bot_id, chat_id, nc_profile, current_index, decoration_index, is_running, target_name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(bot_id, chat_id, nc_profile) DO UPDATE SET
                current_index = excluded.current_index,
                decoration_index = excluded.decoration_index,
                is_running = excluded.is_running,
                target_name = excluded.target_name,
                updated_at = CURRENT_TIMESTAMP
        """, (bot_id, chat_id, nc_profile, current_index, decoration_index, int(is_running), target_name))
        await db.commit()
        logger.debug("Saved NC state for bot=%s chat=%s profile=%s target=%r is_running=%s", bot_id, chat_id, nc_profile, target_name, is_running)

async def set_nc_running(bot_id: int, chat_id: int, nc_profile: int, is_running: bool) -> None:
    """Update running status for a profile in a group."""
    async with get_db() as db:
        await db.execute("""
            UPDATE nc_states SET is_running = ?, updated_at = CURRENT_TIMESTAMP WHERE bot_id = ? AND chat_id = ? AND nc_profile = ?
        """, (int(is_running), bot_id, chat_id, nc_profile))
        await db.commit()

async def stop_all_nc_for_chat(bot_id: int, chat_id: int) -> list[int]:
    """Stop all active profiles for a chat and return list of stopped profile IDs."""
    async with get_db() as db:
        async with db.execute(
            "SELECT nc_profile FROM nc_states WHERE bot_id = ? AND chat_id = ? AND is_running = 1",
            (bot_id, chat_id)
        ) as cursor:
            rows = await cursor.fetchall()
            active_profiles = [row["nc_profile"] for row in rows]

        if active_profiles:
            await db.execute(
                "UPDATE nc_states SET is_running = 0, updated_at = CURRENT_TIMESTAMP WHERE bot_id = ? AND chat_id = ?",
                (bot_id, chat_id)
            )
            await db.commit()
        return active_profiles

async def get_active_nc_states(bot_id: int) -> list[dict]:
    """Get all active NC states for a specific bot."""
    async with get_db() as db:
        async with db.execute(
            "SELECT bot_id, chat_id, nc_profile, current_index, decoration_index, is_running, target_name FROM nc_states WHERE bot_id = ? AND is_running = 1",
            (bot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "bot_id": row["bot_id"],
                    "chat_id": row["chat_id"],
                    "nc_profile": row["nc_profile"],
                    "current_index": row["current_index"],
                    "decoration_index": row["decoration_index"],
                    "is_running": bool(row["is_running"]),
                    "target_name": row["target_name"]
                }
                for row in rows
            ]

async def get_active_nc_target_for_chat(bot_id: int, chat_id: int) -> Optional[str]:
    """Retrieve the active NC target for a specific bot and chat."""
    async with get_db() as db:
        async with db.execute(
            "SELECT target_name FROM nc_states WHERE bot_id IN (?, 0) AND chat_id = ? AND is_running = 1 ORDER BY nc_profile ASC LIMIT 1",
            (bot_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row["target_name"] if row else None

async def get_chat_active_profiles(bot_id: int, chat_id: int) -> list[int]:
    """Get list of active profile IDs for a specific chat and bot."""
    async with get_db() as db:
        async with db.execute(
            "SELECT nc_profile FROM nc_states WHERE bot_id = ? AND chat_id = ? AND is_running = 1 ORDER BY nc_profile ASC",
            (bot_id, chat_id)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row["nc_profile"] for row in rows]

async def validate_group_nc_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str]:
    """Validate chat type, bot admin rights, and user admin rights."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False, "Invalid context for command execution."

    if chat.type not in ["group", "supergroup"]:
        return False, "❌ This command can only be used inside a Group or Supergroup."

    try:
        bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            return False, "❌ I must be an administrator in this group to change the title."
        if not getattr(bot_member, "can_change_info", False):
            return False, "❌ I lack permission to change group info. Please grant me 'Change Group Info' admin permission."
    except Exception as e:
        logger.exception("Failed checking bot permissions in chat %s", chat.id)
        return False, f"❌ Unable to verify bot administrator permissions: {type(e).__name__}: {e}"

    try:
        if is_owner(user.id):
            return True, ""

        user_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
        if user_member.status == "creator":
            return True, ""
        elif user_member.status == "administrator":
            if getattr(user_member, "can_change_info", True):
                return True, ""
            return False, "❌ You must have 'Change Group Info' permission."
        else:
            return False, "❌ Only group administrators can use this command."
    except Exception as e:
        logger.exception("Failed checking user permissions in chat %s", chat.id)
        return False, f"❌ Unable to verify your administrator permissions: {type(e).__name__}: {e}"

async def is_sudo(user_id: int, bot_id: int) -> bool:
    """Return whether the user is a sudo user for the current bot."""
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM sudo_users WHERE bot_id = ? AND user_id = ? LIMIT 1",
            (bot_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def has_permission(user_id: int, bot_id: int, allow_sudo: bool = False) -> bool:
    """Return whether the user is authorized as owner or sudo for the current bot."""
    owner = is_owner(user_id)
    sudo = await is_sudo(user_id, bot_id) if bot_id is not None else False
    allowed = owner or (allow_sudo and sudo)
    logger.info(
        "[SUDO] command=permission sender_id=%s bot_id=%s is_owner=%s is_sudo=%s allowed=%s",
        user_id,
        bot_id,
        owner,
        sudo,
        allowed,
    )
    return allowed

async def require_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Require the command sender to be an owner."""
    user = update.effective_user
    bot_id = getattr(context.bot, "id", None)
    if not user or bot_id is None or not await has_permission(user.id, bot_id, allow_sudo=False):
        await senpai_reply(update, "𝐂ʜᴀʟ 𝐃ᴜʀ 𝐇ᴀᴀᴛ 𝐑ᴀɴᴅɪᴋᴇ 🖕🏻😂")
        return False
    return True

async def add_sudo_user(bot_id: int, user_id: int) -> bool:
    async with _SUDO_DB_LOCK:
        async with get_db() as db:
            async with db.execute(
                "INSERT OR IGNORE INTO sudo_users (bot_id, user_id) VALUES (?, ?)",
                (bot_id, user_id)
            ) as cursor:
                insert_result = cursor.rowcount
            await db.commit()
            logger.info("[SUDO] INSERT result=%s", insert_result)
            async with db.execute(
                "SELECT 1 FROM sudo_users WHERE bot_id = ? AND user_id = ? LIMIT 1",
                (bot_id, user_id)
            ) as cursor:
                verified = await cursor.fetchone() is not None
            logger.info("[SUDO] VERIFY result=%s", verified)
            return verified

async def remove_sudo_user(bot_id: int, user_id: int) -> bool:
    async with _SUDO_DB_LOCK:
        async with get_db() as db:
            async with db.execute(
                "SELECT 1 FROM sudo_users WHERE bot_id = ? AND user_id = ? LIMIT 1",
                (bot_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return False
            async with db.execute(
                "DELETE FROM sudo_users WHERE bot_id = ? AND user_id = ?",
                (bot_id, user_id)
            ):
                pass
            await db.commit()
            return True

async def get_sudo_user_ids(bot_id: int) -> list[int]:
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id FROM sudo_users WHERE bot_id = ? ORDER BY user_id ASC",
            (bot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row["user_id"] for row in rows]

async def add_group_profile_photo(bot_id: int, chat_id: int, file_id: str) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM group_profile_photos WHERE bot_id = ? AND chat_id = ? AND file_id = ? LIMIT 1",
            (bot_id, chat_id, file_id)
        ) as cursor:
            existing = await cursor.fetchone()
        if existing:
            return False

        await db.execute(
            "INSERT INTO group_profile_photos (bot_id, chat_id, file_id) VALUES (?, ?, ?)",
            (bot_id, chat_id, file_id)
        )
        await db.commit()
        return True

async def get_saved_group_profile_photo_ids(bot_id: int, chat_id: int) -> list[str]:
    async with get_db() as db:
        async with db.execute(
            "SELECT file_id FROM group_profile_photos WHERE bot_id = ? AND chat_id = ? ORDER BY file_id ASC",
            (bot_id, chat_id)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row["file_id"] for row in rows]


async def set_help_profile_photo(bot_id: int, file_id: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO help_profile_photos (bot_id, file_id) VALUES (?, ?)"
            " ON CONFLICT(bot_id) DO UPDATE SET file_id = excluded.file_id",
            (bot_id, file_id)
        )
        await db.commit()


async def get_help_profile_photo(bot_id: int) -> Optional[str]:
    async with get_db() as db:
        async with db.execute(
            "SELECT file_id FROM help_profile_photos WHERE bot_id = ? LIMIT 1",
            (bot_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["file_id"] if row else None


async def get_latest_group_profile_photo(bot_id: int, chat_id: int) -> Optional[str]:
    """Return the most recently inserted saved group profile photo for the given bot/chat.

    Uses SQLite ROWID ordering so the latest INSERT is considered the active photo without
    changing the existing table schema.
    """
    async with get_db() as db:
        async with db.execute(
            "SELECT file_id FROM group_profile_photos WHERE bot_id = ? AND chat_id = ? ORDER BY rowid DESC LIMIT 1",
            (bot_id, chat_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row["file_id"] if row else None

async def remove_group_profile_photo(bot_id: int, chat_id: int, file_id: str) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM group_profile_photos WHERE bot_id = ? AND chat_id = ? AND file_id = ? LIMIT 1",
            (bot_id, chat_id, file_id)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False
        await db.execute(
            "DELETE FROM group_profile_photos WHERE bot_id = ? AND chat_id = ? AND file_id = ?",
            (bot_id, chat_id, file_id)
        )
        await db.commit()
        return True

async def require_owner_or_sudo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    bot_id = getattr(context.bot, "id", None)
    if not user or bot_id is None or not await has_permission(user.id, bot_id, allow_sudo=True):
        await senpai_reply(update, "🐶 Shu Shu Doggy")
        return False
    return True

async def require_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Allow only Telegram group admins when the bot is an administrator too."""
    user = update.effective_user
    chat = update.effective_chat
    bot_id = getattr(context.bot, "id", None)
    if not user or not chat or bot_id is None or chat.type not in {"group", "supergroup"}:
        await senpai_reply(update, "🐶 Shu Shu Doggy")
        return False

    try:
        user_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
        if getattr(user_member, "status", None) not in {"creator", "administrator"}:
            await senpai_reply(update, "🐶 Shu Shu Doggy")
            return False

        bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=bot_id)
        if getattr(bot_member, "status", None) not in {"creator", "administrator"}:
            await senpai_reply(update, "❌ I must be an administrator in this group.")
            return False
    except Exception:
        await senpai_reply(update, "🐶 Shu Shu Doggy")
        return False

    return True

async def sudo_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    parts = text.split(None, 1)
    command_token = parts[0].lstrip("/").split("@", 1)[0].lower()
    bot_id = getattr(context.bot, "id", None)
    sender_id = getattr(update.effective_user, "id", None)
    owner = sender_id is not None and is_owner(sender_id)
    sudo = sender_id is not None and bot_id is not None and await is_sudo(sender_id, bot_id)
    logger.info(
        "[SUDO] command=%s sender_id=%s bot_id=%s is_owner=%s is_sudo=%s",
        command_token,
        sender_id,
        bot_id,
        owner,
        sudo,
    )

    if command_token in {"addsudo", "delsudo"}:
        if not await require_owner(update, context):
            return
    elif not await require_owner_or_sudo(update, context):
        return

    def get_target_user_id() -> Optional[int]:
        if len(parts) >= 2 and parts[1].strip():
            candidate = parts[1].strip().split()[0]
            if candidate.isdigit():
                return int(candidate)
            return None

        reply = update.message.reply_to_message
        if reply and reply.from_user and getattr(reply.from_user, "id", None) is not None:
            return reply.from_user.id
        return None

    parts = text.split(None, 1)

    if command_token == "addsudo":
        user_id = get_target_user_id()
        if user_id is None:
            await senpai_reply(update, 
                "❌ Usage: /addsudo <user_id> or reply to a user with /addsudo"
            )
            return

        logger.info("[SUDO] ADD target_user_id=%s", user_id)
        if is_owner(user_id):
            await senpai_reply(update, 
                "ℹ️ This user is already an owner and does not need sudo access."
            )
            return

        added = await add_sudo_user(bot_id, user_id)
        if added:
            await senpai_reply(update, 
                f"✅ User `{user_id}` 𝗟𝗘 𝗔𝗕 𝗦𝗘𝗡𝗣𝗔𝗜 𝗞𝗢 𝗕𝗔𝗔𝗣 𝗕𝗔𝗡𝗔",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await senpai_reply(update, 
                f"ℹ️ User `{user_id}` is already in the sudo list.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if command_token == "delsudo":
        user_id = get_target_user_id()
        if user_id is None:
            await senpai_reply(update, 
                "❌ Usage: /delsudo <user_id> or reply to a user with /delsudo"
            )
            return

        if is_owner(user_id):
            await senpai_reply(update, 
                "❌ Cannot remove the bot owner from sudo access."
            )
            return

        removed = await remove_sudo_user(bot_id, user_id)
        if removed:
            await senpai_reply(update, 
                f"✅ User `{user_id}` has been removed from the sudo list.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await senpai_reply(update, 
                f"ℹ️ User `{user_id}` was not found in the sudo list.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if command_token == "sudolist":
        sudo_ids = await get_sudo_user_ids(bot_id)
        if not sudo_ids:
            await senpai_reply(update, "ℹ️ There are no sudo users configured for this bot.")
            return

        sudo_list_text = "\n".join(f"- `{user_id}`" for user_id in sudo_ids)
        await senpai_reply(update, 
            f"🔐 <b>Sudo Users</b>\n\n{sudo_list_text}",
            parse_mode=ParseMode.HTML,
        )
        return

    await senpai_reply(update, "❌ Unknown sudo command. Available: /addsudo, /delsudo, /sudolist")

class NCEngine:
    """NC rotation task manager and background execution loop."""

    def __init__(self) -> None:
        self._active_tasks: dict[tuple[int, int, int], asyncio.Task] = {}

    def is_running(self, bot_id: int, chat_id: int, nc_profile: int) -> bool:
        task = self._active_tasks.get((bot_id, chat_id, nc_profile))
        return task is not None and not task.done()

    async def start_nc_profile(
        self,
        bot_id: int,
        chat_id: int,
        nc_profile: int,
        target_name: str,
        context: ContextTypes.DEFAULT_TYPE
    ) -> tuple[bool, str]:
        collection = NC_NAMES.get(nc_profile)
        if collection is None:
            return False, f"❌ Invalid NC profile `NC{nc_profile}`. Available profiles: 1-{len(NC_NAMES)}."

        if not collection:
            return False, f"⚠️ NC{nc_profile} collection is empty! Please add names in `NC_NAMES`."

        await self.stop_nc_profile(bot_id, chat_id, nc_profile)

        existing_state = await get_nc_state(bot_id, chat_id, nc_profile)
        current_index = existing_state["current_index"] if existing_state else 0
        decoration_index = existing_state["decoration_index"] if existing_state else 0

        await save_nc_state(
            bot_id=bot_id,
            chat_id=chat_id,
            nc_profile=nc_profile,
            current_index=current_index,
            decoration_index=decoration_index,
            is_running=True,
            target_name=target_name
        )

        logger.info("DEBUG NC: creating task")
        task = asyncio.create_task(
            self._run_nc_loop(bot_id, chat_id, nc_profile, target_name, current_index, decoration_index, context)
        )
        logger.info("DEBUG NC: task created")
        task_key = (bot_id, chat_id, nc_profile)
        self._active_tasks[task_key] = task

        async def restart_nc() -> None:
            await save_nc_state(
                bot_id=bot_id, chat_id=chat_id, nc_profile=nc_profile,
                current_index=current_index, decoration_index=decoration_index,
                is_running=True, target_name=target_name,
            )
            await self._run_nc_loop(
                bot_id, chat_id, nc_profile, target_name, current_index, decoration_index, context
            )

        _install_task_restart_supervisor(
            task, self._active_tasks, task_key, restart_nc, f"NC{nc_profile} {chat_id}",
        )
        logger.info("DEBUG NC: task registered")
        logger.info("TASK CREATED key=%s bot=%s chat=%s profile=%s active_count=%d", task_key, bot_id, chat_id, nc_profile, len(self._active_tasks))
        logger.info("TASK REGISTERED key=%s", task_key)
        logger.info("Started NC%d task for chat %d with target '%s'", nc_profile, chat_id, target_name)
        return True, f"✅ **NC{nc_profile} Started**\nTarget: `{target_name}`\nInterval: `{ROTATION_INTERVAL_SECONDS}s`"

    async def stop_nc_profile(self, bot_id: int, chat_id: int, nc_profile: int) -> tuple[bool, str]:
        task_key = (bot_id, chat_id, nc_profile)
        task = self._active_tasks.pop(task_key, None)

        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.exception(
                    "Existing NC task raised when awaited during stop_nc_profile for key=%s: %s",
                    task_key,
                    e,
                )

        logger.info("TASK CANCELLED key=%s active_count=%d", task_key, len(self._active_tasks))

        await set_nc_running(bot_id, chat_id, nc_profile, is_running=False)
        logger.info("Stopped NC%d task for chat %d (bot %d)", nc_profile, chat_id, bot_id)
        return True, f"⏹️ **NC{nc_profile} Stopped** for this group."

    async def stop_all_nc(self, bot_id: int, chat_id: int) -> tuple[bool, str]:
        keys_to_remove = [k for k in self._active_tasks if k[0] == bot_id and k[1] == chat_id]
        cancelled_tasks = []
        for key in keys_to_remove:
            task = self._active_tasks.pop(key, None)
            if task and not task.done():
                logger.info("TASK CANCELLED key=%s (stop_all_nc)", key)
                task.cancel()
                cancelled_tasks.append(task)

        if cancelled_tasks:
            try:
                results = await asyncio.gather(*cancelled_tasks, return_exceptions=True)
                logger.info("stop_all_nc awaited %d cancelled tasks, results=%s", len(cancelled_tasks), results)
            except Exception:
                pass

        stopped_profiles = await stop_all_nc_for_chat(bot_id, chat_id)
        if not stopped_profiles:
            return True, "𝐍ᴄ 𝐒ᴇ 𝐂ʜᴜᴅᴀɪ 𝐑ᴏᴋ 𝐃ɪʏᴀ 🚩"

        profiles_str = ", ".join(f"NC{p}" for p in sorted(stopped_profiles))
        logger.info("Stopped all NC tasks (%s) for chat %d", profiles_str, chat_id)
        return True, f"⏹️ **Stopped All Active NC Profiles** ({profiles_str})."

    async def _run_nc_loop(
        self,
        bot_id: int,
        chat_id: int,
        nc_profile: int,
        target_name: str,
        start_index: int,
        start_decoration_index: int,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        collection = NC_NAMES.get(nc_profile, [])
        if not collection:
            return

        current_index = start_index
        decoration_index = start_decoration_index
        task_key = (bot_id, chat_id, nc_profile)
        retry_attempt = 0

        try:
            while True:
                current_index %= len(collection)
                raw_name = collection[current_index]
                formatted_name = raw_name.replace("{target}", target_name) if "{target}" in raw_name else raw_name
                decoration = DECORATIONS[decoration_index % len(DECORATIONS)]
                title = f"{formatted_name} {decoration}"[:255]

                try:
                    bot_idx = get_bot_index(bot_id)
                    while not is_bot_ready(bot_idx):
                        await asyncio.sleep(min(0.5, max(0.0, BOT_COOLDOWNS.get(bot_idx, 0.0) - time.time())))
                    await bot_adaptive_rate_limiter.wait(bot_id)
                    await bot_chat_rate_limiter.wait(bot_id, chat_id)
                    await context.bot.set_chat_title(chat_id=chat_id, title=title)
                    current_index = (current_index + 1) % len(collection)
                    decoration_index = (decoration_index + 1) % len(DECORATIONS)
                    await save_nc_state(
                        bot_id=bot_id, chat_id=chat_id, nc_profile=nc_profile,
                        current_index=current_index, decoration_index=decoration_index,
                        is_running=True, target_name=target_name,
                    )
                    retry_attempt = 0
                    await bot_adaptive_rate_limiter.success(bot_id)

                except RetryAfter as e:
                    retry_attempt = 0
                    bot_idx = get_bot_index(bot_id)
                    set_bot_cooldown(bot_idx, float(e.retry_after))
                    await bot_adaptive_rate_limiter.pressure(bot_id, float(e.retry_after))
                    await asyncio.sleep(float(e.retry_after) + TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS)
                    continue

                except (NetworkError, TimedOut, asyncio.TimeoutError, ConnectionError, OSError) as e:
                    retry_attempt += 1
                    set_bot_cooldown(get_bot_index(bot_id), min(5.0, max(1.0, _background_retry_delay(retry_attempt))))
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                    continue

                except BadRequest as e:
                    err_msg = str(e).lower()
                    if "not modified" in err_msg:
                        current_index = (current_index + 1) % len(collection)
                        decoration_index = (decoration_index + 1) % len(DECORATIONS)
                        retry_attempt = 0
                        await asyncio.sleep(ROTATION_INTERVAL_SECONDS)
                        continue
                    # Keep the lifecycle alive; a Telegram-side state can become valid later.
                    retry_attempt += 1
                    await asyncio.sleep(BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS)
                    continue

                except Forbidden:
                    # Permission can change while the process is running; do not kill the background task.
                    await asyncio.sleep(BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS)
                    continue

                except TelegramError:
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                    continue

                except Exception:
                    # DB/runtime hiccups must not silently terminate the long-lived task.
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                    continue

                await asyncio.sleep(ROTATION_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            raise
        finally:
            self._active_tasks.pop(task_key, None)
            try:
                await set_nc_running(bot_id, chat_id, nc_profile, is_running=False)
            except Exception:
                pass

    async def resume_active_tasks(self, application) -> None:
        bot_id = application.bot.id
        active_states = await get_active_nc_states(bot_id=bot_id)
        if not active_states:
            return

        logger.info("Resuming %d active NC tasks from database for bot %s...", len(active_states), bot_id)
        for state in active_states:
            chat_id = state["chat_id"]
            nc_profile = state["nc_profile"]
            target_name = state["target_name"]
            current_index = state["current_index"]
            decoration_index = state["decoration_index"]

            collection = NC_NAMES.get(nc_profile, [])
            if not collection:
                await set_nc_running(bot_id, chat_id, nc_profile, is_running=False)
                continue

            class DummyContext:
                def __init__(self, bot):
                    self.bot = bot

            if self.is_running(bot_id, chat_id, nc_profile):
                logger.info("Skipping resume for NC%d chat %d: task already running", nc_profile, chat_id)
                continue

            ctx = DummyContext(application.bot)
            task = asyncio.create_task(
                self._run_nc_loop(bot_id, chat_id, nc_profile, target_name, current_index, decoration_index, ctx)
            )
            resume_key = (bot_id, chat_id, nc_profile)
            self._active_tasks[resume_key] = task

            async def restart_resumed_nc() -> None:
                await save_nc_state(
                    bot_id=bot_id, chat_id=chat_id, nc_profile=nc_profile,
                    current_index=current_index, decoration_index=decoration_index,
                    is_running=True, target_name=target_name,
                )
                await self._run_nc_loop(
                    bot_id, chat_id, nc_profile, target_name, current_index, decoration_index, ctx
                )

            _install_task_restart_supervisor(
                task, self._active_tasks, resume_key, restart_resumed_nc, f"NC{nc_profile} {chat_id}",
            )
            logger.info("Resumed NC%d task for chat %d for bot %s", nc_profile, chat_id, bot_id)

nc_engine = NCEngine()

class MessageScheduler:
    """Background message sender task manager."""

    def __init__(self) -> None:
        self._active_tasks: dict[tuple[int, int, int], asyncio.Task] = {}

    def is_running(self, bot_id: int, chat_id: int, message_id: int) -> bool:
        task = self._active_tasks.get((bot_id, chat_id, message_id))
        return task is not None and not task.done()

    def get_active_chat_message_ids(self, bot_id: int, chat_id: int) -> list[int]:
        return sorted(
            message_id
            for (b_id, c_id, message_id), task in self._active_tasks.items()
            if b_id == bot_id and c_id == chat_id and not task.done()
        )

    async def start_message_task(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        group_templates: list[str],
        target: str,
        bot,
    ) -> tuple[bool, str]:
        if message_id not in MESSAGE_GROUPS or not MESSAGE_GROUPS[message_id]:
            return False, f"❌ Invalid message option `/msg{message_id}`. Available options: 1-{len(MESSAGE_GROUPS)}."

        rendered_messages = [
            render_message_template(template, target)
            for template in group_templates
        ]

        task_key = (bot_id, chat_id, message_id)
        existing = self._active_tasks.get(task_key)
        if existing and not existing.done():
            return False, f"⚠️ `/mgs{message_id}` with this target is already running in this chat. Use `/stopspam` to stop all scheduled messages."

        task = asyncio.create_task(
            self._run_message_loop(bot_id, chat_id, message_id, rendered_messages, bot)
        )
        self._active_tasks[task_key] = task
        _install_task_restart_supervisor(
            task, self._active_tasks, task_key,
            lambda: self._run_message_loop(bot_id, chat_id, message_id, rendered_messages, bot),
            f"message {task_key}",
        )
        logger.info("Started message task %s for chat %s", task_key, chat_id)
        return True, (
            f"✅ **Scheduled Message /mgs{message_id} Started**\n"
            f"Interval: `{MESSAGE_SEND_INTERVAL_SECONDS}s`\n"
            f"Stop with `/stopspam`."
        )

    async def stop_message_task(self, bot_id: int, chat_id: int, message_id: int) -> tuple[bool, str]:
        task_key = (bot_id, chat_id, message_id)
        task = self._active_tasks.pop(task_key, None)
        if not task or task.done():
            return False, f"ℹ️ No running scheduled message `/msg{message_id}` was found in this chat."

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Exception while stopping message task %s: %s", task_key, e)

        logger.info("Stopped message task %s", task_key)
        return True, f"⏹️ **Stopped scheduled message** `/msg{message_id}`."

    async def cancel_chat_tasks(self, bot_id: int, chat_id: int) -> None:
        tasks = []
        keys_to_remove = [key for key in self._active_tasks if key[0] == bot_id and key[1] == chat_id]
        for key in keys_to_remove:
            task = self._active_tasks.pop(key, None)
            if task and not task.done():
                task.cancel()
                tasks.append(task)

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("Cancelled %d message tasks for chat %s. Results=%s", len(tasks), chat_id, results)

    async def cancel_all_tasks(self) -> None:
        if not self._active_tasks:
            return

        tasks = list(self._active_tasks.values())
        self._active_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Cancelled %d message tasks. Results=%s", len(tasks), results)

    async def _run_message_loop(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        message_texts: list[str],
        bot,
    ) -> None:
        task_key = (bot_id, chat_id, message_id)
        message_index = 0
        retry_attempt = 0
        try:
            while True:
                if not message_texts:
                    await asyncio.sleep(5)
                    continue
                message_text = message_texts[message_index % len(message_texts)]
                try:
                    bot_idx = get_bot_index(bot_id)
                    while not is_bot_ready(bot_idx):
                        await asyncio.sleep(min(0.5, max(0.0, BOT_COOLDOWNS.get(bot_idx, 0.0) - time.time())))
                    await bot_adaptive_rate_limiter.wait(bot_id)
                    await bot_chat_rate_limiter.wait(bot_id, chat_id)
                    await bot.send_message(chat_id=chat_id, text=message_text)
                    message_index = (message_index + 1) % len(message_texts)
                    retry_attempt = 0
                    await bot_adaptive_rate_limiter.success(bot_id)
                    await asyncio.sleep(MESSAGE_SEND_INTERVAL_SECONDS)
                except RetryAfter as e:
                    retry_attempt = 0
                    bot_idx = get_bot_index(bot_id)
                    set_bot_cooldown(bot_idx, float(e.retry_after))
                    await bot_adaptive_rate_limiter.pressure(bot_id, float(e.retry_after))
                    # Telegram RetryAfter is authoritative; never retry before it expires.
                    await asyncio.sleep(float(e.retry_after) + TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS)
                except (NetworkError, TimedOut, asyncio.TimeoutError, ConnectionError, OSError):
                    retry_attempt += 1
                    set_bot_cooldown(get_bot_index(bot_id), min(5.0, max(1.0, _background_retry_delay(retry_attempt))))
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                except Forbidden:
                    await asyncio.sleep(BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS)
                except BadRequest:
                    await asyncio.sleep(BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS)
                except TelegramError:
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                except Exception:
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
        except asyncio.CancelledError:
            raise
        finally:
            self._active_tasks.pop(task_key, None)

message_scheduler = MessageScheduler()


class SliderScheduler:
    """Manage repeating slider messages per chat, slider id, and target message."""

    def __init__(self) -> None:
        self._active_tasks: dict[tuple[int, int, int, int], asyncio.Task] = {}

    def _key(self, bot_id: int, chat_id: int, slider_id: int, target_message_id: int):
        return (bot_id, chat_id, slider_id, target_message_id)

    def is_running(self, bot_id: int, chat_id: int, slider_id: int, target_message_id: int) -> bool:
        task = self._active_tasks.get(self._key(bot_id, chat_id, slider_id, target_message_id))
        return task is not None and not task.done()

    def get_active_chat_sliders(self, bot_id: int, chat_id: int) -> list[tuple[int, int]]:
        return sorted(
            (slider_id, target_message_id)
            for (b_id, c_id, slider_id, target_message_id), task in self._active_tasks.items()
            if b_id == bot_id and c_id == chat_id and not task.done()
        )

    async def start_slider_task(self, bot_id: int, chat_id: int, slider_id: int, target_message_id: int, messages: list[str], bot) -> tuple[bool, str]:
        key = self._key(bot_id, chat_id, slider_id, target_message_id)
        if key in self._active_tasks and not self._active_tasks[key].done():
            return False, "⚠️ This slider is already running for that target in this chat."

        if not messages:
            return False, "❌ No messages configured for this slider."

        task = asyncio.create_task(self._run_slider_loop(bot_id, chat_id, slider_id, target_message_id, messages, bot))
        self._active_tasks[key] = task
        _install_task_restart_supervisor(
            task, self._active_tasks, key,
            lambda: self._run_slider_loop(bot_id, chat_id, slider_id, target_message_id, messages, bot),
            f"slider {key}",
        )
        return True, f"✅ Slider {slider_id} started for the replied message. Interval: `{SLIDER_INTERVAL_SECONDS}s`."

    async def stop_slider_task(self, bot_id: int, chat_id: int, slider_id: int, target_message_id: int) -> tuple[bool, str]:
        key = self._key(bot_id, chat_id, slider_id, target_message_id)
        task = self._active_tasks.pop(key, None)
        if not task or task.done():
            return False, "ℹ️ No running slider task found for that target."
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True, "𝐒ʟɪᴅᴇ 𝐑ᴇᴘʟʏ 𝐒ᴇ 𝐂ʜᴜᴅᴀɪ 𝐑ᴏᴋ 𝐃ɪʏᴀ 🌐"

    async def cancel_chat_tasks(self, bot_id: int, chat_id: int) -> None:
        keys = [k for k in self._active_tasks if k[0] == bot_id and k[1] == chat_id]
        tasks = []
        for k in keys:
            t = self._active_tasks.pop(k, None)
            if t and not t.done():
                t.cancel()
                tasks.append(t)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_slider_loop(self, bot_id: int, chat_id: int, slider_id: int, target_message_id: int, messages: list[str], bot) -> None:
        key = self._key(bot_id, chat_id, slider_id, target_message_id)
        idx = 0
        retry_attempt = 0
        try:
            while True:
                if not messages:
                    await asyncio.sleep(5)
                    continue
                text = messages[idx % len(messages)]
                try:
                    bot_idx = get_bot_index(bot_id)
                    while not is_bot_ready(bot_idx):
                        await asyncio.sleep(min(0.5, max(0.0, BOT_COOLDOWNS.get(bot_idx, 0.0) - time.time())))
                    await bot_adaptive_rate_limiter.wait(bot_id)
                    await bot_chat_rate_limiter.wait(bot_id, chat_id)
                    await bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=target_message_id)
                    idx = (idx + 1) % len(messages)
                    retry_attempt = 0
                    await bot_adaptive_rate_limiter.success(bot_id)
                    await asyncio.sleep(SLIDER_INTERVAL_SECONDS)
                except RetryAfter as e:
                    retry_attempt = 0
                    bot_idx = get_bot_index(bot_id)
                    set_bot_cooldown(bot_idx, float(e.retry_after))
                    await bot_adaptive_rate_limiter.pressure(bot_id, float(e.retry_after))
                    await asyncio.sleep(float(e.retry_after) + TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS)
                except (NetworkError, TimedOut, asyncio.TimeoutError, ConnectionError, OSError):
                    retry_attempt += 1
                    set_bot_cooldown(get_bot_index(bot_id), min(5.0, max(1.0, _background_retry_delay(retry_attempt))))
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                except Forbidden:
                    await asyncio.sleep(BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS)
                except BadRequest:
                    await asyncio.sleep(BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS)
                except TelegramError:
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                except Exception:
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
        except asyncio.CancelledError:
            raise
        finally:
            self._active_tasks.pop(key, None)

slider_scheduler = SliderScheduler()

class GroupPhotoChanger:
    """Background group profile photo changer task manager."""

    def __init__(self) -> None:
        self._active_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._interval: float = float(os.getenv("GC_PROFILE_PHOTO_CHANGER_INTERVAL_SECONDS", "5"))
        self._active_file: dict[tuple[int, int], str] = {}
        self._wake_events: dict[tuple[int, int], asyncio.Event] = {}

    def is_running(self, bot_id: int, chat_id: int) -> bool:
        task = self._active_tasks.get((bot_id, chat_id))
        return task is not None and not task.done()

    async def start_changer_task(
        self,
        bot_id: int,
        chat_id: int,
        file_ids: list[str],
        bot,
    ) -> tuple[bool, str]:
        if self._interval <= 0:
            return False, "❌ The GC profile photo changer interval must be greater than 0 seconds."

        task_key = (bot_id, chat_id)
        existing = self._active_tasks.get(task_key)
        if existing and not existing.done():
            return False, "⚠️ A group profile photo changer is already running for this group."

        latest = await get_latest_group_profile_photo(bot_id, chat_id)
        if not latest:
            return False, "❌ No saved group photos are available to start the changer."

        self._active_file[task_key] = latest
        wake_event = asyncio.Event()
        self._wake_events[task_key] = wake_event

        task = asyncio.create_task(self._run_changer_loop(bot_id, chat_id, bot, wake_event))
        self._active_tasks[task_key] = task
        _install_task_restart_supervisor(
            task, self._active_tasks, task_key,
            lambda: self._run_changer_loop(bot_id, chat_id, bot, wake_event),
            f"group photo {task_key}",
        )
        logger.info("Started group photo changer task %s for chat %s (active_file=%s)", task_key, chat_id, latest)
        return True, (
            "✅ **Group profile photo changer started.**\n"
            f"Interval: `{self._interval}s`\n"
            "Use /savephoto and /savepfp to add more saved photos."
        )

    async def cancel_chat_tasks(self, bot_id: int, chat_id: int) -> None:
        task_key = (bot_id, chat_id)
        task = self._active_tasks.pop(task_key, None)
        ev = self._wake_events.pop(task_key, None)
        if ev:
            try:
                ev.set()
            except Exception:
                pass
        self._active_file.pop(task_key, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.exception("Exception while stopping group photo changer task %s: %s", task_key, e)

    async def cancel_all_tasks(self) -> None:
        if not self._active_tasks:
            return

        tasks = list(self._active_tasks.values())
        self._active_tasks.clear()
        self._wake_events.clear()
        self._active_file.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Cancelled %d group photo changer tasks.", len(tasks))

    async def _download_photo_stream(self, bot, file_id: str) -> io.BytesIO:
        tg_file = await bot.get_file(file_id)
        buffer = io.BytesIO()
        await tg_file.download_to_memory(out=buffer)
        buffer.seek(0)
        return buffer

    async def _run_changer_loop(
        self,
        bot_id: int,
        chat_id: int,
        bot,
        wake_event: asyncio.Event,
    ) -> None:
        task_key = (bot_id, chat_id)
        retry_attempt = 0
        try:
            while True:
                try:
                    file_id = self._active_file.get(task_key) or await get_latest_group_profile_photo(bot_id, chat_id)
                    if not file_id:
                        await asyncio.wait_for(wake_event.wait(), timeout=self._interval)
                        wake_event.clear()
                        continue

                    await bot_chat_rate_limiter.wait(bot_id, chat_id)
                    photo_stream = await self._download_photo_stream(bot, file_id)
                    await bot_chat_rate_limiter.wait(bot_id, chat_id)
                    await bot.set_chat_photo(chat_id=chat_id, photo=photo_stream)
                    retry_attempt = 0

                    try:
                        await asyncio.wait_for(wake_event.wait(), timeout=self._interval)
                        wake_event.clear()
                    except asyncio.TimeoutError:
                        pass

                except RetryAfter as e:
                    retry_attempt = 0
                    await asyncio.sleep(float(e.retry_after) + TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS)
                except (NetworkError, TimedOut, asyncio.TimeoutError, ConnectionError, OSError):
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                except (BadRequest, Forbidden):
                    await asyncio.sleep(BACKGROUND_PERMANENT_ERROR_RETRY_SECONDS)
                except TelegramError:
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
                except Exception:
                    retry_attempt += 1
                    await asyncio.sleep(_background_retry_delay(retry_attempt))
        except asyncio.CancelledError:
            raise
        finally:
            self._active_tasks.pop(task_key, None)
            self._wake_events.pop(task_key, None)
            self._active_file.pop(task_key, None)
    async def set_active_photo(self, bot_id: int, chat_id: int, file_id: str) -> None:
        """Mark a saved photo as the active photo for the running changer and wake it.

        This does not remove old DB records; it only marks which file_id the changer should use.
        """
        task_key = (bot_id, chat_id)
        self._active_file[task_key] = file_id
        ev = self._wake_events.get(task_key)
        if ev:
            try:
                ev.set()
            except Exception:
                pass


group_photo_changer = GroupPhotoChanger()

def parse_nc_command(text: str) -> tuple[Optional[int], Optional[str]]:
    """Parse an /nc1..../nc20 or custom NC alias command and extract profile number and target."""
    if not text:
        return None, None

    normalized_text = text.strip()
    parts = normalized_text.split(None, 1)
    if not parts:
        return None, None

    command_token = parts[0]
    if not command_token.startswith("/"):
        return None, None

    if "@" in command_token:
        command_token = command_token.split("@", 1)[0]

    command_body = command_token[1:].lower()
    profile_num: Optional[int] = None

    if command_body.startswith("nc"):
        command_body = command_body[2:]
        if command_body.isdigit():
            profile_num = int(command_body)
    else:
        profile_num = NC_CUSTOM_COMMAND_LOOKUP.get(command_body)

    if profile_num is None:
        return None, None

    target_text = parts[1].strip() if len(parts) > 1 else None
    return profile_num, target_text

    if profile_num < 1 or profile_num > len(NC_NAMES):
        return None, None

    target_text = parts[1].strip() if len(parts) > 1 else None
    return profile_num, target_text

def render_message_template(message_template: str, target: str) -> str:
    if "{target}" in message_template:
        return message_template.replace("{target}", target)
    return f"{target} {message_template}"


def render_slider_template(message_template: str, target: str) -> str:
    if "{target}" in message_template:
        return message_template.replace("{target}", target)
    return message_template


def get_msg_command_alias(message_id: int) -> str:
    if 1 <= message_id <= len(MSG_CUSTOM_NAMES):
        name = MSG_CUSTOM_NAMES[message_id - 1]
    else:
        name = ""
    return name.strip().lower() if isinstance(name, str) and name.strip() else f"mgs{message_id}"


def parse_msg_command(text: str) -> tuple[Optional[int], Optional[str]]:
    if not text:
        return None, None

    normalized_text = text.strip()
    parts = normalized_text.split(None, 1)
    if not parts:
        return None, None

    command_token = parts[0]
    if not command_token.startswith("/"):
        return None, None

    if "@" in command_token:
        command_token = command_token.split("@", 1)[0]

    command_body = command_token[1:].lower()
    msg_id: Optional[int] = None

    if command_body.startswith("msg") or command_body.startswith("mgs"):
        message_id = command_body[4:]
        if not message_id.isdigit():
            return None, None

        msg_id = int(message_id)
    else:
        msg_id = MSG_CUSTOM_COMMAND_LOOKUP.get(command_body)

    if msg_id is None or msg_id not in MESSAGES:
        return None, None

    target_text = parts[1].strip() if len(parts) > 1 else None
    return msg_id, target_text


def parse_slider_command(text: str) -> tuple[Optional[int], Optional[str]]:
    if not text:
        return None, None

    normalized_text = text.strip()
    parts = normalized_text.split(None, 1)
    if not parts:
        return None, None

    command_token = parts[0]
    if not command_token.startswith("/"):
        return None, None

    if "@" in command_token:
        command_token = command_token.split("@", 1)[0]

    command_body = command_token[1:].lower()
    profile_id: Optional[int] = None

    if command_body.startswith("slide"):
        profile_text = command_body[5:]
        if profile_text.isdigit():
            profile_id = int(profile_text)
    else:
        profile_id = SLIDER_CUSTOM_COMMAND_LOOKUP.get(command_body)

    if profile_id is None or profile_id not in SLIDER_MESSAGES:
        return None, None

    target_text = parts[1].strip() if len(parts) > 1 else None
    return profile_id, target_text


async def slider_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not await require_owner_or_sudo(update, context):
        return
    profile_id, _ = parse_slider_command(update.message.text)
    if profile_id is None:
        return

    reply_msg = update.message.reply_to_message
    if not reply_msg:
        await senpai_reply(update, "Please reply to a user's message with /{0}.".format(get_slider_command_alias(profile_id)))
        return

    target_message_id = reply_msg.message_id
    chat_id = update.effective_chat.id
    bot_id = context.bot.id

    messages = SLIDER_MESSAGES.get(profile_id)
    if not messages:
        await senpai_reply(update, f"❌ Invalid slider profile `/slide{profile_id}`.")
        return

    target_name = "User"
    if reply_msg.from_user:
        target_name = reply_msg.from_user.full_name or reply_msg.from_user.username or reply_msg.from_user.first_name or "User"

    rendered_messages = [render_slider_template(template, target_name) for template in messages]

    if slider_scheduler.is_running(bot_id, chat_id, profile_id, target_message_id):
        await senpai_reply(update, "⚠️ A slider is already running for that target message in this chat.")
        return

    success, msg = await slider_scheduler.start_slider_task(bot_id, chat_id, profile_id, target_message_id, rendered_messages, context.bot)
    await senpai_reply(update, msg)


async def message_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not await require_owner_or_sudo(update, context):
        return

    message_id, target_text = parse_msg_command(update.message.text)
    if message_id is None:
        return

    if not target_text:
        await senpai_reply(update, 
            f"❌ Usage: /{get_msg_command_alias(message_id)} <target>\nExample: /{get_msg_command_alias(message_id)} shivam"
        )
        return

    group_templates = MESSAGE_GROUPS.get(message_id, [])
    if not group_templates:
        await senpai_reply(update, 
            f"❌ Invalid message option `/mgs{message_id}`. Available options: 1-{len(MESSAGE_GROUPS)}."
        )
        return

    bot_id = context.bot.id
    chat_id = update.effective_chat.id
    success, msg = await message_scheduler.start_message_task(
        bot_id, chat_id, message_id, group_templates, target_text, context.bot
    )
    await senpai_reply(update, msg, parse_mode=ParseMode.MARKDOWN)


async def stopspam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await require_group_admin(update, context):
        return

    bot_id = context.bot.id
    chat_id = update.effective_chat.id
    await message_scheduler.cancel_chat_tasks(bot_id, chat_id)
    await senpai_reply(update, 
        "𝐒ᴘᴀᴍ 𝐒ᴇ 𝐂ʜᴜᴅᴀɪ 𝐁ᴀᴀɴᴅ 𝐊ᴀʀ 𝐃ɪʏᴀ 🧨",
        parse_mode=ParseMode.MARKDOWN,
    )


async def stopslide_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop all active slider tasks in the current chat."""
    if not update.message or not update.effective_chat:
        return
    if not await require_group_admin(update, context):
        return

    bot_id = context.bot.id
    chat_id = update.effective_chat.id

    keys = [k for k in slider_scheduler._active_tasks.keys() if k[0] == bot_id and k[1] == chat_id]
    if not keys:
        await senpai_reply(update, "No active slider found.")
        return

    await slider_scheduler.cancel_chat_tasks(bot_id, chat_id)

    await senpai_reply(update, 
        "⏹️ All active sliders stopped in this chat.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def nc_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generic router for /nc1../nc20."""
    if not update.message or not update.message.text:
        return

    logger.info(
        "NC ROUTER INVOKED text=%r chat_id=%s from_user=%s",
        update.message.text,
        update.effective_chat.id if update.effective_chat else None,
        update.effective_user.id if update.effective_user else None,
    )

    if not await require_owner_or_sudo(update, context):
        return

    bot_id = context.bot.id
    profile_num, target_name = parse_nc_command(update.message.text)
    logger.info("NC ROUTE PARSE RESULT profile=%r target=%r", profile_num, target_name)
    if profile_num is None:
        return

    if profile_num < 1 or profile_num > len(NC_NAMES):
        await senpai_reply(update, "❌ Invalid NC profile. Use /nc1 through /nc20.")
        return

    is_valid, err_msg = await validate_group_nc_permissions(update, context)
    if not is_valid:
        await senpai_reply(update, err_msg)
        return

    if not target_name:
        reply_msg = update.message.reply_to_message
        if reply_msg and reply_msg.from_user:
            user_obj = reply_msg.from_user
            target_name = user_obj.first_name or user_obj.full_name

    if not target_name:
        await senpai_reply(update, 
            f"❌ Usage: /nc{profile_num} <target name>\nExample: /nc{profile_num} anshu"
        )
        return

    logger.info(
        "NC COMMAND RECEIVED profile=%s target=%r chat_id=%s user_id=%s",
        profile_num,
        target_name,
        update.effective_chat.id if update.effective_chat else None,
        update.effective_user.id if update.effective_user else None,
    )

    logger.info("Target received: %r", target_name)

    success, msg = await nc_engine.start_nc_profile(
        bot_id=bot_id,
        chat_id=update.effective_chat.id,
        nc_profile=profile_num,
        target_name=target_name,
        context=context
    )
    if success:
        logger.info(
            "NC TASK CREATED profile=%s target=%r chat_id=%s",
            profile_num,
            target_name,
            update.effective_chat.id,
        )
    await senpai_reply(update, msg, parse_mode=ParseMode.MARKDOWN)
    return

async def gc_tool_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    if not await require_owner_or_sudo(update, context):
        return

    bot_id = context.bot.id
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    text = update.message.text.strip()
    command = text.split(None, 1)[0].lstrip("/").lower()

    if command != "savepfp":
        is_valid, err_msg = await validate_group_nc_permissions(update, context)
        if not is_valid:
            await senpai_reply(update, err_msg)
            return

    if command == "savephoto":
        chat = await context.bot.get_chat(chat_id)
        photo = getattr(chat, "photo", None)
        if not photo:
            await senpai_reply(update, "ℹ️ This group does not currently have a profile photo to save.")
            return

        file_id = getattr(photo, "big_file_id", None) or getattr(photo, "small_file_id", None)
        if not file_id:
            await senpai_reply(update, "ℹ️ Unable to determine the current group profile photo file.")
            return

        added = await add_group_profile_photo(bot_id, chat_id, file_id)
        if added:
            await senpai_reply(update, 
                "✅ Current group profile photo has been saved.",
                parse_mode=ParseMode.MARKDOWN,
            )
            if group_photo_changer.is_running(bot_id, chat_id):
                try:
                    await group_photo_changer.set_active_photo(bot_id, chat_id, file_id)
                except Exception:
                    logger.exception("Failed to set active photo after saving current group photo for chat %s", chat_id)
        else:
            await senpai_reply(update, 
                "ℹ️ This group profile photo is already saved.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if command == "savepfp":
        if not update.message.reply_to_message:
            await senpai_reply(update, 
                "❌ Usage: reply to a photo or image message with /savepfp"
            )
            return

        reply = update.message.reply_to_message
        file_id = None
        if getattr(reply, "photo", None):
            photo_sizes = reply.photo
            if photo_sizes:
                file_id = photo_sizes[-1].file_id
        elif getattr(reply, "document", None) and getattr(reply.document, "mime_type", "").startswith("image/"):
            file_id = reply.document.file_id

        if not file_id:
            await senpai_reply(update, 
                "❌ Reply to a photo or image document message to save it with /savepfp"
            )
            return

        try:
            await set_help_profile_photo(bot_id, file_id)
        except Exception as e:
            logger.error(
                "Failed to store Help PFP for bot_id=%s file_id=%s: %s",
                bot_id,
                file_id,
                e,
            )

        added = await add_group_profile_photo(bot_id, chat_id, file_id)
        if added:
            await senpai_reply(update, 
                "✅ Photo has been saved for this group.",
                parse_mode=ParseMode.MARKDOWN,
            )
            if group_photo_changer.is_running(bot_id, chat_id):
                try:
                    await group_photo_changer.set_active_photo(bot_id, chat_id, file_id)
                except Exception:
                    logger.exception("Failed to set active photo after savepfp for chat %s", chat_id)
        else:
            await senpai_reply(update, 
                "ℹ️ This photo is already saved for this group.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if command == "sendpfp":
        file_ids = await get_saved_group_profile_photo_ids(bot_id, chat_id)
        logger.info("sendpfp found %d saved profile photos for chat %s", len(file_ids), chat_id)
        if not file_ids:
            await senpai_reply(update, 
                "ℹ️ There are no saved profile photos for this group."
            )
            return

        file_id = file_ids[0]
        logger.info("sendpfp selected saved photo file_id=%s for chat %s", file_id, chat_id)
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=file_id)
            logger.info("sendpfp successfully sent saved photo file_id=%s to chat %s", file_id, chat_id)
        except (BadRequest, Forbidden, TelegramError) as e:
            logger.exception("Failed to send saved group photo for chat %s file_id=%s: %s", chat_id, file_id, e)
            await senpai_reply(update, "❌ Failed to send the saved profile photo.")
        except Exception as e:
            logger.exception("Unexpected exception sending saved group photo for chat %s file_id=%s: %s", chat_id, file_id, e)
            await senpai_reply(update, "❌ Failed to send the saved profile photo.")
        return

    if command == "stoppfp":
        if not group_photo_changer.is_running(bot_id, chat_id):
            await senpai_reply(update, 
                "ℹ️ No active group photo changer is running for this group."
            )
            return

        await group_photo_changer.cancel_chat_tasks(bot_id, chat_id)
        await senpai_reply(update, "✅ Group profile photo changer stopped.")
        return

    if command == "delgcpfp":
        try:
            await context.bot.delete_chat_photo(chat_id=chat_id)
            await senpai_reply(update, "✅ Group profile photo has been removed.")
        except (BadRequest, Forbidden) as e:
            logger.exception("Failed to remove group profile photo for chat %s: %s", chat_id, e)
            await senpai_reply(update, 
                "❌ Failed to remove the group profile photo. Make sure I have permission to change group info."
            )
        return

    if command == "gcpfpchanger":
        reply = update.message.reply_to_message
        file_id = None
        if reply is not None:
            if getattr(reply, "photo", None):
                photo_sizes = reply.photo
                if photo_sizes:
                    file_id = photo_sizes[-1].file_id
            elif getattr(reply, "document", None) and getattr(reply.document, "mime_type", "").startswith("image/"):
                file_id = reply.document.file_id

        if file_id:
            saved_ids = await get_saved_group_profile_photo_ids(bot_id, chat_id)
            if file_id not in saved_ids:
                await add_group_profile_photo(bot_id, chat_id, file_id)
                saved_ids = await get_saved_group_profile_photo_ids(bot_id, chat_id)
            file_ids = [file_id] + [fid for fid in saved_ids if fid != file_id]
        else:
            file_ids = await get_saved_group_profile_photo_ids(bot_id, chat_id)

        if not file_ids:
            await senpai_reply(update, 
                "ℹ️ No saved group photos are available. Reply to a photo with /gcpfpchanger or save a photo first with /savepfp."
            )
            return

        success, msg = await group_photo_changer.start_changer_task(
            bot_id=bot_id,
            chat_id=chat_id,
            file_ids=file_ids,
            bot=context.bot,
        )
        await senpai_reply(update, msg, parse_mode=ParseMode.MARKDOWN)
        return

    await senpai_reply(update, 
        "❌ Unknown group command. Available: /savephoto, /delgcpfp, /savepfp, /sendpfp, /gcpfpchanger"
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner_or_sudo(update, context):
        return

    is_valid, err_msg = await validate_group_nc_permissions(update, context)
    if not is_valid:
        await senpai_reply(update, err_msg)
        return

    bot_id = context.bot.id
    chat_id = update.effective_chat.id
    success, msg = await nc_engine.stop_all_nc(bot_id=bot_id, chat_id=chat_id)
    await senpai_reply(update, msg, parse_mode=ParseMode.MARKDOWN)

async def statusnc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show active NC profiles in current chat."""
    if update.effective_chat.type not in ["group", "supergroup"]:
        await senpai_reply(update, "❌ This command can only be used inside a group.")
        return

    bot_id = context.bot.id
    chat_id = update.effective_chat.id
    active_profiles = await get_chat_active_profiles(bot_id=bot_id, chat_id=chat_id)

    if not active_profiles:
        await senpai_reply(update, "ℹ️ No NC profiles are currently running in this group.")
        return

    profiles_text = ", ".join(f"**NC{p}**" for p in active_profiles)
    await senpai_reply(update, 
        f"🔄 **Active Group NC Profiles**:\n{profiles_text}",
        parse_mode=ParseMode.MARKDOWN
    )


async def initialize_crtgc_client() -> bool:
    """Connect the configured persistent Telethon user session once."""
    global CRTGC_CLIENT, CRTGC_CLIENT_READY

    if TelegramClient is None or CreateChannelRequest is None:
        logger.warning("[SENPAI CRTGC] Telethon is not installed")
        return False
    if CRTGC_API_ID is None or not CRTGC_API_HASH:
        logger.warning("[SENPAI CRTGC] Telethon session is not configured")
        return False

    async with CRTGC_CLIENT_LOCK:
        if CRTGC_CLIENT_READY:
            return True
        if CRTGC_CLIENT is None:
            CRTGC_CLIENT = TelegramClient(
                CRTGC_USER_SESSION,
                CRTGC_API_ID,
                CRTGC_API_HASH,
            )
        try:
            if not CRTGC_CLIENT.is_connected():
                await CRTGC_CLIENT.connect()
            if not await CRTGC_CLIENT.is_user_authorized():
                logger.warning("[SENPAI CRTGC] Telethon session is not authorized")
                return False
            CRTGC_CLIENT_READY = True
        except Exception as exc:
            logger.warning("[SENPAI CRTGC] Telethon connection failed: %s", type(exc).__name__)
            return False

    logger.warning("[SENPAI CRTGC] Telethon user client connected")
    return True


async def extra_features_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    command = text.split(None, 1)[0].lstrip("/").lower()
    chat = update.effective_chat
    message_id = getattr(update.message, "message_id", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None or message_id is None:
        return
    claim_key = (chat_id, message_id, command)
    if claim_key in NORMAL_COMMAND_CLAIMS:
        return
    NORMAL_COMMAND_CLAIMS.add(claim_key)

    bot = context.bot
    bot_id = getattr(bot, "id", None)

    if not chat or chat_id is None or not bot_id:
        return

    if command == "bye":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        apps = list(ACTIVE_BOT_APPLICATIONS) if ACTIVE_BOT_APPLICATIONS else []
        if not apps:
            apps = [build_application(token) for token in BOT_TOKENS]
        for app in apps:
            try:
                app_bot = getattr(app, "bot", None)
                if app_bot is None:
                    continue
                await app_bot.leave_chat(chat_id=chat_id)
            except Exception:
                continue
        await senpai_reply(update, "✅ Leave requests sent for all configured bots.")
        return

    if command == "crtgc":
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        parts = text.split()
        if len(parts) < 3:
            await senpai_reply(update, "❌ Usage: /crtgc <group name> <count>")
            return
        try:
            count = int(parts[-1])
        except ValueError:
            await senpai_reply(update, "❌ The final value must be a number.")
            return
        if count <= 0 or count > 100:
            await senpai_reply(update, "❌ Count must be a positive integer between 1 and 100.")
            return
        group_name = " ".join(parts[1:-1]).strip()
        if not group_name:
            await senpai_reply(update, "❌ Provide a group name.")
            return
        if not await initialize_crtgc_client():
            await senpai_reply(
                update,
                "❌ Telethon user session is unavailable or unauthorized. "
                "Configure API_ID, API_HASH, and authorize USER_SESSION once. "
                f"Requested {count}; created 0; failed {count}."
            )
            return

        created = 0
        failures: list[str] = []
        for idx in range(1, count + 1):
            title = f"{group_name} {idx}"
            try:
                await CRTGC_CLIENT(
                    CreateChannelRequest(
                        title=title,
                        about="Created by Senpai",
                        megagroup=True,
                    )
                )
                created += 1
            except FloodWaitError as exc:
                failures.append(f"{title}: FloodWait ({exc.seconds}s)")
            except ChatAdminRequiredError:
                failures.append(f"{title}: account lacks permission")
            except RPCError as exc:
                failures.append(f"{title}: {type(exc).__name__}")
            except (ConnectionError, OSError) as exc:
                failures.append(f"{title}: {type(exc).__name__}")
            except Exception as exc:
                failures.append(f"{title}: {type(exc).__name__}")

        summary = f"Requested {count}; created {created}; failed {len(failures)}."
        if failures:
            summary += "\n" + "\n".join(failures[:5])
        await senpai_reply(update, summary if failures else f"✅ {summary}")
        return

    if command == "promotebots":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return

        reports: list[str] = []
        promoted = 0
        try:
            current_bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot_id)
        except Exception as exc:
            await senpai_reply(update, f"❌ Unable to verify my group membership: {exc}")
            return

        if getattr(current_bot_member, "status", None) not in {"administrator", "creator"}:
            await senpai_reply(update, "❌ I must be an administrator to promote other bots.")
            return

        if getattr(current_bot_member, "status", None) != "creator" and not getattr(current_bot_member, "can_promote_members", False):
            await senpai_reply(update, "❌ I lack the Promote Members permission.")
            return

        grantable_permissions = build_grantable_admin_permissions(current_bot_member)
        for username in PROMOTE_BOT_USERNAMES:
            if not isinstance(username, str) or not username.strip():
                continue
            bot_name = username.strip()
            try:
                target_chat = await bot.get_chat(bot_name)
            except Exception:
                reports.append(f"⚠️ {bot_name}: not found.")
                continue

            try:
                member_status = await bot.get_chat_member(chat_id=chat_id, user_id=target_chat.id)
            except BadRequest as exc:
                message = str(exc).lower()
                if any(keyword in message for keyword in ["user not found", "chat not found", "not a member", "left", "kicked"]):
                    reports.append(f"⚠️ {bot_name}: Not in group")
                    continue
                reports.append(f"⚠️ {bot_name}: unable to check membership ({exc})")
                continue
            except Exception as exc:
                reports.append(f"⚠️ {bot_name}: unable to check membership ({type(exc).__name__})")
                continue

            if getattr(member_status, "status", None) not in {"member", "administrator", "creator"}:
                reports.append(f"⚠️ {bot_name}: Not in group")
                continue

            if getattr(member_status, "status", None) in {"administrator", "creator"}:
                reports.append(f"ℹ️ {bot_name}: already an administrator.")
                continue

            if await promote_bot_to_admin(bot, chat_id, target_chat.id, permissions=grantable_permissions):
                promoted += 1
                reports.append(f"✅ {bot_name}: promoted to administrator.")
            else:
                reports.append(f"⚠️ {bot_name}: present but promotion failed.")

        if promoted:
            await senpai_reply(update, 
                "╭━━━━━━━ ✦ 👑 ✦ ━━━━━━━╮\n"
                "┃   ✨ 𝐀ʟʟ 𝐁ᴏᴛs 𝐏ʀᴏᴍᴏᴛᴇᴅ ✨\n"
                "┃        𝐒ᴜᴄᴄᴇssғᴜʟʟʏ ✅\n"
                "╰━━━━━━━ ✦ 👑 ✦ ━━━━━━━╯"
            )
        else:
            await senpai_reply(update, "ℹ️ No bots were promoted.")
        return

    if command == "promote":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        target = await resolve_target_user(update, context)
        if target is None:
            await senpai_reply(update, "❌ Reply to a user or mention them with /promote.")
            return
        if target.id == bot_id:
            await senpai_reply(update, "❌ I cannot promote myself.")
            return
        if target.id == get_owner_id_for_bot(bot_id):
            await senpai_reply(update, "❌ I will not modify the configured owner privileges.")
            return
        try:
            bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot_id)
            if bot_member.status not in {"administrator", "creator"}:
                await senpai_reply(update, "❌ I must be an administrator to promote users.")
                return
            if bot_member.status != "creator" and not getattr(bot_member, "can_promote_members", False):
                await senpai_reply(update, "❌ I lack the Promote Members permission.")
                return
            target_member = await bot.get_chat_member(chat_id=chat_id, user_id=target.id)
            if getattr(target_member, "status", None) in {"administrator", "creator"}:
                await senpai_reply(update, "ℹ️ Target is already an administrator or the group owner.")
                return
            await bot.promote_chat_member(
                chat_id=chat_id,
                user_id=target.id,
                can_change_info=True,
                can_post_messages=True,
                can_edit_messages=True,
                can_delete_messages=True,
                can_invite_users=True,
                can_restrict_members=True,
                can_pin_messages=True,
                can_promote_members=True,
                is_anonymous=False,
                can_manage_chat=True,
                can_manage_video_chats=True,
                can_manage_topics=True,
                can_post_stories=True,
                can_edit_stories=True,
                can_delete_stories=True,
                can_manage_direct_messages=True,
                can_manage_tags=True,
            )
            await bot.get_chat_member(chat_id=chat_id, user_id=target.id)
            await senpai_reply(update, 
                "╭━━━━━━━ ✦ 👑 ✦ ━━━━━━━╮\n"
                "┃   𝐀ᴅᴍɪɴ 𝐀ᴅᴅᴇᴅ ✅\n"
                "┃   𝐅ᴜʟʟ 𝐑ɪɢʜᴛs 𝐆ʀᴀɴᴛᴇᴅ\n"
                "╰━━━━━━━ ✦ 👑 ✦ ━━━━━━━╯"
            )
        except BadRequest as exc:
            await senpai_reply(update, f"❌ Failed to promote target: {exc}")
        except Forbidden:
            await senpai_reply(update, "❌ I lack permission to promote this user.")
        except Exception as exc:
            await senpai_reply(update, f"❌ Failed to promote target: {exc}")
        return

    if command == "gameover":
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        target = await resolve_target_user(update, context)
        owner_name = await resolve_owner_display_name(context)
        target_name = None
        if target is not None:
            target_name = getattr(target, "username", None) or getattr(target, "first_name", None) or getattr(target, "full_name", None)
        if not target_name:
            target_name = "target"
        now = datetime.now()
        message = (
            f"{owner_name}\n"
            f"{now:%Y-%m-%d}\n"
            f"{now:%H:%M}\n\n"
            "𝓨𝓸𝓾 𝓪𝓻𝓮 𝓷𝓸𝓽 𝔀𝓮𝓪𝓴,\n"
            "𝔂𝓸𝓾 𝓪𝓻𝓮 𝓳𝓾𝓼𝓽 𝓯𝓪𝓬𝓲𝓷𝓰 𝓽𝓱𝓮 𝓼𝓽𝓻𝓸𝓷𝓰𝓮𝓼𝓽 𝓸𝓷𝓮\n\n"
            f"{target_name}\n\n"
            "𝓑𝓮𝓽𝓽𝓮𝓻 𝓵𝓾𝓬𝓴 𝓷𝓮𝔁𝓽 𝓽𝓲𝓶𝓮"
        )
        gameover_text = senpai_status(
            command="/gameover",
            status="success",
            action="𝐂ᴏᴍᴘʟᴇᴛᴇ",
            target=target_name,
            detail=message,
        )
        try:
            saved_pfp_file_id = await get_help_profile_photo(bot_id)
        except Exception as exc:
            logger.warning("Game Over PFP lookup failed; falling back to text: %s", type(exc).__name__)
            saved_pfp_file_id = None
        if saved_pfp_file_id:
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=saved_pfp_file_id,
                    caption=gameover_text,
                )
            except Exception as exc:
                logger.warning("Game Over PFP unavailable; falling back to text: %s", type(exc).__name__)
                await senpai_reply(update, message)
        else:
            await senpai_reply(update, message)
        return

    if command == "ping":
        start = time.monotonic()
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        latency_ms = int((time.monotonic() - start) * 1000)
        await senpai_reply(update, f"🏓 Pong!\n⚡ {latency_ms} ms")
        return

    if command == "status":
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        parts = []
        active_profiles = await get_chat_active_profiles(bot_id=bot_id, chat_id=chat_id)
        if active_profiles:
            parts.append("🌀 NC: " + ", ".join(f"NC{p}" for p in active_profiles))
        active_mgs = message_scheduler.get_active_chat_message_ids(bot_id, chat_id)
        if active_mgs:
            parts.append("💬 MGS/messages: active (" + ", ".join(str(i) for i in active_mgs) + ")")
        active_sliders = slider_scheduler.get_active_chat_sliders(bot_id, chat_id)
        if active_sliders:
            parts.append("🎚️ Slider: active (" + ", ".join(f"{sid}@{msgid}" for sid, msgid in active_sliders) + ")")
        if group_photo_changer.is_running(bot_id, chat_id):
            parts.append("🖼️ PFP changer: active")
        if not parts:
            parts.append("ℹ️ No active task systems found in this group.")
        parts.append(f"⏱ Uptime: {get_uptime_text()}")
        await senpai_reply(update, "\n".join(parts))
        return

    if command == "mute":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        target = update.message.reply_to_message.from_user if update.message.reply_to_message and update.message.reply_to_message.from_user else None
        if target is None:
            await senpai_reply(update, "❌ Reply to a user with /mute.")
            return
        try:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=target.id, permissions=ChatPermissions(can_send_messages=False))
            await senpai_reply(update, "✅ User muted.")
        except Exception as exc:
            await senpai_reply(update, f"❌ Failed to mute user: {exc}")
        return

    if command == "unmute":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        target = update.message.reply_to_message.from_user if update.message.reply_to_message and update.message.reply_to_message.from_user else None
        if target is None:
            await senpai_reply(update, "❌ Reply to a user with /unmute.")
            return
        try:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=target.id, permissions=ChatPermissions(can_send_messages=True))
            await senpai_reply(update, "✅ User unmuted.")
        except Exception as exc:
            await senpai_reply(update, f"❌ Failed to unmute user: {exc}")
        return

    if command == "ban":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        target = update.message.reply_to_message.from_user if update.message.reply_to_message and update.message.reply_to_message.from_user else None
        if target is None:
            await senpai_reply(update, "❌ Reply to a user with /ban.")
            return
        if target.id in {bot_id, get_owner_id_for_bot(bot_id)}:
            await senpai_reply(update, "❌ I will not ban the owner or myself.")
            return
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=target.id)
            await senpai_reply(update, "✅ User banned.")
        except Exception as exc:
            await senpai_reply(update, f"❌ Failed to ban user: {exc}")
        return

    if command == "unban":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        target = update.message.reply_to_message.from_user if update.message.reply_to_message and update.message.reply_to_message.from_user else None
        if target is None:
            await senpai_reply(update, "❌ Reply to a user with /unban.")
            return
        try:
            await bot.unban_chat_member(chat_id=chat_id, user_id=target.id)
            await senpai_reply(update, "✅ User unbanned.")
        except Exception as exc:
            await senpai_reply(update, f"❌ Failed to unban user: {exc}")
        return

    if command == "kick":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        target = await resolve_target_user(update, context)
        if target is None:
            await senpai_reply(update, "❌ Reply to a user or mention them with /kick.")
            return
        if target.id in {bot_id, get_owner_id_for_bot(bot_id)}:
            await senpai_reply(update, "❌ I will not kick the owner or myself.")
            return
        try:
            current = await bot.get_chat_member(chat_id=chat_id, user_id=target.id)
            if getattr(current, "status", None) in {"left", "kicked"}:
                await senpai_reply(update, "ℹ️ User is not currently a member of this group.")
                return
            await bot.ban_chat_member(chat_id=chat_id, user_id=target.id, until_date=int(time.time()) + 60)
            await bot.unban_chat_member(chat_id=chat_id, user_id=target.id)
            await senpai_reply(update, "✅ User kicked.")
        except BadRequest as exc:
            message = str(exc).lower()
            if "user not found" in message or "chat not found" in message or "not a member" in message:
                await senpai_reply(update, "ℹ️ User is not currently a member of this group.")
            else:
                await senpai_reply(update, f"❌ Failed to kick user: {exc}")
        except Forbidden:
            await senpai_reply(update, "❌ I lack permission to kick this user.")
        except Exception as exc:
            await senpai_reply(update, f"❌ Failed to kick user: {exc}")
        return

    if command == "react":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        parts = text.split(None, 1)
        if len(parts) < 2:
            await senpai_reply(update, "❌ Usage: /react 🤣")
            return
        emoji = parts[1].strip()
        if not emoji:
            await senpai_reply(update, "❌ Usage: /react 🤣")
            return
        try:
            ReactionTypeEmoji(emoji=emoji)
        except Exception:
            await senpai_reply(update, "❌ Invalid reaction emoji.")
            return
        REACTING_CHATS[(bot_id, chat_id)] = emoji
        await senpai_reply(update, f"✅ Reactions enabled for this group with {emoji}.")
        return

    if command == "stopreact":
        if not await require_group_admin(update, context):
            return
        if chat.type not in ["group", "supergroup"]:
            await senpai_reply(update, "❌ This command can only be used inside a group.")
            return
        if (bot_id, chat_id) in REACTING_CHATS:
            REACTING_CHATS.pop((bot_id, chat_id), None)
            await senpai_reply(update, "✅ Automatic reactions stopped for this group.")
        else:
            await senpai_reply(update, "ℹ️ Automatic reactions were not enabled for this group.")
        return

    await senpai_reply(update, "❌ Unknown extra feature command.")


async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    bot = context.bot
    bot_id = getattr(bot, "id", None)
    chat_id = getattr(chat, "id", None)
    if not bot_id or not chat_id or chat.type not in ["group", "supergroup"]:
        return
    reaction_emoji = REACTING_CHATS.get((bot_id, chat_id))
    if not reaction_emoji:
        return
    if update.message.from_user and update.message.from_user.id == bot_id:
        return
    try:
        await bot.set_message_reaction(chat_id=chat_id, message_id=update.message.message_id, reaction=[ReactionTypeEmoji(emoji=reaction_emoji)])
    except Exception:
        pass

MAIN_MENU_TEXT = """
╭━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╮
┃     ⚕️Harsh 𝐊ᴇɴɢ⚕️
┃
╰━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╯

┏━━━ ★ ① ★ ━━━━━━━━━━━┓
┃ 🌟✨  ➪ /menu1
┃ ╰─➤ 𝐍ᴀᴍᴇ 𝐂ʜᴀɴɢᴇʀ
┗━━━━━━━━━━━━━━━━━━━━━━┛

┏━━━ ★ ② ★ ━━━━━━━━━━━┓
┃ 🌟✨  ➪ /menu2
┃ ╰─➤ 𝐒ᴘᴀᴍ 𝐙ᴏɴᴇ
┗━━━━━━━━━━━━━━━━━━━━━━┛

┏━━━ ★ ③ ★ ━━━━━━━━━━━┓
┃ 🌟✨  ➪ /menu3
┃ ╰─➤ 𝐒ᴜᴅᴏ 𝐀ʀᴇᴀ
┗━━━━━━━━━━━━━━━━━━━━━━┛

┏━━━ ★ ④ ★ ━━━━━━━━━━━┓
┃ 🌟✨  ➪ /menu4
┃ ╰─➤ 𝐒ʟɪᴅᴇ + 𝐆ᴄ 𝐓ᴏᴏʟ
┗━━━━━━━━━━━━━━━━━━━━━━┛

┏━━━ ★ ⑤ ★ ━━━━━━━━━━━┓
┃   ➪ /menu5
┃ ╰─➤  xᴛʀᴀ 𝐅ᴇᴀᴛᴜʀᴇs
┗━━━━━━━━━━━━━━━━━━━━━━┛

        ˚ʚ♡ɞ˚  𝐁ᴏᴛ 𝐈s 𝐑ᴇᴀᴅʏ 𝐓ᴏ 𝐔sᴇ ˚ʚ♡ɞ˚
"""

MENU1_TEXT = """
╭━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╮
┃     ⚕️ 𝐍ᴀᴍᴇ 𝐂ʜᴀɴɢᴇʀ ⚕️
┃
╰━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╯

{nc_commands}

┏━━━ ★ STOP ★ ━━━━━━━━━━━┓
┃ ⏹️  ➪ /stop
┃ ╰─➤ 𝐒𝐓𝐎𝐏 𝐂𝐎𝐍𝐓𝐑𝐎𝐋𝐒
┗━━━━━━━━━━━━━━━━━━━━━━┛

        ˚ʚ♡ɞ˚  𝐁ᴏᴛ 𝐈s 𝐑ᴇᴀᴅʏ 𝐓ᴏ 𝐔sᴇ ˚ʚ♡ɞ˚
"""

MENU2_TEXT = """
╭━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╮
┃     💬 𝐌ᴇssᴀɢᴇ 𝐌ᴇɴᴜ 💬
┃
╰━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╯

{message_entries}

┏━━━ ★ STOP ★ ━━━━━━━━━━━┓
┃ 🧨  ➪ /stopspam
┃ ╰─➤ 𝐒𝐓𝐎𝐏 𝐒𝐏𝐀𝐌
┗━━━━━━━━━━━━━━━━━━━━━━┛

<i>Default interval:</i> {interval}s

        ˚ʚ♡ɞ˚  𝐁ᴏᴛ 𝐈s 𝐑ᴇᴀᴅʏ 𝐓ᴏ 𝐔sᴇ ˚ʚ♡ɞ˚
"""

MENU3_TEXT = """
╭━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╮
┃     🔐 𝐒ᴜᴅᴏ 𝐀ʀᴇᴀ 🔐
┃
╰━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╯

{sudo_entries}

        ˚ʚ♡ɞ˚  𝐁ᴏᴛ 𝐈s 𝐑ᴇᴀᴅʏ 𝐓ᴏ 𝐔sᴇ ˚ʚ♡ɞ˚
"""

MENU4_TEXT = """
╭━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╮
┃     🤖 𝐁𝐎𝐓 𝐌𝐄𝐍𝐔 🤖
┃
╰━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╯

{slider_commands}

{gc_tools}

        ˚ʚ♡ɞ˚  𝐁ᴏᴛ 𝐈s 𝐑ᴇᴀᴅʏ 𝐓ᴏ 𝐔sᴇ ˚ʚ♡ɞ˚
"""

MENU5_TEXT = """
╭━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╮
┃      ⚕️ Harsh ⚕️
┃     𝐄xᴛʀᴀ 𝐅ᴇᴀᴛᴜʀᴇs
╰━━━━━━━ ✦ ⚡ ✦ ━━━━━━━╯

{extra_entries}

        ˚ʚ♡ɞ˚  𝐁ᴏᴛ 𝐈s 𝐑ᴇᴀᴅʏ 𝐓ᴏ 𝐔sᴇ ˚ʚ♡ɞ˚
"""

async def build_main_menu_text() -> str:
    return MAIN_MENU_TEXT.strip()

async def build_nc_menu_text() -> str:
    nc_commands = "\n".join(
        f"┏━━━ ★ {i} ★ ━━━━━━━━━━━┓\n┃ 🤖  ➪ /{get_nc_command_alias(i)}\n┃ ╰─➤ 𝐍𝐂 {i}\n┗━━━━━━━━━━━━━━━━━━━━━━┛"
        for i in range(1, len(NC_NAMES) + 1)
    )
    return MENU1_TEXT.format(nc_commands=nc_commands).strip()

async def build_message_menu_text() -> str:
    message_entries = "\n".join(
        f"┏━━━ ★ {idx} ★ ━━━━━━━━━━━┓\n┃ 💬  ➪ /{get_msg_command_alias(idx)}\n┃ ╰─➤ 𝐌𝐄𝐒𝐒𝐀𝐆𝐄 {idx}\n┗━━━━━━━━━━━━━━━━━━━━━━┛"
        for idx in sorted(MESSAGES)
    )
    return MENU2_TEXT.format(
        message_entries=message_entries,
        interval=MESSAGE_SEND_INTERVAL_SECONDS,
    ).strip()

async def build_menu3_text() -> str:
    sudo_entries = "\n".join(
        [
            "┏━━━ ★ ① ★ ━━━━━━━━━━━┓\n┃ 🔐  ➪ /addsudo\n┃ ╰─➤ 𝐀𝐃𝐃 𝐒𝐔𝐃𝐎\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ② ★ ━━━━━━━━━━━┓\n┃ 🔐  ➪ /delsudo\n┃ ╰─➤ 𝐑𝐄𝐌𝐎𝐕𝐄 𝐒𝐔𝐃𝐎\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ③ ★ ━━━━━━━━━━━┓\n┃ 🔐  ➪ /sudolist\n┃ ╰─➤ 𝐋𝐈𝐒𝐓 𝐒𝐔𝐃𝐎\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
        ]
    )
    return MENU3_TEXT.format(sudo_entries=sudo_entries).strip()

async def build_menu4_text() -> str:
    slider_entries = "\n".join(
        f"┏━━━ ★ {i} ★ ━━━━━━━━━━━┓\n┃ 🔁  ➪ /{get_slider_command_alias(i)}\n┃ ╰─➤ 𝐒𝐋𝐈𝐃𝐄𝐑 {i}\n┗━━━━━━━━━━━━━━━━━━━━━━┛"
        for i in range(1, len(SLIDER_CUSTOM_NAMES) + 1)
    )
    slider_entries += "\n┏━━━ ★ STOP ★ ━━━━━━━━━━━┓\n┃ ⏹️  ➪ /stopslide\n┃ ╰─➤ 𝐒𝐓𝐎𝐏 𝐒𝐋𝐈𝐃𝐄\n┗━━━━━━━━━━━━━━━━━━━━━━┛"
    gc_tools = "\n".join(
        [
            "┏━━━ ★ ① ★ ━━━━━━━━━━━┓\n┃ 🖼️  ➪ /savephoto\n┃ ╰─➤ 𝐆𝐂 𝐓𝐎𝐎𝐋𝐒\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ② ★ ━━━━━━━━━━━┓\n┃ 🗑️  ➪ /delgcpfp\n┃ ╰─➤ 𝐃𝐄𝐋𝐄𝐓𝐄 𝐆𝐂 𝐏𝐇𝐎𝐓𝐎\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ③ ★ ━━━━━━━━━━━┓\n┃ 📷  ➪ /savepfp\n┃ ╰─➤ 𝐒𝐀𝐕𝐄 𝐏𝐇𝐎𝐓𝐎\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ④ ★ ━━━━━━━━━━━┓\n┃ 📤  ➪ /sendpfp\n┃ ╰─➤ 𝐒𝐄𝐍𝐃 𝐏𝐇𝐎𝐓𝐎\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑤ ★ ━━━━━━━━━━━┓\n┃ 🔄  ➪ /gcpfpchanger\n┃ ╰─➤ 𝐆𝐂 𝐂𝐇𝐀𝐍𝐆𝐄𝐑\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑥ ★ ━━━━━━━━━━━┓\n┃ ⏹️  ➪ /stoppfp\n┃ ╰─➤ 𝐒𝐓𝐎𝐏 𝐂𝐇𝐀𝐍𝐆𝐄𝐑\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
        ]
    )
    return MENU4_TEXT.format(slider_commands=slider_entries, gc_tools=gc_tools).strip()

async def build_menu5_text() -> str:
    extra_entries = "\n".join(
        [
            "┏━━━ ★ ① ★ ━━━━━━━━━━━┓\n┃ 🚪  ➪ /bye\n┃ ╰─➤ 𝐁𝐘𝐄\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ② ★ ━━━━━━━━━━━┓\n┃ 🏗️  ➪ /crtgc\n┃ ╰─➤ 𝐂𝐑𝐄𝐀𝐓𝐄 𝐆𝐑𝐎𝐔𝐏\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ③ ★ ━━━━━━━━━━━┓\n┃ 🤖  ➪ /promotebots\n┃ ╰─➤ 𝐏𝐑𝐎𝐌𝐎𝐓𝐄 𝐁𝐎𝐓𝐒\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ④ ★ ━━━━━━━━━━━┓\n┃ 👑  ➪ /promote\n┃ ╰─➤ 𝐏𝐑𝐎𝐌𝐎𝐓𝐄\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑤ ★ ━━━━━━━━━━━┓\n┃ 💀  ➪ /gameover\n┃ ╰─➤ 𝐆𝐀𝐌𝐄 𝐎𝐕𝐄𝐑\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑥ ★ ━━━━━━━━━━━┓\n┃ 🏓  ➪ /ping\n┃ ╰─➤ 𝐏𝐈𝐍𝐆\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑦ ★ ━━━━━━━━━━━┓\n┃ 📊  ➪ /status\n┃ ╰─➤ 𝐒𝐓𝐀𝐓𝐔𝐒\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑧ ★ ━━━━━━━━━━━┓\n┃ 🔇  ➪ /mute\n┃ ╰─➤ 𝐌𝐔𝐓𝐄\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑨ ★ ━━━━━━━━━━━┓\n┃ 🔊  ➪ /unmute\n┃ ╰─➤ 𝐔𝐍𝐌𝐔𝐓𝐄\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑩ ★ ━━━━━━━━━━━┓\n┃ 🚫  ➪ /ban\n┃ ╰─➤ 𝐁𝐀𝐍\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑪ ★ ━━━━━━━━━━━┓\n┃ ♻️  ➪ /unban\n┃ ╰─➤ 𝐔𝐍𝐁𝐀𝐍\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑫ ★ ━━━━━━━━━━━┓\n┃ 👢  ➪ /kick\n┃ ╰─➤ 𝐊𝐈𝐂𝐊\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑬ ★ ━━━━━━━━━━━┓\n┃ 🤣  ➪ /react\n┃ ╰─➤ 𝐑𝐄𝐀𝐂𝐓\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
            "┏━━━ ★ ⑭ ★ ━━━━━━━━━━━┓\n┃ 🛑  ➪ /stopreact\n┃ ╰─➤ 𝐒𝐓𝐎𝐏 𝐑𝐄𝐀𝐂𝐓\n┗━━━━━━━━━━━━━━━━━━━━━━┛",
        ]
    )
    return MENU5_TEXT.format(extra_entries=extra_entries).strip()

async def send_menu_command(update: Update) -> None:
    menu_text = await build_main_menu_text()
    await senpai_reply(update, menu_text, parse_mode=ParseMode.HTML)

def get_update_event_name(update: Update) -> str:
    for attr in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "inline_query",
        "chosen_inline_result",
        "callback_query",
        "poll",
        "poll_answer",
        "chat_member",
        "my_chat_member",
        "chat_join_request",
    ):
        if getattr(update, attr, None) is not None:
            return attr
    return "unknown"


async def log_incoming_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    text = update.message.text if update.message else None
    update_event = get_update_event_name(update)
    logger.info(
        "Incoming update received: type=%s chat_id=%s user_id=%s text=%r",
        update_event,
        chat_id,
        user_id,
        text,
    )

async def start_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome / help message with the current bot owner's profile photo when available."""
    bot_username = getattr(context.bot, "username", "<unknown>")
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(
        "/start or /help handler called for bot=%s chat_id=%s user_id=%s text=%r",
        bot_username,
        chat_id,
        user_id,
        update.message.text if update.message else None,
    )

    menu_text = await build_main_menu_text()
    bot_id = getattr(context.bot, "id", None)
    help_pfp_file_id = None

    if bot_id is not None:
        help_pfp_file_id = await get_help_profile_photo(bot_id)

    logger.info(
        "Help PFP loaded: bot_id=%s file_id_available=%s",
        bot_id,
        bool(help_pfp_file_id),
    )

    if help_pfp_file_id:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=help_pfp_file_id,
                caption=menu_text,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            logger.error(
                "Failed to send Help PFP for bot=%s: %s",
                bot_username,
                e,
            )

    await senpai_reply(update, menu_text, parse_mode=ParseMode.HTML)

async def menu1_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the NC menu (existing NC menu content)."""
    menu_text = await build_nc_menu_text()
    await senpai_reply(update, menu_text, parse_mode=ParseMode.HTML)

async def menu2_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the Message Menu."""
    menu_text = await build_message_menu_text()
    await senpai_reply(update, menu_text, parse_mode=ParseMode.HTML)

async def menu3_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the future menu featuring sudo system entries."""
    menu_text = await build_menu3_text()
    await senpai_reply(update, menu_text, parse_mode=ParseMode.HTML)

async def menu4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the Slider Replies and GC Tools menu."""
    menu_text = await build_menu4_text()
    await senpai_reply(update, menu_text, parse_mode=ParseMode.HTML)

async def menu5_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the Extra Features menu."""
    menu_text = await build_menu5_text()
    await senpai_reply(update, menu_text, parse_mode=ParseMode.HTML)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the same menu as /help."""
    await send_menu_command(update)

async def post_init(application) -> None:
    """Post-initialization hook to set up DB and resume active tasks."""
    bot_username = getattr(application.bot, "username", "<unknown>")
    bot_id = getattr(application.bot, "id", "<unknown>")
    logger.info("Initializing database for bot %s (%s)...", bot_username, bot_id)
    await init_db()
    await initialize_crtgc_client()

    logger.info("Resuming persistent NC tasks from SQLite for bot %s (%s)...", bot_username, bot_id)
    await nc_engine.resume_active_tasks(application)
    logger.info("Bot %s (%s) initialized and ready.", bot_username, bot_id)


def build_application(token: str):
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        MessageHandler(filters.ALL, log_incoming_update),
        group=-1
    )

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_message),
        group=0
    )

    application.add_handler(
        CommandHandler(["start", "help"], start_help_command),
        group=0
    )

    application.add_handler(
        CommandHandler("menu", menu_command),
        group=0
    )

    application.add_handler(
        CommandHandler("menu1", menu1_command),
        group=0
    )

    application.add_handler(
        CommandHandler("menu2", menu2_command),
        group=0
    )

    application.add_handler(
        CommandHandler("menu3", menu3_command),
        group=0
    )

    application.add_handler(
        CommandHandler("menu4", menu4_command),
        group=0
    )

    application.add_handler(
        CommandHandler("menu5", menu5_command),
        group=0
    )

    application.add_handler(
        CommandHandler([f"slide{i}" for i in SLIDER_MESSAGES] + SLIDER_CUSTOM_COMMAND_LIST, slider_command_router),
        group=0
    )

    application.add_handler(
        CommandHandler([f"msg{i}" for i in MESSAGES] + [f"mgs{i}" for i in MESSAGES], message_command_router),
        group=0
    )

    application.add_handler(
        CommandHandler(["savephoto", "delgcpfp", "savepfp", "sendpfp", "gcpfpchanger", "stoppfp"], gc_tool_command_router),
        group=0
    )

    application.add_handler(
        CommandHandler("stopspam", stopspam_command),
        group=0
    )

    application.add_handler(
        CommandHandler("stopslide", stopslide_command),
        group=0
    )

    application.add_handler(
        CommandHandler("statusnc", statusnc_command),
        group=0
    )

    application.add_handler(
        CommandHandler(["addsudo", "delsudo", "sudolist"], sudo_command_router),
        group=0
    )

    application.add_handler(
        CommandHandler("stop", stop_command),
        group=0
    )

    application.add_handler(
        CommandHandler(["bye", "crtgc", "promotebots", "promote", "gameover", "ping", "status", "mute", "unmute", "ban", "unban", "kick", "react", "stopreact"], extra_features_command_router),
        group=0
    )

    if MSG_CUSTOM_COMMAND_LIST:
        application.add_handler(
            CommandHandler(MSG_CUSTOM_COMMAND_LIST, message_command_router),
            group=0
        )

    if NC_CUSTOM_COMMAND_LIST:
        application.add_handler(
            CommandHandler(NC_CUSTOM_COMMAND_LIST, nc_command_router),
            group=0
        )

    nc_filter = filters.COMMAND & filters.Regex(
        r"^/nc(?:[1-9]|1[0-9]|20)(?:@\w+)?(?:\s+.*)?$",
    )

    application.add_handler(
        MessageHandler(nc_filter, nc_command_router),
        group=0
    )

    logger.info(
        "Built application for token ending %s and registered handlers.",
        token[-10:]
    )

    return application


async def start_application(application, bot_idx: int) -> None:
    bot_id = None
    bot_username = "<unknown>"
    try:
        if not getattr(application, "is_initialized", False):
            logger.info("Initializing application for token ending %s...", getattr(application, 'token', '<unknown>')[-10:])
            await application.initialize()

        bot_id = getattr(application.bot, "id", None)
        # Application uses slots; arbitrary attributes are not supported.
        if bot_id is not None:
            BOT_INDEX_BY_ID[int(bot_id)] = int(bot_idx)
        bot_username = getattr(application.bot, "username", "<unknown>")

        logger.info("Starting bot %s (%s)...", bot_username, bot_id)
        await application.start()

        logger.info("Starting polling for bot %s (%s)...", bot_username, bot_id)
        await application.updater.start_polling()
        logger.info("Polling active for bot %s (%s).", bot_username, bot_id)
        print(f"Bot Polling: @{bot_username}")
    except Exception as e:
        logger.error("Failed to start bot %s (%s): %s", bot_username, bot_id, e)
        raise


async def stop_application(application) -> None:
    bot_id = getattr(application.bot, "id", None)
    bot_username = getattr(application.bot, "username", "<unknown>")
    try:
        logger.info("Stopping polling for bot %s (%s)...", bot_username, bot_id)
        await application.updater.stop()
    except Exception as e:
        logger.error("Failed to stop updater for bot %s (%s): %s", bot_username, bot_id, e)

    try:
        logger.info("Cancelling scheduled message tasks for bot %s (%s)...", bot_username, bot_id)
        await message_scheduler.cancel_all_tasks()
    except Exception as e:
        logger.exception("Failed to cancel scheduled message tasks for bot %s (%s): %s", bot_username, bot_id, e)

    try:
        logger.info("Stopping application for bot %s (%s)...", bot_username, bot_id)
        await application.stop()
    except Exception as e:
        logger.error("Failed to stop application for bot %s (%s): %s", bot_username, bot_id, e)

    try:
        logger.info("Shutting down application for bot %s (%s)...", bot_username, bot_id)
        await application.shutdown()
    except Exception as e:
        logger.error("Failed to shutdown application for bot %s (%s): %s", bot_username, bot_id, e)


async def main_async() -> None:
    configure_terminal_logging()
    print_startup_banner()

    if not BOT_TOKENS:
        logger.error("No BOT_TOKENS configured. Please set BOT_TOKENS environment variable or fallback BOT_TOKEN.")
        return

    try:
        logger.info("Initializing database before application startup...")
        await init_db()
    except Exception as e:
        logger.error("Failed to initialize SQLite database: %s", e)
        return

    applications = []
    for bot_idx, token in enumerate(BOT_TOKENS, start=1):
        try:
            application = build_application(token)
            applications.append((bot_idx, application))
        except Exception as e:
            logger.error("Failed to build bot application for configured token: %s", e)

    if not applications:
        logger.error("No valid bot applications could be started.")
        return

    started_apps = []
    try:
        start_tasks = [
            asyncio.create_task(start_application(application, bot_idx))
            for bot_idx, application in applications
        ]
        results = await asyncio.gather(*start_tasks, return_exceptions=True)

        for (bot_idx, application), result in zip(applications, results):
            if isinstance(result, Exception):
                bot_token = getattr(application, 'token', '<unknown>')
                logger.error("Application failed to start for token ending %s", bot_token[-10:])
            else:
                started_apps.append(application)
                ACTIVE_BOT_APPLICATIONS.append(application)

        if not started_apps:
            logger.error("No bot applications started successfully.")
            return

        if len(started_apps) == len(applications):
            logger.info("All started bot applications are polling. Waiting for updates...")
            print_ready_banner()
        else:
            logger.info("Some bot applications did not start successfully; ready banner suppressed.")

        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Shutdown requested, stopping bot applications...")
    finally:
        if started_apps:
            logger.info("Shutting down %d bot applications...", len(started_apps))
            await asyncio.gather(*(stop_application(app) for app in started_apps), return_exceptions=True)


def _run_nc_parser_tests() -> None:
    tests = [
        ("/nc1 Anshu", 1, "Anshu"),
        ("/nc20 Anshu Kumar", 20, "Anshu Kumar"),
        ("/nc1@MyBot anshu", 1, "anshu"),
    ]
    for text, expected_profile, expected_target in tests:
        profile_num, target_name = parse_nc_command(text)
        if profile_num != expected_profile or target_name != expected_target:
            raise AssertionError(
                f"NC parser failed for {text!r}: got profile={profile_num!r}, target={target_name!r},"
                f" expected profile={expected_profile!r}, target={expected_target!r}"
            )
    logger.info("Internal NC parser tests passed.")


def main() -> None:
    """Main entrypoint for Telegram NC Bot."""
    try:
        _run_nc_parser_tests()
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down bot applications.")
    except Exception as e:
        logger.error("Unexpected failure running bot applications: %s", e)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# EMBEDDED COOLDOWN JSON
# This marker intentionally remains in this single-file distribution so the
# default cooldown state travels with the script. Runtime persistence uses
# cooldowns.json when the script is running.
# ---------------------------------------------------------------------------
COOLDOWN_JSON_STATE = {}
