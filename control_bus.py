# control_bus.py
import threading

start_event = threading.Event()
stop_event = threading.Event()
exit_event = threading.Event()


def request_start(): start_event.set()


def request_stop():  stop_event.set()


def request_exit():  exit_event.set()


def clear_all():
    start_event.clear()
    stop_event.clear()
    exit_event.clear()
