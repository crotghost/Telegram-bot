import os
import sys
import asyncio
import logging
import signal
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatJoinRequestHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
CHANNEL_ID    = os.environ["TELEGRAM_CHANNEL_ID"]

_raw_group = os.environ.get("TELEGRAM_ADMIN_GROUP_ID", "").strip()
ADMIN_GROUP_ID: int | None = int(_raw_group) if _raw_group else None

WEBHOOK_PORT = 8082
WEBHOOK_PATH = "/webhook"

VERIFICATION_MESSAGE = (
    "👋 Ciao! Hai richiesto l'accesso al canale.\n\n"
    "Per completare la verifica, inviaci due foto:\n\n"
    "1️⃣ Uno screenshot del tuo profilo Instagram\n"
    "2️⃣ Un selfie con il documento d'identità in mano\n\n"
    "Le foto verranno esaminate dal nostro admin. "
    "Se approvato, verrai aggiunto automaticamente al canale.\n\n"
    "Invia la prima foto quando sei pronto."
)

START_MESSAGE = (
    "Benvenuto! Per completare la verifica, inviaci due foto:\n\n"
    "1️⃣ Uno screenshot del tuo profilo Instagram\n"
    "2️⃣ Un selfie con il documento d'identità in mano\n\n"
    "Le foto verranno esaminate dal nostro admin. "
    "Se approvato, riceverai un link di invito privato al canale.\n\n"
    "Invia la prima foto quando sei pronto."
)

