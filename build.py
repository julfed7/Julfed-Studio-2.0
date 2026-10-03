import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime
from pathlib import Path

from config import CODEX_PATH, CONFIG, DEFAULT_PROJECT_PATH

PROJECT_PATH = Path(
    CONFIG["projects"].get(
        CONFIG.get("current_project", "default"),
        DEFAULT_PROJECT_PATH,
    )
)
CODEX_PATH = str(CODEX_PATH)

EXPORT_PRESETS = PROJECT_PATH / "export_presets.cfg"
ATTACHMENTS_DIR = Path(tempfile.gettempdir()) / "studio_attachments"
OUTPUT_DIR = PROJECT_PATH.parent / "codex_output"

QUEUE_FILE = Path(__file__).parent / "task_queue.json"
LOOP_STATE_FILE = Path(__file__).parent / "loop_state.json"
DIRECTION_FILE = Path(__file__).parent / "direction.txt"
HISTORY_FILE = Path(__file__).parent / "task_history.json"

IGNORE_DIRS = {
    ".git", ".godot", "node_modules", "__pycache__",
    ".venv", "venv", "builds", "output_builds", "codex_output",
    ".import", ".cache",
}

SEND_EXTS = {
    ".gd", ".tscn", ".tres", ".godot",
    ".png", ".jpg", ".jpeg", ".svg", ".webp",
    ".json", ".txt", ".md", ".cfg", ".ini",
    ".wav", ".ogg", ".mp3",
    ".zip", ".js", ".ts",
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
TEXT_PROMPT_EXTS = {".txt", ".md", ".gd", ".py", ".json", ".cfg", ".ini", ".tscn", ".tres"}

MAX_FILE_SIZE = 20 * 1024 * 1024


def log(msg: str) -> None:
    print(f"[BUILD] {msg}", flush=True)


# ============ GIT ============

def git_commit_push(message: str) -> tuple:
    """Git add + commit + push с retry и авто-pull."""
    try:
        # git add
        r0 = subprocess.run(
            ["git", "add", "-A"],
            cwd=str(PROJECT_PATH),
            capture_output=True, text=True, timeout=120,
        )
        log(f"[git add] rc={r0.returncode}")
        if r0.returncode != 0:
            log(f"[git add] stderr: {(r0.stderr or '')[-300:]}")

        # git commit
        r1 = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(PROJECT_PATH),
            capture_output=True, text=True, timeout=120,
        )
        combined1 = (r1.stdout or "") + (r1.stderr or "")
        log(f"[git commit] rc={r1.returncode}")
        log(f"[git commit] {combined1[-300:]}")

        if r1.returncode != 0 and "nothing to commit" in combined1.lower():
            return (True, "nothing to commit")

        # git push — 3 попытки
        for attempt in range(3):
            log(f"[git push] попытка {attempt + 1}/3...")
            try:
                r = subprocess.run(
                    ["git", "push"],
                    cwd=str(PROJECT_PATH),
                    capture_output=True, text=True, timeout=900,
                )
                combined = (r.stdout or "") + (r.stderr or "")
                log(f"[git push] rc={r.returncode}")
                log(f"[git push] {combined[-500:]}")

                if r.returncode == 0:
                    return (True, combined)

                # Конфликт — pull и повторить
                if "rejected" in combined.lower() or "non-fast-forward" in combined.lower():
                    log("[git push] конфликт, делаю pull --rebase...")
                    subprocess.run(
                        ["git", "pull", "--rebase", "--no-edit"],
                        cwd=str(PROJECT_PATH),
                        capture_output=True, text=True, timeout=300,
                    )
            except subprocess.TimeoutExpired:
                log(f"[git push] таймаут попытки {attempt + 1}")
            except Exception as e:
                log(f"[git push] ошибка: {e}")

            if attempt < 2:
                time.sleep(10)

        return (False, "git push failed after 3 attempts")

    except Exception as e:
        return (False, str(e))


# ============ GITHUB API ============

