"""
Режим реплики.

Сервер, у которого НЕТ доступа к банку (например, нидерландская машина с ботом),
не ходит в Райффайзен сам, а раз в несколько минут забирает свежую копию базы
с главного сервера. Так токен банка обновляет только один сервер, а бот и сайт
видят одни и те же данные.

Включается переменными окружения (на главном сервере они не нужны, кроме REPLICA_TOKEN):
  REPLICA_TOKEN — общий секрет, одинаковый на обеих машинах (длинная случайная строка)
  REPLICA_OF    — только на реплике: адрес главного сервера, https://api.zavodzavodov.ru
"""
import os
import shutil
import sqlite3
import tempfile
import logging
import urllib.request

log = logging.getLogger("replica")


def replica_source() -> str:
    return os.getenv("REPLICA_OF", "").strip().rstrip("/")


def is_replica() -> bool:
    return bool(replica_source())


def make_snapshot() -> str:
    """Консистентная копия базы во временном файле, без токена банка. Возвращает путь."""
    from database import DB_PATH
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        try:
            # токен банка не должен уезжать на реплику — иначе она начнёт его обновлять
            dst.execute("DELETE FROM kv_store WHERE key='raiffeisen_token'")
            dst.commit()
        except sqlite3.Error:
            pass
    finally:
        dst.close()
        src.close()
    return tmp


def pull_db(fresh: bool = False, max_age: int = 300) -> dict:
    """Скачивает копию базы с главного сервера и атомарно подменяет локальную.
    fresh=True — главный сервер перед отдачей сам запрашивает выписку у банка,
    но только если последняя синхронизация старше max_age секунд."""
    from database import DB_PATH
    source = replica_source()
    token = os.getenv("REPLICA_TOKEN", "")
    if not source or not token:
        raise RuntimeError("Не заданы REPLICA_OF / REPLICA_TOKEN")

    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=db_dir, suffix=".pull")
    os.close(fd)
    try:
        req = urllib.request.Request(
            source + "/api/export-db" + (f"?fresh=1&max_age={int(max_age)}" if fresh else ""),
            headers={"X-Replica-Token": token, "User-Agent": "novator-replica"},
        )
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            bank_sync = r.headers.get("X-Bank-Sync", "")
            shutil.copyfileobj(r, f)

        size = os.path.getsize(tmp)
        with open(tmp, "rb") as f:
            head = f.read(16)
        if size < 1000 or head != b"SQLite format 3\x00":
            raise RuntimeError(f"Получен не файл базы (размер {size})")
        conn = sqlite3.connect(tmp)
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        if check != "ok":
            raise RuntimeError(f"Копия базы повреждена: {check}")

        os.replace(tmp, DB_PATH)
        log.info("Реплика: база обновлена с %s (%d байт)", source, size)
        return {"size": size, "bank_sync": bank_sync}
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