PAUSED_MESSAGE = "⏸️ Il bot è temporaneamente in pausa. Riprova più tardi."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_webhook_domain() -> str | None:
    for key in ("REPLIT_DOMAINS", "REPLIT_DEV_DOMAIN"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return raw.split(",")[0].strip()
    return None


def _is_bot_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.bot_data.get("bot_active", True)


def _user_display(user) -> str:
    info = f"👤 <b>{user.full_name}</b>"
    if user.username:
        info += f" (@{user.username})"
    info += f"\n🆔 ID: <code>{user.id}</code>"
    return info


async def _is_authorized(bot, user_id: int) -> bool:
    if user_id == ADMIN_CHAT_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


async def _notify_admins(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Failed to notify main admin: {e}")

    if ADMIN_GROUP_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Failed to notify admin group {ADMIN_GROUP_ID}: {e}")


async def _send_admin_error(bot, message: str) -> None:
    """Send an error alert to the admin. Never raises."""
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ <b>Errore nel bot</b>\n\n<code>{message[:800]}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ── Admin control panel ────────────────────────────────────────────────────────

def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Stato Bot",   callback_data="admin:status"),
            InlineKeyboardButton("▶️ Attiva",       callback_data="admin:activate"),
        ],
        [
            InlineKeyboardButton("⏸️ Pausa",       callback_data="admin:pause"),
            InlineKeyboardButton("🔄 Riavvia Bot", callback_data="admin:restart"),
        ],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/admin — show the admin control panel (admin only)."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    active  = _is_bot_active(context)
    status  = "🟢 Attivo" if active else "🔴 In Pausa"
    await update.message.reply_text(
        f"🛠️ <b>Pannello di Controllo Admin</b>\n\n"
        f"Stato corrente: {status}",
        parse_mode="HTML",
        reply_markup=_admin_keyboard(),
    )


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin panel button presses."""
    query = update.callback_query
    try:
        await query.answer()

        if query.from_user.id != ADMIN_CHAT_ID:
            await query.answer("Non autorizzato.", show_alert=True)
            return

        action = query.data.split(":")[1]
        active = _is_bot_active(context)

        if action == "status":
            status = "🟢 Attivo" if active else "🔴 In Pausa"
            await query.answer(f"Stato: {status}", show_alert=True)

        elif action == "activate":
            context.bot_data["bot_active"] = True
            logger.info("Bot activated by admin")
            await query.edit_message_text(
                "✅ <b>Bot Attivato</b>\n\nIl bot è ora attivo e sta elaborando le richieste.",
                parse_mode="HTML",
                reply_markup=_admin_keyboard(),
            )

        elif action == "pause":
            context.bot_data["bot_active"] = False
            logger.info("Bot paused by admin")
            await query.edit_message_text(
                "⏸️ <b>Bot in Pausa</b>\n\nIl bot sta ignorando le richieste degli utenti.",
                parse_mode="HTML",
                reply_markup=_admin_keyboard(),
            )

        elif action == "restart":
            await query.edit_message_text(
                "🔄 <b>Riavvio in corso...</b>\n\nIl bot sarà operativo tra pochi secondi.",
                parse_mode="HTML",
            )
            logger.info("Restart requested by admin")
            asyncio.create_task(_delayed_restart())

    except Exception as e:
        logger.error(f"Error in handle_admin_callback: {e}", exc_info=True)


async def _delayed_restart() -> None:
    """Wait briefly so the callback response is sent, then terminate (watchdog will restart)."""
    await asyncio.sleep(2)
    logger.info("Sending SIGTERM for admin-requested restart")
    os.kill(os.getpid(), signal.SIGTERM)


# ── Heartbeat ──────────────────────────────────────────────────────────────────

async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every 5 minutes to confirm the bot process is alive."""
    active = context.bot_data.get("bot_active", True)
    status = "Active" if active else "PAUSED"
    logger.info(f"[Heartbeat] Bot alive | Status: {status} | Mode: webhook")


# ── User-facing handlers ───────────────────────────────────────────────────────

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not _is_bot_active(context):
            logger.info("Bot paused — join request ignored")
            return

        join_request = update.chat_join_request
        user    = join_request.from_user
        chat_id = join_request.chat.id

        logger.info(f"Join request: user={user.id} (@{user.username}) chat={chat_id}")

        user_info    = _user_display(user)
        approve_data = f"approve:join:{chat_id}:{user.id}"
        reject_data  = f"reject:join:{chat_id}:{user.id}"
        verify_data  = f"verify:join:{chat_id}:{user.id}"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approva",         callback_data=approve_data),
                InlineKeyboardButton("❌ Rifiuta",         callback_data=reject_data),
            ],
            [
                InlineKeyboardButton("🔍 Avvia Verifica", callback_data=verify_data),
            ],
        ])

        await _notify_admins(
            context,
            f"📥 <b>Nuova richiesta di accesso al canale</b>\n\n{user_info}\n\nScegli un'azione:",
            keyboard,
        )
    except Exception as e:
        logger.error(f"Error in handle_join_request: {e}", exc_info=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user

        if context.args:
            param = context.args[0]

            if param == "apply":
                if not _is_bot_active(context):
                    await update.message.reply_text(PAUSED_MESSAGE)
                    return
                logger.info(f"Verification link: user={user.id} (@{user.username})")
                context.user_data["join_chat_id"] = None
                context.user_data["photo_count"]  = 0
                context.user_data["photos"]       = []
                await update.message.reply_text(VERIFICATION_MESSAGE)
                return

            if param.startswith("jc_"):
                try:
                    _, join_chat_id_str, expected_uid_str = param.split("_", 2)
                    join_chat_id     = int(join_chat_id_str)
                    expected_user_id = int(expected_uid_str)

                    if user.id != expected_user_id:
                        await update.message.reply_text(
                            "❌ Questo link di verifica non è per il tuo account."
                        )
                        return

                    if not _is_bot_active(context):
                        await update.message.reply_text(PAUSED_MESSAGE)
                        return

                    logger.info(f"Fallback verification link: user={user.id}")
                    context.user_data["join_chat_id"] = join_chat_id
                    context.user_data["photo_count"]  = 0
                    context.user_data["photos"]       = []
                    context.application.bot_data.get("pending_verifications", {}).pop(user.id, None)
                    await update.message.reply_text(VERIFICATION_MESSAGE)
                    return
                except (ValueError, AttributeError):
                    pass

        context.user_data.pop("join_chat_id", None)
        context.user_data["photo_count"] = 0
        context.user_data["photos"]      = []
        await update.message.reply_text(START_MESSAGE)

    except Exception as e:
        logger.error(f"Error in start handler: {e}", exc_info=True)


async def get_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_user.id != ADMIN_CHAT_ID:
            return
        bot_me = await context.bot.get_me()
        link   = f"https://t.me/{bot_me.username}?start=apply"
        await update.message.reply_text(
            f"🔗 <b>Link di verifica da condividere:</b>\n\n"
            f"{link}\n\n"
            f"Chiunque clicchi questo link verrà guidato attraverso la verifica "
            f"prima di accedere al canale.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error in get_invite_link: {e}", exc_info=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not _is_bot_active(context):
            await update.message.reply_text(PAUSED_MESSAGE)
            return

        user  = update.effective_user
        photo = update.message.photo[-1]

        context.user_data.setdefault("photo_count", 0)
        context.user_data.setdefault("photos", [])

        context.user_data["photo_count"] += 1
        context.user_data["photos"].append(photo.file_id)
        count = context.user_data["photo_count"]

        if count == 1:
            await update.message.reply_text(
                "✅ Prima foto ricevuta!\n\n"
                "Ora invia la seconda foto: un selfie con il documento d'identità in mano."
            )
            return

        if count > 2:
            await update.message.reply_text(
                "Hai già inviato entrambe le foto. "
                "Attendi che il nostro admin esamini la tua richiesta."
            )
            return

        await update.message.reply_text(
            "✅ Entrambe le foto ricevute!\n\n"
            "La tua richiesta è in fase di revisione. Riceverai una risposta a breve."
        )

        join_chat_id = context.user_data.get("join_chat_id")
        if join_chat_id is None:
            pending      = context.application.bot_data.get("pending_verifications", {})
            join_chat_id = pending.pop(user.id, None)

        if join_chat_id:
            approve_data = f"approve:join:{join_chat_id}:{user.id}"
            reject_data  = f"reject:join:{join_chat_id}:{user.id}"
            flow_label   = "Richiesta di accesso al canale"
        else:
            approve_data = f"approve:invite:{user.id}"
            reject_data  = f"reject:invite:{user.id}"
            flow_label   = "Link di verifica"

        keyboard  = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approva", callback_data=approve_data),
            InlineKeyboardButton("❌ Rifiuta", callback_data=reject_data),
        ]])
        user_info = _user_display(user) + f"\n🔗 <b>Flusso:</b> {flow_label}"
        photo_ids = context.user_data["photos"][:]

        for dest in ([ADMIN_CHAT_ID] + ([ADMIN_GROUP_ID] if ADMIN_GROUP_ID else [])):
            try:
                await context.bot.send_photo(
                    chat_id=dest,
                    photo=photo_ids[0],
                    caption=(
                        f"📋 <b>Nuova Richiesta di Verifica</b>\n\n"
                        f"{user_info}\n\n📸 <i>Screenshot Instagram</i>"
                    ),
                    parse_mode="HTML",
                )
                await context.bot.send_photo(
                    chat_id=dest,
                    photo=photo_ids[1],
                    caption=f"🤳 <i>Selfie con documento</i>\n\n{user_info}",
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(f"Could not send photos to {dest}: {e}")

        # Clear state — don't hold file IDs in memory after forwarding
        context.user_data["photo_count"] = 0
        context.user_data["photos"]      = []
        context.user_data.pop("join_chat_id", None)

        logger.info(f"Verification photos forwarded: user={user.id}")

    except Exception as e:
        logger.error(f"Error in handle_photo: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "⚠️ Si è verificato un errore. Riprova o contatta il supporto."
            )
        except Exception:
            pass


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()

        # Route admin panel callbacks separately
        if query.data.startswith("admin:"):
            await handle_admin_callback(update, context)
            return

        if not await _is_authorized(context.bot, query.from_user.id):
            await query.answer("Non sei autorizzato.", show_alert=True)
            return

        parts  = query.data.split(":")
        action = parts[0]
        mode   = parts[1]

        if mode == "join":
            join_chat_id = int(parts[2])
            user_id      = int(parts[3])
        else:
            join_chat_id = None
            user_id      = int(parts[2])

        # ── VERIFY ──────────────────────────────────────────────────────────
        if action == "verify":
            try:
                pending = context.application.bot_data.setdefault("pending_verifications", {})
                pending[user_id] = join_chat_id

                await context.bot.send_message(
                    chat_id=user_id,
                    text=VERIFICATION_MESSAGE,
                )

                new_text = (query.message.text or "") + \
                    "\n\n🔍 <b>Stato: Verifica avviata — in attesa delle foto</b>"
                await query.edit_message_text(text=new_text, parse_mode="HTML")
                logger.info(f"Verification started: user={user_id} admin={query.from_user.id}")

            except Exception as e:
                logger.error(f"Cannot start verification for user={user_id}: {e}")
                try:
                    bot_me    = await context.bot.get_me()
                    param     = f"jc_{join_chat_id}_{user_id}" if join_chat_id else f"jc_0_{user_id}"
                    deep_link = f"https://t.me/{bot_me.username}?start={param}"
                    msg = (
                        f"⚠️ <b>Impossibile contattare l'utente {user_id}.</b>\n\n"
                        f"L'utente non ha ancora avviato il bot.\n\n"
                        f"<b>Soluzione:</b> invia questo link all'utente:\n{deep_link}"
                    )
                except Exception:
                    msg = f"⚠️ Impossibile contattare l'utente {user_id}.\nErrore: {e}"

                for dest in ([ADMIN_CHAT_ID] + ([ADMIN_GROUP_ID] if ADMIN_GROUP_ID else [])):
                    try:
                        await context.bot.send_message(
                            chat_id=dest, text=msg, parse_mode="HTML",
                        )
                    except Exception:
                        pass
            return

        # ── APPROVE ─────────────────────────────────────────────────────────
        if action == "approve":
            try:
                if join_chat_id:
                    await context.bot.approve_chat_join_request(
                        chat_id=join_chat_id, user_id=user_id,
                    )
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "🎉 <b>La tua verifica è stata approvata!</b>\n\n"
                            "Sei stato aggiunto al canale. Benvenuto! 🎊"
                        ),
                        parse_mode="HTML",
                    )
                else:
                    invite = await context.bot.create_chat_invite_link(
                        chat_id=CHANNEL_ID, member_limit=1,
                        name=f"Verified user {user_id}",
                    )
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "🎉 <b>La tua verifica è stata approvata!</b>\n\n"
                            f"Ecco il tuo link di invito privato:\n{invite.invite_link}\n\n"
                            "<i>Questo link è monouso ed è stato creato appositamente per te.</i>"
                        ),
                        parse_mode="HTML",
                    )

                suffix = "\n\n✅ <b>Stato: Approvato</b>"
                if query.message.caption is not None:
                    await query.edit_message_caption(
                        caption=query.message.caption + suffix, parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text(
                        text=(query.message.text or "") + suffix, parse_mode="HTML",
                    )
                logger.info(f"User {user_id} approved (mode={mode}) by {query.from_user.id}")

            except Exception as e:
                logger.error(f"Error approving user {user_id}: {e}")
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=f"⚠️ Errore durante l'approvazione dell'utente {user_id}:\n{e}",
                    )
                except Exception:
                    pass

        # ── REJECT ──────────────────────────────────────────────────────────
        elif action == "reject":
            try:
                if join_chat_id:
                    await context.bot.decline_chat_join_request(
                        chat_id=join_chat_id, user_id=user_id,
                    )

                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "❌ <b>La tua richiesta è stata rifiutata.</b>\n\n"
                        "Non siamo riusciti a verificare la tua identità. "
                        "Se ritieni che si tratti di un errore, contatta il supporto."
                    ),
                    parse_mode="HTML",
                )

                suffix = "\n\n❌ <b>Stato: Rifiutato</b>"
                if query.message.caption is not None:
                    await query.edit_message_caption(
                        caption=query.message.caption + suffix, parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text(
                        text=(query.message.text or "") + suffix, parse_mode="HTML",
                    )
                logger.info(f"User {user_id} rejected (mode={mode}) by {query.from_user.id}")

            except Exception as e:
                logger.error(f"Error rejecting user {user_id}: {e}")
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=f"⚠️ Errore durante il rifiuto dell'utente {user_id}:\n{e}",
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"Error in handle_callback: {e}", exc_info=True)


