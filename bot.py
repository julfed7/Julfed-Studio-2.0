import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from config import TELEGRAM_BOT_TOKEN
from build import (
    run_codex, run_codex_only,
    download_attachment_async,
    classify_attachment, read_text_prompt,
    pack_files_to_zip, find_files_by_pattern, find_files_in_dir,
    list_all_files,
    upload_file_to_project, upload_project_zip, pack_whole_project,
    load_queue, enqueue_task, pop_task, complete_task, clear_queue,
    set_loop_state, get_loop_state,
    set_direction, get_direction,
    add_to_history,
    git_commit_push, ensure_git_config,
    download_latest_apk, get_latest_release_tag, wait_for_new_release,
    get_usage_summary, get_codex_limits, write_credits_snapshot, reset_credit_period,
    trigger_build_workflow,
    PROJECT_PATH,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

user_states: dict = {}
loop_running: bool = False

GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    return f"{n // (1024 * 1024)} MB"


def _consume(user_id: int) -> dict:
    state = user_states.get(user_id, {})
    result = {
        "text": "",
        "images": state.get("pending_images", []),
        "videos": state.get("pending_videos", []),
        "files": state.get("pending_files", []),
        "zips": state.get("pending_zips", []),
    }
    if "pending_prompt" in state:
        result["text"] = read_text_prompt(state["pending_prompt"])
    user_states[user_id] = {}
    return result


def _peek(user_id: int) -> dict:
    state = user_states.get(user_id, {})
    return {
        "has_prompt": "pending_prompt" in state,
        "images": len(state.get("pending_images", [])),
        "videos": len(state.get("pending_videos", [])),
        "files": len(state.get("pending_files", [])),
        "zips": len(state.get("pending_zips", [])),
    }


def _media_for_codex(a: dict) -> list:
    return a["images"] + a["videos"]


def _attach_line(a: dict) -> str:
    parts = []
    if a["text"]:
        parts.append("📝")
    if a["images"]:
        parts.append(f"🖼{len(a['images'])}")
    if a["videos"]:
        parts.append(f"🎬{len(a['videos'])}")
    if a["zips"]:
        parts.append(f"📦{len(a['zips'])}")
    if a["files"]:
        parts.append(f"📎{len(a['files'])}")
    return " ".join(parts)


# ============ СТАРТ ============

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🎮 <b>Студия Cubism (Termux)</b>\n\n"
        "<b>Главное:</b>\n"
        "• <code>/direction &lt;цель&gt;</code> — задать цель\n"
        "• <code>/autopilot [N]</code> — студия: планировщик придумывает идеи под направление, разбивает на задачи, модели из <code>/team</code> их делают (N — макс. идей)\n"
        "• <code>/build_apk</code> — собрать APK\n"
        "• <code>/stop</code> — остановить\n\n"
        "<b>Ручные задачи:</b>\n"
        "• <code>/task &lt;промпт&gt;</code>\n"
        "• <code>/loop</code> / <code>/queue</code> / <code>/clear</code>\n\n"
        "<b>Codex:</b> <code>/luna &lt;промпт&gt;</code>\n\n"
        "<b>Файлы:</b>\n"
        "• <code>/get &lt;файл&gt;</code> / <code>/download_project</code>\n"
        "• <code>/upload_project</code> / <code>/upload_to &lt;путь&gt;</code>\n\n"
        "<b>📎 Вложения — во ВСЕХ командах:</b>\n"
        "• .txt .md .gd → промпт\n"
        "• фото / видео → Codex\n"
        "• .zip → замена проекта\n\n"
        "<b>Прочее:</b> <code>/model</code>, <code>/effort</code>, <code>/team</code>, <code>/credits</code>, <code>/limit5h</code>, <code>/credits_limit</code>, <code>/credits_reset</code>, <code>/status</code>, <code>/cancel</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_states[update.effective_user.id] = {}
    await update.message.reply_text("❌ Все вложения очищены.")


# ============ МОДЕЛЬ ============

DEFAULT_MODELS = ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"]
EFFORTS = ["low", "medium", "high", "xhigh", "ultra"]
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "ultra"]
EFFORT_ALLOWED = set(EFFORT_ORDER)


def _model_keyboard() -> InlineKeyboardMarkup:
    from config import CONFIG
    cur_m = CONFIG.get("model", "gpt-5.6-luna")
    cur_e = CONFIG.get("effort", "high")
    models = list(DEFAULT_MODELS)
    for m in CONFIG.get("models", []):
        if m not in models:
            models.append(m)
    if cur_m not in models:
        models.insert(0, cur_m)
    rows = [[InlineKeyboardButton(("✅ " if m == cur_m else "") + m, callback_data=f"model:{m}")]
            for m in models]
    ebtns = [InlineKeyboardButton(("✅ " if e == cur_e else "") + e, callback_data=f"effort:{e}")
             for e in EFFORTS]
    rows.append(ebtns[:3])
    rows.append(ebtns[3:])
    return InlineKeyboardMarkup(rows)


