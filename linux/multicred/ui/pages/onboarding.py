"""Three-step onboarding (design 02b, approved Linux variant C): Preparar → Perfis → Aplicar.

Lives inside the main window (D31). Step 1 shows what was detected (tools with
their version or the install command, D6; the legacy ~/Perfis card, D13), step 2
edits the suggested profiles, step 3 lists the shells and the exact diff of each
file, then installs, saves the configuration and activates the initial profile.
"""

from __future__ import annotations

import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ...core.engine.types import SwitchError  # noqa: E402
from ...core.install.installer import ChangeKind, InstallError, Integrations, InstallOptions, MarkerMismatch  # noqa: E402
from ...core.install.setup_detector import (  # noqa: E402
    DetectedSetup, default_shells, install_command, suggested_config,
)
from ...core.models import GitIdentity, Profile, ProfileColor, ProfileIcon, ShellKind  # noqa: E402
from ...core.storage import InvalidConfig  # noqa: E402
from ...core.validation import issues_in  # noqa: E402

STEPS = ("Preparar", "Perfis", "Aplicar")
# Shell order on screen: bash first, the default on Ubuntu and Pop!_OS.
SHELL_ORDER = (ShellKind.BASH, ShellKind.ZSH, ShellKind.FISH)
COLOR_NAMES = {
    ProfileColor.INDIGO: "Índigo", ProfileColor.BLUE: "Azul", ProfileColor.TEAL: "Verde-azulado",
    ProfileColor.GREEN: "Verde", ProfileColor.ORANGE: "Laranja", ProfileColor.RED: "Vermelho",
    ProfileColor.PINK: "Rosa", ProfileColor.GRAPHITE: "Grafite",
}
KIND_TEXT = {ChangeKind.CREATE: "criar", ChangeKind.MODIFY: "modificar", ChangeKind.DELETE: "apagar"}

_CSS = """
.mc-step { color: alpha(currentColor, 0.55); font-weight: 600; }
.mc-step.current { color: @window_fg_color; }
.mc-step-number { min-width: 24px; min-height: 24px; border-radius: 12px; font-size: 12px; font-weight: bold;
                  background: alpha(currentColor, 0.12); }
.mc-step.done .mc-step-number { background: alpha(@success_color, 0.18); color: @success_color; }
.mc-step.current .mc-step-number { background: @accent_bg_color; color: @accent_fg_color; }
.mc-step-line { min-width: 50px; min-height: 2px; background: alpha(currentColor, 0.15); }
.mc-diff { background: alpha(currentColor, 0.06); border-radius: 8px; padding: 8px 10px; }
.mc-diff-added { color: @success_color; }
.mc-diff-removed { color: @error_color; }
.mc-pill { border-radius: 999px; padding: 2px 9px; font-size: 11px; font-weight: bold; }
.mc-pill.ok { background: alpha(@success_color, 0.15); color: @success_color; }
.mc-pill.warning { background: alpha(@warning_color, 0.18); color: @warning_color; }
.mc-badge { border-radius: 999px; min-width: 28px; min-height: 28px; color: white; font-weight: bold; }
.mc-swatch { min-width: 22px; min-height: 22px; border-radius: 999px; padding: 0; margin: 0 1px; }
.mc-swatch:checked { box-shadow: 0 0 0 2px @window_bg_color, 0 0 0 4px @accent_color; }
""" + "".join(f".mc-color-{color.value} {{ background: {color.hex}; }}\n" for color in ProfileColor)

_css_loaded = False


def _load_css() -> None:
    global _css_loaded
    display = Gdk.Display.get_default()
    if _css_loaded or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def _label(text: str, *classes: str, wrap: bool = False, xalign: float = 0.0, selectable: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign, wrap=wrap, selectable=selectable)
    if wrap:
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for css in classes:
        label.add_css_class(css)
    return label


def _pill(text: str, tone: str) -> Gtk.Label:
    pill = _label(text, "mc-pill", tone)
    pill.set_valign(Gtk.Align.CENTER)
    return pill


def _badge(profile: Profile) -> Gtk.Label:
    badge = _label(profile.icon.value if profile.icon else "", "mc-badge", f"mc-color-{profile.color.value}", xalign=0.5)
    badge.set_valign(Gtk.Align.CENTER)
    return badge


