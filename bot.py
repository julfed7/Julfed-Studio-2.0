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
    run_codex,
    run_codex_only,
    download_attachment_async,
    pack_files_to_zip,
    find_files_by_pattern,
    find_files_in_dir,
    list_all_files,
    upload_file_to_project,
    upload_project_zip,
    pack_whole_project,
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

pending_attachments: dict = {}
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


# ============ СТАРТ / СПРАВКА ============

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🎮 <b>Студия Cubism D (Termux)</b>\n\n"
        "<b>Автопилот (24/7):</b>\n"
        "• <code>/direction &lt;текст&gt;</code> — задать цель развития\n"
        "• <code>/autopilot</code> — Luna сама придумывает и делает задачи\n"
        "• <code>/stop</code> — остановить\n\n"
        "<b>Задачи вручную:</b>\n"
        "• <code>/task &lt;промпт&gt;</code> — добавить в очередь\n"
        "• <code>/queue</code> — показать очередь\n"
        "• <code>/work</code> — выполнить одну задачу\n"
        "• <code>/loop</code> — выполнить всю очередь\n"
        "• <code>/clear</code> — очистить очередь\n\n"
        "<b>Codex напрямую:</b>\n"
        "• <code>/luna &lt;промпт&gt;</code> — правки без сборки\n\n"
        "<b>Файлы:</b>\n"
        "• <code>/get &lt;файл&gt;</code> — получить файл\n"
        "• <code>/download_project</code> — весь проект ZIP\n"
        "• <code>/upload_project</code> — заменить проект из ZIP\n"
        "• <code>/upload_to &lt;путь&gt;</code> — загрузить файл\n\n"
        "<b>Прочее:</b> <code>/model</code>, <code>/status</code>, <code>/cancel</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_states[update.effective_user.id] = {}
    await update.message.reply_text("❌ Отменено.")


# ============ МОДЕЛЬ / СТАТУС ============

