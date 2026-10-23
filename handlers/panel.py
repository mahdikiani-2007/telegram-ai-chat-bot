import functools
import logging
import os
import threading
import time

from telebot import apihelper, types

from bots import bot
from config import ADMIN_IDS, IRAN_OFFSET_SECONDS, OWNER_ID, TAVILY_API_KEY, TAVILY_DAILY_LIMIT
from db import repo
from handlers.shared import is_admin, is_owner
from metrics import METRICS, _START_TIME
from services import panel_settings as ps
from services.streaming import _safe_reply_to, escape_markdown
from sessions import (
    PANEL_ADD_ADMIN,
    PANEL_BROADCAST_CHAT,
    PANEL_BROADCAST_CONTENT,
    PANEL_GROUP_SEARCH,
    PANEL_DIGEST_GROUP_SEARCH,
    PANEL_DIGEST_PROFILE_NAME,
    PANEL_DIGEST_PROFILE_TOPIC,
    PANEL_DIGEST_PROFILE_EDIT_TOPIC,
    PANEL_LIMIT_ADD,
    PANEL_SETTING_VALUE,
    PANEL_USER_SEARCH,
    TTLSessionStore,
    _panel_input_sessions,
)

log = logging.getLogger("L-bot")

DIGEST_MODE_CODES = {0: "standard", 1: "off"}
DIGEST_FILTER_CODES = {
    0: "all", 1: "standard", 2: "off", 3: "profile", 4: "unreachable",
}


