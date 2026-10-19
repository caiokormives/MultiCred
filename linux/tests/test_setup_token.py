"""`claude setup-token` output: the token is captured whole and masked in the transcript.

Ports RealSetupTokenOutputTests.swift, ClaudeTests.testSetupTokenOutputIsCapturedAndMasked
and SetupTokenEndToEndTests (simulator through the real pseudo-terminal).
"""

import threading
import unittest

from multicred.core.claude.login_flow import LoginEvent, looks_like_authorization_code
from multicred.core.claude.login_simulator import SetupTokenSimulator
from multicred.core.claude.setup_token import SetupTokenParser
from multicred.core.log import MASK, redact_credential_like
from multicred.core.pty import PseudoTerminalProcess

from tests.login_support import wait_until
from tests.support import SafeTestCase

TOKEN = "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz0123456789_-ABCDEFGHIJ"

# Bytes captured from the real `claude setup-token` 2.1.296 (fake code).
REAL_PROMPT = ("\x1b]8;;\x07\r\r\n\r\r\n\r\r\n\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere\x1b[18Gif\x1b[21Gprompted"
               "\x1b[30G>\r\r\n").encode()
REAL_ERROR = ("\x1b(B\x0f\x1b[?2004h\r\x1b[31C\x1b[1A******************ate456\r\r\n\x1b[2K\x1b[1A\x1b[2K\x1b[G"
              "\x1b[1A\r\x1b[1C\x1b[4A\x1b[38;5;211mOAuth error: Request failed with status code 400\x1b[39m\x1b[K"
              "\r\x1b[2B\x1b[K\r\x1b[1C\x1b[1B\x1b[38;5;153mPress \x1b[1mEnter\x1b[22m to retry. \r\x1b[1B\x1b[39m"
              "\x1b[K\r\x1b[1B\x1b[K\r\x1b[1A").encode()


class SetupTokenParserTests(unittest.TestCase):
    def test_setup_token_output_is_captured_and_masked(self):
        parser = SetupTokenParser()
        parser.consume(("Opening browser to sign in…\r\nLogin successful.\r\nYour OAuth token (valid for 1 year):"
                        f"\r\n\r\n\x1b[1m{TOKEN}\x1b[0m\r\n").encode())
        self.assertEqual(parser.token, TOKEN)
        self.assertNotIn(TOKEN, parser.masked_transcript)
        self.assertIn(MASK, parser.masked_transcript)

    def test_ink_cursor_moves_become_spaces_and_the_prompt_is_seen(self):
        parser = SetupTokenParser()
        self.assertEqual(parser.consume(REAL_PROMPT), [LoginEvent.prompting_for_code()])
        self.assertIn("Paste code here if prompted >", parser.masked_transcript)

    def test_oauth_error_is_a_failure(self):
        parser = SetupTokenParser()
        parser.consume(REAL_PROMPT)
        self.assertEqual(parser.consume(REAL_ERROR),
                         [LoginEvent.failed("OAuth error: Request failed with status code 400")])

    def test_a_token_split_across_reads_is_captured_whole(self):
        # A pseudo-terminal read can end in the middle of the token; the first
        # 20 characters must not be taken as the token.
        parser = SetupTokenParser()
        line = f"Your OAuth token (valid for 1 year):\r\n\r\n{TOKEN}\r\n".encode()
        cut = line.index(b"sk-ant") + 30
        parser.consume(line[:cut])
        self.assertIsNone(parser.token)
        parser.consume(line[cut:])
        self.assertEqual(parser.token, TOKEN)

    def test_authorization_code_shape(self):
        self.assertTrue(looks_like_authorization_code(" abcdefghijklmnopqrstuvwxyz012345#ABCDEFGHIJKLMNOP\n"))
        self.assertFalse(looks_like_authorization_code("hello world"))
        self.assertFalse(looks_like_authorization_code("https://claude.com/x#abcdefghijklmnop"))
        self.assertFalse(looks_like_authorization_code("abcdefghijklmnopqrstuvwxyz0123456789"), "needs #state")

    def test_credential_like_text_is_masked_for_logs(self):
        text = "token sk-ant-oat01-abc_DEF-123 and code abcdefghijklmnopqrstuvwxyz0123456789ABCD#xyz and words"
        masked = redact_credential_like(text)
        self.assertNotIn("sk-ant", masked)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789ABCD", masked)
        self.assertIn("and words", masked)


class SetupTokenEndToEndTests(SafeTestCase):
    """setup-token simulator → real pseudo-terminal → parser."""

    def test_simulated_setup_token_is_captured_and_masked(self):
        lock = threading.Lock()
        parser = SetupTokenParser()
        events = []
        exited = threading.Event()

        def output(data):
            with lock:
                events.extend(parser.consume(data))

        pty = PseudoTerminalProcess()
        pty.start(SetupTokenSimulator.EXECUTABLE, SetupTokenSimulator.arguments(),
                  {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, cwd=str(self.home), columns=500,
                  on_output=output, on_exit=lambda _code: exited.set())
        self.addCleanup(pty.terminate)
        self.assertTrue(wait_until(lambda: LoginEvent.prompting_for_code() in events, 5), parser.masked_transcript)
        pty.submit_line("CODE")
        self.assertTrue(exited.wait(10))
        with lock:
            self.assertEqual(parser.token, SetupTokenSimulator.FAKE_TOKEN)
            self.assertNotIn(SetupTokenSimulator.FAKE_TOKEN, parser.masked_transcript)


if __name__ == "__main__":
    unittest.main()