def _gh_request(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_latest_apk(repo: str, token: str, out_path: Path) -> bool:
    try:
        releases = _gh_request(f"https://api.github.com/repos/{repo}/releases", token)
        if not releases:
            return False
        latest = releases[0]
        for asset in latest.get("assets", []):
            if asset["name"].endswith(".apk"):
                req = urllib.request.Request(asset["browser_download_url"])
                req.add_header("Authorization", f"token {token}")
                with urllib.request.urlopen(req, timeout=180) as resp:
                    out_path.write_bytes(resp.read())
                return True
        return False
    except Exception as e:
        log(f"[GH] ошибка: {e}")
        return False


def get_latest_release_tag(repo: str, token: str) -> str:
    try:
        releases = _gh_request(f"https://api.github.com/repos/{repo}/releases", token)
        return releases[0]["tag_name"] if releases else ""
    except Exception:
        return ""


def wait_for_new_release(repo: str, token: str, prev_tag: str, timeout_sec: int = 900) -> str:
    start = time.time()
    while time.time() - start < timeout_sec:
        tag = get_latest_release_tag(repo, token)
        if tag and tag != prev_tag:
            return tag
        time.sleep(20)
    return ""


# ============ ОЧЕРЕДЬ ============

def load_queue() -> list:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_queue(queue: list) -> None:
    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")


def enqueue_task(prompt: str, images: list = None) -> int:
    q = load_queue()
    new_id = max([t["id"] for t in q], default=0) + 1
    q.append({
        "id": new_id,
        "prompt": prompt,
        "images": images or [],
        "status": "pending",
        "created": datetime.now().isoformat(),
    })
    save_queue(q)
    return new_id


def pop_task() -> dict:
    q = load_queue()
    for t in q:
        if t.get("status") == "pending":
            t["status"] = "in_progress"
            save_queue(q)
            return t
    return {}


def complete_task(task_id: int) -> None:
    q = load_queue()
    for t in q:
        if t.get("id") == task_id:
            t["status"] = "done"
    save_queue(q)


def clear_queue() -> None:
    save_queue([])


def set_loop_state(active: bool) -> None:
    LOOP_STATE_FILE.write_text(json.dumps({"active": active}), encoding="utf-8")


def get_loop_state() -> bool:
    if LOOP_STATE_FILE.exists():
        try:
            return bool(json.loads(LOOP_STATE_FILE.read_text(encoding="utf-8")).get("active", False))
        except Exception:
            return False
    return False


def set_direction(text: str) -> None:
    DIRECTION_FILE.write_text(text, encoding="utf-8")


def get_direction() -> str:
    if DIRECTION_FILE.exists():
        return DIRECTION_FILE.read_text(encoding="utf-8").strip()
    return ""


def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def add_to_history(prompt: str) -> None:
    h = load_history()
    h.append({"prompt": prompt, "date": datetime.now().isoformat()})
    h = h[-50:]
    HISTORY_FILE.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")


def generate_next_task() -> str:
    direction = get_direction()
    if not direction:
        return ""
    history = load_history()
    recent = "\n".join(f"- {h['prompt']}" for h in history[-10:]) or "(пусто)"

    meta = f"""Ты геймдизайнер Godot-игры (2D top-down).

НАПРАВЛЕНИЕ: {direction}

СДЕЛАНО:
{recent}

Придумай ОДНУ конкретную задачу под направление. Ответь РОВНО одной строкой без кавычек."""

    try:
        r = run_codex_only(meta, None)
        ans = r.get("answer", "").strip()
        for line in ans.split("\n"):
            line = line.strip().strip('"').strip("-").strip()
            if len(line) > 10 and not line.startswith("["):
                return line
        return ""
    except Exception as e:
        log(f"generate: {e}")
        return ""


# ============ ПОИСК ФАЙЛОВ ============

def find_files_by_pattern(pattern: str) -> list:
    results = []
    if "*" in pattern or "?" in pattern:
        for root, dirs, files in os.walk(PROJECT_PATH):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in files:
                p = Path(root) / f
                try:
                    rel = p.relative_to(PROJECT_PATH)
                except ValueError:
                    continue
                if Path(rel).match(pattern) or Path(f).match(pattern):
                    results.append(p)
    else:
        c = PROJECT_PATH / pattern
        if c.exists() and c.is_file():
            results.append(c)
        else:
            for root, dirs, files in os.walk(PROJECT_PATH):
                dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
                for f in files:
                    if f == pattern:
                        results.append(Path(root) / f)
    return results


def pack_files_to_zip(files: list, zip_name: str = None) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    if zip_name is None:
        zip_name = f"output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    zp = OUTPUT_DIR / zip_name
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            try:
                zf.write(f, arcname=str(f.relative_to(PROJECT_PATH)))
            except Exception:
                zf.write(f, arcname=f.name)
    return zp


def pack_whole_project() -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    zp = OUTPUT_DIR / f"project_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(PROJECT_PATH):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in files:
                p = Path(root) / f
                try:
                    zf.write(p, arcname=str(p.relative_to(PROJECT_PATH)))
                except Exception:
                    pass
    return zp


def list_all_files() -> list:
    out = []
    for root, dirs, files in os.walk(PROJECT_PATH):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in SEND_EXTS:
                try:
                    out.append(str(p.relative_to(PROJECT_PATH)))
                except ValueError:
                    pass
    return sorted(out)


def find_files_in_dir(rel_dir: str) -> list:
    target = PROJECT_PATH / rel_dir
    if not target.exists():
        return []
    out = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in SEND_EXTS:
                out.append(p)
    return out


# ============ UPLOAD ============

def _safe_extract_zip(zip_path: Path, extract_dir: Path) -> int:
    extract_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            mp = (extract_dir / member).resolve()
            if not str(mp).startswith(str(extract_dir.resolve())):
                continue
            zf.extract(member, extract_dir)
            count += 1
    return count


def upload_file_to_project(local_path: str, target_rel: str) -> dict:
    src = Path(local_path)
    if not src.exists():
        return {"ok": False, "message": "Файл не найден"}
    target_rel = target_rel.strip().lstrip("/").rstrip("/")
    target = PROJECT_PATH / target_rel
    if src.suffix.lower() == ".zip":
        extract_dir = target.parent if target.suffix.lower() == ".zip" else target
        try:
            count = _safe_extract_zip(src, extract_dir)
            return {"ok": True, "target": str(extract_dir.relative_to(PROJECT_PATH)),
                    "unpacked": count, "is_archive": True, "size_kb": 0}
        except Exception as e:
            return {"ok": False, "message": str(e)}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)
        return {"ok": True, "target": str(target.relative_to(PROJECT_PATH)),
                "size_kb": target.stat().st_size // 1024, "unpacked": 0, "is_archive": False}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def upload_project_zip(local_path: str) -> dict:
    src = Path(local_path)
    if not src.exists() or src.suffix.lower() != ".zip":
        return {"ok": False, "message": "Нужен ZIP"}
    try:
        count = _safe_extract_zip(src, PROJECT_PATH)
        return {"ok": True, "unpacked": count}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ============ CODEX ============

def _extract_codex_answer(raw: str) -> str:
    for marker in ["\ncodex\n", "\nassistant\n"]:
        idx = raw.find(marker)
        if idx != -1:
            start = idx + len(marker)
            for end_marker in ["\ntokens used", "\n---"]:
                e = raw.find(end_marker, start)
                if e != -1:
                    return raw[start:e].strip()
            return raw[start:].strip()
    return raw.strip()


def _extract_tokens(raw: str) -> int:
    m = re.search(r"tokens used\s*\n?\s*([\d\s,]+)", raw)
    if not m:
        return 0
    d = re.sub(r"[^\d]", "", m.group(1))
    return int(d) if d else 0


def _run_codex(prompt: str, image_paths: list = None) -> dict:
    model = CONFIG.get("model", "gpt-5.6-luna")
    effort = CONFIG.get("effort", "high")

    cmd = [
        CODEX_PATH, "exec",
        "--skip-git-repo-check",
        "-s", "workspace-write",
        "--model", model,
        "--config", f'model_reasoning_effort="{effort}"',
    ]

    if image_paths:
        for img in image_paths:
            if Path(img).exists():
                cmd.extend(["--image", str(img)])

    cmd.append(prompt)

    log(f"run_codex: {' '.join(cmd[:8])} ...")
    log(f"run_codex: images = {len(image_paths) if image_paths else 0}")

    try:
        r = subprocess.run(
            cmd, cwd=str(PROJECT_PATH),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=7200,
        )
    except subprocess.TimeoutExpired:
        return {"answer": "❌ Таймаут 2 часа", "tokens": 0, "ok": False, "files": []}
    except Exception as e:
        return {"answer": f"❌ {e}", "tokens": 0, "ok": False, "files": []}

    if r.returncode != 0:
        return {"answer": f"❌ код {r.returncode}\n{(r.stderr or '')[-500:]}",
                "tokens": 0, "ok": False, "files": []}

    return {
        "answer": _extract_codex_answer(r.stdout or ""),
        "tokens": _extract_tokens(r.stdout or ""),
        "ok": True,
        "files": [],
    }


def run_codex(prompt: str, image_paths: list = None) -> dict:
    return _run_codex(prompt, image_paths)


def run_codex_only(prompt: str, image_paths: list = None) -> dict:
    return _run_codex(prompt, image_paths)


# ============ ВЛОЖЕНИЯ ============

async def download_attachment_async(file_obj, custom_name: str = None) -> Path:
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    if custom_name is None:
        custom_name = f"att_{file_obj.file_unique_id}"
    lp = ATTACHMENTS_DIR / custom_name
    await file_obj.download_to_drive(custom_path=str(lp))
    return lp


def classify_attachment(name: str) -> str:
    """Возвращает: 'image', 'video', 'text_prompt', 'zip', 'other'."""
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext == ".zip":
        return "zip"
    if ext in TEXT_PROMPT_EXTS:
        return "text_prompt"
    return "other"


def read_text_prompt(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log(f"read_text_prompt: {e}")
        return ""


# ============ ЗАГЛУШКИ ============

def get_current_version() -> tuple:
    return (0, "auto")

def increment_version() -> tuple:
    return (0, "auto")

def build_apk() -> Path:
    raise RuntimeError("На Termux — только GitHub Actions.")