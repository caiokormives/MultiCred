"""What the desktop shows after a switch (SwitchFeedbackPresenter.swift, design 08).

Wayland has no positioned overlay, so the Mac's HUD becomes a desktop notification:
`settings.switch_feedback` "hud" = a short notification withdrawn after ~2 s,
"notification" = a normal one, "none" = nothing. When Claude Code sessions are
open after a switch that changed the Claude account or folder, the notification
says so and is not withdrawn, like the Mac's sticky HUD.

`message_for` is pure (tested without a display); the presenter talks to
Gio.Application, which forwards to the desktop's notification service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..core.engine.types import SwitchStepKind
from ..core.models import SwitchFeedback

HUD_MILLISECONDS = 2000
SWITCH_ID = "switch"
ERROR_ID = "error"
OPEN_VERIFY_ACTION = "open-verify"
OPEN_VERIFY_LABEL = "Abrir Verificar"


@dataclass(frozen=True)
class SwitchOutcome:
    profile_name: str | None
    # The Claude account applied after the switch; None = the folder's own login.
    claude_account_name: str | None
    steps: tuple = ()
    claude_only: bool = False
    # The Claude folder or the applied account differs from before the switch.
    claude_changed: bool = False
    # Claude Code processes running right after the switch; they keep the old account.
    running_claude_sessions: int = 0


@dataclass(frozen=True)
class FeedbackMessage:
    title: str
    body: str
    # Withdrawn after HUD_MILLISECONDS (the "Notificação breve" that stands in for the HUD).
    transient: bool


def changed_items(outcome: SwitchOutcome) -> list[str]:
    items = []
    if SwitchStepKind.GIT in outcome.steps:
        items.append("git")
    if SwitchStepKind.GITHUB in outcome.steps:
        items.append("GitHub")
    if outcome.claude_changed or outcome.claude_only:
        items.append("Claude")
    return items


def sessions_warning(outcome: SwitchOutcome) -> str | None:
    """Only after a switch that moved Claude, and only with sessions open. The Mac's
    sticky-HUD text (SwitchFeedbackPresenter.swift:125), agreeing in the singular."""
    count = outcome.running_claude_sessions
    if count <= 0 or not (outcome.claude_changed or outcome.claude_only):
        return None
    target = outcome.claude_account_name or "o login da pasta"
    if count == 1:
        return (f"1 sessão do Claude Code aberta antes da troca continua na conta anterior. "
                f"Reinicie-a para usar {target}.")
    return (f"{count} sessões do Claude Code abertas antes da troca continuam na conta anterior. "
            f"Reinicie-as para usar {target}.")


def message_for(outcome: SwitchOutcome, feedback: SwitchFeedback) -> FeedbackMessage | None:
    """The Mac's notification (SwitchFeedbackPresenter.swift:73-79)."""
    if feedback == SwitchFeedback.NONE:
        return None
    if outcome.claude_only:
        title = f"Conta Claude: {outcome.claude_account_name or 'login da pasta'}"
        body = "Só o login do Claude mudou; agents, skills e conversas ficam. Vale para novos terminais."
    else:
        title = f"Agora em {outcome.profile_name or 'nenhum perfil'}"
        items = changed_items(outcome) or ["Perfil"]
        # The Mac joins with commas and always says "trocados"; one item stays singular.
        body = f"{', '.join(items)} {'trocados' if len(items) > 1 else 'trocado'}."
    warning = sessions_warning(outcome)
    if warning is not None:
        body = f"{body} {warning}"
    return FeedbackMessage(title, body, transient=feedback == SwitchFeedback.HUD and warning is None)


class SwitchFeedbackPresenter:
    """Sends the notifications through the Gio.Application. Every switch uses the same
    id, so the desktop replaces the previous one instead of stacking them.

    Error notifications (a switch or a re-apply that failed) carry the Mac alert's
    "Abrir Verificar" button (AppDelegate.swift:234-243); it activates `app.open-verify`,
    which the presenter registers on the application (→ application.open_page("verify"))."""

    def __init__(self, application, schedule: Callable | None = None, cancel: Callable | None = None,
                 notification: Callable | None = None):
        from gi.repository import Gio, GLib

        self.application = application
        self.schedule = schedule or GLib.timeout_add
        self.cancel = cancel or GLib.source_remove
        self.new_notification = notification or Gio.Notification.new
        self._withdraw_source = None
        self._register_open_verify()

    def _register_open_verify(self) -> None:
        from gi.repository import Gio

        if self.application.lookup_action(OPEN_VERIFY_ACTION) is not None:
            return
        action = Gio.SimpleAction.new(OPEN_VERIFY_ACTION, None)
        action.connect("activate", lambda *_: self.application.open_page("verify"))
        self.application.add_action(action)

    def present(self, outcome: SwitchOutcome, feedback: SwitchFeedback) -> FeedbackMessage | None:
        message = message_for(outcome, feedback)
        if message is None:
            return None
        self._cancel_withdraw()
        self._send(SWITCH_ID, message.title, message.body)
        if message.transient:
            self._withdraw_source = self.schedule(HUD_MILLISECONDS, self._withdraw)
        return message

    def notify_error(self, title: str, body: str) -> None:
        from gi.repository import Gio

        self._send(ERROR_ID, title, body, Gio.NotificationPriority.HIGH,
                   buttons=[(OPEN_VERIFY_LABEL, f"app.{OPEN_VERIFY_ACTION}")])

    def _send(self, identifier: str, title: str, body: str, priority=None, buttons=()) -> None:
        from gi.repository import Gio

        notification = self.new_notification(title)
        notification.set_body(body)
        notification.set_priority(priority if priority is not None else Gio.NotificationPriority.NORMAL)
        for label, action in buttons:
            notification.add_button(label, action)
        self.application.send_notification(identifier, notification)

    def _withdraw(self) -> bool:
        self._withdraw_source = None
        self.application.withdraw_notification(SWITCH_ID)
        return False  # GLib: run once

    def _cancel_withdraw(self) -> None:
        if self._withdraw_source is not None:
            self.cancel(self._withdraw_source)
            self._withdraw_source = None
