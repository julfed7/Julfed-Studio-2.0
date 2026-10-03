import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
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
    generate_next_task, add_to_history,
    git_commit_push,
    download_latest_apk, get_latest_release_tag, wait_for_new_release,
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


# ============ УТИЛИТЫ ============

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    return f"{n // (1024 * 1024)} MB"


def _consume(user_id: int) -> dict:
    """Забирает ВСЕ вложения пользователя и очищает состояние."""
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
    """Смотрит вложения, не удаляя."""
    state = user_states.get(user_id, {})
    return {
        "has_prompt": "pending_prompt" in state,
        "images": len(state.get("pending_images", [])),
        "videos": len(state.get("pending_videos", [])),
        "files": len(state.get("pending_files", [])),
        "zips": len(state.get("pending_zips", [])),
    }


def _media_for_codex(a: dict) -> list:
    """Codex --image принимает и фото, и видео."""
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
        "<b>Автопилот:</b>\n"
        "• <code>/direction &lt;текст&gt;</code>\n"
        "• <code>/autopilot</code>\n"
        "• <code>/stop</code>\n\n"
        "<b>Задачи:</b>\n"
        "• <code>/task &lt;промпт&gt;</code>\n"
        "• <code>/build &lt;промпт&gt;</code>\n"
        "• <code>/queue</code> / <code>/work</code> / <code>/loop</code> / <code>/clear</code>\n\n"
        "<b>Codex:</b> <code>/luna &lt;промпт&gt;</code>\n\n"
        "<b>Файлы:</b>\n"
        "• <code>/get &lt;файл&gt;</code> / <code>/download_project</code>\n"
        "• <code>/upload_project</code> / <code>/upload_to &lt;путь&gt;</code>\n\n"
        "<b>📎 Вложения — работают во ВСЕХ командах:</b>\n"
        "• Любой <b>.txt .md .gd .json .py</b> → текстовый промпт\n"
        "• Любое <b>фото</b> → идёт в Codex\n"
        "• Любое <b>видео</b> → идёт в Codex\n"
        "• Любой <b>.zip</b> → замена проекта или распаковка\n\n"
        "Просто отправь файл → потом напиши команду.\n\n"
        "<b>Прочее:</b> <code>/model</code>, <code>/status</code>, <code>/cancel</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_states[update.effective_user.id] = {}
    await update.message.reply_text("❌ Все вложения очищены.")


# ============ МОДЕЛЬ ============

