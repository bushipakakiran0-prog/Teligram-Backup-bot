#!/usr/bin/env python3
"""
Private-group Telegram file backup bot.

Features:
  * /start replies with the current chat ID.
  * Saves photos, videos, documents, audio, voice notes, video notes,
    and animations under:
        BACKUP_DIR/<chat_id>/<YYYY-MM-DD>/
  * Writes a small .json metadata file next to each saved file.
  * Supports polling and webhook modes.

Environment variables:
  TELEGRAM_BOT_TOKEN   required
  BACKUP_DIR           default: ./backups
  ALLOWED_CHAT_ID      optional but strongly recommended
  MODE                 polling | webhook   default: polling
  PORT                 default: 8443       webhook mode only
  WEBHOOK_URL          required in webhook mode unless RENDER_EXTERNAL_URL is set
  WEBHOOK_PATH         default: telegram-webhook
  WEBHOOK_SECRET       optional shared secret for webhook requests
  LOG_LEVEL            default: INFO
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import timezone
from pathlib import Path
from typing import Optional

from telegram import Message, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "backups")).expanduser().resolve()

ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "").strip()

MODE = os.environ.get("MODE", "polling").strip().lower()
PORT = int(os.environ.get("PORT", "8443"))

WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL", "").strip()
    or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
).rstrip("/")

WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "telegram-webhook").strip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip() or None

MEDIA_FILTER = (
    filters.PHOTO
    | filters.VIDEO
    | filters.Document.ALL
    | filters.AUDIO
    | filters.VOICE
    | filters.VIDEO_NOTE
    | filters.ANIMATION
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("telegram-backup-bot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_allowed(chat_id: int) -> bool:
    """If ALLOWED_CHAT_ID is set, only save files from that chat."""
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == ALLOWED_CHAT_ID


def _largest_attachment(message: Message):
    """Return the largest photo or the single attachment object."""
    try:
        attachment = message.effective_attachment
    except ValueError:
        return None

    # Telegram returns photo sizes smallest -> largest.
    if isinstance(attachment, (list, tuple)):
        return attachment[-1]
    return attachment


def _safe_filename(
    original_name: Optional[str],
    fallback: str,
    suffix: str,
) -> str:
    """Sanitize a Telegram-supplied filename for safe local storage."""
    if original_name:
        # Strip any directory components supplied by Telegram.
        name = Path(original_name).name
        name = re.sub(
            r"[^\w.\- ()\[\]]+",
            "_",
            name,
            flags=re.UNICODE,
        ).strip(" ._")
        name = name[:150]
    else:
        name = ""

    if not name:
        name = fallback

    # Preserve an extension if the original name lacks one.
    if suffix and not Path(name).suffix:
        name += suffix

    return name


def _destination_for(
    message: Message,
    tg_file,
    original_name: Optional[str],
    suffix: str,
) -> Path:
    """Build a collision-resistant destination path."""
    day = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d")

    folder = BACKUP_DIR / str(message.chat_id) / day
    folder.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_filename(
        original_name,
        fallback=f"message_{message.message_id}",
        suffix=suffix,
    )

    destination = folder / f"{message.message_id}_{safe_name}"

    # Avoid overwriting if the same update is retried.
    if destination.exists():
        unique = getattr(tg_file, "file_unique_id", "duplicate")[:16]
        destination = folder / f"{message.message_id}_{unique}_{safe_name}"

    return destination


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply with the chat ID."""
    message = update.effective_message
    chat = update.effective_chat

    if message is None or chat is None:
        return

    await message.reply_text(
        f"Chat ID: {chat.id}\n"
        f"Chat type: {chat.type}\n\n"
        "Set this value as ALLOWED_CHAT_ID to restrict the bot "
        "to this chat."
    )


async def save_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Download and save any supported media attachment."""
    message = update.effective_message

    if message is None:
        return

    if not _is_allowed(message.chat_id):
        logger.warning(
            "Ignoring media from disallowed chat %s",
            message.chat_id,
        )
        return

    attachment = _largest_attachment(message)
    if attachment is None:
        return

    try:
        # Get Telegram File object.
        tg_file = await context.bot.get_file(attachment.file_id)

        # Derive original name and extension.
        original_name = (
            None if message.photo else getattr(attachment, "file_name", None)
        )
        suffix = Path(tg_file.file_path).suffix if tg_file.file_path else ""

        destination = _destination_for(
            message=message,
            tg_file=tg_file,
            original_name=original_name,
            suffix=suffix,
        )

        # Download the file to disk.
        await tg_file.download_to_drive(custom_path=destination)

        # Save metadata alongside the file.
        metadata = {
            "chat_id": message.chat_id,
            "chat_title": message.chat.title,
            "message_id": message.message_id,
            "date": message.date.isoformat(),
            "sender_id": message.from_user.id if message.from_user else None,
            "sender_username": (
                message.from_user.username if message.from_user else None
            ),
            "file_id": tg_file.file_id,
            "file_unique_id": tg_file.file_unique_id,
            "saved_file_name": destination.name,
            "mime_type": getattr(attachment, "mime_type", None),
            "file_size": getattr(attachment, "file_size", None),
        }

        metadata_path = destination.parent / f"{destination.name}.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        logger.info("Saved %s", destination)

    except Exception:
        logger.exception(
            "Failed to save media from chat %s, message %s",
            message.chat_id,
            message.message_id,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    application = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(MEDIA_FILTER, save_media))

    logger.info("Backup directory: %s", BACKUP_DIR)

    if not ALLOWED_CHAT_ID:
        logger.warning(
            "ALLOWED_CHAT_ID is not set. The bot will save media from "
            "any chat it can read."
        )

    if MODE == "webhook":
        if not WEBHOOK_URL:
            raise SystemExit(
                "WEBHOOK_URL or RENDER_EXTERNAL_URL must be set in webhook mode"
            )

        logger.info(
            "Starting webhook server on port %s, path /%s",
            PORT,
            WEBHOOK_PATH,
        )

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_PATH}",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )

    elif MODE == "polling":
        logger.info("Starting long polling")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )

    else:
        raise SystemExit(
            f"Unsupported MODE={MODE!r}. Use 'polling' or 'webhook'."
        )


if __name__ == "__main__":
    main()
