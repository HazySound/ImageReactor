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
            print(f"[info] 죽은 프로세스 lock 제거됨 (PID={pid})")
            return False
    except Exception as e:
        print(f"[경고] lock 검사 실패: {e}")
        os.remove(LOCK_FILE)
        return False