async def handle_non_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.message is None:
            return
        if not _is_bot_active(context):
            await update.message.reply_text(PAUSED_MESSAGE)
            return
        count = context.user_data.get("photo_count", 0)
        if count == 0:
            await update.message.reply_text(
                "Per favore invia una foto per la verifica. "
                "I messaggi di testo non sono accettati.\n\n"
                "Usa /start per rivedere le istruzioni."
            )
        else:
            await update.message.reply_text(
                "Hai già inviato la prima foto. "
                "Ora invia un selfie con il documento d'identità in mano."
            )
    except Exception as e:
        logger.error(f"Error in handle_non_photo: {e}", exc_info=True)


# ── Global error handler ───────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.warning("Conflict — another bot instance may still be shutting down.")
        return
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f"Network issue (auto-recovering): {context.error}")
        return

    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)
    await _send_admin_error(context.bot, str(context.error))


# ── Application setup ──────────────────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    """Schedule background jobs after the application initialises."""
    application.job_queue.run_repeating(
        heartbeat,
        interval=300,   # every 5 minutes
        first=300,
        name="heartbeat",
    )
    logger.info("Heartbeat job scheduled (every 5 minutes)")


def build_application() -> Application:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    application.add_handler(ChatJoinRequestHandler(handle_join_request))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("link",  get_invite_link))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^admin:"))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_non_photo)
    )
    application.add_handler(
        MessageHandler(~filters.PHOTO & ~filters.TEXT & ~filters.COMMAND, handle_non_photo)
    )
    application.add_error_handler(error_handler)
    return application