def _model_text() -> str:
    from config import CONFIG
    return (
        f"🤖 Модель: <code>{CONFIG.get('model', 'gpt-5.6-luna')}</code>\n"
        f"⚙️ Усилие: <code>{CONFIG.get('effort', 'high')}</code>\n\n"
        "Выбери кнопкой или напиши: <code>/model &lt;имя&gt; [low|medium|high|xhigh|ultra]</code>"
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config
    user_id = update.effective_user.id
    args = context.args or []
    a = _consume(user_id)
    if not args and a["text"]:
        args = a["text"].strip().split()

    if not args:
        await update.message.reply_text(
            _model_text(), parse_mode="HTML", reply_markup=_model_keyboard()
        )
        return

    CONFIG["model"] = args[0]
    if len(args) >= 2:
        CONFIG["effort"] = args[1]
    models = CONFIG.setdefault("models", list(DEFAULT_MODELS))
    if args[0] not in models:
        models.append(args[0])
    save_config(CONFIG)
    await update.message.reply_text(
        f"✅ <code>{CONFIG['model']}</code> / <code>{CONFIG.get('effort', 'high')}</code>",
        parse_mode="HTML",
    )


async def cb_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config
    q = update.callback_query
    kind, _, value = (q.data or "").partition(":")
    if kind == "model" and value:
        CONFIG["model"] = value
    elif kind == "effort" and value in EFFORTS:
        CONFIG["effort"] = value
    else:
        await q.answer()
        return
    save_config(CONFIG)
    await q.answer("Сохранено")
    try:
        await q.edit_message_text(
            _model_text(), parse_mode="HTML", reply_markup=_model_keyboard()
        )
    except Exception:
        pass


def _effort_keyboard() -> InlineKeyboardMarkup:
    from config import CONFIG
    cur = CONFIG.get("effort", "high")
    btns = [InlineKeyboardButton(("✅ " if e == cur else "") + e, callback_data=f"eff:{e}")
            for e in EFFORTS]
    return InlineKeyboardMarkup([btns[:3], btns[3:]])


def _effort_text() -> str:
    from config import CONFIG
    return (
        f"⚙️ Усилие рассуждения: <code>{CONFIG.get('effort', 'high')}</code>\n"
        "low: быстро и дёшево, high/xhigh/ultra: глубже, но дороже по токенам.\n"
        "Выбери кнопкой или: <code>/effort medium</code>"
    )


async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config
    args = context.args or []
    if not args:
        await update.message.reply_text(
            _effort_text(), parse_mode="HTML", reply_markup=_effort_keyboard()
        )
        return
    value = args[0].strip().lower()
    if value not in EFFORT_ALLOWED:
        await update.message.reply_text(
            "❌ Допустимо: " + ", ".join(EFFORT_ORDER)
        )
        return
    CONFIG["effort"] = value
    save_config(CONFIG)
    await update.message.reply_text(
        f"✅ Усилие: <code>{value}</code>", parse_mode="HTML"
    )


async def cb_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config
    q = update.callback_query
    _, _, value = (q.data or "").partition(":")
    if value not in EFFORT_ALLOWED:
        await q.answer()
        return
    CONFIG["effort"] = value
    save_config(CONFIG)
    await q.answer("Сохранено")
    try:
        await q.edit_message_text(
            _effort_text(), parse_mode="HTML", reply_markup=_effort_keyboard()
        )
    except Exception:
        pass


# ============ КРЕДИТЫ / ЛИМИТЫ ============

def _fmt_reset(rl: dict) -> str:
    secs = rl.get("resets_in_seconds")
    if secs is None:
        return ""
    secs = int(secs)
    h, m = secs // 3600, (secs % 3600) // 60
    return f", сброс через {h} ч {m} мин" if h else f", сброс через {m} мин"


def _bar(percent: float, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _n(x: int) -> str:
    return f"{int(x):,}".replace(",", " ")


async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG
    u = write_credits_snapshot()
    lines = [
        "💳 <b>Расход кредитов</b>\n",
        f"🤖 Модель: <code>{CONFIG.get('model', '?')}</code>",
    ]
    # --- 5-часовое окно ---
    lines.append("\n<b>⏱ 5 часов</b>")
    if u["w5_limit"] > 0:
        p5 = u["w5_percent_used"] or 0.0
        lines.append(f"<b>{_bar(p5)} {p5:.1f}%</b>")
        lines.append(f"Потрачено: <b>{_n(u['w5_used'])}</b> из {_n(u['w5_limit'])}")
        lines.append(f"Осталось: <b>{_n(u['w5_remaining'])}</b> ({u['w5_percent_left']:.1f}%)")
    else:
        lines.append(f"Потрачено в окне: <b>{_n(u['w5_used'])}</b>")
        lines.append("Лимит не задан: <code>/limit5h 500000</code>")
    if u["w5_active"]:
        lines.append(f"Сброс окна{_fmt_reset({'resets_in_seconds': u['w5_resets_in']})[1:]}")
    else:
        lines.append("Окно не активно, оно начнётся с ближайшей задачи")

    lines.append("\n<b>📅 Период подписки</b>")
    if u["limit"] > 0:
        pct = u["percent_used"] or 0.0
        lines += [
            f"\n<b>{_bar(pct)} {pct:.1f}%</b>",
            f"Потрачено: <b>{_n(u['period_spent'])}</b> из {_n(u['limit'])}",
            f"Осталось: <b>{_n(u['remaining'])}</b> ({u['percent_left']:.1f}%)",
        ]
        if u["period_start"]:
            lines.append(f"С: {u['period_start']}")
    else:
        lines += [
            f"\nПотрачено с начала учёта: <b>{_n(u['period_spent'])}</b>",
            "Максимум не задан. Укажи его: <code>/credits_limit 5000000</code>",
        ]
    lines += [
        f"\n🔢 Сегодня: {_n(u['today'])} | Всего: {_n(u['total'])} ({u['runs']} запусков)",
    ]
    if u["by_model"]:
        lines.append("\n<b>По моделям:</b>")
        for m, t in sorted(u["by_model"].items(), key=lambda x: -x[1]):
            lines.append(f"• <code>{m}</code>: {_n(t)}")

    lim = get_codex_limits()
    rl = lim.get("limits") if lim else None
    if rl:
        lines.append("\n<b>Лимиты подписки (по данным Codex):</b>")
        for key, title in (("primary", "5 часов (Codex)"), ("secondary", "Недельное окно")):
            w = rl.get(key)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                used = float(w["used_percent"])
                lines.append(f"• {title}: {used:.0f}% использовано, осталось {100 - used:.0f}%{_fmt_reset(w)}")
    lines.append("\n💾 Отчёт сохранён: <code>credits_report.json</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_credits_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config
    args = context.args or []
    if not args:
        cur = int(CONFIG.get("credit_limit", 0) or 0)
        await update.message.reply_text(
            f"Максимум сейчас: <b>{_n(cur) if cur else 'не задан'}</b>\n"
            "Задать: <code>/credits_limit 5000000</code> (в токенах)",
            parse_mode="HTML",
        )
        return
    try:
        value = int(args[0].replace("_", "").replace(" ", "").replace(",", ""))
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Нужно положительное число, например /credits_limit 5000000")
        return
    CONFIG["credit_limit"] = value
    save_config(CONFIG)
    write_credits_snapshot()
    await update.message.reply_text(f"✅ Максимум: <b>{_n(value)}</b> токенов", parse_mode="HTML")


async def cmd_limit5h(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config
    args = context.args or []
    if not args:
        cur = int(CONFIG.get("limit_5h", 0) or 0)
        await update.message.reply_text(
            f"5-часовой лимит: <b>{_n(cur) if cur else 'не задан'}</b>\n"
            "Задать: <code>/limit5h 500000</code> (в токенах)",
            parse_mode="HTML",
        )
        return
    try:
        value = int(args[0].replace("_", "").replace(" ", "").replace(",", ""))
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Нужно положительное число, например /limit5h 500000")
        return
    CONFIG["limit_5h"] = value
    save_config(CONFIG)
    write_credits_snapshot()
    await update.message.reply_text(f"✅ 5-часовой лимит: <b>{_n(value)}</b> токенов", parse_mode="HTML")


async def cmd_credits_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_credit_period()
    await update.message.reply_text("♻️ Счётчик потраченного сброшен (новый период).")


# ============ СТАТУС ============

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG
    queue = load_queue()
    pending = len([t for t in queue if t.get("status") == "pending"])
    in_prog = len([t for t in queue if t.get("status") == "in_progress"])
    done = len([t for t in queue if t.get("status") == "done"])
    direction = get_direction()
    dir_text = (direction[:150] + "...") if len(direction) > 150 else (direction or "<i>не задано</i>")

    p = _peek(update.effective_user.id)
    att = []
    if p["has_prompt"]:
        att.append("📝")
    if p["images"]:
        att.append(f"🖼{p['images']}")
    if p["videos"]:
        att.append(f"🎬{p['videos']}")
    if p["zips"]:
        att.append(f"📦{p['zips']}")
    if p["files"]:
        att.append(f"📎{p['files']}")

    lines = [
        "🔍 <b>Статус</b>\n",
        f"🤖 <code>{CONFIG.get('model', '?')}</code> / <code>{CONFIG.get('effort', '?')}</code>",
        f"♻️ Автопилот: <b>{'АКТИВЕН' if get_loop_state() else 'выкл'}</b>",
        f"📋 Очередь: ⏳ {pending} | 🔄 {in_prog} | ✅ {done}",
        f"🎯 Направление: {dir_text}",
        f"🐙 GitHub: <code>{GITHUB_REPO or 'не задан'}</code>",
        f"📁 Проект: <code>{escape_html(str(PROJECT_PATH))}</code>",
    ]
    if att:
        lines.append(f"\n📎 Ожидают: {' '.join(att)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ============ НАПРАВЛЕНИЕ ============

async def cmd_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = " ".join(context.args) if context.args else ""
    a = _consume(user_id)
    if not text:
        text = a["text"]

    if not text:
        current = get_direction()
        if current:
            await update.message.reply_text(
                f"🎯 <b>Направление:</b>\n\n<i>{escape_html(current)}</i>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "🎯 Не задано.\n\n"
                "<code>/direction Сделай игру как Brawl Stars</code>",
                parse_mode="HTML",
            )
        return

    await asyncio.to_thread(set_direction, text)
    await update.message.reply_text(
        f"✅ Задано:\n\n<i>{escape_html(text[:500])}</i>\n\nЗапусти: <code>/autopilot</code>",
        parse_mode="HTML",
    )


# ============ ЗАДАЧИ ============

async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    prompt = " ".join(context.args) if context.args else ""
    a = _consume(user_id)

    if not prompt:
        prompt = a["text"]
    media = _media_for_codex(a)

    if not prompt and not media:
        await update.message.reply_text("❌ Пусто.", parse_mode="HTML")
        return

    if not prompt:
        prompt = "Опиши вложение и предложи улучшения."

    task_id = await asyncio.to_thread(enqueue_task, prompt, media)
    queue = load_queue()
    pending = len([t for t in queue if t.get("status") == "pending"])
    info = _attach_line(a)
    await update.message.reply_text(
        f"✅ Задача #{task_id} добавлена.\n📎 {info}\n📋 В очереди: <b>{pending}</b>",
        parse_mode="HTML",
    )


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global loop_running
    user_id = update.effective_user.id
    prompt = " ".join(context.args) if context.args else ""
    a = _consume(user_id)

    if not prompt:
        prompt = a["text"]
    media = _media_for_codex(a)

    if not prompt and not media:
        await update.message.reply_text("❌ Пусто.", parse_mode="HTML")
        return

    if not prompt:
        prompt = "Опиши вложение и сделай."

    task_id = await asyncio.to_thread(enqueue_task, prompt, media)
    info = _attach_line(a)
    await update.message.reply_text(
        f"🚀 <b>Задача #{task_id}</b> добавлена.\n📎 {info}\nЗапускаю...",
        parse_mode="HTML",
    )

    if not loop_running:
        asyncio.create_task(_loop_queue(update))
    else:
        await update.message.reply_text("♻️ Loop уже работает.")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue = load_queue()
    if not queue:
        await update.message.reply_text("📋 Очередь пуста.")
        return
    lines = ["📋 <b>Очередь:</b>\n"]
    for t in queue[-20:]:
        icon = {"pending": "⏳", "in_progress": "🔄", "done": "✅"}.get(t.get("status"), "❓")
        n_media = len(t.get("images", []))
        attach = f" 📎{n_media}" if n_media else ""
        lines.append(f"{icon} #{t['id']} — {escape_html(t['prompt'][:70])}{attach}")
    if len(queue) > 20:
        lines.append(f"\n<i>...и ещё {len(queue) - 20}</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(clear_queue)
    await update.message.reply_text("🗑 Очередь очищена.")


# ============ ВЫПОЛНЕНИЕ РУЧНОЙ ОЧЕРЕДИ ============

async def _process_one_task(update: Update, task: dict) -> bool:
    task_id = task["id"]
    prompt = task["prompt"]
    media = task.get("images", [])

    status = await update.message.reply_text(
        f"🔄 <b>Задача #{task_id}</b>\n<i>{escape_html(prompt[:200])}</i>"
        + (f"\n📎 Вложений: {len(media)}" if media else ""),
        parse_mode="HTML",
    )

    try:
        result = await asyncio.to_thread(run_codex, prompt, media or None)
    except Exception as e:
        await status.edit_text(f"❌ Codex: {escape_html(str(e))}", parse_mode="HTML")
        return False

    if not result or not result.get("ok"):
        answer = (result.get("answer", "?") if result else "пусто")[:500]
        await status.edit_text(
            f"❌ Codex упал:\n<pre>{escape_html(answer)}</pre>", parse_mode="HTML"
        )
        return False

    answer = result.get("answer", "")
    short = answer[:600] if len(answer) > 600 else answer

    commit_msg = f"[auto] {prompt[:60]}"
    ok, git_out = await asyncio.to_thread(git_commit_push, commit_msg)

    if not ok:
        await status.edit_text(
            f"❌ <b>Задача #{task_id} — Git НЕ прошёл</b>\n"
            f"<pre>{escape_html(git_out[-400:])}</pre>",
            parse_mode="HTML",
        )
        await asyncio.to_thread(complete_task, task_id)
        return False

    await status.edit_text(
        f"✅ <b>Задача #{task_id}</b>\n"
        f"Код запушен.\n\n"
        f"<pre>{escape_html(short)}</pre>\n\n"
        f"<i>APK: /build_apk</i>",
        parse_mode="HTML",
    )

    await asyncio.to_thread(complete_task, task_id)
    return True


async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = await asyncio.to_thread(pop_task)
    if not task:
        await update.message.reply_text("📋 Очередь пуста.")
        return
    await _process_one_task(update, task)


async def _loop_queue(update: Update) -> None:
    global loop_running
    loop_running = True
    set_loop_state(True)
    await update.message.reply_text("♻️ Выполняю очередь...")

    ok_count = 0
    fail_count = 0

    while loop_running:
        task = await asyncio.to_thread(pop_task)
        if not task:
            await update.message.reply_text(
                f"✅ Очередь пуста.\n📊 Успешно: {ok_count} | Ошибок: {fail_count}"
            )
            break
        try:
            success = await _process_one_task(update, task)
            if success:
                ok_count += 1
            else:
                fail_count += 1
        except Exception as e:
            fail_count += 1
            await update.message.reply_text(
                f"❌ Ошибка #{task.get('id')}: {escape_html(str(e))}",
                parse_mode="HTML",
            )
        await asyncio.sleep(3)

    loop_running = False
    set_loop_state(False)


async def cmd_loop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global loop_running
    if loop_running:
        await update.message.reply_text("♻️ Уже работает.")
        return
    asyncio.create_task(_loop_queue(update))


# ============ СТУДИЯ: ПЛАНИРОВЩИК + КОМАНДА МОДЕЛЕЙ ============

MAX_FAILS_IN_ROW = 3
PAUSE_BETWEEN_IDEAS = 20  # секунд
MAX_TASKS_PER_IDEA = 8
IDEAS_FILE = Path(__file__).parent / "ideas_log.json"
IDEAS_IN_PROMPT = 40

ROLES = ["planner", "heavy", "mid", "light", "checker"]
ROLE_DEFAULTS = {
    "planner": ("gpt-6-luna", "medium"),
    "heavy": ("gpt-6-astra", "high"),
    "mid": ("gpt-6.1-sol", "medium"),
    "light": ("gpt-5.6-terra", "low"),
    "checker": ("gpt-5.6-terra", "low"),
}
ROLE_ICON = {"planner": "🧠", "heavy": "🔴", "mid": "🟡", "light": "🟢", "checker": "🛡"}
ROLE_TITLE = {
    "planner": "Планировщик (идеи и разбивка)",
    "heavy": "Тайлмапы и расстановка объектов в сценах (дорогая)",
    "mid": "Основные задачи (сильная, медленная, дешевле heavy)",
    "light": "Простые задачи (персонажи, анимации)",
    "checker": "Контролёр: проверка и починка проекта перед пушем",
}
HEAVY_KEYWORDS = (
    "tilemap", "tileset", "tile", "тайл",
    "расстанов", "расстав", "компоновк", "level design", "левел-дизайн", "level layout",
)


def team_get(role: str):
    from config import CONFIG
    dm, de = ROLE_DEFAULTS[role]
    return CONFIG.get(f"team_{role}_model", dm), CONFIG.get(f"team_{role}_effort", de)


def _team_text() -> str:
    lines = ["👥 <b>Команда студии</b>\n"]
    for r in ROLES:
        m, e = team_get(r)
        lines.append(f"{ROLE_ICON[r]} <b>{r}</b> — <code>{m}</code> / <code>{e}</code>\n   <i>{ROLE_TITLE[r]}</i>")
    lines.append(
        "\nИзменить: <code>/team heavy gpt-6-astra high</code>\n"
        "Роли: planner, heavy, mid, light, checker"
    )
    return "\n".join(lines)


async def cmd_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config
    args = context.args or []
    if not args:
        await update.message.reply_text(_team_text(), parse_mode="HTML")
        return
    role = args[0].lower()
    if role not in ROLES or len(args) < 2:
        await update.message.reply_text("❌ Формат: /team <planner|heavy|mid|light|checker> <модель> [усилие]")
        return
    model = args[1]
    CONFIG[f"team_{role}_model"] = model
    if len(args) >= 3:
        eff = args[2].lower()
        if eff not in EFFORT_ALLOWED:
            await update.message.reply_text("❌ Усилие: " + ", ".join(EFFORT_ORDER))
            return
        CONFIG[f"team_{role}_effort"] = eff
    models = CONFIG.setdefault("models", list(DEFAULT_MODELS))
    if model not in models:
        models.append(model)
    save_config(CONFIG)
    await update.message.reply_text(_team_text(), parse_mode="HTML")


def _load_ideas() -> list:
    try:
        return json.loads(IDEAS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_idea(title: str) -> None:
    ideas = _load_ideas()
    ideas.append({"idea": title, "time": time.strftime("%Y-%m-%d %H:%M")})
    try:
        IDEAS_FILE.write_text(
            json.dumps(ideas[-300:], indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _build_planner_prompt(direction: str, idea_no: int) -> str:
    done = [i["idea"] for i in _load_ideas()[-IDEAS_IN_PROMPT:]]
    done_txt = "\n".join(f"- {t}" for t in done) if done else "(пока ничего)"
    return f"""Ты ведущий геймдизайнер и технический директор студии, которая делает Godot-игру в текущей папке. Это идея №{idea_no}.

НАПРАВЛЕНИЕ ИГРЫ:
{direction}

УЖЕ РЕАЛИЗОВАННЫЕ ИДЕИ (не повторяй их и не делай похожие):
{done_txt}

ТВОЯ ЗАДАЧА, ТОЛЬКО ПЛАНИРОВАНИЕ. НЕ ИЗМЕНЯЙ И НЕ СОЗДАВАЙ НИКАКИЕ ФАЙЛЫ.
1. Изучи проект (project.godot, scenes/, scripts/, autoloads/), пойми, что в игре уже есть.
2. Придумай ОДНУ новую конкретную идею, которая усиливает игру в рамках направления (механика, персонаж, враг, предмет, способность, уровень, режим, событие, прогрессия, интерфейс, звук, баланс). Чередуй категории.
3. Разбей идею на {2}-{MAX_TASKS_PER_IDEA} последовательных задач. Порядок важен: следующая задача может опираться на результат предыдущих.
4. Каждой задаче назначь исполнителя:
   - "heavy": самая дорогая модель. Назначай ТОЛЬКО для: (а) тайлмапов, тайлсетов и карт уровней; (б) сборки и компоновки сцен, то есть расстановки объектов на уровне и в сцене (позиции, слои, спавны, препятствия, кусты, стены, ящики, точки появления); (в) критически сложных мест, где дешёвые модели ошибаются. Не трать её на остальное.
   - "mid": сильная, но медленная и заметно дешевле heavy. Основная рабочая лошадка: все нетривиальные задачи, архитектура систем, ИИ врагов, механики боя, сцены, интерфейс и меню, интеграция частей, режимы. Если сомневаешься между heavy и mid, выбирай mid.
   - "light": самая дешёвая и быстрая модель. Персонажи со спрайтами и анимациями (idle, walk, run, attack, hit, death), простые скрипты, заглушки ресурсов, звуки, мелкий баланс, тексты.
   ПРАВИЛО: любая задача с тайлмапами, тайлсетами, построением карты уровня или расстановкой объектов в сцене ВСЕГДА "heavy". Код механик и логики объектов (скрипты) при этом остаётся у "mid". Не назначай "heavy" без необходимости, это дорого.
5. Описание каждой задачи должно быть самодостаточным: что сделать, какие файлы создать или изменить, как это связано с остальным.

ОТВЕТ СТРОГО В ФОРМАТЕ JSON, без пояснений и без markdown:
{{"idea": "короткое название идеи, до 10 слов", "summary": "1-2 предложения о сути идеи", "tasks": [{{"title": "короткое название задачи", "description": "подробное описание", "model": "heavy|mid|light"}}]}}"""


def _parse_plan(text: str):
    m = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    raw = data.get("tasks")
    if not isinstance(raw, list) or not raw:
        return None
    tasks = []
    for t in raw[:MAX_TASKS_PER_IDEA]:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title", "")).strip()[:100]
        desc = str(t.get("description", "")).strip()
        if not title or not desc:
            continue
        role = str(t.get("model", "mid")).strip().lower()
        if role not in ("heavy", "mid", "light"):
            role = "mid"
        if any(k in (title + " " + desc).lower() for k in HEAVY_KEYWORDS):
            role = "heavy"
        tasks.append({"title": title, "description": desc, "role": role})
    if not tasks:
        return None
    return {
        "idea": str(data.get("idea", "без названия")).strip()[:120] or "без названия",
        "summary": str(data.get("summary", "")).strip()[:400],
        "tasks": tasks,
    }


def _plan_text(plan: dict, idea_no: int) -> str:
    lines = [
        f"💡 <b>Идея #{idea_no}: {escape_html(plan['idea'])}</b>",
        f"<i>{escape_html(plan['summary'])}</i>\n",
        "<b>План задач:</b>",
    ]
    for i, t in enumerate(plan["tasks"], 1):
        m, _ = team_get(t["role"])
        lines.append(
            f"{i}. {ROLE_ICON[t['role']]} {escape_html(t['title'])} "
            f"→ <code>{m}</code>"
        )
    lines.append("\n🔴 сложные  🟡 обычные  🟢 простые")
    return "\n".join(lines)


def _build_checker_prompt(direction: str, plan: dict, statuses: list) -> str:
    done = "\n".join(
        f"- {t['title']}" for t, s in zip(plan["tasks"], statuses) if s == "ok"
    )
    return f"""Ты контролёр качества Godot-проекта в текущей папке. Студия только что реализовала идею «{plan['idea']}».

СДЕЛАННЫЕ ЗАДАЧИ:
{done}

НАПРАВЛЕНИЕ ИГРЫ (его ограничения нарушать нельзя):
{direction}

ТВОЯ ЗАДАЧА, ПРОВЕРКА И МИНИМАЛЬНАЯ ПОЧИНКА:
1. Проверь файлы, изменённые в этой идее (.gd, .tscn, .tres, project.godot): синтаксис GDScript, корректность структуры .tscn и .tres, незакрытые скобки, неверные отступы.
2. Проверь ссылки: все пути res:// существуют, скрипты подключены к существующим узлам, сигналы и autoload-ы существуют, главная сцена указана верно.
3. Если в системе есть godot, запусти headless-проверку проекта, иначе проверяй вручную.
4. Исправляй только найденные поломки, минимальными правками. Не добавляй новые функции и не меняй дизайн. Не удаляй то, что запрещено направлением (например, режимы игры).
5. В конце ответа: список найденных и исправленных проблем (1-6 строк) или «проблем не найдено»."""


def _build_task_prompt(direction: str, plan: dict, index: int, statuses: list) -> str:
    plan_lines = []
    for i, t in enumerate(plan["tasks"]):
        mark = "ГОТОВО" if statuses[i] == "ok" else ("ПРОПУЩЕНО" if statuses[i] == "fail" else ("← ТВОЯ ЗАДАЧА" if i == index else "впереди"))
        plan_lines.append(f"{i + 1}. [{t['role']}] {t['title']} ({mark})")
    t = plan["tasks"][index]
    return f"""Ты разработчик Godot-проекта в текущей папке. Студия реализует идею «{plan['idea']}»: {plan['summary']}

НАПРАВЛЕНИЕ ИГРЫ:
{direction}

ПЛАН ИДЕИ:
{chr(10).join(plan_lines)}

ТВОЯ ЗАДАЧА (№{index + 1} из {len(plan['tasks'])}): {t['title']}
{t['description']}

ПРАВИЛА:
- Делай только свою задачу, остальные пункты плана сделают другие.
- Сначала посмотри, что уже есть в проекте (в том числе результат предыдущих задач), и опирайся на это.
- Соблюдай ограничения из направления игры (то, что нельзя удалять или менять).
- Недостающие ресурсы (спрайты, звуки) заменяй заглушками. Ничего не спрашивай, делай.
- После изменений проект должен запускаться без ошибок парсинга в .gd и .tscn.
- В конце ответа напиши 2-4 строки: что сделано и какие файлы затронуты."""


async def _wait_for_5h_reset(update: Update) -> bool:
    """Если 5-часовой лимит исчерпан, ждёт сброса окна. False, если нажали /stop."""
    global loop_running
    notified = False
    while loop_running:
        u = get_usage_summary()
        if u["w5_limit"] <= 0 or (u["w5_percent_used"] or 0) < 100 or not u["w5_active"]:
            return True
        if not notified:
            mins = max(1, u["w5_resets_in"] // 60)
            await update.message.reply_text(
                f"⏸ 5-часовой лимит исчерпан. Пауза примерно {mins} мин до сброса окна."
            )
            notified = True
        await asyncio.sleep(60)
    return False


async def _run_with_progress(update: Update, label: str, prompt: str, model: str, effort: str):
    """Запускает Codex в потоке, обновляет статус раз в минуту. None, если остановили."""
    global loop_running
    status = await update.message.reply_text(
        f"⏳ {escape_html(label)}\n<code>{model}</code> / <code>{effort}</code>, 0 мин",
        parse_mode="HTML",
    )
    task = asyncio.create_task(asyncio.to_thread(run_codex, prompt, None, model, effort))
    elapsed = 0
    while not task.done() and loop_running:
        await asyncio.sleep(30)
        if task.done():
            break
        elapsed += 0.5
        if elapsed == int(elapsed):
            try:
                await status.edit_text(
                    f"⏳ {escape_html(label)}\n<code>{model}</code> / <code>{effort}</code>, {int(elapsed)} мин",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    try:
        await status.delete()
    except Exception:
        pass
    if not loop_running and not task.done():
        return None
    try:
        return task.result()
    except Exception as e:
        return {"ok": False, "answer": str(e), "tokens": 0}


async def _autopilot_loop(update: Update, max_ideas: int = 0) -> None:
    global loop_running
    direction = get_direction()
    if not direction:
        await update.message.reply_text(
            "❌ Сначала: <code>/direction &lt;текст&gt;</code>", parse_mode="HTML"
        )
        return

    loop_running = True
    set_loop_state(True)

    limit_txt = f"Максимум идей: {max_ideas}\n" if max_ideas else "Идеи идут бесконечно, пока не нажмёшь /stop\n"
    await update.message.reply_text(
        f"🚀 <b>Студия запущена</b>\n\n"
        f"🎯 <i>{escape_html(direction[:300])}</i>\n\n"
        f"{limit_txt}\n"
        f"{_team_text()}\n\n"
        f"Планировщик придумывает идею и разбивает её на задачи, "
        f"каждую задачу делает назначенная модель. Потом контролёр проверяет проект, затем коммит и пуш.\n"
        f"Остановить: /stop",
        parse_mode="HTML",
    )

    idea_no = len(_load_ideas())
    ideas_this_run = 0
    fails = 0
    reason = ""

    try:
        while loop_running:
            if max_ideas and ideas_this_run >= max_ideas:
                reason = f"сделано идей: {ideas_this_run}"
                break
            if not await _wait_for_5h_reset(update):
                break

            idea_no += 1
            idea_tokens = 0

            # ---- 1. планирование ----
            pm, pe = team_get("planner")
            res = await _run_with_progress(
                update, f"Идея #{idea_no}: планировщик придумывает идею и задачи",
                _build_planner_prompt(direction, idea_no), pm, pe,
            )
            if res is None:
                reason = "остановлено пользователем"
                break
            idea_tokens += res.get("tokens", 0) or 0
            plan = _parse_plan(res.get("answer", "")) if res.get("ok") else None
            if not plan:
                fails += 1
                detail = (res.get("answer", "") or "")[:300]
                await update.message.reply_text(
                    f"❌ Планировщик не выдал рабочий план ({fails}/{MAX_FAILS_IN_ROW})\n"
                    f"<pre>{escape_html(detail)}</pre>",
                    parse_mode="HTML",
                )
                idea_no -= 1
                if fails >= MAX_FAILS_IN_ROW:
                    reason = f"{MAX_FAILS_IN_ROW} сбоя планировщика подряд"
                    break
                await asyncio.sleep(PAUSE_BETWEEN_IDEAS)
                continue

            await update.message.reply_text(_plan_text(plan, idea_no), parse_mode="HTML")

            # ---- 2. исполнение задач ----
            statuses = ["pending"] * len(plan["tasks"])
            stopped = False
            for i, t in enumerate(plan["tasks"]):
                if not loop_running:
                    stopped = True
                    break
                if not await _wait_for_5h_reset(update):
                    stopped = True
                    break
                model, effort = team_get(t["role"])
                prompt = _build_task_prompt(direction, plan, i, statuses)
                label = f"Задача {i + 1}/{len(plan['tasks'])}: {t['title']}"

                result = None
                for attempt in (1, 2):
                    run_model, run_effort = model, effort
                    if attempt == 2:
                        up = {"light": "mid", "mid": "heavy"}.get(t["role"])
                        if up:
                            run_model, run_effort = team_get(up)
                    result = await _run_with_progress(update, label, prompt, run_model, run_effort)
                    if result is None:
                        break
                    idea_tokens += result.get("tokens", 0) or 0
                    if result.get("ok"):
                        model = run_model
                        break
                    if attempt == 1:
                        up_model = team_get({"light": "mid", "mid": "heavy"}.get(t["role"], t["role"]))[0]
                        await update.message.reply_text(
                            f"⚠️ {escape_html(t['title'])}: ошибка, пробую ещё раз на <code>{up_model}</code>",
                            parse_mode="HTML",
                        )
                if result is None:
                    stopped = True
                    break

                if result.get("ok"):
                    statuses[i] = "ok"
                    out = (result.get("answer", "") or "").strip()
                    await update.message.reply_text(
                        f"✅ <b>{i + 1}/{len(plan['tasks'])} {escape_html(t['title'])}</b> "
                        f"{ROLE_ICON[t['role']]} <code>{model}</code>\n"
                        f"<pre>{escape_html(out[:500])}</pre>",
                        parse_mode="HTML",
                    )
                else:
                    statuses[i] = "fail"
                    await update.message.reply_text(
                        f"❌ <b>{i + 1}/{len(plan['tasks'])} {escape_html(t['title'])}</b> пропущена\n"
                        f"<pre>{escape_html((result.get('answer', '') or '')[:300])}</pre>",
                        parse_mode="HTML",
                    )

            if stopped:
                reason = "остановлено пользователем"
                break

            ok_count = statuses.count("ok")
            if ok_count == 0:
                fails += 1
                await update.message.reply_text(
                    f"❌ Идея #{idea_no}: ни одна задача не выполнена ({fails}/{MAX_FAILS_IN_ROW})"
                )
                if fails >= MAX_FAILS_IN_ROW:
                    reason = f"{MAX_FAILS_IN_ROW} неудачные идеи подряд"
                    break
                await asyncio.sleep(PAUSE_BETWEEN_IDEAS)
                continue
            fails = 0

            # ---- 2.5 контролёр ----
            if loop_running:
                cm, ce = team_get("checker")
                cres = await _run_with_progress(
                    update, f"Идея #{idea_no}: контролёр проверяет проект",
                    _build_checker_prompt(direction, plan, statuses), cm, ce,
                )
                if cres is None:
                    reason = "остановлено пользователем"
                    break
                idea_tokens += cres.get("tokens", 0) or 0
                if cres.get("ok"):
                    await update.message.reply_text(
                        f"🛡 <b>Контролёр</b> <code>{cm}</code>\n"
                        f"<pre>{escape_html((cres.get('answer', '') or '').strip()[:500])}</pre>",
                        parse_mode="HTML",
                    )
                else:
                    await update.message.reply_text(
                        "🛡 Контролёр не отработал (ошибка), пушу как есть:\n"
                        f"<pre>{escape_html((cres.get('answer', '') or '')[:200])}</pre>",
                        parse_mode="HTML",
                    )

            # ---- 3. коммит и пуш ----
            ok, git_out = await asyncio.to_thread(
                git_commit_push, f"[auto] идея #{idea_no}: {plan['idea'][:60]}"
            )
            if not ok:
                await update.message.reply_text(
                    f"❌ <b>Git НЕ прошёл</b> (идея #{idea_no})\n"
                    f"<pre>{escape_html(git_out[-400:])}</pre>",
                    parse_mode="HTML",
                )
                reason = "ошибка git, остановил, чтобы не копить непушенные правки"
                break

            _save_idea(plan["idea"])
            ideas_this_run += 1
            u = get_usage_summary()
            pct = f" ({idea_tokens / u['w5_limit'] * 100:.1f}% от 5ч лимита)" if u["w5_limit"] > 0 and idea_tokens else ""
            skipped = statuses.count("fail")
            await update.message.reply_text(
                f"🎉 <b>Идея #{idea_no} готова и запушена</b>: {escape_html(plan['idea'])}\n"
                f"Задач выполнено: {ok_count}/{len(statuses)}"
                f"{f', пропущено: {skipped}' if skipped else ''}\n"
                f"Токенов на идею: {idea_tokens:,}{pct}".replace(",", " "),
                parse_mode="HTML",
            )

            await asyncio.sleep(PAUSE_BETWEEN_IDEAS)
    finally:
        loop_running = False
        set_loop_state(False)

    await update.message.reply_text(
        f"🏁 <b>Студия остановлена</b>: {reason or 'остановлена'}\n"
        f"Идей за этот запуск: {ideas_this_run}\n\nСобрать APK: <code>/build_apk</code>",
        parse_mode="HTML",
    )


async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global loop_running
    user_id = update.effective_user.id

    direction = get_direction()
    if not direction:
        a = _consume(user_id)
        if a["text"]:
            await asyncio.to_thread(set_direction, a["text"])
            direction = a["text"]
            await update.message.reply_text(
                f"📝 Направление из файла:\n\n<i>{escape_html(a['text'][:500])}</i>",
                parse_mode="HTML",
            )

    if loop_running:
        await update.message.reply_text("🚀 Уже работает.")
        return

    max_cycles = 0
    if context.args:
        try:
            max_cycles = max(0, int(context.args[0]))
        except ValueError:
            pass
    asyncio.create_task(_autopilot_loop(update, max_cycles))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global loop_running
    loop_running = False
    set_loop_state(False)
    await update.message.reply_text("⏹ Остановлено.")


# ============ СБОРКА APK ============

async def cmd_build_apk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (GITHUB_REPO and GITHUB_TOKEN):
        await update.message.reply_text("❌ GITHUB_REPO / GITHUB_TOKEN не заданы.")
        return

    status = await update.message.reply_text("🚀 Запускаю сборку APK на GitHub...")

    prev_tag = await asyncio.to_thread(get_latest_release_tag, GITHUB_REPO, GITHUB_TOKEN)

    ok = await asyncio.to_thread(trigger_build_workflow, GITHUB_REPO, GITHUB_TOKEN)
    if not ok:
        await status.edit_text("❌ Не удалось запустить workflow. Проверь токен.")
        return

    await status.edit_text("⏳ Сборка запущена. Жду APK (до 15 мин)...")

    new_tag = await asyncio.to_thread(
        wait_for_new_release, GITHUB_REPO, GITHUB_TOKEN, prev_tag, 900
    )
    if not new_tag:
        await status.edit_text("⚠️ APK не пришёл за 15 мин. Проверь Actions.")
        return

    apk_path = Path(tempfile.gettempdir()) / f"build_{new_tag}.apk"
    got = await asyncio.to_thread(download_latest_apk, GITHUB_REPO, GITHUB_TOKEN, apk_path, new_tag)
    if not got:
        await status.edit_text(f"⚠️ Релиз {new_tag} есть, но APK не скачался.")
        return

    size_mb = apk_path.stat().st_size / (1024 * 1024)
    with open(apk_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=f"cubism_{new_tag}.apk",
            caption=f"✅ <b>Сборка готова</b>\n📦 {new_tag}\n💾 {size_mb:.1f} MB",
            parse_mode="HTML",
        )
    await status.delete()


# ============ CODEX ============

async def cmd_luna(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    prompt = " ".join(context.args) if context.args else ""
    a = _consume(user_id)

    if not prompt:
        prompt = a["text"]
    media = _media_for_codex(a)

    if not prompt and not media:
        await update.message.reply_text(
            "❌ <code>/luna &lt;промпт&gt;</code>", parse_mode="HTML"
        )
        return

    if not prompt:
        prompt = "Опиши вложение и предложи улучшения."

    info = _attach_line(a)
    status = await update.message.reply_text(
        f"🌙 Luna работает... 📎 {info}", parse_mode="HTML"
    )

    try:
        result = await asyncio.to_thread(run_codex_only, prompt, media or None)
        answer = result.get("answer", "") if result else ""
        if not answer.strip():
            answer = "(пустой ответ)"

        if len(answer) > 3500:
            fp = Path(tempfile.gettempdir()) / "luna_out.txt"
            fp.write_text(answer, encoding="utf-8")
            with open(fp, "rb") as f:
                await update.message.reply_document(document=f, filename="luna_out.txt")
            await status.delete()
        else:
            await status.edit_text(
                f"✅ <b>Luna</b>\n\n<pre>{escape_html(answer[:3500])}</pre>",
                parse_mode="HTML",
            )
    except Exception as e:
        await status.edit_text(f"❌ {escape_html(str(e))}", parse_mode="HTML")


# ============ ФАЙЛЫ ============

async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = context.args or []
    a = _consume(user_id)

    if not args and a["text"]:
        args = a["text"].strip().split("\n")

    if not args:
        await update.message.reply_text(
            "📂 <code>/get player.gd</code>\n"
            "📂 <code>/get *.gd</code>\n"
            "📂 <code>/get list</code>",
            parse_mode="HTML",
        )
        return

    if args[0] == "list":
        files = await asyncio.to_thread(list_all_files)
        if not files:
            await update.message.reply_text("📂 Пусто.")
            return
        text = "\n".join(files)
        if len(text) > 3500:
            fp = Path(tempfile.gettempdir()) / "project_files.txt"
            fp.write_text(text, encoding="utf-8")
            with open(fp, "rb") as f:
                await update.message.reply_document(
                    document=f, filename="project_files.txt",
                    caption=f"📂 Всего: {len(files)}",
                )
        else:
            await update.message.reply_text(
                f"<pre>{escape_html(text)}</pre>", parse_mode="HTML"
            )
        return

    query = " ".join(args)
    status = await update.message.reply_text(f"🔍 Ищу {escape_html(query)}...")
    files = await asyncio.to_thread(find_files_by_pattern, query)
    if not files:
        await status.edit_text("❌ Не найдено.")
        return

    if len(files) == 1:
        f = files[0]
        with open(f, "rb") as fp:
            await update.message.reply_document(document=fp, filename=f.name)
        await status.delete()
        return

    zp = await asyncio.to_thread(pack_files_to_zip, files, "query.zip")
    with open(zp, "rb") as f:
        await update.message.reply_document(
            document=f, filename=zp.name,
            caption=f"📦 {len(files)} файл(ов)",
        )
    await status.delete()


async def cmd_download_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text("📦 Собираю проект...")
    try:
        zp = await asyncio.to_thread(pack_whole_project)
        size_mb = zp.stat().st_size / (1024 * 1024)
        if size_mb > 45:
            await status.edit_text(f"⚠️ {size_mb:.1f} MB — больше лимита.")
            return
        with open(zp, "rb") as f:
            await update.message.reply_document(
                document=f, filename=zp.name,
                caption=f"📦 Проект ({size_mb:.1f} MB)",
            )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ {escape_html(str(e))}", parse_mode="HTML")


# ============ ВЛОЖЕНИЯ ============

async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = update.effective_user.id
    state = user_states.setdefault(user_id, {})

    if msg.photo:
        file_obj = await msg.photo[-1].get_file()
        name = f"photo_{msg.photo[-1].file_unique_id}.jpg"
        local = await download_attachment_async(file_obj, name)
        state.setdefault("pending_images", []).append(str(local))
        await msg.reply_text(
            f"🖼 Фото ({human_size(local.stat().st_size)}).\nОтправь команду.",
            parse_mode="HTML",
        )
        return

    if msg.video:
        file_obj = await msg.video.get_file()
        name = f"video_{msg.video.file_unique_id}.mp4"
        local = await download_attachment_async(file_obj, name)
        size_mb = local.stat().st_size / (1024 * 1024)

        if local.stat().st_size > 20 * 1024 * 1024:
            await msg.reply_text(
                f"⚠️ Видео {size_mb:.1f} MB — больше 20 МБ.", parse_mode="HTML"
            )
            return

        state.setdefault("pending_videos", []).append(str(local))
        await msg.reply_text(
            f"🎬 Видео ({size_mb:.1f} MB).\nОтправь команду.", parse_mode="HTML"
        )
        return

    if msg.document:
        file_obj = await msg.document.get_file()
        name = msg.document.file_name or f"doc_{msg.document.file_unique_id}"
        local = await download_attachment_async(file_obj, name)
        size = human_size(local.stat().st_size)
        kind = classify_attachment(name)

        if kind == "image":
            state.setdefault("pending_images", []).append(str(local))
            await msg.reply_text(
                f"🖼 Картинка: <code>{escape_html(name)}</code> ({size})",
                parse_mode="HTML",
            )
        elif kind == "video":
            state.setdefault("pending_videos", []).append(str(local))
            await msg.reply_text(
                f"🎬 Видео: <code>{escape_html(name)}</code> ({size})",
                parse_mode="HTML",
            )
        elif kind == "text_prompt":
            state["pending_prompt"] = str(local)
            state["prompt_name"] = name
            await msg.reply_text(
                f"📝 Промпт: <code>{escape_html(name)}</code> ({size})\n"
                f"Отправь команду без текста.",
                parse_mode="HTML",
            )
        elif kind == "zip":
            state.setdefault("pending_zips", []).append(str(local))
            await msg.reply_text(
                f"📦 ZIP: <code>{escape_html(name)}</code>\n"
                f"<code>/upload_project</code> или <code>/upload_to &lt;путь&gt;</code>",
                parse_mode="HTML",
            )
        else:
            state.setdefault("pending_files", []).append(str(local))
            await msg.reply_text(
                f"📎 Файл: <code>{escape_html(name)}</code> ({size})",
                parse_mode="HTML",
            )
        return


async def cmd_upload_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})

    zips = state.get("pending_zips", [])
    if not zips:
        await update.message.reply_text("❌ Сначала отправь ZIP-архив.")
        return

    status = await update.message.reply_text("📦 Распаковываю в проект...")
    result = await asyncio.to_thread(upload_project_zip, zips[0])

    if result["ok"]:
        await status.edit_text(
            f"✅ Проект заменён\n📦 Распаковано: <b>{result['unpacked']}</b>",
            parse_mode="HTML",
        )
        user_states[user_id] = {}
    else:
        await status.edit_text(f"❌ {escape_html(result['message'])}", parse_mode="HTML")


async def cmd_upload_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})

    candidates = state.get("pending_zips", []) + state.get("pending_files", [])
    if not candidates:
        await update.message.reply_text("❌ Сначала отправь файл.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "❌ <code>/upload_to &lt;путь&gt;</code>", parse_mode="HTML"
        )
        return

    target = " ".join(args)
    status = await update.message.reply_text(f"📤 Загружаю в {escape_html(target)}...")
    result = await asyncio.to_thread(upload_file_to_project, candidates[0], target)

    if result["ok"]:
        if result.get("is_archive"):
            await status.edit_text(
                f"✅ Распаковано <b>{result['unpacked']}</b> в "
                f"<code>{escape_html(result['target'])}</code>",
                parse_mode="HTML",
            )
        else:
            await status.edit_text(
                f"✅ <code>{escape_html(result['target'])}</code>",
                parse_mode="HTML",
            )
        user_states[user_id] = {}
    else:
        await status.edit_text(f"❌ {escape_html(result['message'])}", parse_mode="HTML")


# ============ ЗАПУСК ============

def main() -> None:
    import asyncio as _asyncio
    try:
        _asyncio.get_event_loop()
    except RuntimeError:
        _asyncio.set_event_loop(_asyncio.new_event_loop())

    try:
        ensure_git_config()
        logger.info("Git настроен")
    except Exception as e:
        logger.warning(f"Git config: {e}")

    request = HTTPXRequest(
        connect_timeout=30.0, read_timeout=30.0,
        write_timeout=30.0, pool_timeout=30.0,
    )
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("team", cmd_team))
    app.add_handler(CallbackQueryHandler(cb_effort, pattern=r"^eff:"))
    app.add_handler(CommandHandler("credits", cmd_credits))
    app.add_handler(CommandHandler("credits_limit", cmd_credits_limit))
    app.add_handler(CommandHandler("credits_reset", cmd_credits_reset))
    app.add_handler(CommandHandler("limit5h", cmd_limit5h))
    app.add_handler(CallbackQueryHandler(cb_model, pattern=r"^(model|effort):"))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CommandHandler("direction", cmd_direction))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("build_apk", cmd_build_apk))

    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("loop", cmd_loop))
    app.add_handler(CommandHandler("clear", cmd_clear))

    app.add_handler(CommandHandler("luna", cmd_luna))

    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("download_project", cmd_download_project))
    app.add_handler(CommandHandler("upload_project", cmd_upload_project))
    app.add_handler(CommandHandler("upload_to", cmd_upload_to))

    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL,
        handle_attachment,
    ))

    logger.info("Бот запущен (Termux).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