def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    clamp = Adw.Clamp(maximum_size=720, tightening_threshold=560, child=child)
    clamp.set_margin_top(12)
    clamp.set_margin_bottom(24)
    clamp.set_margin_start(16)
    clamp.set_margin_end(16)
    return Gtk.ScrolledWindow(child=clamp, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def describe_issues(issues, config) -> str:
    """The first validation problem, in words the user can act on."""
    issue = issues[0]
    owner_profile = config.profile(issue.owner) or next(
        (p for p in config.profiles if p.git_identity_id == issue.owner), None)
    name = f" do perfil {owner_profile.name}" if owner_profile and owner_profile.name.strip() else ""
    if issue.kind == "emptyField" and config.profile(issue.owner):
        return "Dê um nome ao perfil."
    if issue.kind == "emptyField" and issue.detail == "name":
        return f"Preencha o nome no git{name}."
    if issue.kind in ("emptyField", "invalidEmail"):
        return f"O e-mail do git{name} não parece válido."
    if issue.kind == "controlCharacters":
        return f"Há caracteres de controle em um dos campos{name}."
    return f"A configuração tem um problema ({issue.kind}){name}."


def describe_error(error: Exception, config) -> str:
    if isinstance(error, MarkerMismatch):
        return (f"O bloco do MultiCred em {error.path} foi editado à mão (falta o marcador de fim). "
                "Corrija o arquivo e tente de novo.")
    if isinstance(error, InstallError):
        return f"Não consegui alterar {error.path}: {error.reason}. Nada foi mudado."
    if isinstance(error, InvalidConfig):
        return describe_issues(error.issues, config)
    return str(error)


class ProfileCard(Adw.PreferencesGroup):
    """One editable suggested profile (step 2)."""

    def __init__(self, page: "OnboardingPage", profile: Profile, removable: bool):
        super().__init__(title=GLib.markup_escape_text(profile.name or "Sem nome"))
        self.page, self.profile = page, profile
        config = page.draft
        header = Gtk.Box(spacing=8)
        self.badge = _badge(profile)
        header.append(self.badge)
        if removable:
            remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remover perfil", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda *_: page.remove_profile(profile))
            header.append(remove)
        self.set_header_suffix(header)

        self.name_row = Adw.EntryRow(title="Nome do perfil", text=profile.name)
        self.name_row.connect("changed", self._name_changed)
        self.add(self.name_row)

        color_row = Adw.ActionRow(title="Cor")
        swatches = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        first = None
        for color in ProfileColor:
            swatch = Gtk.ToggleButton(tooltip_text=COLOR_NAMES[color], active=color == profile.color)
            swatch.add_css_class("mc-swatch")
            swatch.add_css_class(f"mc-color-{color.value}")
            if first is None:
                first = swatch
            else:
                swatch.set_group(first)
            swatch.connect("toggled", self._color_toggled, color)
            swatches.append(swatch)
        color_row.add_suffix(swatches)
        self.add(color_row)

        identity = config.git_identity(profile.git_identity_id)
        self.git_name_row = Adw.EntryRow(title="Nome no git", text=identity.name if identity else "")
        self.git_name_row.connect("changed", self._git_changed, "name")
        self.add(self.git_name_row)
        self.git_email_row = Adw.EntryRow(title="E-mail no git", text=identity.email if identity else "")
        self.git_email_row.connect("changed", self._git_changed, "email")
        self.add(self.git_email_row)

        self.github_ids = [None] + [account.id for account in config.github_accounts]
        names = ["Não trocar"] + [account.username for account in config.github_accounts]
        self.github_row = Adw.ComboRow(title="Conta do GitHub", model=Gtk.StringList.new(names))
        self.github_row.set_selected(self.github_ids.index(profile.github_account_id)
                                     if profile.github_account_id in self.github_ids else 0)
        self.github_row.connect("notify::selected", self._github_changed)
        self.add(self.github_row)

        self.folders = page.folder_options(profile.claude_config_dir)
        self.claude_row = Adw.ComboRow(title="Pasta do Claude",
                                       model=Gtk.StringList.new([page.folder_label(d) for d in self.folders]))
        self.claude_row.set_selected(self.folders.index(profile.claude_config_dir))
        self.claude_row.connect("notify::selected", self._claude_changed)
        self.add(self.claude_row)

    def _name_changed(self, row):
        name = row.get_text()
        self.profile.name = name
        self.profile.icon = ProfileIcon("letter", name.strip()[:1].upper())
        self.set_title(GLib.markup_escape_text(name or "Sem nome"))
        self.badge.set_label(self.profile.icon.value)
        self.page.draft_changed()

    def _color_toggled(self, button, color):
        if not button.get_active():
            return
        self.badge.remove_css_class(f"mc-color-{self.profile.color.value}")
        self.profile.color = color
        self.badge.add_css_class(f"mc-color-{color.value}")

    def _git_changed(self, row, attribute):
        """Edits the profile's git identity, creating one on the first edit."""
        config = self.page.draft
        identity = config.git_identity(self.profile.git_identity_id)
        if identity is None:
            identity = GitIdentity(nickname=self.profile.name, name="", email="", color=self.profile.color)
            config.git_identities.append(identity)
            self.profile.git_identity_id = identity.id
        setattr(identity, attribute, row.get_text().strip())

    def _github_changed(self, row, _param):
        self.profile.github_account_id = self.github_ids[row.get_selected()]

    def _claude_changed(self, row, _param):
        self.profile.claude_config_dir = self.folders[row.get_selected()]


class OnboardingPage(Gtk.Box):
    def __init__(self, model, on_finished):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        _load_css()
        self.model = model
        self.on_finished = on_finished
        self.step = 0
        self.setup: DetectedSetup | None = None
        # Re-running onboarding on a configured app starts from the current configuration.
        self.draft = copy.deepcopy(model.config) if model.config is not None else None
        self.initial_profile_id = model.state.profile_id if model.config is not None else None
        self.shells: set[ShellKind] = set()
        self.remove_legacy = False
        self.applying = False
        self.install_command_labels: dict[str, str] = {}
        self.profile_cards: list[ProfileCard] = []
        self.shell_checks: dict[ShellKind, Gtk.CheckButton] = {}
        self.preview_changes = []
        self.preview_failed = False

        self.stepper = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=16, margin_bottom=8)
        self.append(self.stepper)
        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self.stack)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        loading.append(spinner)
        loading.append(_label("Procurando ferramentas e contas…", "dim-label", xalign=0.5))
        self.stack.add_named(loading, "loading")
        self.append(Gtk.Separator())
        self.append(self._build_bottom_bar())
        self._render_stepper()
        self._update_bottom_bar()
        self._detect()

    # MARK: chrome

    def _build_bottom_bar(self) -> Gtk.Box:
        bar = Gtk.Box(spacing=10, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        self.back_button = Gtk.Button(label="Voltar")
        self.back_button.connect("clicked", lambda *_: self.go_to(self.step - 1))
        bar.append(self.back_button)
        self.hint_label = _label("Dá para continuar sem o Claude Code e adicioná-lo depois.", "dim-label", "caption")
        self.hint_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.hint_label.set_hexpand(True)
        bar.append(self.hint_label)
        self.error_label = _label("", "error", wrap=True, selectable=True)
        self.error_label.set_hexpand(True)
        self.error_label.set_visible(False)
        bar.append(self.error_label)
        bar.append(Gtk.Box(hexpand=True))
        self.initial_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.initial_box.append(_label("Perfil inicial:", "dim-label"))
        self.initial_profile_label = _label("", "heading")
        self.initial_box.append(self.initial_profile_label)
        self.initial_dropdown = Gtk.DropDown(valign=Gtk.Align.CENTER)
        self.initial_dropdown.connect("notify::selected", self._initial_changed)
        self.initial_box.append(self.initial_dropdown)
        bar.append(self.initial_box)
        self.next_button = Gtk.Button(label="Continuar")
        self.next_button.add_css_class("suggested-action")
        self.next_button.connect("clicked", self._next_clicked)
        bar.append(self.next_button)
        self.apply_button = Gtk.Button(label="Aplicar e ativar")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._apply_clicked)
        bar.append(self.apply_button)
        return bar

    def _render_stepper(self) -> None:
        child = self.stepper.get_first_child()
        while child is not None:
            self.stepper.remove(child)
            child = self.stepper.get_first_child()
        for index, title in enumerate(STEPS):
            if index:
                line = Gtk.Box(valign=Gtk.Align.CENTER)
                line.add_css_class("mc-step-line")
                self.stepper.append(line)
            item = Gtk.Box(spacing=8)
            item.add_css_class("mc-step")
            item.add_css_class("done" if index < self.step else "current" if index == self.step else "todo")
            number = _label("✓" if index < self.step else str(index + 1), "mc-step-number", xalign=0.5)
            number.set_valign(Gtk.Align.CENTER)
            item.append(number)
            item.append(_label(title))
            self.stepper.append(item)

    def _update_bottom_bar(self) -> None:
        ready = self.setup is not None and not self.applying
        self.back_button.set_visible(self.step > 0)
        self.back_button.set_sensitive(not self.applying)
        self.hint_label.set_visible(self.step == 0 and not self.error_label.get_visible())
        self.next_button.set_visible(self.step < 2)
        self.next_button.set_sensitive(ready and (self.step == 0 or bool(self.draft and self.draft.profiles)))
        self.apply_button.set_visible(self.step == 2)
        self.apply_button.set_label("Aplicando…" if self.applying else "Aplicar e ativar")
        self.apply_button.set_sensitive(ready and bool(self.shells) and not self.preview_failed)
        self.initial_box.set_visible(self.step == 2)
        self.initial_dropdown.set_sensitive(not self.applying)

    def show_error(self, text: str | None) -> None:
        self.error_label.set_label(text or "")
        self.error_label.set_visible(bool(text))
        self._update_bottom_bar()

    def go_to(self, step: int) -> None:
        self.step = max(0, min(step, 2))
        self.show_error(None)
        if self.step == 0:
            self._show_page(self._build_prepare(), "prepare")
        elif self.step == 1:
            self._show_page(self._build_profiles(), "profiles")
        else:
            self._show_page(self._build_apply(), "apply")
        self._render_stepper()
        self._update_bottom_bar()

    def _show_page(self, widget: Gtk.Widget, name: str) -> None:
        old = self.stack.get_child_by_name(name)
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(widget, name)
        self.stack.set_visible_child_name(name)

    # MARK: detection

    def _detect(self) -> None:
        self.setup = None
        self.stack.set_visible_child_name("loading")
        self._update_bottom_bar()
        self.model.detect_setup(self._detected)

    def _detected(self, setup, error) -> None:
        self.setup = setup or DetectedSetup()
        if self.draft is None:
            self.draft, self.initial_profile_id = suggested_config(self.setup, self.model.environment)
            self.shells = set(default_shells(self.setup, self.model.environment))
            self.remove_legacy = bool(self.setup.legacy_findings)
        elif not self.shells:
            self.shells = set(self.draft.settings.enabled_shells if self.model.config else
                              default_shells(self.setup, self.model.environment))
            self.remove_legacy = bool(self.setup.legacy_findings)
        self.go_to(self.step)
        if error is not None:
            self.show_error(f"Não consegui verificar tudo: {error}")

    # MARK: step 1 — Preparar

    def _build_prepare(self) -> Gtk.Widget:
        setup = self.setup
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        intro.append(_label("Bem-vindo ao MultiCred", "title-1", wrap=True))
        intro.append(_label("Um clique na bandeja troca, juntos, a identidade do git, a conta do GitHub CLI, "
                            "a conta do Claude Code e as chaves de API dos terminais.", "dim-label", wrap=True))
        for icon, text in (
            ("system-lock-screen-symbolic", "Nunca vê senhas ou tokens. O login é feito pelas próprias ferramentas."),
            ("text-x-generic-symbolic", "Altera só um bloco marcado no fim de cada arquivo de shell e do "
                                        "gitconfig, com backup."),
            ("edit-undo-symbolic", "Pode desinstalar e restaurar tudo a qualquer momento."),
        ):
            line = Gtk.Box(spacing=10)
            line.append(Gtk.Image.new_from_icon_name(icon))
            line.append(_label(text, wrap=True))
            intro.append(line)
        box.append(intro)

        tools = Adw.PreferencesGroup(title="Ferramentas")
        self.install_command_labels = {}
        for info in setup.tools:
            row = Adw.ActionRow(title=info.tool.display_name,
                                subtitle=GLib.markup_escape_text(info.version or info.path or "Não encontrado"))
            row.add_suffix(_pill("Instalado", "ok") if info.path else _pill("Faltando", "warning"))
            tools.add(row)
            if info.path is None:
                command = install_command(info.tool)
                self.install_command_labels[info.tool.value] = command
                tools.add(self._command_row(command))
        box.append(tools)

        found = Adw.PreferencesGroup(title="Encontrado nesta máquina")
        identity = (f"{setup.git_name or ''} <{setup.git_email}>".strip() if setup.git_email
                    else "nenhuma identidade global")
        logins = ", ".join(login.login for login in setup.github_logins) or "nenhuma conta no gh"
        folders = ", ".join(self.folder_label(f.config_dir) for f in setup.claude_folders) or "nenhuma pasta"
        for title, value in (("Identidade git", identity), ("Contas do GitHub", logins), ("Pastas do Claude", folders)):
            found.add(Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(value)))
        box.append(found)

        if setup.has_legacy_install:
            legacy = Adw.PreferencesGroup(
                title="Encontramos a versão antiga (~/Perfis)",
                description="Os perfis do perfis.conf viram sugestões no próximo passo. As linhas antigas "
                            "brigariam com o MultiCred pela pasta do Claude.")
            if setup.legacy_findings:
                files = ", ".join(self.model.environment.abbreviate(str(f.file)) for f in setup.legacy_findings)
                switch = Adw.SwitchRow(title="Remover linhas antigas", subtitle=GLib.markup_escape_text(files),
                                       active=self.remove_legacy)
                switch.connect("notify::active", self._legacy_toggled)
                legacy.add(switch)
            box.append(legacy)

        again = Gtk.Button(label="Verificar de novo", halign=Gtk.Align.START)
        again.connect("clicked", lambda *_: self._detect())
        box.append(again)
        return _scrolled(box)

    def _command_row(self, command: str) -> Gtk.Widget:
        row = Adw.ActionRow(title=GLib.markup_escape_text(command), title_selectable=True)
        row.add_css_class("monospace")
        copy_button = Gtk.Button(label="Copiar", valign=Gtk.Align.CENTER)
        copy_button.add_css_class("flat")
        copy_button.connect("clicked", lambda *_: self.get_clipboard().set(command))
        row.add_suffix(copy_button)
        return row

    def _legacy_toggled(self, row, _param):
        self.remove_legacy = row.get_active()

    # MARK: step 2 — Perfis

    def folder_options(self, current: str | None) -> list[str | None]:
        options: list[str | None] = [None]
        for folder in [f.config_dir for f in (self.setup.claude_folders if self.setup else [])] + [current]:
            if folder not in options:
                options.append(folder)
        return options

    def folder_label(self, config_dir: str | None) -> str:
        """Folder name plus who is logged in there, e.g. ".claude-pessoal · eu@x.com"."""
        name = config_dir.rsplit("/", 1)[-1] if config_dir else ".claude"
        status = next((f.status for f in (self.setup.claude_folders if self.setup else [])
                       if f.config_dir == config_dir), None)
        if status is not None and status.is_logged_in and status.email:
            return f"{name} · {status.email}"
        if status is not None and status.kind == "loggedOut":
            return f"{name} · entrar depois"
        return name

    def _build_profiles(self) -> Gtk.Widget:
        setup = self.setup
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(_label("Perfis sugeridos", "title-1", wrap=True))
        counts = (f"{_plural(1 if setup.git_email else 0, 'identidade git', 'identidades git')}, "
                  f"{_plural(len(setup.github_logins), 'conta GitHub', 'contas GitHub')} e "
                  f"{_plural(len(setup.claude_folders), 'pasta do Claude', 'pastas do Claude')}")
        box.append(_label(f"Montados a partir do que já existe nesta máquina: {counts}. Dá para ajustar tudo depois.",
                          "dim-label", wrap=True))
        if setup.has_legacy_install:
            box.append(_label("Os perfis abaixo vieram do ~/Perfis/perfis.conf da versão antiga.", "dim-label",
                              wrap=True))
        self.profile_cards = []
        removable = len(self.draft.profiles) > 1
        for profile in self.draft.profiles:
            card = ProfileCard(self, profile, removable)
            self.profile_cards.append(card)
            box.append(card)
        add = Gtk.Button(halign=Gtk.Align.START)
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Adicionar perfil"))
        add.connect("clicked", lambda *_: self.add_profile())
        box.append(add)
        return _scrolled(box)

    def add_profile(self) -> None:
        colors = list(ProfileColor)
        self.draft.profiles.append(Profile(name="Novo perfil", color=colors[len(self.draft.profiles) % len(colors)]))
        self.go_to(1)

    def remove_profile(self, profile: Profile) -> None:
        self.draft.profiles = [p for p in self.draft.profiles if p.id != profile.id]
        if self.initial_profile_id == profile.id:
            self.initial_profile_id = None
        self.go_to(1)

    def draft_changed(self) -> None:
        self._update_bottom_bar()

    def _normalized_draft(self):
        """Drops git identities left completely empty (the profile then does not switch git)."""
        draft = self.draft
        for profile in draft.profiles:
            identity = draft.git_identity(profile.git_identity_id)
            if identity is None:
                continue
            if not identity.name and not identity.email:
                draft.git_identities = [i for i in draft.git_identities if i.id != identity.id]
                profile.git_identity_id = None
            elif not identity.nickname.strip():
                identity.nickname = profile.name.strip() or "Git"
        used = {p.git_identity_id for p in draft.profiles}
        draft.git_identities = [i for i in draft.git_identities if i.id in used or not self._created_here(i)]
        if self.initial_profile_id is None or draft.profile(self.initial_profile_id) is None:
            self.initial_profile_id = draft.profiles[0].id if draft.profiles else None
        return draft

    def _created_here(self, identity) -> bool:
        existing = self.model.config
        return existing is None or existing.git_identity(identity.id) is None

    def _next_clicked(self, _button) -> None:
        if self.step == 1:
            draft = self._normalized_draft()
            issues = issues_in(draft)
            if issues:
                self.show_error(describe_issues(issues, draft))
                return
        self.go_to(self.step + 1)

    # MARK: step 3 — Aplicar

    def _build_apply(self) -> Gtk.Widget:
        environment = self.model.environment
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading.append(_label("Revise antes de aplicar", "title-1", wrap=True))
        heading.append(_label("Cada arquivo ganha só um bloco marcado. Backup em ~/.config/multicred/backups/.",
                              "dim-label", wrap=True))
        box.append(heading)

        shells = Adw.PreferencesGroup(title="Shells")
        self.shell_checks = {}
        for shell in SHELL_ORDER:
            installed = shell in self.setup.installed_shells
            files = (Integrations.rc_files(shell, environment) if shell != ShellKind.FISH
                     else [Integrations.fish_conf_file(environment)])
            subtitle = ", ".join(environment.abbreviate(str(f)) for f in files) if installed else "não instalado"
            row = Adw.ActionRow(title=shell.value, subtitle=GLib.markup_escape_text(subtitle))
            check = Gtk.CheckButton(active=installed and shell in self.shells, sensitive=installed,
                                    valign=Gtk.Align.CENTER)
            check.connect("toggled", self._shell_toggled, shell)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            row.set_sensitive(installed)
            if shell == self.setup.login_shell:
                row.add_suffix(_pill("seu shell", "ok"))
            self.shell_checks[shell] = check
            shells.add(row)
        self.shells = {s for s in self.shells if s in self.setup.installed_shells}
        box.append(shells)

        self.files_group = Adw.PreferencesGroup(title="Arquivos")
        self.files_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.files_list.add_css_class("boxed-list")
        self.files_group.add(self.files_list)
        box.append(self.files_group)
        box.append(_label("Abra um novo terminal depois de aplicar para que o shell carregue o perfil. Apps gráficos "
                          "pegam a pasta do Claude no próximo login da sessão.", "dim-label", "caption", wrap=True))
        self._refresh_initial_profile()
        self.refresh_preview()
        return _scrolled(box)

    def _shell_toggled(self, check, shell) -> None:
        if check.get_active():
            self.shells.add(shell)
        else:
            self.shells.discard(shell)
        self.refresh_preview()

    def options(self) -> InstallOptions:
        return InstallOptions(shells=[s for s in ShellKind if s in self.shells], remove_legacy=self.remove_legacy)

    def refresh_preview(self) -> None:
        child = self.files_list.get_first_child()
        while child is not None:
            self.files_list.remove(child)
            child = self.files_list.get_first_child()
        try:
            self.preview_changes = self.model.preview_install(self.options())
            self.preview_failed = False
        except InstallError as error:
            self.preview_changes, self.preview_failed = [], True
            self.files_list.append(self._message_row(describe_error(error, self.draft), "error"))
            self._update_bottom_bar()
            return
        if not self.preview_changes:
            self.files_list.append(self._message_row("Nada a mudar: tudo já está instalado.", "dim-label"))
        for change in self.preview_changes:
            self.files_list.append(self._change_row(change))
        self._update_bottom_bar()

    @staticmethod
    def _message_row(text: str, css: str) -> Gtk.ListBoxRow:
        label = _label(text, css, wrap=True)
        label.set_margin_top(12)
        label.set_margin_bottom(12)
        label.set_margin_start(12)
        label.set_margin_end(12)
        return Gtk.ListBoxRow(child=label, activatable=False)

    @staticmethod
    def _change_row(change) -> Gtk.ListBoxRow:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                          margin_start=12, margin_end=12)
        header = Gtk.Box(spacing=8)
        header.append(_label(change.path, "monospace", "heading", selectable=True))
        summary = KIND_TEXT[change.kind]
        if change.added_lines:
            summary += f" · +{_plural(len(change.added_lines), 'linha', 'linhas')}"
        if change.removed_lines:
            summary += f" · −{_plural(len(change.removed_lines), 'linha', 'linhas')}"
        header.append(_label(summary, "dim-label"))
        content.append(header)
        if change.added_lines or change.removed_lines:
            diff = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            diff.add_css_class("mc-diff")
            for prefix, lines, css in (("−", change.removed_lines, "mc-diff-removed"),
                                       ("+", change.added_lines, "mc-diff-added")):
                for line in lines:
                    text = f"{prefix} {line.expandtabs(4)}"
                    label = _label(text, "monospace", css, wrap=True, selectable=True)
                    diff.append(label)
            content.append(diff)
        return Gtk.ListBoxRow(child=content, activatable=False)

    def _refresh_initial_profile(self) -> None:
        profiles = self.draft.profiles
        if self.initial_profile_id is None or self.draft.profile(self.initial_profile_id) is None:
            self.initial_profile_id = profiles[0].id if profiles else None
        self._updating_dropdown = True
        self.initial_dropdown.set_model(Gtk.StringList.new([p.name for p in profiles]))
        index = next((i for i, p in enumerate(profiles) if p.id == self.initial_profile_id), 0)
        self.initial_dropdown.set_selected(index)
        self._updating_dropdown = False
        self.initial_dropdown.set_visible(len(profiles) > 1)
        self.initial_profile_label.set_visible(len(profiles) <= 1)
        profile = self.draft.profile(self.initial_profile_id)
        self.initial_profile_label.set_label(profile.name if profile else "")

    def _initial_changed(self, dropdown, _param) -> None:
        if getattr(self, "_updating_dropdown", False) or self.draft is None:
            return
        index = dropdown.get_selected()
        if 0 <= index < len(self.draft.profiles):
            self.initial_profile_id = self.draft.profiles[index].id
            self.initial_profile_label.set_label(self.draft.profiles[index].name)

    def _apply_clicked(self, _button) -> None:
        if self.applying:
            return
        self.applying = True
        self.show_error(None)
        draft = self.draft
        self.model.complete_onboarding(draft, self.options(), self.initial_profile_id, self._applied)

    def _applied(self, _result, error) -> None:
        self.applying = False
        saved = self.model.config is self.draft
        if error is not None and not (saved and isinstance(error, SwitchError)):
            self.show_error(describe_error(error, self.draft))
            return
        # Installed and saved; a failed first switch is reported by the window, which offers the profiles.
        self.on_finished(error)