async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG, save_config

    args = context.args or []
    if not args:
        await update.message.reply_text(
            f"🤖 Модель: <code>{CONFIG.get('model', 'gpt-5.6-luna')}</code>\n"
            f"Уровень: <code>{CONFIG.get('effort', 'high')}</code>\n\n"
            f"Сменить:\n"
            f"<code>/model gpt-5.6-luna</code>\n"
            f"<code>/model gpt-5.6-sol</code>\n"
            f"<code>/model gpt-6-astra</code>\n\n"
            f"Уровень: <code>/model sol high</code>",
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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from config import CONFIG

    queue = load_queue()
    pending = len([t for t in queue if t.get("status") == "pending"])
    in_progress = len([t for t in queue if t.get("status") == "in_progress"])
    done = len([t for t in queue if t.get("status") == "done"])

    direction = get_direction()
    dir_text = direction[:150] + "..." if len(direction) > 150 else direction
    if not dir_text:
        dir_text = "<i>не задано</i>"

    lines = [
        "🔍 <b>Статус</b>\n",
        f"🤖 Модель: <code>{CONFIG.get('model', '?')}</code> / <code>{CONFIG.get('effort', '?')}</code>",
        f"♻️ Автопилот: <b>{'АКТИВЕН' if get_loop_state() else 'выкл'}</b>",
        f"📋 Очередь: ⏳ {pending} | 🔄 {in_progress} | ✅ {done}",
        f"🎯 Направление: {dir_text}",
        f"🐙 GitHub: <code>{GITHUB_REPO or 'не задан'}</code>",
        f"📁 Проект: <code>{escape_html(str(PROJECT_PATH))}</code>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ============ НАПРАВЛЕНИЕ ============

async def cmd_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args) if context.args else ""

    if not text:
        current = get_direction()
        if current:
            await update.message.reply_text(
                f"🎯 <b>Направление:</b>\n\n<i>{escape_html(current)}</i>\n\n"
                f"Сменить: <code>/direction &lt;новый текст&gt;</code>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "🎯 Направление не задано.\n\n"
                "Пример:\n<code>/direction Сделай игру как Brawl Stars — "
                "короткие матчи, разные персонажи, стенды, анимации</code>",
                parse_mode="HTML",
            )
        return

    await asyncio.to_thread(set_direction, text)
    await update.message.reply_text(
        f"✅ <b>Направление задано:</b>\n\n<i>{escape_html(text)}</i>\n\n"
        f"Запусти: <code>/autopilot</code>",
        parse_mode="HTML",
    )


# ============ ОЧЕРЕДЬ ============

async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text(
            "❌ <code>/task &lt;промпт&gt;</code>", parse_mode="HTML"
        )
        return
    task_id = await asyncio.to_thread(enqueue_task, prompt)
    queue = load_queue()
    pending = len([t for t in queue if t.get("status") == "pending"])
    await update.message.reply_text(
        f"✅ Задача #{task_id} добавлена.\n"
        f"📋 В очереди: <b>{pending}</b>\n\n"
        f"Запустить: <code>/loop</code> или <code>/autopilot</code>",
        parse_mode="HTML",
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue = load_queue()
    if not queue:
        await update.message.reply_text("📋 Очередь пуста.")
        return
    lines = ["📋 <b>Очередь задач:</b>\n"]
    for t in queue[-20:]:
        icon = {"pending": "⏳", "in_progress": "🔄", "done": "✅"}.get(t.get("status"), "❓")
        lines.append(f"{icon} #{t['id']} — {escape_html(t['prompt'][:80])}")
    if len(queue) > 20:
        lines.append(f"\n<i>...и ещё {len(queue) - 20} задач</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(clear_queue)
    await update.message.reply_text("🗑 Очередь очищена.")


# ============ ОБРАБОТКА ОДНОЙ ЗАДАЧИ ============

async def _process_one_task(update: Update, task: dict) -> bool:
    task_id = task["id"]
    prompt = task["prompt"]

    status = await update.message.reply_text(
        f"🔄 <b>Задача #{task_id}</b>\n<i>{escape_html(prompt[:200])}</i>",
        parse_mode="HTML",
    )

    # 1. Codex правит
    try:
        result = await asyncio.to_thread(run_codex, prompt, None)
    except Exception as e:
        await status.edit_text(
            f"❌ Codex: {escape_html(str(e))}", parse_mode="HTML"
        )
        return False

    if not result or not result.get("ok"):
        answer = result.get("answer", "?")[:500] if result else "пусто"
        await status.edit_text(
            f"❌ Codex упал:\n<pre>{escape_html(answer)}</pre>", parse_mode="HTML"
        )
        return False

    answer = result.get("answer", "")
    short = answer[:600] if len(answer) > 600 else answer

    # 2. Git push
    commit_msg = f"[auto] {prompt[:60]}"
    ok, git_out = await asyncio.to_thread(git_commit_push, commit_msg)
    if not ok:
        await status.edit_text(
            f"⚠️ Git push не удался:\n<pre>{escape_html(git_out[-400:])}</pre>",
            parse_mode="HTML",
        )
    else:
        await status.edit_text(
            f"✅ <b>Задача #{task_id}</b>\n"
            f"Код запушен в GitHub.\n\n"
            f"<pre>{escape_html(short)}</pre>",
            parse_mode="HTML",
        )

    # 3. Ждём APK из GitHub Actions
    if not (GITHUB_REPO and GITHUB_TOKEN):
        await update.message.reply_text(
            "⚠️ GITHUB_REPO или GITHUB_TOKEN не заданы в .env — APK не получен."
        )
        await asyncio.to_thread(complete_task, task_id)
        return True

    prev_tag = await asyncio.to_thread(get_latest_release_tag, GITHUB_REPO, GITHUB_TOKEN)
    await update.message.reply_text(
        f"⏳ Жду сборку APK из GitHub Actions (до 15 мин)...", parse_mode="HTML"
    )

    new_tag = await asyncio.to_thread(
        wait_for_new_release, GITHUB_REPO, GITHUB_TOKEN, prev_tag, 900
    )

    if not new_tag:
        await update.message.reply_text(
            "⚠️ APK не пришёл за 15 минут. Проверь вкладку Actions в GitHub."
        )
        await asyncio.to_thread(complete_task, task_id)
        return False

    apk_path = Path(tempfile.gettempdir()) / f"build_{new_tag}.apk"
    got = await asyncio.to_thread(
        download_latest_apk, GITHUB_REPO, GITHUB_TOKEN, apk_path
    )
    if not got:
        await update.message.reply_text(
            f"⚠️ Релиз {new_tag} есть, но APK не скачался."
        )
        await asyncio.to_thread(complete_task, task_id)
        return False

    size_mb = apk_path.stat().st_size / (1024 * 1024)
    with open(apk_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=f"cubism_{new_tag}.apk",
            caption=(
                f"✅ <b>Задача #{task_id} собрана</b>\n"
                f"📦 {new_tag}\n"
                f"💾 {size_mb:.1f} MB\n\n"
                f"Установи и проверь."
            ),
            parse_mode="HTML",
        )

    await asyncio.to_thread(complete_task, task_id)
    return True


# ============ РУЧНОЕ ВЫПОЛНЕНИЕ ============

async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = await asyncio.to_thread(pop_task)
    if not task:
        await update.message.reply_text("📋 Очередь пуста.")
        return
    await _process_one_task(update, task)


async def _loop_queue(update: Update) -> None:
    """Выполняет очередь один раз до конца."""
    global loop_running
    loop_running = True
    set_loop_state(True)

    await update.message.reply_text("♻️ Начинаю выполнять очередь...")

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


# ============ АВТОПИЛОТ 24/7 ============

async def _autopilot_loop(update: Update) -> None:
    global loop_running

    direction = get_direction()
    if not direction:
        await update.message.reply_text(
            "❌ Сначала задай направление: <code>/direction &lt;текст&gt;</code>",
            parse_mode="HTML",
        )
        return

    loop_running = True
    set_loop_state(True)

    await update.message.reply_text(
        f"🚀 <b>Автопилот 24/7 запущен</b>\n\n"
        f"🎯 <i>{escape_html(direction[:200])}</i>\n\n"
        f"Luna сама придумывает и делает задачи. Остановить: /stop",
        parse_mode="HTML",
    )

    iteration = 0
    while loop_running:
        iteration += 1

        # Сначала — ручные задачи
        task = await asyncio.to_thread(pop_task)

        # Если очередь пуста — Luna придумывает
        if not task:
            await update.message.reply_text(
                f"🧠 <b>Итерация {iteration}</b> — Luna думает...",
                parse_mode="HTML",
            )
            new_prompt = await asyncio.to_thread(generate_next_task)
            if not new_prompt:
                await update.message.reply_text(
                    "⚠️ Luna не придумала задачу. Жду 5 минут."
                )
                await asyncio.sleep(300)
                continue

            await update.message.reply_text(
                f"💡 <b>Luna придумала:</b>\n<i>{escape_html(new_prompt)}</i>",
                parse_mode="HTML",
            )
            await asyncio.to_thread(add_to_history, new_prompt)
            task = {"id": -iteration, "prompt": new_prompt, "status": "auto"}

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
    if loop_running:
        await update.message.reply_text("🚀 Автопилот уже работает.")
        return
    asyncio.create_task(_autopilot_loop(update))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global loop_running
    loop_running = False
    set_loop_state(False)
    await update.message.reply_text("⏹ Остановлено. Текущая задача доработается.")


# ============ РУЧНЫЕ КОМАНДЫ CODEX ============

async def cmd_luna(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text(
            "❌ <code>/luna &lt;промпт&gt;</code>", parse_mode="HTML"
        )
        return

    status = await update.message.reply_text("🌙 Luna работает...")

    try:
        result = await asyncio.to_thread(run_codex_only, prompt, None)
        answer = result.get("answer", "") if result else ""
        if not answer.strip():
            answer = "(пустой ответ)"

        if len(answer) > 3500:
            fp = Path(tempfile.gettempdir()) / "luna_out.txt"
            fp.write_text(answer, encoding="utf-8")
            with open(fp, "rb") as f:
                await update.message.reply_document(
                    document=f, filename="luna_out.txt"
                )
            await status.delete()
        else:
            await status.edit_text(
                f"✅ <b>Luna</b>\n\n<pre>{escape_html(answer[:3500])}</pre>",
                parse_mode="HTML",
            )
    except Exception as e:
        await status.edit_text(
            f"❌ {escape_html(str(e))}", parse_mode="HTML"
        )


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚠️ На Termux локальная сборка APK недоступна.\n\n"
        "Используй:\n"
        "• <code>/task &lt;промпт&gt;</code> — добавить задачу\n"
        "• <code>/autopilot</code> — Luna сама делает и пушит в GitHub\n\n"
        "APK прилетит из GitHub Actions автоматически.",
        parse_mode="HTML",
    )


# ============ ФАЙЛЫ ============

async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📂 <b>Запрос файлов</b>\n\n"
            "• <code>/get player.gd</code>\n"
            "• <code>/get *.gd</code>\n"
            "• <code>/get sprites/</code>\n"
            "• <code>/get list</code>",
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
                f"📂 <b>Файлы ({len(files)}):</b>\n\n<pre>{escape_html(text)}</pre>",
                parse_mode="HTML",
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
            await update.message.reply_document(
                document=fp, filename=f.name
            )
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
            await status.edit_text(f"⚠️ {size_mb:.1f} MB — больше лимита Telegram.")
            return
        with open(zp, "rb") as f:
            await update.message.reply_document(
                document=f, filename=zp.name,
                caption=f"📦 Весь проект ({size_mb:.1f} MB)",
            )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ {escape_html(str(e))}", parse_mode="HTML")


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = update.effective_user.id

    if not msg.document:
        return

    file_obj = await msg.document.get_file()
    name = msg.document.file_name or "file"
    local = await download_attachment_async(file_obj, name)

    user_states[user_id] = {"pending_upload": str(local)}

    await msg.reply_text(
        f"📦 <code>{escape_html(name)}</code> ({human_size(local.stat().st_size)})\n\n"
        f"Куда положить?\n"
        f"• <code>/upload_project</code> — заменить весь проект\n"
        f"• <code>/upload_to &lt;путь&gt;</code> — распаковать в папку",
        parse_mode="HTML",
    )


async def cmd_upload_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    if "pending_upload" not in state:
        await update.message.reply_text(
            "❌ Сначала отправь ZIP-архив боту."
        )
        return

    status = await update.message.reply_text("📦 Распаковываю в проект...")
    result = await asyncio.to_thread(upload_project_zip, state["pending_upload"])

    if result["ok"]:
        await status.edit_text(
            f"✅ <b>Проект заменён</b>\n📦 Распаковано: <b>{result['unpacked']}</b>",
            parse_mode="HTML",
        )
        user_states[user_id] = {}
    else:
        await status.edit_text(
            f"❌ {escape_html(result['message'])}", parse_mode="HTML"
        )


async def cmd_upload_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    if "pending_upload" not in state:
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
    result = await asyncio.to_thread(
        upload_file_to_project, state["pending_upload"], target
    )

    if result["ok"]:
        if result.get("is_archive"):
            await status.edit_text(
                f"✅ Распаковано <b>{result['unpacked']}</b> файлов в "
                f"<code>{escape_html(result['target'])}</code>",
                parse_mode="HTML",
            )
        else:
            await status.edit_text(
                f"✅ Загружено: <code>{escape_html(result['target'])}</code>",
                parse_mode="HTML",
            )
        user_states[user_id] = {}
    else:
        await status.edit_text(
            f"❌ {escape_html(result['message'])}", parse_mode="HTML"
        )


# ============ ЗАПУСК ============

def main() -> None:
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(request).build()

    # Автопилот
    app.add_handler(CommandHandler("direction", cmd_direction))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot))
    app.add_handler(CommandHandler("stop", cmd_stop))

    # Очередь
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("loop", cmd_loop))
    app.add_handler(CommandHandler("clear", cmd_clear))

    # Codex
    app.add_handler(CommandHandler("luna", cmd_luna))
    app.add_handler(CommandHandler("build", cmd_build))

    # Файлы
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("download_project", cmd_download_project))
    app.add_handler(CommandHandler("upload_project", cmd_upload_project))
    app.add_handler(CommandHandler("upload_to", cmd_upload_to))

    # Прочее
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Вложения
    app.add_handler(MessageHandler(filters.Document.ALL, handle_attachment))

    logger.info("Бот запущен (Termux).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()