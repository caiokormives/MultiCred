"""S-20: switch failures in Portuguese, like the Mac's UserMessages.swift (no GTK needed)."""

import unittest

from multicred.core.claude.login_vault import OccupancyUnreadable
from multicred.core.engine.types import (
    Busy, InvalidConfiguration, Preflight, PreflightFailure, RollbackOutcome, StateUnreadable, StepFailed,
    SwitchStepKind,
)
from multicred.ui.messages import describe_switch_error

K = SwitchStepKind


class SwitchMessageTests(unittest.TestCase):
    def test_busy_invalid_and_unreadable_state(self):
        self.assertEqual(describe_switch_error(Busy()), "Uma troca já está em andamento.")
        self.assertEqual(describe_switch_error(InvalidConfiguration([object()])),
                         "A configuração tem campos inválidos. Revise as contas e perfis.")
        self.assertEqual(describe_switch_error(StateUnreadable("JSON quebrado")),
                         "Não consegui ler o estado atual: JSON quebrado")

    def test_every_preflight_kind_has_a_sentence(self):
        cases = {
            PreflightFailure("profileNotFound"): "Esse perfil não existe mais.",
            PreflightFailure("claudeAccountNotFound"): "Essa conta Claude não existe mais.",
            PreflightFailure("toolMissing", "gh"): "GitHub CLI não foi encontrado neste computador.",
            PreflightFailure("claudeDirectoryMissing", "/h/.claude-x"): "A pasta do Claude deste perfil não existe: /h/.claude-x",
            PreflightFailure("apiKeyMissing", "Pessoal", "GEMINI_API_KEY"):
                "A chave Pessoal (GEMINI_API_KEY) não está no chaveiro do sistema.",
            PreflightFailure("claudeTokenMissing", "Marco"):
                "O token da conta Claude Marco não está no chaveiro do sistema. Gere outro em Contas › Claude.",
            PreflightFailure("claudeLoginMissing", "Marco"):
                "O login da conta Claude Marco não está no chaveiro do sistema. Em Contas › Claude › Marco, use \"Entrar de novo\".",
            PreflightFailure("secretsUnavailable", "locked"): "Não consegui consultar o chaveiro do sistema: locked",
            PreflightFailure("noActiveProfile", "Marco"):
                "Ative um perfil antes de trocar só a conta Claude para Marco.",
        }
        for failure, expected in cases.items():
            with self.subTest(kind=failure.kind):
                self.assertEqual(describe_switch_error(Preflight(failure)), expected)
        unreadable = describe_switch_error(Preflight(PreflightFailure("claudeFoldersUnreadable", "/h/.config/multicred/claude-folders.json")))
        self.assertIn("/h/.config/multicred/claude-folders.json", unreadable)
        self.assertNotIn("Preflight", unreadable)

    def test_step_failed_names_the_step_and_the_rollback(self):
        complete = StepFailed(K.GITHUB, "gh: keyring locked", RollbackOutcome())
        self.assertEqual(describe_switch_error(complete), "Falhou em GitHub CLI: gh: keyring locked. Nada foi alterado.")
        incomplete = StepFailed(K.ENV_FILES, "disco cheio", RollbackOutcome((K.SESSION, K.CLAUDE_LOGIN)))
        self.assertEqual(describe_switch_error(incomplete),
                         "Falhou em arquivos de ambiente do shell: disco cheio. Não consegui desfazer: "
                         "variável do Claude Code na sessão gráfica, login da conta Claude na pasta. Use Verificar.")

    def test_occupancy_and_unknown_errors_keep_their_own_text(self):
        error = OccupancyUnreadable("/h/.config/multicred/claude-folders.json")
        self.assertEqual(describe_switch_error(error), str(error))
        self.assertEqual(describe_switch_error(RuntimeError("algo")), "algo")

    def test_every_step_kind_has_a_name(self):
        from multicred.ui.messages import _STEP_NAMES
        self.assertEqual(set(_STEP_NAMES), set(SwitchStepKind), "a new step needs a Portuguese name")
