"""Helpers for the pseudo-terminal login tests (S-07)."""

import queue
import threading
import time


class SerialDispatcher:
    """Runs dispatched calls one at a time, in order, on one worker thread, like
    GLib.idle_add on the main loop. `drain()` waits until everything queued ran."""

    def __init__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="test-dispatch", daemon=True)
        self._thread.start()

    def __call__(self, fn, *args):
        self._queue.put((fn, args))

    def _work(self):
        while True:
            fn, args = self._queue.get()
            try:
                fn(*args)
            finally:
                self._queue.task_done()

    def drain(self):
        self._queue.join()


class ToolsAt:
    """Tool locator for tests: only the given tools, at the given paths."""

    def __init__(self, **paths):
        self.paths = paths

    def locate(self, tool):
        return self.paths.get(tool.value)

    def search_directories(self):
        return ["/usr/bin", "/bin"]


def wait_until(predicate, timeout=10.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
