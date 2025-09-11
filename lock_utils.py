# lock_utils.py
import os
import psutil

LOCK_FILE = "routine.lock"


def create_lock():
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)


def release_lock(lock_path: str):
    try:
        import os
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception:
        pass


def acquire_lock(lock_path: str) -> bool:
    """
    단일 실행 보장. stale은 조용히 청소.
    """
    import os, json, time
    silent_cleanup_stale_lock(lock_path)
    if os.path.exists(lock_path):
        return False
    try:
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "ts": time.time()}))
        return True
    except Exception:
        return False


def check_stale_lock():
    if not os.path.exists(LOCK_FILE):
        return False  # 잠금 없음

    try:
        with open(LOCK_FILE, "r") as f:
            pid = int(f.read().strip())

        if psutil.pid_exists(pid):
            return True  # 여전히 실행 중
        else:
            os.remove(LOCK_FILE)
            return False
    except Exception as e:
        print(f"[경고] lock 검사 실패: {e}")
        os.remove(LOCK_FILE)
        return False


def silent_cleanup_stale_lock(lock_path: str, pid_getter=None):
    """
    stale lock 발견 시 아무 메시지도 남기지 않고 조용히 정리.
    """
    import os, json, psutil
    try:
        if not os.path.exists(lock_path):
            return
        data = None
        try:
            data = json.loads(open(lock_path, "r", encoding="utf-8").read())
        except Exception:
            # 손상됐으면 그냥 삭제
            os.remove(lock_path)
            return
        pid = int(data.get("pid", -1))
        if pid <= 0:
            os.remove(lock_path)
            return
        # 프로세스 존재 여부
        if not psutil.pid_exists(pid):
            os.remove(lock_path)
            return
        # 같은 앱인지 더 확인할 수도 있으나, 여기선 침묵 유지
    except Exception:
        # 어떤 예외도 노출하지 않음
        pass