async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config
    user_id = update.effective_user.id
    args = context.args or []
    a = _consume(user_id)
    if not args and a["text"]:
        args = a["text"].strip().split()

    if not args:
        await update.message.reply_text(
            f"🤖 <code>{CONFIG.get('model', 'gpt-5.6-luna')}</code>\n"
            f"⚙️ <code>{CONFIG.get('effort', 'high')}</code>\n\n"
            f"Сменить: <code>/model gpt-5.6-sol</code>",
            parse_mode="HTML",
        )
        return

    CONFIG["model"] = args[0]
    if len(args) >= 2:
        CONFIG["effort"] = args[1]
    save_config(CONFIG)
    await update.message.reply_text(
        f"✅ <code>{CONFIG['model']}</code> / <code>{CONFIG.get('effort', 'high')}</code>",
        parse_mode="HTML",
    )


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
                "<code>/direction Сделай игру как Brawl Stars</code>\n"
                "Или отправь <b>любой .txt</b> → потом <code>/direction</code>",
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
        await update.message.reply_text(
            "❌ Пусто.\n\n"
            "• <code>/task добавь стенда</code>\n"
            "• Или отправь <b>ЛЮБОЙ файл</b> → потом <code>/task</code>",
            parse_mode="HTML",
        )
        return

    if not prompt:
        prompt = "Опиши вложение и предложи улучшения."

    task_id = await asyncio.to_thread(enqueue_task, prompt, media)
    queue = load_queue()
    pending = len([t for t in queue if t.get("status") == "pending"])
    info = _attach_line(a)
    await update.message.reply_text(
        f"✅ Задача #{task_id} добавлена.\n"
        f"📎 {info}\n📋 В очереди: <b>{pending}</b>\n\n"
        f"Запустить: <code>/loop</code> или <code>/autopilot</code>",
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
        await update.message.reply_text(
            "❌ Пусто.\n\n<code>/build добавь стенда</code>", parse_mode="HTML"
        )
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


# ============ ВЫПОЛНЕНИЕ ============

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
            f"⚠️ Git push не удался:\n<pre>{escape_html(git_out[-400:])}</pre>",
            parse_mode="HTML",
        )
    else:
        await status.edit_text(
            f"✅ <b>Задача #{task_id}</b>\nКод запушен.\n\n<pre>{escape_html(short)}</pre>",
            parse_mode="HTML",
        )

    if not (GITHUB_REPO and GITHUB_TOKEN):
        await update.message.reply_text("⚠️ GITHUB_REPO / GITHUB_TOKEN не заданы.")
        await asyncio.to_thread(complete_task, task_id)
        return True

    prev_tag = await asyncio.to_thread(get_latest_release_tag, GITHUB_REPO, GITHUB_TOKEN)
    await update.message.reply_text("⏳ Жду APK из GitHub Actions (до 15 мин)...")

    new_tag = await asyncio.to_thread(
        wait_for_new_release, GITHUB_REPO, GITHUB_TOKEN, prev_tag, 900
    )
    if not new_tag:
        await update.message.reply_text("⚠️ APK не пришёл за 15 мин.")
        await asyncio.to_thread(complete_task, task_id)
        return False

    apk_path = Path(tempfile.gettempdir()) / f"build_{new_tag}.apk"
    got = await asyncio.to_thread(download_latest_apk, GITHUB_REPO, GITHUB_TOKEN, apk_path)
    if not got:
        await update.message.reply_text(f"⚠️ Релиз {new_tag} есть, но APK не скачался.")
        await asyncio.to_thread(complete_task, task_id)
        return False

    size_mb = apk_path.stat().st_size / (1024 * 1024)
    with open(apk_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=f"cubism_{new_tag}.apk",
            caption=f"✅ <b>Задача #{task_id}</b>\n📦 {new_tag}\n💾 {size_mb:.1f} MB",
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

    while loop_running:
        task = await asyncio.to_thread(pop_task)
        if not task:
            await update.message.reply_text("✅ Очередь пуста.")
            break
        try:
            await _process_one_task(update, task)
        except Exception as e:
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


# ============ АВТОПИЛОТ ============

async def _autopilot_loop(update: Update) -> None:
    global loop_running
    direction = get_direction()
    if not direction:
        await update.message.reply_text(
            "❌ Сначала: <code>/direction &lt;текст&gt;</code>", parse_mode="HTML"
        )
        return

    loop_running = True
    set_loop_state(True)
    await update.message.reply_text(
        f"🚀 <b>Автопилот запущен</b>\n\n🎯 <i>{escape_html(direction[:200])}</i>",
        parse_mode="HTML",
    )

    iteration = 0
    while loop_running:
        iteration += 1
        task = await asyncio.to_thread(pop_task)

        if not task:
            await update.message.reply_text(
                f"🧠 Итерация {iteration} — Luna думает...", parse_mode="HTML"
            )
            new_prompt = await asyncio.to_thread(generate_next_task)
            if not new_prompt:
                await update.message.reply_text("⚠️ Luna не придумала. Жду 5 мин.")
                await asyncio.sleep(300)
                continue
            await update.message.reply_text(
                f"💡 <b>Luna придумала:</b>\n<i>{escape_html(new_prompt)}</i>",
                parse_mode="HTML",
            )
            await asyncio.to_thread(add_to_history, new_prompt)
            task = {"id": -iteration, "prompt": new_prompt, "images": [], "status": "auto"}

        try:
            await _process_one_task(update, task)
        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML"
            )
            await asyncio.sleep(30)

        await asyncio.sleep(5)

    loop_running = False
    set_loop_state(False)
    await update.message.reply_text("⏹ Автопилот остановлен.")


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
    asyncio.create_task(_autopilot_loop(update))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global loop_running
    loop_running = False
    set_loop_state(False)
    await update.message.reply_text("⏹ Остановлено.")


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
            "❌ <code>/luna &lt;промпт&gt;</code>\n"
            "Или отправь <b>любой файл</b> → потом <code>/luna</code>.",
            parse_mode="HTML",
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
    """Универсальный приём ЛЮБЫХ вложений: фото, видео, документы."""
    msg = update.message
    user_id = update.effective_user.id
    state = user_states.setdefault(user_id, {})

    # --- Фото ---
    if msg.photo:
        file_obj = await msg.photo[-1].get_file()
        name = f"photo_{msg.photo[-1].file_unique_id}.jpg"
        local = await download_attachment_async(file_obj, name)
        state.setdefault("pending_images", []).append(str(local))
        await msg.reply_text(
            f"🖼 Фото сохранено ({human_size(local.stat().st_size)}).\n\n"
            f"Отправь любую команду: <code>/luna</code>, <code>/task</code>, "
            f"<code>/build</code>, <code>/direction</code>",
            parse_mode="HTML",
        )
        return

    # --- Видео ---
    if msg.video:
        file_obj = await msg.video.get_file()
        name = f"video_{msg.video.file_unique_id}.mp4"
        local = await download_attachment_async(file_obj, name)
        size_mb = local.stat().st_size / (1024 * 1024)

        if local.stat().st_size > 20 * 1024 * 1024:
            await msg.reply_text(
                f"⚠️ Видео {size_mb:.1f} MB — больше 20 МБ.\n"
                f"Telegram не отдаёт такое боту. Обрежь или сожми.",
                parse_mode="HTML",
            )
            return

        state.setdefault("pending_videos", []).append(str(local))
        await msg.reply_text(
            f"🎬 Видео сохранено ({size_mb:.1f} MB).\n\n"
            f"Отправь любую команду: <code>/luna</code>, <code>/task</code>, "
            f"<code>/build</code>",
            parse_mode="HTML",
        )
        return

    # --- Документ ---
    if msg.document:
        file_obj = await msg.document.get_file()
        name = msg.document.file_name or f"doc_{msg.document.file_unique_id}"
        local = await download_attachment_async(file_obj, name)
        size = human_size(local.stat().st_size)
        kind = classify_attachment(name)

        if kind == "image":
            state.setdefault("pending_images", []).append(str(local))
            await msg.reply_text(
                f"🖼 Картинка: <code>{escape_html(name)}</code> ({size})\n"
                f"Отправь команду.",
                parse_mode="HTML",
            )

        elif kind == "text_prompt":
            state["pending_prompt"] = str(local)
            state["prompt_name"] = name
            await msg.reply_text(
                f"📝 Текстовый промпт: <code>{escape_html(name)}</code> ({size})\n"
                f"Отправь команду без текста — файл станет промптом.",
                parse_mode="HTML",
            )

        elif kind == "zip":
            state.setdefault("pending_zips", []).append(str(local))
            await msg.reply_text(
                f"📦 ZIP: <code>{escape_html(name)}</code> ({size})\n\n"
                f"• <code>/upload_project</code> — заменить проект\n"
                f"• <code>/upload_to &lt;путь&gt;</code> — распаковать",
                parse_mode="HTML",
            )

        else:
            state.setdefault("pending_files", []).append(str(local))
            await msg.reply_text(
                f"📎 Файл: <code>{escape_html(name)}</code> ({size})\n"
                f"Отправь команду (<code>/luna</code>, <code>/task</code>) "
                f"или <code>/upload_to &lt;путь&gt;</code>",
                parse_mode="HTML",
            )
        return


