from __future__ import annotations

import argparse
import json
import logging
import mailbox
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE = "https://e.mail.ru"
LIST_PATH = "/api/v1/threads/status/smart"
THREAD_PATH = "/api/v1/threads/thread"
DOWNLOAD_PATH = "/api/v1/messages/message/download"
DEFAULT_LIST_PARAMS = {
    "limit": "50",
    "filters": "{}",
    "sort": '{"type":"date","order":"desc"}',
    "last_modified": "1",
    "force_custom_thread": "true",
    "with_thread_representations": "false",
    "supported_custom_metathreads": '["tomyself"]',
    "pinned_limit": "50",
    "remove_emoji_opts": '{"remove_from_sender_name":true,"remove_from_snippet":true,"remove_from_subject":true}',
    "offset": "0",
    "htmlencoded": "false",
}
DEFAULT_THREAD_PARAMS = {
    "quotes_version": "1",
    "offset": "0",
    "last_modified": "1",
    "force_custom_thread": "true",
    "use_color_scheme": "1",
    "remove_emoji_opts": '{"remove_from_sender_name":true,"remove_from_snippet":true,"remove_from_subject":true}',
    "disable_quotation_parser": "true",
    "no_wrap_links": "true",
    "disable_xshtml_parser": "true",
    "htmlencoded": "false",
}


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:100] or "Folder"


def make_url(path: str, params: dict[str, str]) -> str:
    return urlunsplit(("https", "e.mail.ru", path, urlencode(params), ""))


class SessionExpiredError(RuntimeError):
    """Raised when VK rejects a request made with the captured browser session."""


def parse_json_response(response, label: str) -> dict:
    if response.status in (401, 403):
        raise SessionExpiredError(
            f"{label}: HTTP {response.status}. Сессия VK истекла или API отклонил запрос. "
            "Остановите выгрузку, войдите в VK WorkSpace заново и повторите запуск."
        )
    if not response.ok:
        raise RuntimeError(f"{label}: HTTP {response.status} {response.status_text}")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"{label}: ответ не JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}: неожиданный формат JSON")
    return data


class ArchiveDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS folders(
            folder_id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT,
            folder_type TEXT, messages_total INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages(
            folder_id TEXT NOT NULL, message_id TEXT NOT NULL, thread_id TEXT,
            eml_path TEXT NOT NULL, size INTEGER NOT NULL, downloaded_at TEXT NOT NULL,
            PRIMARY KEY(folder_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS errors(
            id INTEGER PRIMARY KEY AUTOINCREMENT, folder_id TEXT, thread_id TEXT,
            message_id TEXT, error TEXT, created_at TEXT NOT NULL
        );
        """)
        self.db.commit()

    def save_folders(self, folders):
        for f in folders:
            self.db.execute(
                "INSERT INTO folders(folder_id,name,parent_id,folder_type,messages_total) VALUES(?,?,?,?,?) "
                "ON CONFLICT(folder_id) DO UPDATE SET name=excluded.name,parent_id=excluded.parent_id,folder_type=excluded.folder_type,messages_total=excluded.messages_total",
                (str(f.get("id")), f.get("name") or str(f.get("id")), str(f.get("parent", "-1")), f.get("type"), int(f.get("messages_total") or 0)),
            )
        self.db.commit()

    def exists(self, folder_id, message_id):
        return self.db.execute("SELECT 1 FROM messages WHERE folder_id=? AND message_id=?", (str(folder_id), str(message_id))).fetchone() is not None

    def add_message(self, folder_id, message_id, thread_id, path, size):
        self.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)", (str(folder_id), str(message_id), str(thread_id), str(path), int(size), datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def error(self, folder_id, thread_id, message_id, exc):
        self.db.execute("INSERT INTO errors(folder_id,thread_id,message_id,error,created_at) VALUES(?,?,?,?,?)", (str(folder_id), str(thread_id), str(message_id), str(exc)[:2000], datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def close(self):
        self.db.close()


def wait_for_list_request(page, timeout_ms=300000):
    captured = {"url": None}

    def on_request(req):
        if LIST_PATH in req.url and "access_token=" in req.url:
            captured["url"] = req.url

    page.on("request", on_request)
    print("\nВ браузере войди в VK WorkSpace, открой почту и дождись загрузки списка писем.")
    print("Скрипт ждёт авторизованный запрос списка (до 5 минут)...")
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline and not captured["url"]:
        page.wait_for_timeout(500)
    page.remove_listener("request", on_request)
    if not captured["url"]:
        raise RuntimeError("Не пойман запрос threads/status/smart. Открой папку почты и обнови её.")
    return captured["url"]


def get_template_params(captured_url, email):
    params = dict(parse_qsl(urlsplit(captured_url).query, keep_blank_values=True))
    params["email"] = email
    params["last_modified"] = "1"
    params["limit"] = params.get("limit", "50")
    params["htmlencoded"] = "false"
    return params


def request_json(context, path, params, label):
    url = make_url(path, params)
    response = context.request.get(url, headers={"Referer": "https://e.mail.ru/", "Accept": "application/json, text/plain, */*"}, timeout=60000)
    return parse_json_response(response, label)


def get_list(context, template, email, folder_id, offset):
    p = dict(template)
    p.update({"folder": str(folder_id), "offset": str(offset), "last_modified": "1", "email": email, "_": str(int(time.time() * 1000))})
    data = request_json(context, LIST_PATH, p, f"Список папки {folder_id}, offset={offset}")
    if data.get("status") in (401, 403):
        raise SessionExpiredError(
            f"Список папки {folder_id}: API status={data.get('status')}. "
            "Войдите в VK WorkSpace заново и повторите запуск."
        )
    if data.get("status") != 200 or not isinstance(data.get("body"), dict):
        raise RuntimeError(f"Список папки {folder_id}: API status={data.get('status')} (ожидали 200).")
    if data.get("email") and data.get("email").lower() != email.lower():
        raise RuntimeError(f"Авторизован другой ящик: API вернул {data.get('email')}, ожидался {email}.")
    return data["body"]


def get_thread(context, email, thread_id, auth_params):
    p = dict(DEFAULT_THREAD_PARAMS)
    # VK требует те же параметры авторизации, что и браузерный запрос.
    for key in ("access_token", "token"):
        if auth_params.get(key):
            p[key] = auth_params[key]
    p.update({"id": str(thread_id), "email": email, "_": str(int(time.time() * 1000))})
    data = request_json(context, THREAD_PATH, p, f"Переписка {thread_id}")
    if data.get("status") in (401, 403):
        raise SessionExpiredError(
            f"Переписка {thread_id}: API status={data.get('status')}. "
            "Сессия VK истекла или запрос отклонён; повторите запуск после входа в браузере."
        )
    if data.get("status") != 200 or not isinstance(data.get("body"), dict):
        raise RuntimeError(f"Переписка {thread_id}: API status={data.get('status')}")
    return data["body"]


def download_message(context, email, message_id, folder_id, auth_params):
    p = {"id": str(message_id), "folder_id": str(folder_id), "email": email, "htmlencoded": "false", "_": str(int(time.time() * 1000))}
    for key in ("access_token", "token"):
        if auth_params.get(key):
            p[key] = auth_params[key]
    url = make_url(DOWNLOAD_PATH, p)
    response = context.request.get(url, headers={"Referer": "https://e.mail.ru/", "Accept": "message/rfc822, */*"}, timeout=120000)
    if response.status in (401, 403):
        raise SessionExpiredError(
            f"Скачивание {message_id}: HTTP {response.status}. "
            "Войдите в VK WorkSpace заново и повторите запуск."
        )
    if not response.ok:
        raise RuntimeError(f"Скачивание {message_id}: HTTP {response.status}")
    raw = response.body()
    if len(raw) < 20 or not re.match(rb"^[!-9;-~]+:", raw[:512]):
        # Не записываем JSON/HTML ошибок под расширением .eml.
        preview = raw[:200].decode("utf-8", "replace")
        raise RuntimeError(f"Ответ {message_id} не похож на RFC822: {preview}")
    return raw


def build_mbox(eml_files, mbox_path: Path):
    mbox_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = mbox_path.with_suffix(mbox_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    box = mailbox.mbox(tmp, create=True)
    try:
        for path in sorted(eml_files):
            raw = path.read_bytes()
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            box.add(msg)
        box.flush()
    finally:
        box.close()
    tmp.replace(mbox_path)


def main():
    ap = argparse.ArgumentParser(description="Экспериментальный экспорт VK WorkSpace в EML + MBOX")
    ap.add_argument("--email", required=True, help="Адрес экспортируемого ящика")
    ap.add_argument("--out", required=True, type=Path, help="Каталог архива")
    ap.add_argument("--max-threads", type=int, default=0, help="Ограничить число переписок (для теста)")
    ap.add_argument("--max-messages", type=int, default=0, help="Ограничить число писем (для теста)")
    ap.add_argument("--limit", type=int, default=50, help="Размер страницы API (по HAR подтверждено 50)")
    ap.add_argument("--delay", type=float, default=0.25, help="Пауза между запросами, секунд")
    ap.add_argument("--headless", action="store_true", help="Не показывать браузер (только после проверки авторизации)")
    args = ap.parse_args()

    out = args.out.resolve()
    (out / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=out / "logs" / "archive.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    db = ArchiveDB(out / "archive.sqlite3")
    browser_profile = out / "browser-profile"
    browser_profile.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(browser_profile), headless=args.headless,
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://app.workspace.vk.ru/mail/inbox/", wait_until="domcontentloaded", timeout=90000)
            captured_url = wait_for_list_request(page)
            template = get_template_params(captured_url, args.email)
            template["limit"] = str(max(1, min(args.limit, 100)))
            logging.info("Authenticated list request captured for %s; secrets are not logged", args.email)

            # Первый ответ содержит дерево папок. Берём его без предположений о структуре интерфейса.
            first = get_list(context, template, args.email, "0", 0)
            folders = first.get("folders") or []
            if not folders:
                raise RuntimeError("API не вернул дерево папок.")
            db.save_folders(folders)
            print(f"\nПапок обнаружено: {len(folders)}")
            for f in folders:
                print(f"  [{f.get('id')}] {f.get('name')} — {f.get('messages_total', 0)} сообщений")

            # Учитываем все системные и пользовательские папки, включая вложенные.
            total_downloaded = 0
            total_threads_processed = 0
            for folder in folders:
                folder_id = str(folder.get("id"))
                folder_name = safe_name(folder.get("name") or folder_id)
                expected = int(folder.get("messages_total") or 0)
                if expected == 0:
                    continue
                print(f"\nПапка: {folder_name} (ID {folder_id}), сообщений по счётчику: {expected}")
                folder_eml_dir = out / "eml" / f"{folder_name}__{folder_id}"
                folder_eml_dir.mkdir(parents=True, exist_ok=True)
                offset = 0
                seen_threads = 0
                while True:
                    listing = first if folder_id == "0" and offset == 0 else get_list(context, template, args.email, folder_id, offset)
                    threads = listing.get("threads") or []
                    if not threads:
                        break
                    for thread in threads:
                        if args.max_threads and total_threads_processed >= args.max_threads:
                            break
                        thread_id = thread.get("id")
                        if not thread_id:
                            continue
                        seen_threads += 1
                        total_threads_processed += 1
                        try:
                            thread_data = get_thread(context, args.email, thread_id, template)
                            messages = thread_data.get("messages") or []
                            for msg in messages:
                                message_id = str(msg.get("id") or "")
                                if not message_id:
                                    continue
                                if db.exists(folder_id, message_id):
                                    continue
                                if args.max_messages and total_downloaded >= args.max_messages:
                                    break
                                raw = download_message(context, args.email, message_id, folder_id, template)
                                eml_path = folder_eml_dir / f"{safe_name(message_id)}.eml"
                                eml_path.write_bytes(raw)
                                db.add_message(folder_id, message_id, thread_id, eml_path, len(raw))
                                total_downloaded += 1
                                if total_downloaded % 10 == 0 or total_downloaded <= 3:
                                    print(f"  Скачано писем: {total_downloaded}")
                                time.sleep(args.delay)
                            time.sleep(args.delay)
                        except SessionExpiredError:
                            raise
                        except Exception as exc:
                            db.error(folder_id, thread_id, "", exc)
                            logging.exception("Thread failed folder=%s thread=%s", folder_id, thread_id)
                            print(f"  ОШИБКА переписки {thread_id}: {exc}")
                        if args.max_messages and total_downloaded >= args.max_messages:
                            break
                    if (args.max_threads and total_threads_processed >= args.max_threads) or (args.max_messages and total_downloaded >= args.max_messages):
                        break
                    offset += len(threads)
                    if len(threads) < int(template["limit"]):
                        break
                if (args.max_threads and total_threads_processed >= args.max_threads) or (args.max_messages and total_downloaded >= args.max_messages):
                    break

                # Пересобираем MBOX конкретной папки из всех сохранённых EML.
                files = list(folder_eml_dir.glob("*.eml"))
                if files:
                    build_mbox(files, out / "mbox" / f"{folder_name}__{folder_id}.mbox")

            # Собрать MBOX и для папок, которые были частично выгружены перед остановкой.
            for folder_dir in (out / "eml").iterdir() if (out / "eml").exists() else []:
                if folder_dir.is_dir():
                    files = list(folder_dir.glob("*.eml"))
                    if files:
                        build_mbox(files, out / "mbox" / f"{folder_dir.name}.mbox")
            print(f"\nГотово. Новых оригиналов EML скачано: {total_downloaded}")
            print(f"Архив: {out}")
            print("Браузерную сессию можно оставить для следующего запуска либо закрыть окно.")
            context.close()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем. Прогресс сохранён в SQLite; повторный запуск продолжит выгрузку.")
    except SessionExpiredError as exc:
        logging.warning("VK session rejected a request; export paused: %s", exc)
        print(f"\nПАУЗА: {exc}")
        print("Уже скачанные EML и состояние SQLite сохранены. После повторного входа запустите ту же команду.")
        sys.exit(2)
    except Exception as exc:
        logging.exception("Fatal error")
        print(f"\nОШИБКА: {exc}")
        sys.exit(1)
    finally:
        # Keep MBOX files usable even if the run is interrupted or the session expires.
        try:
            eml_root = out / "eml"
            if eml_root.exists():
                for folder_dir in eml_root.iterdir():
                    if folder_dir.is_dir():
                        files = list(folder_dir.glob("*.eml"))
                        if files:
                            build_mbox(files, out / "mbox" / f"{folder_dir.name}.mbox")
        except Exception:
            logging.exception("Could not rebuild one or more MBOX files")
        db.close()


if __name__ == "__main__":
    main()