# ── Entry point ────────────────────────────────────────────────────────────────

async def run_bot() -> None:
    domain      = _get_webhook_domain()
    use_webhook = domain is not None

    logger.info(
        f"Starting bot | mode={'webhook' if use_webhook else 'polling'}"
        + (f" | domain={domain}" if use_webhook else "")
    )

    attempt = 0

    while True:
        attempt += 1
        application = build_application()
        wait = 0

        try:
            logger.info(f"Bot starting (attempt {attempt})...")
            await application.initialize()
            await application.start()

            if use_webhook:
                webhook_url = f"https://{domain}/webhook/telegram"
                await application.updater.start_webhook(
                    listen="127.0.0.1",
                    port=WEBHOOK_PORT,
                    url_path=WEBHOOK_PATH,
                    webhook_url=webhook_url,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                )
                logger.info(f"Bot running (webhook → {webhook_url})")
            else:
                await application.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                )
                logger.info("Bot running (polling mode)")

            attempt = 0
            await asyncio.Event().wait()
            return

        except Conflict:
            wait = min(10 * attempt, 60)
            logger.warning(f"Conflict — retry in {wait}s (attempt {attempt})")
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down...")
            return
        except Exception as e:
            wait = min(5 * attempt, 30)
            logger.error(f"Unexpected error: {e} — retry in {wait}s", exc_info=True)
        finally:
            try:
                await application.updater.stop()
            except Exception:
                pass
            try:
                await application.stop()
                await application.shutdown()
            except Exception:
                pass

        if wait:
            await asyncio.sleep(wait)


def main() -> None:
    # Global handler for truly uncaught synchronous exceptions
    original_excepthook = sys.excepthook

    def _excepthook(exctype, value, tb):
        logger.error("Uncaught exception", exc_info=(exctype, value, tb))
        original_excepthook(exctype, value, tb)

    sys.excepthook = _excepthook

    # Global handler for unhandled asyncio exceptions
    def _asyncio_exception_handler(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "Unknown asyncio error")
        if exc:
            logger.error(f"Unhandled asyncio exception: {exc}", exc_info=exc)
        else:
            logger.error(f"Unhandled asyncio error: {msg}")

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_asyncio_exception_handler)
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(run_bot())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
