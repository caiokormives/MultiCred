"""Switch failures in Portuguese, like the Mac's UserMessages.swift. No GTK here: the tray,
the window toasts and the notifications all use it."""

from __future__ import annotations

from ..core.engine.types import (
    Busy, InvalidConfiguration, Preflight, PreflightFailure, StateUnreadable, StepFailed, SwitchStepKind,
)
# The secret-store wording lives in the core (Verificar uses it too); re-exported for the UI.
from ..core.platform import SYSTEM_KEYRING, SecretStoreWording, secret_store_wording  # noqa: F401
from ..core.tools import Tool


_STEP_NAMES = {
    SwitchStepKind.GIT: "git",
    SwitchStepKind.GITHUB: "GitHub CLI",
    SwitchStepKind.SESSION: "variável do Claude Code na sessão gráfica",
    SwitchStepKind.CLAUDE_SETTINGS: "co-autoria da Claude (settings.json)",
    SwitchStepKind.CLAUDE_LOGIN: "login da conta Claude na pasta",
    SwitchStepKind.ENV_FILES: "arquivos de ambiente do shell",
    SwitchStepKind.STATE: "estado do perfil",
}


def step_name(step: SwitchStepKind) -> str:
    return _STEP_NAMES.get(step, step.value)


def _tool_name(value: str | None) -> str:
    try:
        return Tool(value).display_name
    except ValueError:
        return value or "Uma ferramenta"


def describe_preflight(failure: PreflightFailure, store: SecretStoreWording = SYSTEM_KEYRING) -> str:
    detail, extra = failure.detail, failure.extra
    located = store.located
    messages = {
        "profileNotFound": "Esse perfil não existe mais.",
        "claudeAccountNotFound": "Essa conta Claude não existe mais.",
        "toolMissing": f"{_tool_name(detail)} não foi encontrado neste computador.",
        "claudeDirectoryMissing": f"A pasta do Claude deste perfil não existe: {detail}",
        "apiKeyMissing": f"A chave {detail} ({extra}) não está {located}.",
        "claudeTokenMissing": f"O token da conta Claude {detail} não está {located}. "
                              "Gere outro em Contas › Claude.",
        "claudeLoginMissing": f"O login da conta Claude {detail} não está {located}. "
                              f"Em Contas › Claude › {detail}, use \"Entrar de novo\".",
        "secretsUnavailable": f"Não consegui consultar {store.store}: {detail}",
        "noActiveProfile": f"Ative um perfil antes de trocar só a conta Claude para {detail}.",
        "claudeFoldersUnreadable": f"Não consegui ler {detail} (qual conta do Claude está em cada pasta). "
                                   "Nada foi alterado.",
    }
    return messages.get(failure.kind, f"Não dá para trocar agora ({failure.kind}).")


def describe_switch_error(error: Exception, store: SecretStoreWording = SYSTEM_KEYRING) -> str:
    if isinstance(error, Busy):
        return "Uma troca já está em andamento."
    if isinstance(error, InvalidConfiguration):
        return "A configuração tem campos inválidos. Revise as contas e perfis."
    if isinstance(error, StateUnreadable):
        return f"Não consegui ler o estado atual: {error.reason}"
    if isinstance(error, Preflight):
        return describe_preflight(error.failure, store)
    if isinstance(error, StepFailed):
        reason = error.reason.strip().rstrip(".")
        if error.rollback.complete:
            return f"Falhou em {step_name(error.step)}: {reason}. Nada foi alterado."
        failed = ", ".join(step_name(step) for step in error.rollback.failed)
        return f"Falhou em {step_name(error.step)}: {reason}. Não consegui desfazer: {failed}. Use Verificar."
    return str(error)