# =========================
# panel_guard: admin + private-chat enforcement for every panel entry point
# =========================
def panel_guard(owner_only=False):
    """Re-check permission on EVERY panel update (commands, callbacks, text
    inputs) via check_panel_access — never rely on 'the menu was shown
    earlier', never trust callback data. Silent denial (no existence leak);
    callback spinners dismissed quietly. Crashes contained: short error to
    the admin, full traceback to the log."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(message_or_call):
            is_callback = hasattr(message_or_call, "data")
            allowed, _chat, _uid = ps.check_panel_access(
                message_or_call, owner_only=owner_only
            )
            if not allowed:
                if is_callback:
                    try:
                        bot.answer_callback_query(message_or_call.id)
                    except Exception:
                        pass
                return None
            try:
                return fn(message_or_call)
            except Exception:
                log.error("Panel handler %s crashed", fn.__name__, exc_info=True)
                try:
                    if is_callback:
                        bot.answer_callback_query(message_or_call.id, "❌ Internal error.")
                    else:
                        _safe_reply_to(bot, message_or_call, "❌ Internal error, logged.")
                except Exception:
                    pass
                return None

        return wrapper

    return decorator


# =========================
# Navigation helpers (single message edited in place)
# =========================
def _nav_markup(extra_rows=(), back="pnl_home"):
    markup = types.InlineKeyboardMarkup(row_width=2)
    for row in extra_rows:
        markup.add(*row)
    nav = []
    if back and back != "pnl_home":
        nav.append(types.InlineKeyboardButton("◀ Back", callback_data=back))
    nav.append(types.InlineKeyboardButton("🏠 Home", callback_data="pnl_home"))
    markup.add(*nav)
    return markup


def _edit_panel(call, text, markup=None):
    """Edit the panel message in place; tolerate 'message is not modified'
    and always dismiss the callback spinner."""
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e).lower():
            log.debug("panel edit failed", exc_info=True)
    except Exception:
        log.debug("panel edit failed", exc_info=True)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def _btn(label, data):
    return types.InlineKeyboardButton(label, callback_data=data)


def _maybe_remove_legacy_keyboard(message):
    """F1: the removed reply-keyboard broadcast flow may still be displayed
    in the owner's client. Once (tracked flag), send a minimal
    ReplyKeyboardRemove and delete it right after. Best-effort only."""
    try:
        if not is_owner(message.from_user.id):
            return
        if ps.get_setting("ui.kb_removed", "0") == "1":
            return
        rm = bot.send_message(
            message.chat.id, "⌨",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        ps.set_setting("ui.kb_removed", "1")
        try:
            bot.delete_message(message.chat.id, rm.message_id)
        except Exception:
            pass
    except Exception:
        log.debug("legacy keyboard removal failed", exc_info=True)


# Stale texts from the removed keyboard: never let them reach the LLM chat
# pipeline for the owner — answer with a pointer instead. Private only,
# owner only, silent for everyone else. Registered here (before the chat
# catch-all) so first-match dispatch consumes them.
STALE_KEYBOARD_TEXTS = frozenset({
    "👥 Broadcast to Groups", "👤 Broadcast to Users", "🔄 Broadcast to Both",
    "🎯 Send to Specific Chat", "📋 Group List", "❌ Cancel",
})


def _is_stale_keyboard_text(message):
    try:
        return (bool(message.text) and message.text in STALE_KEYBOARD_TEXTS
                and message.chat is not None
                and getattr(message.chat, "type", None) == "private"
                and message.from_user is not None
                and is_owner(message.from_user.id))
    except Exception:
        return False


@bot.message_handler(func=_is_stale_keyboard_text, content_types=["text"])
def handle_stale_keyboard_text(message):
    try:
        _safe_reply_to(bot, message, "That panel is gone — use /admin for the new control panel.")
    except Exception:
        log.debug("stale keyboard hint failed", exc_info=True)


# =========================
# /admin home
# =========================
@bot.message_handler(commands=["admin"])
@panel_guard(owner_only=False)
def handle_admin_panel(message):
    """Panel entry: private chat + admin only, silent otherwise (guard)."""
    _maybe_remove_legacy_keyboard(message)
    owner = is_owner(message.from_user.id)
    rows = [
        (_btn("📊 Status", "pnl_status"), _btn("🚧 Maintenance", "pnl_maint")),
        (_btn("👥 Users", "pnl_users_0"), _btn("💬 Groups", "pnl_groups_0")),
        (_btn("📰 Digest", "pnl_digest"),),
        (_btn("🎛 Features", "pnl_features"), _btn("⚙ Settings", "pnl_settings")),
        (_btn("⏰ Jobs", "pnl_jobs"), _btn("📈 Stats", "pnl_stats")),
        (_btn("📜 Audit log", "pnl_audit_0"),),
    ]
    if owner:
        rows.append((_btn("🛡 Admins", "pnl_admins"), _btn("📢 Broadcast", "pnl_bcast_menu")))
    warn = _paused_warning_line()
    text = (
        "🔧 **Admin Panel**\n\n"
        f"Owner: `{OWNER_ID}`\n"
        f"Role: {'owner' if owner else 'admin'}\n"
        + (f"{warn}\n" if warn else "") +
        "\nChoose a section below."
    )
    bot.send_message(
        message.chat.id, text, parse_mode="Markdown",
        reply_markup=_nav_markup(rows, back=None),
    )


@bot.callback_query_handler(func=lambda c: c.data == "pnl_home")
@panel_guard(owner_only=False)
def handle_panel_home(call):
    owner = is_owner(call.from_user.id)
    rows = [
        (_btn("📊 Status", "pnl_status"), _btn("🚧 Maintenance", "pnl_maint")),
        (_btn("👥 Users", "pnl_users_0"), _btn("💬 Groups", "pnl_groups_0")),
        (_btn("📰 Digest", "pnl_digest"),),
        (_btn("🎛 Features", "pnl_features"), _btn("⚙ Settings", "pnl_settings")),
        (_btn("⏰ Jobs", "pnl_jobs"), _btn("📈 Stats", "pnl_stats")),
        (_btn("📜 Audit log", "pnl_audit_0"),),
    ]
    if owner:
        rows.append((_btn("🛡 Admins", "pnl_admins"), _btn("📢 Broadcast", "pnl_bcast_menu")))
    warn = _paused_warning_line()
    _edit_panel(
        call,
        "🔧 **Admin Panel**\n\n"
        f"Owner: `{OWNER_ID}`\n"
        f"Role: {'owner' if owner else 'admin'}"
        + (f"\n{warn}" if warn else ""),
        _nav_markup(rows, back=None),
    )


# =========================
# Status & health
# =========================
def _build_status_text():
    lines = ["📊 **Status & health**\n"]
    try:
        import datetime as _dt

        uptime = str(_dt.timedelta(seconds=int(time.time() - _START_TIME)))
        lines.append(f"⏱ Uptime: `{uptime}`")
    except Exception:
        lines.append("⏱ Uptime: `n/a`")
    try:
        import importlib.metadata as _ilm

        lines.append(f"📦 pyTelegramBotAPI: `{_ilm.version('pyTelegramBotAPI')}`")
    except Exception:
        lines.append("📦 pyTelegramBotAPI: `n/a`")
    try:
        from config import DB_FILE

        size_mb = os.path.getsize(DB_FILE) / 1024 / 1024
        lines.append(f"💾 DB size: `{size_mb:.1f} MB`")
    except Exception:
        lines.append("💾 DB size: `n/a`")
    try:
        import resource as _res

        rss_mb = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024
        lines.append(f"🧠 Peak RSS: `{rss_mb:.0f} MB`")
    except Exception:
        lines.append("🧠 Peak RSS: `n/a`")
    try:
        lines.append(f"🧵 Threads: `{len(threading.enumerate())}`")
    except Exception:
        lines.append("🧵 Threads: `n/a`")
    try:
        import sessions as _sess

        total = sum(len(s) for s in _sess._ALL_SESSION_STORES + _sess._ALL_SESSION_STORES_LATE)
        lines.append(f"🗂 Session entries: `{total}`")
    except Exception:
        lines.append("🗂 Session entries: `n/a`")
    try:
        lines.append(f"✅ Handled: `{METRICS.get('handled', 0)}`")
        lines.append(f"❌ LLM errors: `{METRICS.get('llm_errors', 0)}`")
        lines.append(f"🚫 Rate-limited: `{METRICS.get('ratelimited', 0)}`")
        lines.append(f"🔄 Poller restarts: `{METRICS.get('poller_restarts', 0)}`")
    except Exception:
        lines.append("counters: `n/a`")
    try:
        from services.media import userbot_available

        lines.append(f"📤 Userbot: `{'up' if userbot_available() else 'down'}`")
    except Exception:
        lines.append("📤 Userbot: `n/a`")
    lines.append(
        "🔎 Tavily: `{}`".format(
            "disabled" if not TAVILY_API_KEY
            else (f"cap {TAVILY_DAILY_LIMIT}/day" if TAVILY_DAILY_LIMIT > 0 else "uncapped")
        )
    )
    lines.append(f"🚧 Maintenance: `{'ON' if ps.maintenance_active() else 'off'}`")
    return "\n".join(lines)


@bot.callback_query_handler(func=lambda c: c.data == "pnl_status")
@panel_guard(owner_only=False)
def handle_panel_status(call):
    _edit_panel(call, _build_status_text(), _nav_markup())


# =========================
# Maintenance (confirm-gated, audited)
# =========================
@bot.callback_query_handler(func=lambda c: c.data == "pnl_maint")
@panel_guard(owner_only=False)
def handle_panel_maint(call):
    on = ps.maintenance_active()
    rows = [(
        _btn("🔴 Turn OFF", "pnl_maint_cf_off") if on
        else _btn("🟢 Turn ON", "pnl_maint_cf_on"),
    )]
    _edit_panel(
        call,
        "🚧 **Maintenance mode**\n\n"
        f"State: `{'ON — non-admins get a maintenance reply' if on else 'off — bot behaves normally'}`\n\n"
        "Admins are always exempt. Scheduled jobs keep running.",
        _nav_markup(rows),
    )


@bot.callback_query_handler(func=lambda c: c.data in ("pnl_maint_cf_on", "pnl_maint_cf_off"))
@panel_guard(owner_only=False)
def handle_panel_maint_confirm(call):
    turn_on = call.data.endswith("_on")
    rows = [(_btn("✅ Confirm", "pnl_maint_on" if turn_on else "pnl_maint_off"),)]
    _edit_panel(
        call,
        "🚧 **Confirm**\n\n" + (
            "Turn maintenance **ON**? Non-admin updates will be held with a "
            "rate-limited notice until you turn it off."
            if turn_on else
            "Turn maintenance **OFF**? The bot resumes normal replies."
        ),
        _nav_markup(rows, back="pnl_maint"),
    )


@bot.callback_query_handler(func=lambda c: c.data in ("pnl_maint_on", "pnl_maint_off"))
@panel_guard(owner_only=False)
def handle_panel_maint_apply(call):
    turn_on = call.data == "pnl_maint_on"
    ps.set_setting(ps.MAINTENANCE_KEY, "1" if turn_on else "0")
    ps.audit(call.from_user.id, "maintenance", "on" if turn_on else "off", "ok")
    _edit_panel(
        call,
        f"🚧 Maintenance is now `{'ON' if turn_on else 'off'}`.",
        _nav_markup(),
    )


# =========================
# Admins (OWNER ONLY): list / add via typed input / remove DB-added
# =========================
def _build_admins_text():
    lines = ["🛡 **Admins**\n", f"Owner: `{OWNER_ID}` (config, unremovable)\n"]
    others = [i for i in ADMIN_IDS if i != OWNER_ID]
    if others:
        lines.append("Via config: `" + "`, `".join(str(i) for i in others) + "`")
    try:
        rows = repo.list_panel_admins()
    except Exception:
        rows = []
        lines.append("\n⚠️ DB lookup failed.")
    if rows:
        lines.append("\nVia panel:")
        for uid, added_by, added_at in rows:
            lines.append(f"• `{uid}` (by `{added_by}`, {added_at})")
    elif not others:
        lines.append("\nNo additional admins.")
    return "\n".join(lines)


@bot.callback_query_handler(func=lambda c: c.data == "pnl_admins")
@panel_guard(owner_only=True)
def handle_panel_admins(call):
    rows = [(_btn("➕ Add admin", "pnl_admin_add"),)]
    try:
        for uid, _added_by, _at in repo.list_panel_admins():
            rows.append((_btn(f"➖ Remove {uid}", f"pnl_admin_del_{uid}"),))
    except Exception:
        pass
    _edit_panel(call, _build_admins_text(), _nav_markup(rows))


@bot.callback_query_handler(func=lambda c: c.data == "pnl_admin_add")
@panel_guard(owner_only=True)
def handle_panel_admin_add(call):
    _panel_input_sessions.set(call.from_user.id, {"state": PANEL_ADD_ADMIN})
    _edit_panel(
        call,
        "➕ **Add admin**\n\nSend the numeric Telegram user ID to grant admin rights.\n"
        "A /command aborts the input; the ❌ button cancels. Expires with the session TTL.",
        _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)], back="pnl_admins"),
    )


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_admin_del_")
    and not c.data.startswith("pnl_admin_del_ok_")
)
@panel_guard(owner_only=True)
def handle_panel_admin_del(call):
    try:
        target = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    rows = [(_btn("✅ Confirm remove", f"pnl_admin_del_ok_{target}"),)]
    _edit_panel(
        call,
        f"➖ **Confirm**\n\nRemove admin `{target}`? They lose panel access immediately.",
        _nav_markup(rows, back="pnl_admins"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_admin_del_ok_"))
@panel_guard(owner_only=True)
def handle_panel_admin_del_ok(call):
    try:
        target = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    if target <= 0 or target == OWNER_ID or target in ADMIN_IDS:
        _edit_panel(call, "⛔ That ID cannot be removed here (owner/config).", _nav_markup(back="pnl_admins"))
        return
    try:
        removed = ps.revoke_admin(target)
    except Exception:
        log.error("panel admin remove failed", exc_info=True)
        _edit_panel(call, "❌ DB error while removing.", _nav_markup(back="pnl_admins"))
        return
    ps.audit(call.from_user.id, "admin_remove", str(target), "ok" if removed else "not-found")
    _edit_panel(
        call,
        f"✅ Admin `{target}` removed." if removed else f"ℹ️ `{target}` was not a panel admin.",
        _nav_markup(back="pnl_admins"),
    )


@bot.callback_query_handler(func=lambda c: c.data == "pnl_cancel")
@panel_guard(owner_only=False)
def handle_panel_cancel(call):
    _panel_input_sessions.discard(call.from_user.id)
    try:
        bot.answer_callback_query(call.id, "❎ Cancelled.")
    except Exception:
        pass
    handle_panel_home(call)


def _is_panel_input_waiting(message):
    """Panel text-input interceptor guard: only an admin in private chat with
    an ACTIVE panel input session; /commands cancel the session and pass
    through to their real handlers; everything else passes through."""
    try:
        if message.chat is None or getattr(message.chat, "type", None) != "private":
            return False
        if not message.from_user or not is_admin(message.from_user.id):
            return False
        session = _panel_input_sessions.get(message.from_user.id)
        if session is None:
            return False
        if message.text and message.text.startswith("/"):
            _panel_input_sessions.discard(message.from_user.id)
            return False
        if session.get("state") == PANEL_BROADCAST_CONTENT:
            return True  # any supported content type accepted as draft
        return bool(message.text)
    except Exception:
        return False


BROADCAST_CONTENT_TYPES = [
    "text", "photo", "video", "document", "audio", "voice",
    "video_note", "animation", "sticker", "contact", "location", "poll",
]


@bot.message_handler(func=_is_panel_input_waiting, content_types=BROADCAST_CONTENT_TYPES)
def handle_panel_text_input(message):
    """Consume one typed panel input (add-admin ID, user/group search)."""
    try:
        session = _panel_input_sessions.pop(message.from_user.id)
        if not session:
            return
        state = session.get("state")
        if state == PANEL_ADD_ADMIN:
            _handle_add_admin_text(message)
        elif state == PANEL_USER_SEARCH:
            _handle_user_search_text(message)
        elif state == PANEL_GROUP_SEARCH:
            _handle_group_search_text(message)
        elif state == PANEL_DIGEST_GROUP_SEARCH:
            _handle_digest_group_search_text(message)
        elif state == PANEL_DIGEST_PROFILE_NAME:
            _handle_digest_profile_name_text(message)
        elif state == PANEL_DIGEST_PROFILE_TOPIC:
            _handle_digest_profile_topic_text(message, session)
        elif state == PANEL_DIGEST_PROFILE_EDIT_TOPIC:
            _handle_digest_profile_edit_topic_text(message, session)
        elif state == PANEL_BROADCAST_CONTENT:
            _handle_broadcast_content(message, session)
        elif state == PANEL_BROADCAST_CHAT:
            _handle_broadcast_chat_text(message)
        elif state == PANEL_SETTING_VALUE:
            key = session.get("key")
            if key in ps.SETTINGS_SPEC:
                _handle_setting_value_text(message, key)
        elif state == PANEL_LIMIT_ADD:
            _handle_limit_add_text(message)
    except Exception:
        log.error("PANEL TEXT INPUT ERROR", exc_info=True)
        _safe_reply_to(bot, message, "❌ Internal error, logged.")


def _handle_add_admin_text(message):
    raw = (message.text or "").strip()
    if len(raw) > 32:
        _safe_reply_to(bot, message, "❌ ID too long. Send /admin to reopen the panel.")
        return
    try:
        target = int(raw)
    except ValueError:
        _safe_reply_to(bot, message, "❌ That is not a numeric ID. Send /admin to reopen the panel.")
        return
    if target <= 0:
        _safe_reply_to(bot, message, "❌ ID must be a positive integer.")
        return
    me = message.from_user.id
    if target == me:
        _safe_reply_to(bot, message, "❌ You are already an admin.")
        return
    if not is_owner(me):
        return  # owner-only step, re-checked: never trust flow state
    if is_admin(target):
        _safe_reply_to(bot, message, f"ℹ️ `{target}` is already an admin.")
        return
    try:
        ps.grant_admin(target, me)
    except Exception:
        log.error("panel admin add failed", exc_info=True)
        _safe_reply_to(bot, message, "❌ DB error while adding.")
        return
    ps.audit(me, "admin_add", str(target), "ok")
    _safe_reply_to(bot, message, f"✅ `{target}` is now an admin. Send /admin to reopen the panel.")


# =========================
# Audit log viewer (paged)
# =========================
AUDIT_PAGE = 20


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_audit_"))
@panel_guard(owner_only=False)
def handle_panel_audit(call):
    try:
        offset = max(0, int(call.data.rsplit("_", 1)[1]))
    except (ValueError, IndexError):
        offset = 0
    try:
        total = repo.count_panel_audit()
        rows = repo.list_panel_audit(limit=AUDIT_PAGE, offset=offset)
    except Exception:
        log.error("panel audit read failed", exc_info=True)
        _edit_panel(call, "❌ DB error while reading audit log.", _nav_markup())
        return
    if not rows and offset == 0:
        _edit_panel(call, "📜 **Audit log**\n\nNo entries yet.", _nav_markup())
        return
    lines = [f"📜 **Audit log** ({total} entries)\n"]
    for _id, admin_id, action, target, result, created_at in rows:
        line = f"`#{_id}` `{admin_id}` {escape_markdown(action)}"
        if target:
            line += f" → `{escape_markdown(target)}`"
        if result:
            line += f" ({escape_markdown(result)})"
        line += f"\n_{escape_markdown(str(created_at))}_"
        lines.append(line)
    pager = []
    if offset > 0:
        pager.append(_btn("◀ Newer", f"pnl_audit_{max(0, offset - AUDIT_PAGE)}"))
    if offset + AUDIT_PAGE < total:
        pager.append(_btn("Older ▶", f"pnl_audit_{offset + AUDIT_PAGE}"))
    rows_nav = [tuple(pager)] if pager else []
    _edit_panel(call, "\n".join(lines)[:4000], _nav_markup(rows_nav))


# =========================
# Stage 2A: Users / Groups / Ban / Leave (G2: escape + truncate everything
# user-originated; G3: admins/owner/self are never valid targets)
# =========================
PAGE_SIZE = 10


def _txt(s, limit=80):
    """Escape + truncate user-originated text for Markdown messages."""
    text = str(s or "").replace("\r", " ").replace("\n", " ")
    text = "".join(ch if ord(ch) >= 32 else " " for ch in text)
    return escape_markdown(text[:limit]).replace("<", "\\<").replace(">", "\\>")


def _target_allowed(target, me):
    """G3: owner/admins/self can never be banned or touched through the panel."""
    if target == me:
        return False, "You cannot act on yourself."
    if is_admin(target):
        return False, "That user is an admin — admins and the owner can never be touched here."
    return True, ""


def _pager_rows(prefix, offset, total):
    row = []
    if offset > 0:
        row.append(_btn("◀ Prev", f"{prefix}{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        row.append(_btn("Next ▶", f"{prefix}{offset + PAGE_SIZE}"))
    return [tuple(row)] if row else []


# ---------- Users list + detail + search ----------
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_users_"))
@panel_guard(owner_only=False)
def handle_panel_users(call):
    try:
        offset = max(0, int(call.data.rsplit("_", 1)[1]))
    except (ValueError, IndexError):
        offset = 0
    try:
        total = repo.count_known_chats(("private",))
        rows = repo.list_known_chats_paged(("private",), PAGE_SIZE, offset)
    except Exception:
        log.error("panel users list failed", exc_info=True)
        _edit_panel(call, "❌ DB error while listing users.", _nav_markup())
        return
    if not rows and offset == 0:
        text = "👥 **Users**\n\nNo users seen yet."
    else:
        lines = [f"👥 **Users** ({total})\n"]
        buttons = []
        for cid, _typ, username, _title, last_seen in rows:
            label = f"@{username}" if username else cid
            lines.append(f"• {_txt(label, 40)} — `{cid}`")
            try:
                buttons.append((_btn(_txt(label, 28), f"pnl_user_{int(cid)}"),))
            except (TypeError, ValueError):
                continue
        text = "\n".join(lines)
        buttons.extend(_pager_rows("pnl_users_", offset, total))
        buttons.append((_btn("🔍 Search", "pnl_user_search"),))
        _edit_panel(call, text, _nav_markup(buttons))
        return
    _edit_panel(
        call, text,
        _nav_markup([(_btn("🔍 Search", "pnl_user_search"),)]),
    )


def _build_user_detail(uid):
    lines = [f"👤 **User `{uid}`**\n"]
    try:
        known = repo.get_known_chat(str(uid))
    except Exception:
        known = None
    if known:
        _cid, typ, username, title, last_seen = known
        if username:
            lines.append(f"Username: {_txt('@' + username)}")
        lines.append(f"Seen: {_txt(typ)} / {_txt(last_seen)}")
    try:
        lines.append(f"💬 DM messages: `{repo.count_messages(str(uid))}`")
        lines.append(f"🧠 Facts: `{repo.count_user_facts(uid)}`")
        lines.append(f"🗂 Sessions: `{repo.count_sessions(uid)}`")
        lines.append(f"📋 Active todos: `{repo.count_todos_by_creator(uid)}`")
        lines.append(f"🔔 Active reminders: `{repo.count_reminders_by_creator(uid)}`")
    except Exception:
        lines.append("⚠️ Counters unavailable (DB error).")
    if is_owner(uid):
        lines.append("Role: `owner`")
    elif is_admin(uid):
        lines.append("Role: `admin`")
    try:
        from handlers.shared import get_cached_ban_ids

        lines.append(f"Ban: `{'banned' if uid in get_cached_ban_ids() else 'no'}`")
    except Exception:
        pass
    return "\n".join(lines)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_upick_"))
@panel_guard(owner_only=False)
def handle_panel_user_pick(call):
    try:
        uid = int(call.data.rsplit("_", 1)[1])
        if uid <= 0:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    handle_panel_user_show(call, uid)


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_user_")
    and c.data[len("pnl_user_"):].isdigit()
)
@panel_guard(owner_only=False)
def handle_panel_user(call):
    # Numeric-shape guard keeps pnl_user_search / pnl_users_* on their own
    # handlers regardless of registration order (first-match dispatch).
    try:
        uid = int(call.data.rsplit("_", 1)[1])
        if uid <= 0:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    handle_panel_user_show(call, uid)


def handle_panel_user_show(call, uid):
    rows = []
    try:
        from handlers.shared import get_cached_ban_ids

        banned = uid in get_cached_ban_ids()
    except Exception:
        banned = False
    if banned:
        rows.append((_btn("✅ Unban", f"pnl_unban_{uid}"),))
    else:
        rows.append((_btn("🚫 Ban", f"pnl_ban_{uid}"),))
    if is_owner(call.from_user.id):
        rows.append((_btn("🗑 Reset data", f"pnl_reset_{uid}"),))
    _edit_panel(call, _build_user_detail(uid), _nav_markup(rows, back="pnl_users_0"))


@bot.callback_query_handler(func=lambda c: c.data == "pnl_user_search")
@panel_guard(owner_only=False)
def handle_panel_user_search(call):
    _panel_input_sessions.set(call.from_user.id, {"state": PANEL_USER_SEARCH})
    _edit_panel(
        call,
        "🔍 **User search**\n\nSend a numeric ID or @username. Exact matches first, "
        "then substring (max 10 rows) — you pick from the results.",
        _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)], back="pnl_users_0"),
    )


def _handle_user_search_text(message):
    raw = (message.text or "").strip()[:64]
    try:
        rows = repo.search_known_chats(raw, limit=10)
    except Exception:
        log.error("panel user search failed", exc_info=True)
        _safe_reply_to(bot, message, "❌ DB error while searching.")
        return
    picks, seen = [], set()
    for cid, typ, username, title, _last in rows:
        if typ != "private":
            continue
        try:
            uid = int(cid)
        except (TypeError, ValueError):
            continue
        if uid in seen or uid <= 0:
            continue
        seen.add(uid)
        picks.append((uid, username))
    try:
        for _chat, muid, muser, _fn, _ls in repo.search_group_members(raw, limit=10):
            if muid in seen or muid <= 0:
                continue
            seen.add(muid)
            picks.append((muid, muser))
    except Exception:
        log.error("panel member search failed", exc_info=True)
    if not picks:
        _safe_reply_to(bot, message, "🔍 No match. Send /admin to reopen the panel.")
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    lines = ["🔍 **Pick a user** (never acts on first match):\n"]
    for uid, username in picks[:10]:
        label = f"@{username}" if username else str(uid)
        lines.append(f"• {_txt(label, 40)} — `{uid}`")
        markup.add(_btn(_txt(label, 28), f"pnl_upick_{uid}"))
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown", reply_markup=markup)


# ---------- Digest overview + groups ----------
def _digest_today():
    return __import__("timeutil").format_trigger_time_iran(time.time()).split(" ")[0]


def _digest_overview_text():
    snap = __import__("services.digest", fromlist=["digest_run_snapshot"]).digest_run_snapshot()
    report = snap.get("report") or {}
    try:
        from services.search import tavily_budget
        remaining = tavily_budget.remaining()
        limit = TAVILY_DAILY_LIMIT
        used = "uncapped" if remaining is None else max(0, int(limit) - int(remaining))
    except Exception:
        remaining, limit, used = "unknown", "unknown", "unknown"
    reserve = ps.setting_value("digest.search_reserve")
    counts = " ".join(
        f"{key}={int(report.get(key, 0) or 0)}"
        for key in ("sent", "failed", "unreachable", "skipped_off",
                    "skipped_budget", "skipped_no_results")
    )
    return (
        "📰 **Digest**\n\n"
        f"Today: `{_txt(_digest_today())}`\n"
        f"Tavily: used `{used}` / limit `{limit}` / remaining `{remaining}`\n"
        f"Reserve: `{reserve}`\n"
        f"Last run: {counts or 'not run'}\n"
        f"Run state: `{'running' if snap.get('active') else 'idle'}`"
    )


@bot.callback_query_handler(func=lambda c: c.data == "pnl_digest")
@panel_guard(owner_only=False)
def handle_panel_digest(call):
    rows = [(_btn("💬 Groups", "pnl_dglist_0"),)]
    if is_owner(call.from_user.id):
        rows.append((_btn("🗂 Profiles", "pnl_dgprofiles_0"),))
    _edit_panel(
        call,
        _digest_overview_text(),
        _nav_markup(rows + [(_btn("🔄 Refresh", "pnl_digest"),)]),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_dgprofiles_"))
@panel_guard(owner_only=True)
def handle_panel_digest_profiles(call):
    profiles = repo.list_digest_profiles()
    lines = ["🗂 **Digest profiles**\n"]
    rows = [(_btn("➕ Create profile", "pnl_dgprofile_new_0"),)]
    for pid, name, _topic, _created, _at, previewed in profiles:
        state = "previewed" if previewed else "needs preview"
        lines.append(f"• `{pid}` {_txt(name, 40)} — {state}")
        rows.append((_btn(f"✏️ {_txt(name, 24)}", f"pnl_dgprofile_edit_{pid}"),
                     _btn("👁 Preview", f"pnl_dgprofile_preview_{pid}")))
        rows.append((_btn("🗑 Delete", f"pnl_dgprofile_delete_{pid}"),))
    _edit_panel(call, "\n".join(lines), _nav_markup(rows, back="pnl_digest"))


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_dgprofile_new_"))
@panel_guard(owner_only=True)
def handle_panel_digest_profile_new(call):
    _panel_input_sessions.set(call.from_user.id, {
        "state": PANEL_DIGEST_PROFILE_NAME, "owner_only": True,
    })
    _edit_panel(call, "🗂 **New profile**\n\nSend a unique name (1–40 chars).",
                _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)],
                            back="pnl_dgprofiles_0"))


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_dgprofile_edit_")
    and c.data[len("pnl_dgprofile_edit_"):].isdigit()
)
@panel_guard(owner_only=True)
def handle_panel_digest_profile_edit(call):
    pid = int(call.data[len("pnl_dgprofile_edit_"):])
    profile = repo.get_digest_profile(pid)
    if not profile:
        bot.answer_callback_query(call.id, "❌ Profile unavailable.")
        return
    _panel_input_sessions.set(call.from_user.id, {
        "state": PANEL_DIGEST_PROFILE_EDIT_TOPIC,
        "profile_id": pid, "owner_only": True,
    })
    _edit_panel(call, f"✏️ **Edit `{_txt(profile[1], 40)}`**\n\nSend the new topic.",
                _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)],
                            back="pnl_dgprofiles_0"))


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_dgprofile_preview_")
    and c.data[len("pnl_dgprofile_preview_"):].isdigit()
)
@panel_guard(owner_only=True)
def handle_panel_digest_profile_preview(call):
    pid = int(call.data[len("pnl_dgprofile_preview_"):])
    from services import digest as digest_service

    result = digest_service.preview_profile(call.from_user.id, pid, call.message.chat.id)
    if not result.get("ok"):
        reasons = {
            "rate_limit": "preview rate limit reached",
            "budget": "remaining budget must keep the reserve",
            "unpreviewed": "profile is unavailable",
            "no_results": "no search results",
        }
        _edit_panel(call, f"❌ Preview refused: {reasons.get(result.get('reason'), 'unavailable')}.",
                    _nav_markup(back="pnl_dgprofiles_0"))
        return
    for chunk in _split_digest_text("Digest preview", result["text"]):
        bot.send_message(call.message.chat.id, chunk, parse_mode="Markdown")
    _edit_panel(call, "✅ Preview sent only to your private chat.",
                _nav_markup(back="pnl_dgprofiles_0"))


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_dgprofile_delete_")
    and c.data[len("pnl_dgprofile_delete_"):].isdigit()
)
@panel_guard(owner_only=True)
def handle_panel_digest_profile_delete(call):
    pid = int(call.data[len("pnl_dgprofile_delete_"):])
    profile = repo.get_digest_profile(pid)
    if not profile:
        bot.answer_callback_query(call.id, "❌ Profile unavailable.")
        return
    count = repo.count_digest_profile_groups(pid)
    _edit_panel(
        call,
        f"🗑 **Confirm delete**\n\n`{_txt(profile[1], 40)}` is assigned to `{count}` group(s). "
        "Those groups revert to Standard.",
        _nav_markup([(_btn("✅ Confirm delete", f"pnl_dgprofile_delete_ok_{pid}"),)],
                    back="pnl_dgprofiles_0"),
    )


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_dgprofile_delete_ok_")
    and c.data[len("pnl_dgprofile_delete_ok_"):].isdigit()
)
@panel_guard(owner_only=True)
def handle_panel_digest_profile_delete_ok(call):
    pid = int(call.data[len("pnl_dgprofile_delete_ok_"):])
    affected = repo.delete_digest_profile(pid)
    ps.audit(call.from_user.id, "digest_profile_delete", str(pid), f"groups={len(affected)}")
    _edit_panel(call, f"✅ Profile deleted; `{len(affected)}` group(s) reverted to Standard.",
                _nav_markup(back="pnl_dgprofiles_0"))


def _render_digest_groups(call, filter_key="all", query="", page=0):
    offset = max(0, int(page)) * 10
    rows = repo.list_digest_groups_paged(filter_key, query, 10, offset)
    total = repo.count_digest_groups(filter_key, query)
    lines = [f"💬 **Digest groups** ({total})\n"]
    buttons = []
    for cid, _typ, username, title, _last, mode, _pid, status, _error, failures in rows:
        name = (title or "").strip() or (f"@{username}" if username else str(cid))
        icon = "⛔" if mode == "off" else ("🟣" if mode == "profile" else "🟢")
        if failures >= 3:
            icon = "⚠️"
        lines.append(f"{icon} {_txt(name, 42)} — `{cid}` {status or 'pending'}")
        buttons.append((_btn(_txt(name, 28), f"pnl_dggrp_{cid}"),))
    buttons.append(tuple(_btn(label, f"pnl_dgfilter_{code}")
                         for code, label in (("0", "All"), ("1", "Standard"),
                                             ("2", "Off"), ("3", "Profile"),
                                             ("4", "Unreachable"))))
    if total > 10:
        nav = []
        if page:
            nav.append(_btn("◀", f"pnl_dglist_{page - 1}"))
        if offset + len(rows) < total:
            nav.append(_btn("▶", f"pnl_dglist_{page + 1}"))
        if nav:
            buttons.append(tuple(nav))
    buttons.append((_btn("🔎 Search", "pnl_dgsearch_0"),))
    return "\n".join(lines), buttons


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_dglist_"))
@panel_guard(owner_only=False)
def handle_panel_digest_groups(call):
    try:
        page = max(0, int(call.data.rsplit("_", 1)[1]))
    except (ValueError, IndexError):
        page = 0
    try:
        text, buttons = _render_digest_groups(call, page=page)
    except Exception:
        log.error("panel digest groups list failed", exc_info=True)
        _edit_panel(call, "❌ DB error.", _nav_markup(back="pnl_digest"))
        return
    _edit_panel(call, text, _nav_markup(buttons, back="pnl_digest"))


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_dgfilter_"))
@panel_guard(owner_only=False)
def handle_panel_digest_filter(call):
    try:
        code = int(call.data.rsplit("_", 1)[1])
        filter_key = DIGEST_FILTER_CODES[code]
        text, buttons = _render_digest_groups(call, filter_key=filter_key)
    except (ValueError, KeyError):
        bot.answer_callback_query(call.id, "❌ Bad button payload.")
        return
    _edit_panel(call, text, _nav_markup(buttons, back="pnl_digest"))


@bot.callback_query_handler(func=lambda c: c.data == "pnl_dgsearch_0")
@panel_guard(owner_only=False)
def handle_panel_digest_search(call):
    _panel_input_sessions.set(call.from_user.id, {"state": PANEL_DIGEST_GROUP_SEARCH})
    _edit_panel(call, "🔎 **Digest group search**\n\nSend a title, @username or chat ID.",
                _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)], back="pnl_dglist_0"))


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_dggrp_")
    and c.data[len("pnl_dggrp_"):].lstrip("-").isdigit()
)
@panel_guard(owner_only=False)
def handle_panel_digest_group(call):
    cid = int(call.data[len("pnl_dggrp_"):])
    known = repo.get_known_chat(str(cid))
    if not repo.is_known_digest_group(cid):
        bot.answer_callback_query(call.id, "❌ Group unavailable.")
        return
    if not known:
        _edit_panel(call, "That group is no longer available.", _nav_markup(back="pnl_dglist_0"))
        return
    modes = repo.get_digest_modes()
    mode, pid = modes.get(str(cid), ("standard", None))
    profile_name = ""
    if pid:
        profile = repo.get_digest_profile(pid)
        profile_name = profile[1] if profile else "deleted"
    today = _digest_today()
    delivery = repo.get_digest_delivery(cid, today)
    status = delivery[1] if delivery else "no ledger row"
    title = known[3] or known[2] or str(cid)
    text = (
        f"💬 **{_txt(title, 60)}**\n\n"
        f"Mode: `{_txt(mode)}`"
        + (f" (`{_txt(profile_name, 40)}`)" if pid else "")
        + f"\nToday's ledger: `{_txt(status)}`"
    )
    rows = [
        (_btn("🟢 Standard", f"pnl_dgset_{cid}_0"), _btn("⛔ Off", f"pnl_dgset_{cid}_1")),
        (_btn("🟣 Profile", f"pnl_dgprof_{cid}"),),
    ]
    _edit_panel(call, text, _nav_markup(rows, back="pnl_dglist_0"))


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_dgset_")
)
@panel_guard(owner_only=False)
def handle_panel_digest_group_set(call):
    parts = call.data.split("_")
    if (len(parts) != 4 or not parts[2].lstrip("-").isdigit()
            or not parts[3].isdigit() or int(parts[3]) not in DIGEST_MODE_CODES):
        bot.answer_callback_query(call.id, "❌ Bad button payload.")
        return
    cid, mode = int(parts[2]), DIGEST_MODE_CODES[int(parts[3])]
    if not repo.is_known_digest_group(cid):
        bot.answer_callback_query(call.id, "❌ Group unavailable.")
        return
    try:
        old = repo.get_digest_modes().get(str(cid), ("standard", None))[0]
        repo.set_digest_mode(cid, mode, None, call.from_user.id)
        ps.invalidate_digest_modes()
        ps.audit(call.from_user.id, "digest_mode", str(cid), f"{old} -> {mode}")
    except Exception:
        _edit_panel(call, "❌ Unable to update digest mode.", _nav_markup(back="pnl_dglist_0"))
        return
    handle_panel_digest_group(call)


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_dgprof_")
    and c.data[len("pnl_dgprof_"):].lstrip("-").isdigit()
)
@panel_guard(owner_only=False)
def handle_panel_digest_profile_picker(call):
    cid = int(call.data[len("pnl_dgprof_"):])
    if not repo.is_known_digest_group(cid):
        bot.answer_callback_query(call.id, "❌ Group unavailable.")
        return
    rows = []
    for pid, name, _topic, _created, _at, previewed in repo.list_digest_profiles():
        if previewed:
            rows.append((_btn(_txt(name, 32), f"pnl_dgassign_{cid}_{pid}"),))
    rows.append((_btn("🟢 Standard", f"pnl_dgset_{cid}_0"),))
    _edit_panel(call, "🟣 **Choose a previewed profile**", _nav_markup(rows, back=f"pnl_dggrp_{cid}"))


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_dgassign_"))
@panel_guard(owner_only=False)
def handle_panel_digest_profile_assign(call):
    parts = call.data.split("_")
    if len(parts) != 4 or not parts[2].lstrip("-").isdigit() or not parts[3].isdigit():
        bot.answer_callback_query(call.id, "❌ Bad button payload.")
        return
    cid, pid = int(parts[2]), int(parts[3])
    if not repo.is_known_digest_group(cid):
        bot.answer_callback_query(call.id, "❌ Group unavailable.")
        return
    try:
        repo.set_digest_mode(cid, "profile", pid, call.from_user.id)
        ps.invalidate_digest_modes()
        ps.audit(call.from_user.id, "digest_profile_assign", str(cid), str(pid))
    except Exception:
        _edit_panel(call, "❌ Profile is unavailable or not previewed.", _nav_markup(back=f"pnl_dggrp_{cid}"))
        return
    handle_panel_digest_group(call)


# ---------- Groups list + detail + search + leave (owner-only) ----------
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_groups_"))
@panel_guard(owner_only=False)
def handle_panel_groups(call):
    try:
        offset = max(0, int(call.data.rsplit("_", 1)[1]))
    except (ValueError, IndexError):
        offset = 0
    types_ = ("group", "supergroup")
    try:
        total = repo.count_known_chats(types_)
        rows = repo.list_known_chats_paged(types_, PAGE_SIZE, offset)
    except Exception:
        log.error("panel groups list failed", exc_info=True)
        _edit_panel(call, "❌ DB error while listing groups.", _nav_markup())
        return
    if not rows and offset == 0:
        text = "💬 **Groups**\n\nNo groups seen yet."
    else:
        lines = [f"💬 **Groups** ({total})\n"]
        buttons = []
        for cid, _typ, username, title, _last in rows:
            name = (title or "").strip() or (f"@{username}" if username else "بدون عنوان")
            lines.append(f"• {_txt(name, 50)} — `{cid}`")
            buttons.append((_btn(_txt(name, 28), f"pnl_group_{cid}"),))
        text = "\n".join(lines)
        buttons.extend(_pager_rows("pnl_groups_", offset, total))
        buttons.append((_btn("🔍 Search", "pnl_group_search"),))
        _edit_panel(call, text, _nav_markup(buttons))
        return
    _edit_panel(call, text, _nav_markup([(_btn("🔍 Search", "pnl_group_search"),)]))


def _build_group_detail(cid):
    lines = [f"💬 **Group `{cid}`**\n"]
    try:
        known = repo.get_known_chat(str(cid))
    except Exception:
        known = None
    title = ""
    if known:
        _c, typ, username, title, last_seen = known
        lines.append(f"Type: {_txt(typ)}")
        if username:
            lines.append(f"Username: {_txt('@' + username)}")
        lines.append(f"Last seen: {_txt(last_seen)}")
    else:
        lines.append("Not in known_chats (stale button?).")
    try:
        lines.append(f"💬 Messages: `{repo.count_messages(str(cid))}`")
        nmembers = repo.count_group_members(str(cid))
        lines.append(f"👥 Observed members: `{nmembers}`")
        members = repo.list_group_members_paged(str(cid), 10, 0)
        for muid, muser, mfirst, _ls in members:
            mname = mfirst or (f"@{muser}" if muser else str(muid))
            lines.append(f"  • {_txt(mname, 40)} (`{muid}`)")
        if nmembers > len(members):
            lines.append(f"  … +{nmembers - len(members)} more")
    except Exception:
        lines.append("⚠️ Counters unavailable (DB error).")
    return "\n".join(lines), (title or "")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_gpick_"))
@panel_guard(owner_only=False)
def handle_panel_group_pick(call):
    try:
        cid = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    handle_panel_group_show(call, cid)


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_group_")
    and c.data[len("pnl_group_"):].lstrip("-").isdigit()
)
@panel_guard(owner_only=False)
def handle_panel_group(call):
    try:
        cid = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    handle_panel_group_show(call, cid)


def handle_panel_group_show(call, cid):
    text, _title = _build_group_detail(cid)
    rows = []
    try:
        debate_on = ps.feature_enabled("debate", chat_id=cid)
    except Exception:
        debate_on = True
    rows.append((_btn(
        f"{'🔴' if debate_on else '🟢'} Debate: {'ON' if debate_on else 'OFF'}",
        f"pnl_gdebate_{cid}"),))
    try:
        from services.panel_settings import get_digest_modes

        mode = get_digest_modes().get(str(cid), ("standard", None))[0]
    except Exception:
        mode = "standard"
    rows.append((_btn(
        f"📰 Digest: {'OFF' if mode == 'off' else 'ON'}",
        f"pnl_dgmode_{cid}"),))
    if is_owner(call.from_user.id):
        rows.append((_btn("🚪 Leave chat", f"pnl_leave_{cid}"),))
    _edit_panel(call, text, _nav_markup(rows, back="pnl_groups_0"))


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_dgmode_"))
@panel_guard(owner_only=False)
def handle_panel_group_digest_mode(call):
    """Stage A minimal digest switch: Standard <-> Off only (profile
    assignment UI arrives in Stage B). Any admin may flip; audited."""
    try:
        cid = int(call.data[len("pnl_dgmode_"):])
    except (TypeError, ValueError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    try:
        from services.panel_settings import get_digest_modes

        old = get_digest_modes().get(str(cid), ("standard", None))[0]
    except Exception:
        old = "standard"
    new = "off" if old != "off" else "standard"
    try:
        repo.set_digest_mode(cid, new, None, call.from_user.id)
        ps.invalidate_digest_modes()
    except Exception:
        log.error("panel digest mode flip failed", exc_info=True)
        _edit_panel(call, "❌ DB error.", _nav_markup(back="pnl_groups_0"))
        return
    ps.audit(call.from_user.id, "digest_mode", str(cid), f"{old} -> {new}")
    handle_panel_group_show(call, cid)


@bot.callback_query_handler(func=lambda c: c.data == "pnl_group_search")
@panel_guard(owner_only=False)
def handle_panel_group_search(call):
    _panel_input_sessions.set(call.from_user.id, {"state": PANEL_GROUP_SEARCH})
    _edit_panel(
        call,
        "🔍 **Group search**\n\nSend a numeric chat ID, @username or title fragment.",
        _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)], back="pnl_groups_0"),
    )


def _handle_group_search_text(message):
    raw = (message.text or "").strip()[:64]
    try:
        rows = repo.search_known_chats(raw, limit=10)
    except Exception:
        log.error("panel group search failed", exc_info=True)
        _safe_reply_to(bot, message, "❌ DB error while searching.")
        return
    picks = [(cid, title or username) for cid, typ, username, title, _l in rows
             if typ in ("group", "supergroup")]
    if not picks:
        _safe_reply_to(bot, message, "🔍 No match. Send /admin to reopen the panel.")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    lines = ["🔍 **Pick a group**:\n"]
    for cid, name in picks[:10]:
        label = (name or "").strip() or cid
        lines.append(f"• {_txt(label, 50)} — `{cid}`")
        markup.add(_btn(_txt(label, 28), f"pnl_gpick_{cid}"))
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown", reply_markup=markup)


def _handle_digest_group_search_text(message):
    raw = (message.text or "").strip()[:64]
    try:
        rows = repo.search_known_chats(raw, limit=10)
        rows = [r for r in rows if r[1] in ("group", "supergroup")]
    except Exception:
        log.error("panel digest group search failed", exc_info=True)
        _safe_reply_to(bot, message, "❌ DB error while searching.")
        return
    if not rows:
        _safe_reply_to(bot, message, "🔍 No group match. Send /admin to reopen the panel.")
        return
    lines = ["🔎 **Digest group results**\n"]
    markup = types.InlineKeyboardMarkup(row_width=1)
    for cid, _typ, username, title, _last in rows:
        label = (title or "").strip() or (f"@{username}" if username else str(cid))
        lines.append(f"• {_txt(label, 50)} — `{cid}`")
        markup.add(_btn(_txt(label, 32), f"pnl_dggrp_{cid}"))
    markup.add(_btn("◀ Back", "pnl_dglist_0"))
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown",
                     reply_markup=markup)


def _profile_input_owner(message, session):
    return (getattr(message.chat, "type", None) == "private"
            and message.from_user is not None
            and session.get("owner_only") is True
            and is_owner(message.from_user.id))


def _handle_digest_profile_name_text(message):
    if not (message.chat.type == "private" and is_owner(message.from_user.id)):
        return
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 40:
        _safe_reply_to(bot, message, "❌ Name must be 1–40 characters.")
        _panel_input_sessions.set(message.from_user.id, {
            "state": PANEL_DIGEST_PROFILE_NAME, "owner_only": True,
        })
        return
    _panel_input_sessions.set(message.from_user.id, {
        "state": PANEL_DIGEST_PROFILE_TOPIC, "name": name, "owner_only": True,
    })
    _safe_reply_to(bot, message, "Now send the profile topic, or tap Cancel in the panel.")


def _handle_digest_profile_topic_text(message, session):
    if not _profile_input_owner(message, session):
        return
    try:
        pid = repo.create_digest_profile(
            session.get("name"), message.text, message.from_user.id)
    except ValueError as exc:
        _safe_reply_to(bot, message, f"❌ {_txt(exc, 160)}")
        _panel_input_sessions.set(message.from_user.id, session)
        return
    ps.audit(message.from_user.id, "digest_profile_create", str(pid), "ok")
    _safe_reply_to(bot, message, "✅ Profile created. Preview it before assignment.")


def _handle_digest_profile_edit_topic_text(message, session):
    if not _profile_input_owner(message, session):
        return
    try:
        ok = repo.update_digest_profile_topic(session.get("profile_id"), message.text)
    except ValueError as exc:
        _safe_reply_to(bot, message, f"❌ {_txt(exc, 160)}")
        _panel_input_sessions.set(message.from_user.id, session)
        return
    if not ok:
        _safe_reply_to(bot, message, "❌ Profile unavailable.")
        return
    ps.audit(message.from_user.id, "digest_profile_topic", str(session["profile_id"]), "updated")
    _safe_reply_to(bot, message, "✅ Topic updated; preview is required again.")


# ---------- Leave chat (OWNER ONLY, confirm-gated) ----------
@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_leave_")
    and not c.data.startswith("pnl_leave_ok_")
)
@panel_guard(owner_only=True)
def handle_panel_leave(call):
    try:
        cid = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    text, title = _build_group_detail(cid)
    rows = [(_btn("✅ Confirm leave", f"pnl_leave_ok_{cid}"),)]
    _edit_panel(
        call,
        f"🚪 **Confirm**\n\nLeave {_txt(title or cid, 60)} (`{cid}`)?\n"
        "The bot stops receiving updates until re-added. Known data stays.",
        _nav_markup(rows, back="pnl_groups_0"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_leave_ok_"))
@panel_guard(owner_only=True)
def handle_panel_leave_ok(call):
    try:
        cid = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    try:
        bot.leave_chat(cid)
    except Exception as e:
        log.error("panel leave chat failed for %s", cid, exc_info=True)
        _edit_panel(call, f"❌ Leave failed: {_txt(e, 200)}", _nav_markup(back="pnl_groups_0"))
        return
    ps.audit(call.from_user.id, "group_leave", str(cid), "ok")
    _edit_panel(call, f"🚪 Left `{cid}`.", _nav_markup(back="pnl_groups_0"))


# ---------- Ban / Unban (any admin, confirm-gated, G3-guarded) ----------
@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_ban_")
    and not c.data.startswith("pnl_ban_ok_")
)
@panel_guard(owner_only=False)
def handle_panel_ban(call):
    try:
        target = int(call.data.rsplit("_", 1)[1])
        if target <= 0:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    ok, reason = _target_allowed(target, call.from_user.id)
    if not ok:
        _edit_panel(call, f"⛔ {reason}", _nav_markup(back="pnl_users_0"))
        return
    rows = [(_btn("✅ Confirm ban", f"pnl_ban_ok_{target}"),)]
    _edit_panel(
        call,
        f"🚫 **Confirm**\n\nBan `{target}`?\n"
        "• No reply to anything they send (silently held)\n"
        "• Scheduled reminders/todos pause until unban\n"
        "• Broadcasts skip them\n"
        "• Admins/owner/self can never be banned",
        _nav_markup(rows, back="pnl_users_0"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_ban_ok_"))
@panel_guard(owner_only=False)
def handle_panel_ban_ok(call):
    try:
        target = int(call.data.rsplit("_", 1)[1])
        if target <= 0:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    ok, reason = _target_allowed(target, call.from_user.id)
    if not ok:
        _edit_panel(call, f"⛔ {reason}", _nav_markup(back="pnl_users_0"))
        return
    try:
        ps.ban_uid(target, "", call.from_user.id)
    except Exception:
        log.error("panel ban failed", exc_info=True)
        _edit_panel(call, "❌ DB error while banning.", _nav_markup(back="pnl_users_0"))
        return
    ps.audit(call.from_user.id, "ban", str(target), "ok")
    _edit_panel(call, f"🚫 `{target}` banned.", _nav_markup(back="pnl_users_0"))


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_unban_")
    and not c.data.startswith("pnl_unban_ok_")
)
@panel_guard(owner_only=False)
def handle_panel_unban(call):
    try:
        target = int(call.data.rsplit("_", 1)[1])
        if target <= 0:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    rows = [(_btn("✅ Confirm unban", f"pnl_unban_ok_{target}"),)]
    _edit_panel(
        call,
        f"✅ **Confirm**\n\nUnban `{target}`? Paused schedules resume.",
        _nav_markup(rows, back="pnl_users_0"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_unban_ok_"))
@panel_guard(owner_only=False)
def handle_panel_unban_ok(call):
    try:
        target = int(call.data.rsplit("_", 1)[1])
        if target <= 0:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    try:
        removed = ps.unban_uid(target)
    except Exception:
        log.error("panel unban failed", exc_info=True)
        _edit_panel(call, "❌ DB error while unbanning.", _nav_markup(back="pnl_users_0"))
        return
    ps.audit(call.from_user.id, "unban", str(target), "ok" if removed else "not-found")
    _edit_panel(
        call,
        f"✅ `{target}` unbanned." if removed else f"ℹ️ `{target}` was not banned.",
        _nav_markup(back="pnl_users_0"),
    )


# =========================
# Stage 2B: Features / Settings / per-user limits (any admin; mutating
# actions audited with old -> new). Admins/owner are EXEMPT from toggles.
# =========================
def _feature_rows():
    rows = []
    pair = []
    for name in ps.FEATURES:
        try:
            on = ps.feature_enabled(name)
        except Exception:
            on = True
        pair.append(_btn(f"{'🟢' if on else '🔴'} {name}", f"pnl_feat_{name}"))
        if len(pair) == 2:
            rows.append(tuple(pair))
            pair = []
    if pair:
        rows.append(tuple(pair))
    return rows


@bot.callback_query_handler(func=lambda c: c.data == "pnl_features")
@panel_guard(owner_only=False)
def handle_panel_features(call):
    _edit_panel(
        call,
        "🎛 **Features** (global; absent = ON)\n\n"
        "Tap to flip. Admins and the owner are EXEMPT from toggles.\n"
        "Ambient triggers go silent in groups / rate-limited notice in DM.",
        _nav_markup(_feature_rows()),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_feat_"))
@panel_guard(owner_only=False)
def handle_panel_feature_flip(call):
    name = call.data[len("pnl_feat_"):]
    if name not in ps.FEATURES:
        try:
            bot.answer_callback_query(call.id, "❌ Unknown feature.")
        except Exception:
            pass
        return
    try:
        old = ps.feature_enabled(name)
    except Exception:
        old = True
    try:
        ps.set_feature(name, not old)
    except Exception:
        log.error("panel feature flip failed", exc_info=True)
        _edit_panel(call, "❌ DB error.", _nav_markup(back="pnl_features"))
        return
    ps.audit(call.from_user.id, "feature", name, f"{old} -> {not old}")
    handle_panel_features(call)


def _settings_rows():
    rows = []
    for key, (label, default, _mn, _mx) in ps.SETTINGS_SPEC.items():
        try:
            cur = ps.setting_value(key)
        except Exception:
            cur = default
        rows.append((_btn(f"{label}: {cur} (def {default})", f"pnl_set_{len(rows)}"),))
    return rows


_SETTINGS_KEYS = list(ps.SETTINGS_SPEC.keys())


@bot.callback_query_handler(func=lambda c: c.data == "pnl_settings")
@panel_guard(owner_only=False)
def handle_panel_settings(call):
    rows = _settings_rows()
    rows.append((_btn("👤 Per-user limits", "pnl_limits"),))
    _edit_panel(
        call,
        "⚙ **Settings** (live; out-of-range stored values fall back to default)\n\n"
        "Tap a row to change it. Changes are audited old → new.",
        _nav_markup(rows),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_set_"))
@panel_guard(owner_only=False)
def handle_panel_setting_detail(call):
    try:
        idx = int(call.data[len("pnl_set_"):])
        key = _SETTINGS_KEYS[idx]
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    label, default, minimum, maximum = ps.SETTINGS_SPEC[key]
    try:
        cur = ps.setting_value(key)
    except Exception:
        cur = default
    _panel_input_sessions.set(call.from_user.id, {"state": PANEL_SETTING_VALUE, "key": key})
    rows = [
        (_btn("↩ Reset to default", f"pnl_setdef_{idx}"),),
        (_btn("❌ Cancel", "pnl_cancel"),),
    ]
    _edit_panel(
        call,
        f"⚙ **{escape_markdown(label)}**\n\n"
        f"Current: `{cur}`\nDefault: `{default}`\nAllowed: `{minimum}–{maximum}`\n\n"
        "Send a new number, or reset. A /command aborts.",
        _nav_markup(rows, back="pnl_settings"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_setdef_"))
@panel_guard(owner_only=False)
def handle_panel_setting_reset(call):
    try:
        idx = int(call.data[len("pnl_setdef_"):])
        key = _SETTINGS_KEYS[idx]
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    label, default, _mn, _mx = ps.SETTINGS_SPEC[key]
    try:
        old = ps.setting_value(key)
    except Exception:
        old = default
    try:
        ps.write_setting(key, str(default))
    except Exception:
        log.error("panel setting reset failed", exc_info=True)
        _edit_panel(call, "❌ DB error.", _nav_markup(back="pnl_settings"))
        return
    ps.audit(call.from_user.id, "setting", key, f"{old} -> {default}")
    _edit_panel(call, f"✅ `{escape_markdown(key)}` reset to `{default}`.", _nav_markup(back="pnl_settings"))


def _handle_setting_value_text(message, key):
    raw = (message.text or "").strip()
    if key not in ps.SETTINGS_SPEC:
        _safe_reply_to(bot, message, "❌ Unknown setting. Send /admin to reopen the panel.")
        return
    label, default, minimum, maximum = ps.SETTINGS_SPEC[key]
    ok, value = ps.validate_setting_value(key, raw)
    if not ok:
        _panel_input_sessions.set(message.from_user.id, {"state": PANEL_SETTING_VALUE, "key": key})
        _safe_reply_to(bot, message,
                       f"❌ {escape_markdown(str(value))}. Allowed `{minimum}–{maximum}`, "
                       "default `{default}`. Try again or /cancel.".format(default=default))
        return
    try:
        old = ps.setting_value(key)
    except Exception:
        old = default
    try:
        ps.write_setting(key, str(value))
    except Exception:
        log.error("panel setting write failed", exc_info=True)
        _safe_reply_to(bot, message, "❌ DB error while saving.")
        return
    ps.audit(message.from_user.id, "setting", key, f"{old} -> {value}")
    _safe_reply_to(bot, message,
                   f"✅ `{escape_markdown(key)}`: `{old}` → `{value}`. "
                   "Send /admin to reopen the panel.")


# ---------- Per-user rate overrides (one cached query, numeric payloads) ----------
def _limit_cat_name(idx):
    try:
        return ps.LIMIT_CATEGORIES[int(idx)]
    except (ValueError, IndexError):
        return None


@bot.callback_query_handler(func=lambda c: c.data == "pnl_limits")
@panel_guard(owner_only=False)
def handle_panel_limits(call):
    try:
        overrides = ps.get_limit_overrides()
    except Exception:
        overrides = {}
    lines = ["👤 **Per-user rate overrides**\n"]
    rows = []
    for (uid, cat), per_min in sorted(overrides.items()):
        try:
            idx = ps.LIMIT_CATEGORIES.index(cat)
        except ValueError:
            continue
        lines.append(f"• `{uid}` / {cat}: `{per_min}/min`")
        rows.append((_btn(f"❌ {uid}/{cat}", f"pnl_limdel_{uid}_{idx}"),))
    if not rows:
        lines.append("None set. Global per-category budgets apply.")
    rows.append((_btn("➕ Add override", "pnl_limadd"),))
    _edit_panel(call, "\n".join(lines), _nav_markup(rows, back="pnl_settings"))


@bot.callback_query_handler(func=lambda c: c.data == "pnl_limadd")
@panel_guard(owner_only=False)
def handle_panel_limit_add(call):
    _panel_input_sessions.set(call.from_user.id, {"state": PANEL_LIMIT_ADD})
    _edit_panel(
        call,
        "➕ **Add override**\n\nSend one line: `uid category per-min`\n"
        f"Categories: {', '.join(ps.LIMIT_CATEGORIES)}\n"
        "Example: `123456 chat 20` (per-min 1–60). A /command aborts.",
        _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)], back="pnl_limits"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_limdel_"))
@panel_guard(owner_only=False)
def handle_panel_limit_del(call):
    try:
        _p, uid_s, idx_s = call.data.rsplit("_", 2)
        uid = int(uid_s)
        cat = _limit_cat_name(idx_s)
        if uid <= 0 or cat is None:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    try:
        ps.delete_setting(f"limit.{uid}.{cat}")
        ps.invalidate_limit_snapshot()
        from limits import rate_limiter

        rate_limiter.clear_user_override(cat, uid)
    except Exception:
        log.error("panel limit delete failed", exc_info=True)
    ps.audit(call.from_user.id, "limit_override_remove", f"{uid}/{cat}", "ok")
    handle_panel_limits(call)


def _handle_limit_add_text(message):
    parts = (message.text or "").strip().split()
    if len(parts) != 3:
        _panel_input_sessions.set(message.from_user.id, {"state": PANEL_LIMIT_ADD})
        _safe_reply_to(bot, message, "❌ Format: `uid category per-min` (e.g. `123456 chat 20`). Try again or /cancel.")
        return
    uid_s, cat, per_s = parts
    try:
        uid = int(uid_s)
        per_min = int(per_s)
    except ValueError:
        uid, per_min = -1, -1
    if uid <= 0 or cat not in ps.LIMIT_CATEGORIES or not (1 <= per_min <= 60):
        _panel_input_sessions.set(message.from_user.id, {"state": PANEL_LIMIT_ADD})
        _safe_reply_to(bot, message,
                       f"❌ Invalid. uid positive int, category one of {', '.join(ps.LIMIT_CATEGORIES)}, "
                       "per-min 1–60. Try again or /cancel.")
        return
    if is_admin(uid):
        _safe_reply_to(bot, message, "⛔ Admins are exempt from limits — override refused.")
        return
    try:
        old = ps.get_limit_overrides().get((uid, cat), "none")
        ps.write_setting(f"limit.{uid}.{cat}", str(per_min))
    except Exception:
        log.error("panel limit add failed", exc_info=True)
        _safe_reply_to(bot, message, "❌ DB error while saving.")
        return
    ps.audit(message.from_user.id, "limit_override", f"{uid}/{cat}", f"{old} -> {per_min}")
    _safe_reply_to(bot, message, f"✅ `{uid}` / {cat}: `{per_min}/min`. Send /admin to reopen the panel.")


# ---------- Per-group debate toggle (any admin; digest toggle deferred) ----------
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_gdebate_"))
@panel_guard(owner_only=False)
def handle_panel_group_debate(call):
    try:
        cid = int(call.data[len("pnl_gdebate_"):])
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    key = f"chat:{cid}:debate"
    try:
        old = ps.get_setting(key, "1") != "0"
    except Exception:
        old = True
    try:
        ps.set_chat_feature(cid, "debate", not old)
    except Exception:
        log.error("panel group debate flip failed", exc_info=True)
        _edit_panel(call, "❌ DB error.", _nav_markup(back="pnl_groups_0"))
        return
    ps.audit(call.from_user.id, "group_debate", str(cid), f"{old} -> {not old}")
    handle_panel_group_show(call, cid)


# =========================
# Stage 2C: Reset user data (OWNER ONLY, confirm-gated). Counts shown first;
# bans/audit/admin rows untouched; group history/summaries untouched; group
# tasks kept (counted); banned users MAY be reset (ban kept).
# =========================
_RESET_COUNT_LABELS = [
    ("messages", "Messages"), ("summaries", "Summaries"),
    ("sessions", "Sessions"), ("active_session", "Active pointer"),
    ("facts", "Facts"), ("behavior", "Behavior rows"),
    ("chat_log", "Chat-log rows"), ("members", "Member traces"),
    ("known_dm", "DM registration"), ("bot_media", "Media markers"),
    ("todos_private", "Private todos"), ("reminders_private", "Private reminders"),
    ("todos_unassign", "Todo unassigns"), ("reminders_unassign", "Reminder unassigns"),
]


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("pnl_reset_")
    and not c.data.startswith("pnl_reset_ok_")
)
@panel_guard(owner_only=True)
def handle_panel_reset(call):
    try:
        target = int(call.data[len("pnl_reset_"):])
        if target <= 0:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    ok, reason = _target_allowed(target, call.from_user.id)
    if not ok:
        _edit_panel(call, f"⛔ {reason}", _nav_markup(back="pnl_users_0"))
        return
    try:
        counts = repo.count_user_data(target)
    except Exception:
        log.error("panel reset counts failed", exc_info=True)
        _edit_panel(call, "❌ DB error while counting.", _nav_markup(back="pnl_users_0"))
        return
    lines = [f"🗑 **Confirm reset of `{target}`?**\n", "Will delete:"]
    for key, label in _RESET_COUNT_LABELS:
        lines.append(f"• {label}: `{counts.get(key, 0)}`")
    lines += [
        "",
        "NOT touched:",
        "• Ban rows, audit log, admin rows",
        "• Group conversation history and group summaries mentioning them",
        f"• `{counts.get('todos_group_kept', 0)}` group todos + "
        f"`{counts.get('reminders_group_kept', 0)}` group reminders they created (kept)",
        "• In-memory wizard sessions are cleared too",
        "• Their DM registration is removed (they vanish from the Users list "
        "and from “all users” broadcasts until they write to the bot again)",
        "• Per-user limit overrides are kept",
    ]
    rows = [(_btn("✅ Confirm reset", f"pnl_reset_ok_{target}"),)]
    _edit_panel(call, "\n".join(lines), _nav_markup(rows, back="pnl_users_0"))


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_reset_ok_"))
@panel_guard(owner_only=True)
def handle_panel_reset_ok(call):
    try:
        target = int(call.data[len("pnl_reset_ok_"):])
        if target <= 0:
            raise ValueError
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    ok, reason = _target_allowed(target, call.from_user.id)
    if not ok:
        _edit_panel(call, f"⛔ {reason}", _nav_markup(back="pnl_users_0"))
        return
    try:
        counts = repo.reset_user_data(call.from_user.id, target)
    except Exception:
        log.error("panel reset failed for %s", target, exc_info=True)
        _edit_panel(call, "❌ Reset failed and was rolled back. See logs.",
                    _nav_markup(back="pnl_users_0"))
        return
    ps.note_reset(target)  # background jobs queued before this instant are dropped
    try:
        from sessions import clear_user_state
        from services.media import clear_user_yt_sessions
        from services.streaming import drop_user_requests

        clear_user_state(target)
        clear_user_yt_sessions(target)
        drop_user_requests(target)
    except Exception:
        log.warning("panel reset in-memory clear failed for %s", target, exc_info=True)
    deleted = sum(v for k, v in counts.items() if not k.endswith("_group_kept"))
    _edit_panel(
        call,
        f"🗑 `{target}` reset: `{deleted}` rows deleted, "
        f"`{counts.get('todos_group_kept', 0)}` group todos + "
        f"`{counts.get('reminders_group_kept', 0)}` group reminders kept. "
        "Ban/audit/admin rows untouched.",
        _nav_markup(back="pnl_users_0"),
    )


# =========================
# Stage 3A: Broadcast (OWNER ONLY, every step). Legacy reply-keyboard flow
# is fully removed; this is the only broadcast path.
# Flow: target -> content -> draft+preview (recipients frozen here) ->
# confirm (single-use token) -> background runner thread.
# =========================
import re as _re
import uuid as _uuid

from db import fetch_all_group_ids, fetch_all_user_ids

BROADCAST_DRAFT_TTL = 600  # seconds
BROADCAST_SENDS_PER_SECOND = 20
BROADCAST_FLOOD_RETRIES = 3
BROADCAST_FLOOD_SLEEP_CAP = 60
# Circuit breaker: N consecutive sends failing with the SAME non-unreachable
# error abort the run (e.g. the content message was deleted). Unreachable
# recipients neither increment nor reset the streak.
BROADCAST_ABORT_STREAK = 10

_panel_broadcast_drafts = TTLSessionStore(BROADCAST_DRAFT_TTL)  # token -> draft

_broadcast_lock = threading.Lock()  # one running broadcast at a time
_broadcast_run = {}  # {"id": int, "event": threading.Event} under _broadcast_run_lock
_broadcast_run_lock = threading.Lock()

_BOT_TOKEN_RE = _re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
_UNREACHABLE_RE = _re.compile(
    r"bot can'?t initiate|chat not found|user is deactivated|deactivated|"
    r"blocked|bot was blocked|bot was kicked|group chat was deleted|"
    r"chat_id is empty|PEER_ID_INVALID|USER_DEACTIVATED",
    _re.IGNORECASE,
)


def _redact(text):
    """Strip tokens/secrets from error strings (max 200 chars)."""
    s = str(text or "")
    s = _BOT_TOKEN_RE.sub("BOT_TOKEN_REDACTED", s)
    try:
        import config as _cfg

        for name in dir(_cfg):
            if any(k in name for k in ("TOKEN", "KEY", "PASSWORD", "HASH", "SECRET")):
                val = getattr(_cfg, name, "")
                if isinstance(val, str) and len(val) >= 8:
                    s = s.replace(val, "***")
    except Exception:
        pass
    return s[:200]


def _classify_broadcast_error(exc):
    """unreachable (403 / bot-can't-initiate / not-found / deactivated —
    never retried) vs failed (everything else)."""
    try:
        code = getattr(exc, "error_code", None)
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if code == 403:
        return "unreachable"
    if _UNREACHABLE_RE.search(str(exc)):
        return "unreachable"
    return "failed"


def _retry_after_seconds(exc):
    try:
        params = (getattr(exc, "result_json", None) or {}).get("parameters", {})
        return max(0, int(params.get("retry_after", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _broadcast_recipients(kind, one_chat=None, owner_id=None):
    """Draft-time recipient snapshot from known_chats. Returns
    (recipients, skipped_banned, label). Banned users excluded; owner's own
    chat excluded from all-users. NOTE: known_chats has no per-bot column —
    both bots' middleware write to it, so users seen ONLY via the Light bot
    can appear here; sending is via the main bot only, and unreachable
    recipients are counted, never retried."""
    from handlers.shared import get_cached_ban_ids

    try:
        banned = set(get_cached_ban_ids())
    except Exception:
        banned = set()
    try:
        owner = int(owner_id) if owner_id else None
    except (TypeError, ValueError):
        owner = None
    skipped = 0
    if kind == "users":
        ids = fetch_all_user_ids()
        out = [i for i in ids if i not in banned and i != owner]
        skipped = sum(1 for i in ids if i in banned)
        return out, skipped, "all users"
    if kind == "groups":
        return list(fetch_all_group_ids()), 0, "all groups"
    if kind == "both":
        groups = list(fetch_all_group_ids())
        users = [i for i in fetch_all_user_ids() if i not in banned and i != owner]
        skipped = sum(1 for i in fetch_all_user_ids() if i in banned)
        seen = set()
        out = []
        for cid in groups + users:
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out, skipped, "groups + users"
    # one explicit chat id
    cid = int(one_chat)
    if cid in banned:
        return [], 1, f"chat {cid}"
    return [cid], 0, f"chat {cid}"


@bot.callback_query_handler(func=lambda c: c.data == "pnl_bcast_menu")
@panel_guard(owner_only=True)
def handle_panel_broadcast_menu(call):
    try:
        recent = repo.list_broadcasts(limit=10)
    except Exception:
        recent = []
    lines = ["📢 **Broadcast** (owner only)\n", "Choose a target:\n"]
    if recent:
        lines.append("Recent runs:")
        for bid, _adm, target, _ct, total, sent, unreach, failed, skipped, status, _st, _fin in recent:
            lines.append(f"• `#{bid}` {escape_markdown(target)}: "
                         f"{sent}/{total} `{escape_markdown(status)}`")
    else:
        lines.append("No previous runs.")
    rows = [
        (_btn("👥 All users", "pnl_bc_users"), _btn("💬 All groups", "pnl_bc_groups")),
        (_btn("🔄 Both", "pnl_bc_both"), _btn("🎯 One chat", "pnl_bc_one")),
    ]
    _edit_panel(call, "\n".join(lines), _nav_markup(rows))


@bot.callback_query_handler(
    func=lambda c: c.data in ("pnl_bc_users", "pnl_bc_groups", "pnl_bc_both"))
@panel_guard(owner_only=True)
def handle_panel_broadcast_target(call):
    kind = {"pnl_bc_users": "users", "pnl_bc_groups": "groups",
            "pnl_bc_both": "both"}[call.data]
    _panel_input_sessions.set(call.from_user.id,
                              {"state": PANEL_BROADCAST_CONTENT, "kind": kind})
    _edit_panel(
        call,
        f"📢 **Broadcast → {kind}**\n\nSend the content now (text or any media). "
        "Nothing is sent until you Confirm the preview. A /command aborts.",
        _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)], back="pnl_bcast_menu"),
    )


@bot.callback_query_handler(func=lambda c: c.data == "pnl_bc_one")
@panel_guard(owner_only=True)
def handle_panel_broadcast_one(call):
    _panel_input_sessions.set(call.from_user.id, {"state": PANEL_BROADCAST_CHAT})
    _edit_panel(
        call,
        "🎯 **One chat**\n\nSend the numeric chat ID (user `123…`, group `-100…`).",
        _nav_markup([(_btn("❌ Cancel", "pnl_cancel"),)], back="pnl_bcast_menu"),
    )


def _handle_broadcast_chat_text(message):
    raw = (message.text or "").strip()
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        _panel_input_sessions.set(message.from_user.id, {"state": PANEL_BROADCAST_CHAT})
        _safe_reply_to(bot, message, "❌ Not an integer. Send a chat ID or /cancel.")
        return
    try:
        known = repo.get_known_chat(str(cid))
    except Exception:
        known = None
    _panel_input_sessions.set(
        message.from_user.id,
        {"state": PANEL_BROADCAST_CONTENT, "kind": "one", "one_chat": cid})
    note = ""
    if known:
        _cid, typ, username, title, _last = known
        name = (title or "").strip() or (f"@{username}" if username else "")
        note = f"Known {_txt(typ)} {_txt(name, 60)}. "
    else:
        note = "⚠️ Not in known chats — unverified manual target. "
    bot.send_message(
        message.chat.id,
        f"🎯 Target: `{cid}`. {note}Now send the content (text or any media).",
        parse_mode="Markdown",
    )


def _handle_broadcast_content(message, session):
    kind = session.get("kind", "users")
    one_chat = session.get("one_chat")
    owner_id = message.from_user.id
    try:
        recipients, skipped, label = _broadcast_recipients(kind, one_chat, owner_id)
    except Exception:
        log.error("broadcast recipient computation failed", exc_info=True)
        _safe_reply_to(bot, message, "❌ Could not compute recipients.")
        return
    if kind == "one" and not recipients:
        _safe_reply_to(bot, message, "⛔ That chat is banned — nothing to send. Send /admin to reopen.")
        return
    if not recipients:
        _safe_reply_to(bot, message, "⚠️ No recipients for this target. Send /admin to reopen.")
        return
    token = _uuid.uuid4().hex[:12]
    _panel_broadcast_drafts.set(token, {
        "owner": owner_id,
        "kind": kind,
        "label": label,
        "recipients": recipients,
        "skipped_banned": skipped,
        "source_chat": message.chat.id,
        "source_msg": message.message_id,
        "content_type": message.content_type,
    })
    try:
        bot.copy_message(chat_id=message.chat.id, from_chat_id=message.chat.id,
                         message_id=message.message_id)
    except Exception:
        log.warning("broadcast preview copy failed", exc_info=True)
    est = max(1, int(len(recipients) / BROADCAST_SENDS_PER_SECOND) + 1)
    group_note = ("Group sends are whole-chat." if kind in ("groups", "both") else "")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Confirm", callback_data=f"pnl_bc_ok_{token}"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="pnl_cancel"),
    )
    bot.send_message(
        message.chat.id,
        "📢 **Preview — nothing sent yet**\n\n"
        f"Target: `{escape_markdown(label)}` {escape_markdown(group_note)}\n"
        f"Recipients: `{len(recipients)}`\n"
        f"🚫 Banned skipped: `{skipped}`\n"
        f"⏱ Estimated: `~{est}s` at ~{BROADCAST_SENDS_PER_SECOND}/s\n\n"
        "⚠️ Recipients include chats seen by either bot; sending is via the "
        "main bot only — some may be unreachable (counted, not retried).",
        parse_mode="Markdown",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pnl_bc_ok_"))
@panel_guard(owner_only=True)
def handle_panel_broadcast_confirm(call):
    token = call.data[len("pnl_bc_ok_"):]
    draft = _panel_broadcast_drafts.pop(token)  # atomic single-use
    if not draft or draft.get("owner") != call.from_user.id:
        try:
            bot.answer_callback_query(call.id, "❌ Expired or already used.", show_alert=True)
        except Exception:
            pass
        return
    if not _broadcast_lock.acquire(blocking=False):
        _panel_broadcast_drafts.set(token, draft)  # give the draft back
        try:
            bot.answer_callback_query(call.id, "⛔ Another broadcast is running.", show_alert=True)
        except Exception:
            pass
        return
    try:
        broadcast_id = repo.create_broadcast(
            call.from_user.id, draft["label"], draft["content_type"],
            len(draft["recipients"]), draft["skipped_banned"])
    except Exception:
        log.error("broadcast row create failed", exc_info=True)
        _broadcast_lock.release()
        try:
            bot.answer_callback_query(call.id, "❌ DB error.")
        except Exception:
            pass
        return
    ps.audit(call.from_user.id, "broadcast_start", draft["label"],
             f"total={len(draft['recipients'])}")
    cancel = threading.Event()
    with _broadcast_run_lock:
        _broadcast_run.clear()
        _broadcast_run.update({"id": broadcast_id, "event": cancel})
    try:
        progress = bot.send_message(call.message.chat.id, "📡 Starting…")
    except Exception:
        # Without the progress message the worker would run blind — abort
        # BEFORE any recipient send, restore the draft for a retry, close the
        # row, and release the lock (it used to stay held until restart).
        log.error("broadcast progress message failed; aborting before any send",
                  exc_info=True)
        with _broadcast_run_lock:
            if _broadcast_run.get("id") == broadcast_id:
                _broadcast_run.clear()
        try:
            repo.finish_broadcast(broadcast_id, "failed")
        except Exception:
            pass
        ps.audit(call.from_user.id, "broadcast_start", draft["label"],
                 "progress-message-failed")
        _panel_broadcast_drafts.set(token, draft)
        _broadcast_lock.release()
        try:
            bot.answer_callback_query(
                call.id, "❌ Starting the broadcast failed — draft restored, try again.")
        except Exception:
            pass
        return
    stop_markup = types.InlineKeyboardMarkup()
    stop_markup.add(types.InlineKeyboardButton("🛑 Stop", callback_data="pnl_bc_stop"))
    worker = threading.Thread(
        target=_broadcast_worker,
        args=(broadcast_id, draft, cancel, call.message.chat.id, progress.message_id),
        daemon=True,
        name="panel-broadcast-runner",
    )
    worker.start()
    try:
        bot.answer_callback_query(call.id, "📡 Broadcast started.")
        bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                      message_id=progress.message_id,
                                      reply_markup=stop_markup)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "pnl_bc_stop")
@panel_guard(owner_only=True)
def handle_panel_broadcast_stop(call):
    with _broadcast_run_lock:
        event = _broadcast_run.get("event")
    if event is None:
        try:
            bot.answer_callback_query(call.id, "ℹ️ No broadcast running.")
        except Exception:
            pass
        return
    event.set()
    try:
        bot.answer_callback_query(call.id, "🛑 Stopping…")
    except Exception:
        pass


def _broadcast_worker(broadcast_id, draft, cancel_event, progress_chat, progress_msg):
    """Background sender (never the polling thread). Pacing ≤ ~20/s, flood
    retries bounded, progress edits + DB updates every ~10 sends."""
    recipients = draft["recipients"]
    total = len(recipients)
    sent = unreachable = failed = 0
    errors = []
    streak_error, streak, abort_error = None, 0, None
    try:
        from wiring import shutdown_event
    except Exception:
        shutdown_event = None

    def _progress(final=False):
        try:
            repo.update_broadcast_progress(broadcast_id, sent, unreachable, failed)
        except Exception:
            log.warning("broadcast progress DB update failed", exc_info=True)
        stop_markup = types.InlineKeyboardMarkup()
        if not final:
            stop_markup.add(types.InlineKeyboardButton("🛑 Stop", callback_data="pnl_bc_stop"))
        try:
            bot.edit_message_text(
                f"📡 Broadcasting… `{sent + unreachable + failed}/{total}`"
                f" (✅{sent} 🚫{unreachable} ❌{failed})",
                chat_id=progress_chat, message_id=progress_msg,
                parse_mode="Markdown", reply_markup=stop_markup,
            )
        except apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e).lower():
                log.debug("broadcast progress edit failed", exc_info=True)
        except Exception:
            log.debug("broadcast progress edit failed", exc_info=True)

    status = "done"
    try:
        for i, cid in enumerate(recipients):
            if cancel_event.is_set():
                status = "stopped"
                break
            if shutdown_event is not None and shutdown_event.is_set():
                status = "interrupted"
                break
            outcome = _send_broadcast_one(cid, draft)
            if outcome == "sent":
                sent += 1
            elif outcome == "unreachable":
                unreachable += 1
            else:
                failed += 1
                if len(errors) < 5:
                    errors.append(outcome)
                if outcome == streak_error:
                    streak += 1
                else:
                    streak_error, streak = outcome, 1
                if streak >= BROADCAST_ABORT_STREAK:
                    status = "aborted"
                    abort_error = outcome
                    break
            if (i + 1) % 10 == 0:
                _progress()
            time.sleep(1.0 / BROADCAST_SENDS_PER_SECOND)
        _progress(final=True)
    finally:
        with _broadcast_run_lock:
            if _broadcast_run.get("id") == broadcast_id:
                _broadcast_run.clear()
        try:
            repo.finish_broadcast(broadcast_id, status)
        except Exception:
            log.warning("broadcast finish DB update failed", exc_info=True)
        ps.audit(draft.get("owner", 0) or 0, "broadcast_end", draft.get("label", ""),
                 f"sent={sent} unreachable={unreachable} failed={failed} "
                 f"skipped={draft.get('skipped_banned', 0)} "
                 f"not_attempted={total - sent - unreachable - failed} status={status}"
                 + (f" abort={abort_error}" if abort_error else ""))
        try:
            stop_markup = types.InlineKeyboardMarkup()
            bot.edit_message_text(
                "📢 **Broadcast finished**\n\n"
                f"✅ Sent: `{sent}`\n🚫 Unreachable: `{unreachable}`\n"
                f"❌ Failed: `{failed}`\n"
                f"🚫 Skipped (banned): `{draft.get('skipped_banned', 0)}`\n"
                f"⏭ Not attempted: `{total - sent - unreachable - failed}`\n"
                f"Status: `{status}`"
                + (f"\n⚠ Aborted after {BROADCAST_ABORT_STREAK} identical failures:\n`{abort_error}`"
                   if abort_error else "")
                + ("".join(f"\n`{_redact(e)}`" for e in errors[:5]) if errors else ""),
                chat_id=progress_chat, message_id=progress_msg,
                parse_mode="Markdown", reply_markup=stop_markup,
            )
        except Exception:
            log.debug("broadcast final edit failed", exc_info=True)
        try:
            _broadcast_lock.release()
        except RuntimeError:
            pass  # direct invocation without acquiring first (tests only)


def _send_broadcast_one(cid, draft):
    """Send to one recipient. Returns 'sent', 'unreachable', or a redacted
    error string (failed). Flood-wait is honored with bounded retries."""
    for attempt in range(BROADCAST_FLOOD_RETRIES + 1):
        try:
            bot.copy_message(chat_id=cid, from_chat_id=draft["source_chat"],
                             message_id=draft["source_msg"])
            return "sent"
        except apihelper.ApiTelegramException as e:
            kind = _classify_broadcast_error(e)
            if kind == "unreachable":
                return "unreachable"
            wait = _retry_after_seconds(e)
            if wait > 0 and attempt < BROADCAST_FLOOD_RETRIES:
                time.sleep(min(wait, BROADCAST_FLOOD_SLEEP_CAP))
                continue
            return _redact(f"Chat {cid}: {e}")
        except Exception as e:
            return _redact(f"Chat {cid}: {e}")


# =========================
# Stage 3B: Jobs + Statistics
# =========================
from services import panel_jobs as pj

_JOB_ORDER = ["scheduler", "backup", "profiler", "relay"]
_JOB_LABELS = {"scheduler": "Reminders/todos", "backup": "Daily backup",
               "profiler": "Behavior profiler", "relay": "IG relay poll"}


def _paused_warning_line():
    """One-liner for home/Jobs when anything is paused (backup extra)."""
    try:
        paused = [(n, lbl) for n, _t, _a, p, lbl, _r, _f, _age, _e
                  in pj.job_snapshot() if p]
    except Exception:
        return ""
    if not paused:
        return ""
    parts = ", ".join(f"{n} ({lbl})" for n, lbl in paused)
    warn = f"⏸ Paused: {parts}"
    if any(n == "backup" for n, _l in paused):
        warn += " — ⚠️ backups are NOT being taken"
    return warn


@bot.callback_query_handler(func=lambda c: c.data == "pnl_jobs")
@panel_guard(owner_only=False)
def handle_panel_jobs(call):
    is_owner_view = is_owner(call.from_user.id)
    lines = ["⏰ **Jobs**\n"]
    warn = _paused_warning_line()
    if warn:
        lines.append(warn + "\n")
    rows = []
    try:
        snap = pj.job_snapshot()
    except Exception:
        snap = []
    for name, thread, alive, paused, label, runs, fails, age, err in snap:
        alive_s = "🟢" if alive else ("⚪" if alive is None else "🔴")
        state = f"⏸ {label}" if paused else "running"
        age_s = (f", ok {int(age // 3600)}h ago" if age is not None and age < 86400 * 30
                 else "")
        lines.append(f"{alive_s} **{escape_markdown(name)}** — {state}"
                     f" (runs `{runs}`, fails `{fails}`{age_s})")
        if err:
            lines.append(f"  last error: `{escape_markdown(err)[:160]}`")
        if name in _JOB_ORDER and is_owner_view:
            try:
                idx = _JOB_ORDER.index(name)
            except ValueError:
                continue
            if paused:
                rows.append((_btn(f"▶ Resume {name}", f"pnl_jr_{idx}"),))
            else:
                row = [_btn(f"⏸ {name} 1h", f"pnl_jp_{idx}_1"),
                       _btn(f"{name} 6h", f"pnl_jp_{idx}_6"),
                       _btn(f"{name} 24h", f"pnl_jp_{idx}_24")]
                rows.append(tuple(row))
                if name in ("relay", "profiler"):
                    rows.append((_btn(f"⏸ {name} indefinitely", f"pnl_jp_{idx}_0"),))
    if is_owner_view:
        rows.append((_btn("💾 Backup now", "pnl_jrun"),))
    _edit_panel(call, "\n".join(lines)[:4000], _nav_markup(rows))


@bot.callback_query_handler(
    func=lambda c: c.data and _re.match(r"^pnl_jp_\d+_(?:0|1|6|24)$", c.data or ""))
@panel_guard(owner_only=True)
def handle_panel_job_pause(call):
    try:
        _p, idx_s, hours_s = call.data.rsplit("_", 2)
        name = _JOB_ORDER[int(idx_s)]
        hours = int(hours_s)
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    try:
        old_paused, old_label = pj.job_paused_until(name)
        old = old_label if old_paused else "running"
    except Exception:
        old = "?"
    ok, msg = pj.job_pause(name, hours)
    if ok:
        ps.audit(call.from_user.id, "job_pause", name, f"{old} -> {msg}")
    _edit_panel(call, f"{'⏸' if ok else '❌'} `{escape_markdown(name)}`: {escape_markdown(msg)}",
                _nav_markup(back="pnl_jobs"))


@bot.callback_query_handler(
    func=lambda c: c.data and _re.match(r"^pnl_jr_\d+$", c.data or ""))
@panel_guard(owner_only=True)
def handle_panel_job_resume(call):
    try:
        name = _JOB_ORDER[int(call.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        try:
            bot.answer_callback_query(call.id, "❌ Bad button payload.")
        except Exception:
            pass
        return
    try:
        old_paused, old_label = pj.job_paused_until(name)
        old = old_label if old_paused else "running"
    except Exception:
        old = "?"
    ok, msg = pj.job_resume(name)
    if ok:
        ps.audit(call.from_user.id, "job_resume", name, f"{old} -> resumed")
    _edit_panel(call, f"{'▶' if ok else '❌'} `{escape_markdown(name)}`: {escape_markdown(msg)}",
                _nav_markup(back="pnl_jobs"))


@bot.callback_query_handler(func=lambda c: c.data == "pnl_jrun")
@panel_guard(owner_only=True)
def handle_panel_backup_run(call):
    rows = [(_btn("✅ Confirm backup now", "pnl_jrun_ok"),)]
    _edit_panel(
        call,
        "💾 **Confirm**\n\nSnapshot + send to the owner now. "
        "No retention purge, no VACUUM (scheduled-only steps).",
        _nav_markup(rows, back="pnl_jobs"),
    )


@bot.callback_query_handler(func=lambda c: c.data == "pnl_jrun_ok")
@panel_guard(owner_only=True)
def handle_panel_backup_run_ok(call):
    from services.backup import run_backup_now

    owner_id = call.from_user.id
    try:
        bot.answer_callback_query(call.id, "💾 Backup started.")
    except Exception:
        pass
    progress = bot.send_message(call.message.chat.id, "💾 Building snapshot…")

    def _run():
        ok, text = run_backup_now(owner_id)
        try:
            bot.edit_message_text(
                f"💾 Backup {'done' if ok else 'FAILED'}: {escape_markdown(text)}",
                chat_id=progress.chat.id, message_id=progress.message_id)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="panel-backup-runnnow").start()


# ---------- Statistics (any admin, view-only, 60s cache) ----------
_stats_cache = {"text": None, "exp": 0.0}
_stats_lock = threading.Lock()
STATS_TTL = 60.0


def _build_stats_text():
    import datetime as _dt

    lines = ["📈 **Statistics**\n"]
    try:
        uptime = str(_dt.timedelta(seconds=int(time.time() - _START_TIME)))
    except Exception:
        uptime = "n/a"
    lines.append(f"⏱ Uptime: `{uptime}` _(since last restart — counters reset on every deploy)_\n")
    try:
        lines.append(f"✅ Handled: `{METRICS.get('handled', 0)}`")
        lines.append(f"❌ LLM errors: `{METRICS.get('llm_errors', 0)}`")
        lines.append(f"🚫 Rate-limited: `{METRICS.get('ratelimited', 0)}`")
        lines.append(f"🗜 Summaries: `{METRICS.get('summaries', 0)}`")
        lines.append(f"🔎 Web searches: `{METRICS.get('web_searches', 0)}`")
        lines.append(f"📰 Digests: `{METRICS.get('digests_sent', 0)}`")
        lines.append(f"📥 Instagram: `{METRICS.get('instagram_downloads', 0)}`")
        lines.append(f"🎬 YouTube: `{METRICS.get('youtube_downloads', 0)}`")
        lines.append(f"🧠 Facts: `{METRICS.get('facts_learned', 0)}`")
        lines.append(f"💾 Backups: `{METRICS.get('backups_sent', 0)}`")
        lines.append(f"📋 TLDRs: `{METRICS.get('tldr_requests', 0)}`")
        lines.append(f"🔄 Poller restarts: `{METRICS.get('poller_restarts', 0)}`")
    except Exception:
        lines.append("counters unavailable")
    lines.append("")
    try:
        offset = f"+{int(IRAN_OFFSET_SECONDS)} seconds"
        rows = repo._connect().execute(
            "SELECT date(timestamp, ?) AS day, COUNT(*) FROM messages "
            "WHERE timestamp > datetime('now', '-14 days') AND role = 'user' "
            "AND chat_id NOT LIKE 'debate\\_%' ESCAPE '\\' "
            "AND chat_id NOT LIKE 'tldr\\_%' ESCAPE '\\' "
            "GROUP BY day ORDER BY day DESC",
            (offset,),
        ).fetchall()
        lines.append("user messages = role 'user', excluding debate_/tldr_ namespaces\n")
        peak = max((n for _d, n in rows), default=0)
        for day, n in rows:
            bar = "█" * (max(1, round(n / peak * 8)) if peak else 0)
            lines.append(f"`{day}` {bar} `{n}`")
        if not rows:
            lines.append("(no messages in the last 14 days)")
    except Exception:
        log.warning("stats per-day query failed", exc_info=True)
        lines.append("(per-day stats unavailable)")
    lines.append("")
    try:
        for days in (1, 7, 30):
            counts = dict(repo._connect().execute(
                "SELECT type, COUNT(*) FROM known_chats "
                "WHERE last_seen > datetime('now', ?) GROUP BY type",
                (f"-{days} days",)).fetchall())
            users = counts.get("private", 0)
            groups = counts.get("group", 0) + counts.get("supergroup", 0)
            lines.append(f"Active {days}d: 👤 `{users}` users, 💬 `{groups}` groups")
    except Exception:
        lines.append("(activity stats unavailable)")
    return "\n".join(lines)


@bot.callback_query_handler(func=lambda c: c.data == "pnl_stats")
@panel_guard(owner_only=False)
def handle_panel_stats(call):
    now = time.time()
    with _stats_lock:
        text, exp = _stats_cache["text"], _stats_cache["exp"]
        if text is not None and now < exp:
            _edit_panel(call, text, _nav_markup())
            return
    try:
        text = _build_stats_text()
    except Exception:
        log.error("stats build failed", exc_info=True)
        _edit_panel(call, "❌ Could not build statistics.", _nav_markup())
        return
    with _stats_lock:
        _stats_cache["text"], _stats_cache["exp"] = text, now + STATS_TTL
    _edit_panel(call, text, _nav_markup())
