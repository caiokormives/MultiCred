"""S-04: Claude as co-author in settings.json (D38, ClaudeAttributionTests.swift)."""

import json
import unittest

from multicred.core.claude import attribution
from multicred.core.claude.attribution import HIDDEN, UnreadableSettings
from multicred.core.models import GitHubAccount
from tests.support import SafeTestCase


def parsed(data):
    return json.loads(data)


class ClaudeAttributionTests(unittest.TestCase):
    def test_opt_out_writes_hidden_attribution_keeping_other_settings(self):
        current = b'{"model":"opus","permissions":{"allow":["Bash(ls)"]}}'
        updated = parsed(attribution.updated(current, co_author=False))
        self.assertEqual(updated["attribution"], {"commit": "", "pr": ""})
        self.assertEqual(updated["model"], "opus")
        self.assertEqual(updated["permissions"], {"allow": ["Bash(ls)"]})

    def test_opt_out_on_missing_or_blank_file_creates_it(self):
        self.assertEqual(parsed(attribution.updated(None, co_author=False))["attribution"], HIDDEN)
        self.assertEqual(parsed(attribution.updated(b" \n\t", co_author=False)), {"attribution": HIDDEN})

    def test_nothing_changes_when_already_in_the_wanted_state(self):
        self.assertIsNone(attribution.updated(b'{"attribution":{"commit":"","pr":""}}', co_author=False))
        self.assertIsNone(attribution.updated(b'{"model":"opus"}', co_author=True))
        self.assertIsNone(attribution.updated(None, co_author=True))

    def test_opt_in_removes_only_what_multicred_wrote(self):
        ours = parsed(attribution.updated(b'{"attribution":{"commit":"","pr":""},"model":"opus"}', co_author=True))
        self.assertNotIn("attribution", ours)
        self.assertEqual(ours["model"], "opus")
        # Attribution text the user wrote by hand is theirs.
        self.assertIsNone(attribution.updated(b'{"attribution":{"commit":"Feito com IA"}}', co_author=True))
        self.assertIsNone(attribution.updated(b'{"attribution":{"commit":"","pr":"","x":""}}', co_author=True))

    def test_hand_written_attribution_is_replaced_only_on_opt_out(self):
        updated = parsed(attribution.updated(b'{"attribution":{"commit":"Feito com IA"}}', co_author=False))
        self.assertEqual(updated["attribution"], HIDDEN)

    def test_unreadable_settings_are_left_alone(self):
        for data in (b"{not json", b"[1, 2]", b"\xff\xfe"):
            with self.assertRaises(UnreadableSettings):
                attribution.updated(data, co_author=False)

    def test_output_is_pretty_with_a_final_newline_and_keeps_unicode_and_slashes(self):
        out = attribution.updated('{"statusLine":"a/b","nome":"João"}'.encode(), co_author=False).decode()
        self.assertTrue(out.endswith("}\n"))
        self.assertIn('"a/b"', out)
        self.assertIn("João", out)
        self.assertIn('\n  "attribution"', out)

    def test_old_config_without_the_field_keeps_co_author_on(self):
        account = GitHubAccount.from_dict({"id": "11111111-1111-1111-1111-111111111111", "nickname": "P",
                                           "username": "p", "host": "github.com", "color": "orange"})
        self.assertTrue(account.claude_co_author)


class SettingsFileTests(SafeTestCase):
    def test_settings_file_of_each_folder(self):
        self.assertEqual(attribution.settings_file(None, self.environment), self.home / ".claude" / "settings.json")
        self.assertEqual(str(attribution.settings_file("/x/.claude-w", self.environment)), "/x/.claude-w/settings.json")


if __name__ == "__main__":
    unittest.main()
