"""S-04: what a Claude folder contains (ClaudeFolderInspectorTests.swift)."""

import os
from datetime import datetime, timezone

from multicred.core.claude.folder_inspector import discover_folders, summarize
from tests.support import SafeTestCase


class ClaudeFolderInspectorTests(SafeTestCase):
    def session(self, folder, project, name, lines, modified):
        directory = folder / "projects" / project
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(path, (modified.timestamp(), modified.timestamp()))

    def test_summarizes_login_projects_and_recent_conversations(self):
        work = self.home / ".claude"
        work.mkdir()
        (self.home / ".claude.json").write_text('{"oauthAccount":{"emailAddress":"r@npu.com.br","organizationName":"NPU"}}')
        old = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
        new = datetime.fromtimestamp(1_790_000_000, tz=timezone.utc)
        self.session(work, "-home-x-npu-api", "a", [
            '{"type":"mode","mode":"x","sessionId":"a"}',
            f'{{"type":"user","cwd":"{self.home}/npu/api","message":{{"content":"secret message body"}}}}',
            '{"type":"ai-title","aiTitle":"Corrigir deploy da API","sessionId":"a"}',
        ], new)
        self.session(work, "-home-x-npu-web", "b", [
            f'{{"type":"user","cwd":"{self.home}/npu/web","message":{{"content":"x"}}}}',
            '{"type":"ai-title","aiTitle":"Título automático","sessionId":"b"}',
            '{"type":"custom-title","customTitle":"Migração do login","sessionId":"b"}',
        ], old)
        self.session(work, "-home-x-npu-web", "c", [
            '{"type":"last-prompt","lastPrompt":"ajuste o\\nbotão","sessionId":"c"}',
        ], datetime.fromtimestamp(old.timestamp() - 10, tz=timezone.utc))
        (work / "projects" / "-home-x-npu-web" / "notes.txt").write_text("not a session")

        summary = summarize(None, self.environment)
        self.assertEqual(summary.path, str(work))
        self.assertEqual(summary.email, "r@npu.com.br")
        self.assertEqual(summary.organization, "NPU")
        self.assertEqual(summary.project_count, 2)
        self.assertEqual(summary.conversation_count, 3)
        self.assertEqual(summary.last_activity, new)
        self.assertEqual([c.title for c in summary.recent_conversations],
                         ["Corrigir deploy da API", "Migração do login", "ajuste o botão"])
        self.assertEqual(summary.recent_conversations[0].project, "~/npu/api")
        self.assertNotIn("secret message body", repr(summary), "message bodies are never read into the summary")

    def test_limit_long_prompts_and_untitled(self):
        folder = self.home / ".claude-l"
        when = datetime.fromtimestamp(1_790_000_000, tz=timezone.utc)
        self.session(folder, "p", "long", ['{"type":"last-prompt","lastPrompt":"' + "x" * 100 + '"}'], when)
        self.session(folder, "p", "none", ['{"type":"user","message":{"content":"hi"}}', "not json"],
                     datetime.fromtimestamp(1_780_000_000, tz=timezone.utc))
        summary = summarize(str(folder), self.environment, limit=1)
        self.assertEqual(summary.conversation_count, 2)
        self.assertEqual([c.title for c in summary.recent_conversations], ["x" * 80 + "…"])
        summary = summarize(str(folder), self.environment)
        self.assertEqual(summary.recent_conversations[1].title, "(sem título)")
        self.assertIsNone(summary.recent_conversations[1].project)

    def test_empty_and_logged_out_folders(self):
        folder = self.home / ".claude-novo"
        folder.mkdir()
        summary = summarize(str(folder), self.environment)
        self.assertIsNone(summary.email)
        self.assertEqual(summary.conversation_count, 0)
        self.assertIsNone(summary.last_activity)
        self.assertTrue(summary.exists)
        self.assertFalse(summarize(f"{self.home}/.claude-nada", self.environment).exists)

    def test_discovers_default_and_named_folders(self):
        for name in (".claude", ".claude-pessoal", ".claude-cliente"):
            (self.home / name).mkdir()
        (self.home / ".claude.json").write_text("{}")
        (self.home / ".claude-arquivo").write_text("not a folder")
        self.assertEqual(discover_folders(self.environment),
                         [None, f"{self.home}/.claude-cliente", f"{self.home}/.claude-pessoal"])