async def cmd_upload_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})

    zips = state.get("pending_zips", [])
    if not zips and "pending_upload" not in state:
        await update.message.reply_text("❌ Сначала отправь ZIP-архив.")
        return

    zip_path = zips[0] if zips else state["pending_upload"]
    status = await update.message.reply_text("📦 Распаковываю в проект...")
    result = await asyncio.to_thread(upload_project_zip, zip_path)

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

    # Берём первый доступный файл
    candidates = (
        state.get("pending_zips", [])
        + state.get("pending_files", [])
        + ([state["pending_upload"]] if "pending_upload" in state else [])
    )
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
    request = HTTPXRequest(
        connect_timeout=30.0, read_timeout=30.0,
        write_timeout=30.0, pool_timeout=30.0,
    )
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(request).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Направление / автопилот
    app.add_handler(CommandHandler("direction", cmd_direction))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot))
    app.add_handler(CommandHandler("stop", cmd_stop))

    # Очередь
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("loop", cmd_loop))
    app.add_handler(CommandHandler("clear", cmd_clear))

    # Codex
    app.add_handler(CommandHandler("luna", cmd_luna))

    # Файлы
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("download_project", cmd_download_project))
    app.add_handler(CommandHandler("upload_project", cmd_upload_project))
    app.add_handler(CommandHandler("upload_to", cmd_upload_to))

    # Универсальный приём вложений — фото, видео, документы
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL,
        handle_attachment,
    ))

    logger.info("Бот запущен (Termux).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()