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

def ensure_git_config() -> bool:
    try:
        name = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        email = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()

        if not name:
            subprocess.run(
                ["git", "config", "--global", "user.name", "julfed7"],
                capture_output=True, text=True, timeout=10,
            )
            log("[git] установлен user.name")

        if not email:
            subprocess.run(
                ["git", "config", "--global", "user.email",
                 "xxxlolxxx338@gmail.com"],
                capture_output=True, text=True, timeout=10,
            )
            log("[git] установлен user.email")

        return True
    except Exception as e:
        log(f"[git] ошибка: {e}")
        return False


def git_commit_push(message: str) -> tuple:
    try:
        ensure_git_config()

        r0 = subprocess.run(
            ["git", "add", "-A"],
            cwd=str(PROJECT_PATH),
            capture_output=True, text=True, timeout=120,
        )
        log(f"[git add] rc={r0.returncode}")
        if r0.returncode != 0:
            return (False, f"git add failed: {(r0.stderr or '')[-300:]}")

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(PROJECT_PATH),
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if not status:
            log("[git] изменений нет")
            return (False, "nothing to commit — Codex ничего не изменил")

        r1 = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(PROJECT_PATH),
            capture_output=True, text=True, timeout=120,
        )
        combined1 = (r1.stdout or "") + (r1.stderr or "")
        log(f"[git commit] rc={r1.returncode}")
        log(f"[git commit] {combined1[-300:]}")

        if r1.returncode != 0:
            return (False, f"git commit failed: {combined1[-300:]}")

        for attempt in range(3):
            log(f"[git push] попытка {attempt + 1}/3...")
            try:
                gh_repo = os.getenv("GITHUB_REPO", "")
                gh_token = os.getenv("GITHUB_TOKEN", "")
                if gh_repo and gh_token:
                    push_cmd = [
                        "git", "-c", "credential.helper=", "push",
                        f"https://x-access-token:{gh_token}@github.com/{gh_repo}.git",
                        "HEAD",
                    ]
                else:
                    push_cmd = ["git", "push"]
                r = subprocess.run(
                    push_cmd,
                    cwd=str(PROJECT_PATH),
                    capture_output=True, text=True, timeout=180,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                combined_safe = (r.stdout or "") + (r.stderr or "")
                if gh_token:
                    combined_safe = combined_safe.replace(gh_token, "***")
                combined = combined_safe
                log(f"[git push] rc={r.returncode}")
                log(f"[git push] {combined[-500:]}")

                if r.returncode == 0:
                    return (True, combined)

                if "rejected" in combined.lower() or "non-fast-forward" in combined.lower():
                    log("[git push] конфликт, pull --rebase...")
                    if gh_repo and gh_token:
                        br = subprocess.run(
                            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=str(PROJECT_PATH),
                            capture_output=True, text=True, timeout=30,
                        ).stdout.strip() or "main"
                        pull_cmd = [
                            "git", "-c", "credential.helper=", "pull", "--rebase", "--no-edit",
                            f"https://x-access-token:{gh_token}@github.com/{gh_repo}.git", br,
                        ]
                    else:
                        pull_cmd = ["git", "pull", "--rebase", "--no-edit"]
                    pr = subprocess.run(
                        pull_cmd,
                        cwd=str(PROJECT_PATH),
                        capture_output=True, text=True, timeout=300,
                        stdin=subprocess.DEVNULL,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                    pr_out = (pr.stdout or "") + (pr.stderr or "")
                    if gh_token:
                        pr_out = pr_out.replace(gh_token, "***")
                    log(f"[git pull] rc={pr.returncode} {pr_out[-300:]}")
                    if pr.returncode != 0:
                        subprocess.run(
                            ["git", "rebase", "--abort"],
                            cwd=str(PROJECT_PATH),
                            capture_output=True, text=True, timeout=30,
                        )
                        return (False, "Конфликт при pull --rebase, нужно решить вручную:\n" + pr_out[-300:])
            except subprocess.TimeoutExpired:
                log(f"[git push] таймаут {attempt + 1}")
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


def _apk_asset(release: dict):
    for asset in release.get("assets", []) or []:
        if asset.get("name", "").endswith(".apk"):
            return asset
    return None


def _build_number(tag: str) -> int:
    nums = re.findall(r"\d+", tag or "")
    return int(nums[-1]) if nums else -1


def _parse_gh_time(value: str) -> float:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=__import__("datetime").timezone.utc
        ).timestamp()
    except Exception:
        return 0.0


def _release_key(rel: dict):
    return (
        _build_number(rel.get("tag_name", "")),
        _parse_gh_time(rel.get("published_at") or rel.get("created_at") or ""),
    )


def _latest_releases(repo: str, token: str) -> list:
    """Релизы от новых к старым: по номеру сборки в теге, потом по дате публикации.
    Порядок GitHub API не используем: он может быть неверным."""
    try:
        data = _gh_request(f"https://api.github.com/repos/{repo}/releases?per_page=100", token)
        releases = [r for r in data if isinstance(r, dict) and not r.get("draft")] if isinstance(data, list) else []
        return sorted(releases, key=_release_key, reverse=True)
    except Exception as e:
        log(f"[GH] не удалось получить релизы: {e}")
        return []


def download_latest_apk(repo: str, token: str, out_path: Path, tag: str = None) -> bool:
    try:
        releases = _latest_releases(repo, token)
        if tag:
            releases = [r for r in releases if r.get("tag_name") == tag] + [
                r for r in releases if r.get("tag_name") != tag
            ]
        for rel in releases:
            asset = _apk_asset(rel)
            if not asset:
                continue
            req = urllib.request.Request(asset["url"])
            req.add_header("Authorization", f"token {token}")
            req.add_header("Accept", "application/octet-stream")
            with urllib.request.urlopen(req, timeout=300) as resp:
                out_path.write_bytes(resp.read())
            log(f"[GH] скачан APK из релиза {rel.get('tag_name')}")
            return True
        log("[GH] ни в одном релизе нет .apk")
        return False
    except Exception as e:
        log(f"[GH] ошибка скачивания APK: {e}")
        return False


def get_latest_release_tag(repo: str, token: str) -> str:
    releases = _latest_releases(repo, token)
    return releases[0]["tag_name"] if releases else ""


def wait_for_new_release(repo: str, token: str, prev_tag: str, timeout_sec: int = 900) -> str:
    start = time.time()
    prev_num = _build_number(prev_tag)
    while time.time() - start < timeout_sec:
        for rel in _latest_releases(repo, token):  # от самого нового номера
            if not _apk_asset(rel):
                continue
            tag = rel.get("tag_name", "")
            published = _release_key(rel)[1]
            newer_num = _build_number(tag) > prev_num >= 0
            if newer_num or tag != prev_tag or published >= start - 120:
                log(f"[GH] найден релиз {tag} с APK")
                return tag
        time.sleep(20)
    return ""


def trigger_build_workflow(repo: str, token: str, workflow_file: str = "release.yml") -> bool:
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    data = json.dumps({
        "ref": "main",
        "inputs": {"reason": "Build from Telegram bot"},
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"[GH] dispatch: {resp.status}")
            return resp.status == 204
    except Exception as e:
        log(f"[GH] dispatch ошибка: {e}")
        return False


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


# ============ ЛИМИТЫ / РАСХОД ============

USAGE_FILE = Path(__file__).parent / "usage_stats.json"
CREDITS_FILE = Path(__file__).parent / "credits_report.json"


def _load_usage() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"total": 0, "runs": 0, "by_model": {}, "by_day": {}}


def record_usage(model: str, tokens: int) -> None:
    if tokens <= 0:
        return
    try:
        u = _load_usage()
        day = datetime.now().strftime("%Y-%m-%d")
        u["total"] = u.get("total", 0) + tokens
        u["period_spent"] = u.get("period_spent", 0) + tokens
        u.setdefault("period_start", datetime.now().strftime("%Y-%m-%d %H:%M"))
        u["runs"] = u.get("runs", 0) + 1
        now_ts = time.time()
        ev = u.setdefault("events", [])
        ev.append({"t": now_ts, "tokens": tokens, "model": model})
        u["events"] = [e for e in ev if now_ts - e.get("t", 0) < 8 * 86400]
        u.setdefault("by_model", {})[model] = u.setdefault("by_model", {}).get(model, 0) + tokens
        u.setdefault("by_day", {})[day] = u.setdefault("by_day", {}).get(day, 0) + tokens
        # храним только последние 14 дней
        for d in sorted(u["by_day"])[:-14]:
            u["by_day"].pop(d, None)
        USAGE_FILE.write_text(json.dumps(u, indent=2, ensure_ascii=False), encoding="utf-8")
        write_credits_snapshot()
    except Exception as e:
        log(f"[usage] не удалось записать: {e}")


WINDOW_5H = 5 * 3600


def get_5h_window() -> dict:
    """Текущее 5-часовое окно: начинается с первого запуска после окончания прошлого окна."""
    events = sorted(_load_usage().get("events", []), key=lambda e: e.get("t", 0))
    start, used = None, 0
    for e in events:
        t = e.get("t", 0)
        if start is None or t >= start + WINDOW_5H:
            start, used = t, 0
        used += int(e.get("tokens", 0))
    now = time.time()
    if start is None or now >= start + WINDOW_5H:
        return {"active": False, "used": 0, "start": None, "resets_in": 0}
    return {"active": True, "used": used, "start": start, "resets_in": int(start + WINDOW_5H - now)}


def get_usage_summary() -> dict:
    u = _load_usage()
    day = datetime.now().strftime("%Y-%m-%d")
    limit = int(CONFIG.get("credit_limit", 0) or 0)
    spent = int(u.get("period_spent", 0))
    percent = round(spent / limit * 100, 1) if limit > 0 else None
    w5 = get_5h_window()
    lim5 = int(CONFIG.get("limit_5h", 0) or 0)
    p5 = round(w5["used"] / lim5 * 100, 1) if lim5 > 0 else None
    return {
        "w5_active": w5["active"],
        "w5_used": w5["used"],
        "w5_limit": lim5,
        "w5_percent_used": p5,
        "w5_percent_left": round(max(0.0, 100 - p5), 1) if p5 is not None else None,
        "w5_remaining": max(0, lim5 - w5["used"]) if lim5 > 0 else None,
        "w5_resets_in": w5["resets_in"],
        "total": u.get("total", 0),
        "runs": u.get("runs", 0),
        "today": u.get("by_day", {}).get(day, 0),
        "by_model": u.get("by_model", {}),
        "limit": limit,
        "period_spent": spent,
        "period_start": u.get("period_start", ""),
        "percent_used": percent,
        "percent_left": round(max(0.0, 100 - percent), 1) if percent is not None else None,
        "remaining": max(0, limit - spent) if limit > 0 else None,
    }


def write_credits_snapshot() -> dict:
    """Сохраняет текущую сводку расхода в credits_report.json."""
    snap = get_usage_summary()
    snap["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        CREDITS_FILE.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log(f"[credits] не удалось записать отчёт: {e}")
    return snap


def reset_credit_period() -> None:
    u = _load_usage()
    u["period_spent"] = 0
    u["period_start"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        USAGE_FILE.write_text(json.dumps(u, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log(f"[usage] не удалось сбросить: {e}")
    write_credits_snapshot()


def get_codex_limits() -> dict:
    """Лимиты ChatGPT-подписки из последнего лога сессии Codex (если они там есть)."""
    base = Path(os.path.expanduser("~/.codex/sessions"))
    if not base.exists():
        return {}
    try:
        files = sorted(base.rglob("rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    except Exception:
        return {}
    for f in files[:5]:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            if "rate_limits" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            payload = obj.get("payload", obj)
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            if rl:
                return {"limits": rl, "file_time": f.stat().st_mtime}
    return {}



def _run_codex(prompt: str, image_paths: list = None, model: str = None, effort: str = None) -> dict:
    model = model or CONFIG.get("model", "gpt-5.6-luna")
    effort = effort or CONFIG.get("effort", "high")

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

    tokens_used = _extract_tokens(r.stdout or "")
    record_usage(model, tokens_used)
    return {
        "answer": _extract_codex_answer(r.stdout or ""),
        "tokens": tokens_used,
        "ok": True,
        "files": [],
    }


def run_codex(prompt: str, image_paths: list = None, model: str = None, effort: str = None) -> dict:
    return _run_codex(prompt, image_paths, model, effort)


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
