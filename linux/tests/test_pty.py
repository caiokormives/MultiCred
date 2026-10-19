"""PseudoTerminalProcess (port of PseudoTerminalProcessTests.swift).

Uses only /bin/sh with inline scripts; never a real claude or gh.
"""

import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path

from multicred.core.pty import PseudoTerminalProcess, PTYError

from tests.login_support import wait_until

PATH_ONLY = {"PATH": "/usr/bin:/bin"}


class Collector:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = bytearray()

    def append(self, chunk):
        with self._lock:
            self._data.extend(chunk)

    @property
    def text(self):
        with self._lock:
            return self._data.decode("utf-8", "replace")


def process_gone(pid):
    """True when `pid` no longer runs (absent, or a zombie waiting for its new parent)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return True
    return state in ("Z", "X")


class PseudoTerminalProcessTests(unittest.TestCase):
    def start(self, script, environment=None, cwd=None, columns=100, rows=24):
        pty = PseudoTerminalProcess()
        output = Collector()
        exited = threading.Event()
        status = {}

        def on_exit(code):
            status["code"] = code
            exited.set()

        pty.start("/bin/sh", ["-c", script], PATH_ONLY if environment is None else environment, cwd=cwd,
                  columns=columns, rows=rows, on_output=output.append, on_exit=on_exit)
        self.addCleanup(pty.terminate)
        return pty, output, exited, status

    def run_script(self, script, input_text=None, input_delay=0.3, timeout=10, **kwargs):
        pty, output, exited, status = self.start(script, **kwargs)
        if input_text is not None:
            threading.Timer(input_delay, pty.write, [input_text.encode()]).start()
        self.assertTrue(exited.wait(timeout), f"no exit; output so far: {output.text!r}")
        return output.text, status.get("code")

    def test_child_sees_a_terminal_with_the_window_size(self):
        text, code = self.run_script("[ -t 0 ] && [ -t 1 ] && echo tty-ok; stty size")
        self.assertEqual(code, 0)
        self.assertIn("tty-ok", text)
        self.assertIn("24 100", text, "window size is applied")

    def test_terminal_is_the_controlling_terminal_of_a_new_session(self):
        script = ("sid=$(cut -d' ' -f6 /proc/$$/stat); [ \"$sid\" = \"$$\" ] && echo leader-ok; "
                  "exec 3</dev/tty && echo ctty-ok")
        text, code = self.run_script(script)
        self.assertEqual(code, 0, text)
        self.assertIn("leader-ok", text)
        self.assertIn("ctty-ok", text, "/dev/tty opens only with a controlling terminal")

    def test_no_preexec_fn_setsid_makes_the_session_and_the_controlling_terminal(self):
        """S-29: preexec_fn is not safe with threads (the reader/waiter threads, GTK);
        `setsid --ctty` does the same in the child."""
        from unittest import mock
        import multicred.core.pty as pty_module
        calls = []
        real_popen = pty_module.subprocess.Popen

        def recording(*args, **kwargs):
            calls.append((args, kwargs))
            return real_popen(*args, **kwargs)
        with mock.patch.object(pty_module.subprocess, "Popen", recording):
            self.run_script("exit 0")
        (argv,), kwargs = calls[0]
        self.assertIsNone(kwargs.get("preexec_fn"))
        self.assertFalse(kwargs.get("start_new_session"))
        self.assertEqual(argv[:2], ["/usr/bin/setsid", "--ctty"])
        self.assertEqual(argv[2:], ["/bin/sh", "-c", "exit 0"])

    def test_the_pid_is_the_program_itself_not_a_wrapper(self):
        pty, output, exited, _ = self.start("echo pid:$$; read line")
        self.assertTrue(wait_until(lambda: "pid:" in output.text and "\n" in output.text.split("pid:")[1]))
        self.assertEqual(int(output.text.split("pid:")[1].split()[0]), pty.pid)
        self.assertEqual(os.getpgid(pty.pid), pty.pid, "group leader: terminate() signals its group")
        pty.write(b"\r")
        self.assertTrue(exited.wait(5))

    def test_input_is_delivered_like_typing(self):
        text, code = self.run_script(
            "printf 'Paste code here if prompted > '; IFS= read -r code; printf 'got:%s\\n' \"$code\"",
            input_text="ABC-123\r")
        self.assertEqual(code, 0)
        self.assertIn("got:ABC-123", text)

    def test_cat_echoes_through_the_terminal_and_ends_on_eof(self):
        pty, output, exited, status = self.start("cat")
        time.sleep(0.3)
        pty.write(b"hello pty\r")
        self.assertTrue(wait_until(lambda: output.text.count("hello pty") >= 2), output.text)
        pty.write(b"\x04")  # Ctrl-D: end of input for cat
        self.assertTrue(exited.wait(5))
        self.assertEqual(status["code"], 0)

    def test_exit_status_and_all_output_arrive_before_exit(self):
        text, code = self.run_script("i=0; while [ $i -lt 2000 ]; do echo line$i; i=$((i+1)); done; exit 7")
        self.assertEqual(code, 7)
        self.assertIn("line1999", text, "output must be drained before on_exit")

    def test_exit_is_reported_even_if_a_grandchild_keeps_the_terminal(self):
        started = time.monotonic()
        # The grandchild ignores the SIGHUP the session leader's exit sends, so it
        # keeps the terminal open.
        text, code = self.run_script("(trap '' HUP; exec sleep 30) & echo started:$!; sleep 0.3")
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 5)
        pid = int(text.split("started:")[1].split()[0])
        self.assertFalse(process_gone(pid), "the grandchild still holds the terminal")
        os.kill(pid, signal.SIGKILL)  # the grandchild this test started

    def test_terminate_stops_the_child_and_its_process_group(self):
        pty, output, exited, _ = self.start("sleep 60 & echo child:$!; wait")
        self.assertTrue(wait_until(lambda: "child:" in output.text and "\n" in output.text.split("child:")[1]))
        grandchild = int(output.text.split("child:")[1].split()[0])
        self.assertTrue(pty.is_running)
        pty.terminate()
        self.assertTrue(exited.wait(5))
        self.assertFalse(pty.is_running)
        self.assertTrue(wait_until(lambda: process_gone(grandchild), 3), "the whole group gets the signal")

    def test_terminate_escalates_to_sigkill_after_two_seconds(self):
        pty, output, exited, status = self.start("trap '' TERM; echo ready; while :; do sleep 0.1; done")
        self.assertTrue(wait_until(lambda: "ready" in output.text))
        started = time.monotonic()
        pty.terminate()
        self.assertTrue(exited.wait(6))
        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        self.assertEqual(status["code"], -signal.SIGKILL)

    def test_environment_is_exactly_the_one_given(self):
        text, code = self.run_script("env", environment={"PATH": "/usr/bin:/bin", "MC_ONLY": "yes"})
        self.assertEqual(code, 0)
        names = {line.split("=", 1)[0] for line in text.replace("\r", "").splitlines() if "=" in line}
        self.assertIn("MC_ONLY", names)
        self.assertLessEqual(names, {"PATH", "MC_ONLY", "PWD", "OLDPWD", "SHLVL", "_"},
                             "nothing inherited from the test runner")

    def test_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            text, code = self.run_script("pwd", cwd=directory)
        self.assertEqual(code, 0)
        self.assertIn(os.path.realpath(directory), text)

    def test_submit_line_sends_the_text_and_enter_in_separate_writes(self):
        # Raw mode: each `dd` read returns what had arrived at that moment.
        script = ("stty raw -echo; echo ready; "
                  "a=$(dd bs=64 count=1 2>/dev/null | od -An -c); b=$(dd bs=64 count=1 2>/dev/null | od -An -c); "
                  "stty sane; printf '[%s][%s]\\n' \"$a\" \"$b\"")
        pty, output, exited, status = self.start(script)
        self.assertTrue(wait_until(lambda: "ready" in output.text))
        time.sleep(0.2)
        pty.submit_line("ABC", enter_delay=0.3)
        self.assertTrue(exited.wait(5), output.text)
        first, second = output.text.split("[", 1)[1].split("][", 1)
        self.assertIn("A   B   C", first)
        self.assertNotIn("\\r", first, "Enter must not arrive with the code (Claude Code treats it as a paste)")
        self.assertIn("\\r", second)

    def test_write_after_exit_is_ignored(self):
        pty, _, exited, _ = self.start("exit 0")
        self.assertTrue(exited.wait(5))
        pty.write(b"late\r")  # must not raise or write to a reused descriptor
        pty.terminate()

    def test_launch_failure_and_double_start(self):
        pty = PseudoTerminalProcess()
        with self.assertRaises(PTYError) as failure:
            pty.start("/nonexistent/claude", [], {}, on_output=lambda _d: None, on_exit=lambda _c: None)
        self.assertEqual(failure.exception.kind, "launchFailed")

        running = PseudoTerminalProcess()
        running.start("/bin/sh", ["-c", "sleep 5"], {}, on_output=lambda _d: None, on_exit=lambda _c: None)
        self.addCleanup(running.terminate)
        with self.assertRaises(PTYError) as again:
            running.start("/bin/sh", [], {}, on_output=lambda _d: None, on_exit=lambda _c: None)
        self.assertEqual(again.exception.kind, "alreadyStarted")


if __name__ == "__main__":
    unittest.main()
