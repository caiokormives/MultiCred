"""Perfis (approved Linux design A + extras): the active profile's card, "Trocar só a conta
Claude", the other profiles with Ctrl+N and Ativar, "+ Novo perfil", and the Verificar
footer. Rebuilt from the model on every change (it is small)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ...core.models import ClaudeFolder  # noqa: E402
from .. import widgets  # noqa: E402
from ..widgets import escape  # noqa: E402


class ProfilesPage(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Perfis", tag="main")
        self.window, self.model = window, window.model
        self.rows: list[Adw.ActionRow] = []
        self.other_rows: list[Adw.ActionRow] = []
        self.hero_row: Adw.ActionRow | None = None
        self.detail_rows: dict[str, Adw.ActionRow] = {}
        self.claude_only_row: Adw.ComboRow | None = None
        self._claude_only_values: list = []
        self._updating = False

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.footer_dot = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.footer_dot.add_css_class("mc-dot")
        self.footer_label = widgets.label("Verificar", "dim-label")
        again = widgets.button("Verificar de novo", self.window.run_verification, "flat")
        again.add_css_class("accent")
        footer = Gtk.Box(spacing=10)
        footer.add_css_class("mc-footer")
        footer.append(self.footer_dot)
        footer.append(self.footer_label)
        footer.append(Gtk.Box(hexpand=True))
        footer.append(again)
        self.stack = Gtk.Stack()
        self.stack.add_named(widgets.scrolled(self.box), "profiles")
        new_button = widgets.button("Novo perfil", lambda: self.window.show_profile_editor(None), "pill",
                                    "suggested-action")
        new_button.set_halign(Gtk.Align.CENTER)
        self.empty = Adw.StatusPage(title="Nenhum perfil", icon_name="avatar-default-symbolic",
                                    description="Um perfil junta a identidade do git, a conta do GitHub, a pasta "
                                                "do Claude e as chaves de API que valem juntas.",
                                    child=new_button)
        self.stack.add_named(self.empty, "empty")
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.stack)
        view.add_bottom_bar(footer)
        self.set_child(view)
        self.refresh()

    # MARK: building

    def refresh(self) -> None:
        self._updating = True
        try:
            self._rebuild()
        finally:
            self._updating = False

    def _rebuild(self) -> None:
        child = self.box.get_first_child()
        while child is not None:
            self.box.remove(child)
            child = self.box.get_first_child()
        self.rows, self.other_rows, self.hero_row, self.detail_rows = [], [], None, {}
        self.claude_only_row = None
        self._refresh_footer()
        config = self.model.config
        if config is None or not config.profiles:
            self.stack.set_visible_child_name("empty")
            return
        self.stack.set_visible_child_name("profiles")
        active = self.model.active_profile
        if active is not None:
            self.box.append(self._hero(active))
            self.box.append(self._claude_only(active))
        else:
            hint = widgets.label("Nenhum perfil ativo: escolha um abaixo (Ctrl+1…9).", "dim-label", wrap=True)
            self.box.append(hint)

        group = Adw.PreferencesGroup(title="Outros perfis" if active is not None else "Perfis")
        group.set_margin_top(10)
        for index, profile in enumerate(config.profiles):
            if active is not None and profile.id == active.id:
                self.rows.append(self.hero_row)
                continue
            row = self._other_row(profile, index)
            group.add(row)
            self.other_rows.append(row)
            self.rows.append(row)
        # A PreferencesRow (not a plain ListBoxRow) so the group keeps it inside the boxed list.
        new_row = Adw.PreferencesRow(activatable=True, title="Novo perfil")
        new_row.add_css_class("mc-new-row")
        new_label = widgets.label("+ Novo perfil", xalign=0.5)
        new_label.set_margin_top(12)
        new_label.set_margin_bottom(12)
        new_row.set_child(new_label)
        new_row.connect("activate", lambda *_: self.window.show_profile_editor(None))
        group.add(new_row)
        self.box.append(group)

    def _hero(self, profile) -> Gtk.Widget:
        config, state = self.model.config, self.model.state
        card = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        card.add_css_class("boxed-list")
        since = state.activated_at.astimezone().strftime("%H:%M") if state.activated_at else None
        hero = Adw.ActionRow(title=escape(profile.name), subtitle=f"Ativo desde {since}" if since else "Ativo",
                             activatable=False)
        hero.add_css_class("mc-hero")
        hero.add_prefix(widgets.avatar(profile.icon.value if profile.icon else profile.name, profile.color, 64))
        if self.model.inconsistent or self._verification_has_problems():
            hero.add_suffix(widgets.pill("● Inconsistente", "warning"))
        else:
            hero.add_suffix(widgets.pill("● Tudo certo", "ok"))
        hero.add_suffix(widgets.button("Editar", lambda: self.window.show_profile_editor(profile.id)))
        hero.connect("activated", self.window.on_profile, profile.id)
        card.append(hero)
        self.hero_row = hero

        identity = config.git_identity(profile.git_identity_id)
        github = config.github_account(profile.github_account_id)
        applied = config.claude_account(state.applied_claude_account_id)
        claude = applied.nickname if applied is not None else "login da pasta"
        keys = [config.api_key(k) for k in profile.api_key_ids]
        details = {
            "git": (f"{escape(identity.name)} · <tt>{escape(identity.email)}</tt>" if identity
                    else "<span alpha='60%'>não troca o git</span>"),
            "GitHub": (f"{escape(github.username)} <span alpha='60%'>· {escape(github.host)}</span>" if github
                       else "<span alpha='60%'>não troca o GitHub</span>"),
            "Claude": (f"{escape(claude)} <span alpha='60%'>· pasta "
                       f"<tt>{escape(self.model.folder_name(profile.claude_config_dir))}</tt></span>"),
            "Chaves": (" <span alpha='60%'>·</span> ".join(f"<tt>{escape(k.env_var)}</tt>" for k in keys if k)
                       or "<span alpha='60%'>nenhuma</span>"),
        }
        for key, value in details.items():
            row = Adw.ActionRow(title=value, activatable=False)
            row.add_css_class("mc-detail")
            row.add_prefix(widgets.label(key, "mc-key"))
            card.append(row)
            self.detail_rows[key] = row
        return card

    def _claude_only(self, profile) -> Gtk.Widget:
        config, state = self.model.config, self.model.state
        default = config.claude_account(profile.claude_account_id)
        options = [f"Conta do perfil ({default.nickname if default else 'login da pasta'})"]
        values = [profile.claude_account_id]
        if default is not None:
            options.append("Login da pasta")
            values.append(None)
        for account in config.claude_accounts:
            if default is None or account.id != default.id:
                options.append(account.nickname)
                values.append(account.id)
        override = state.claude_override_id
        if override is None:
            selected = 0
        elif override == ClaudeFolder.OWN_LOGIN_OVERRIDE:
            selected = values.index(None) if default is not None else 0
        else:
            selected = values.index(override) if override in values else 0
        row = widgets.combo("Trocar só a conta Claude", options, selected,
                            subtitle="Git, GitHub e pasta continuam os do perfil")
        row.set_sensitive(not self.model.switching)
        row.connect("notify::selected", self._claude_only_changed)
        self.claude_only_row, self._claude_only_values = row, values
        group = Adw.PreferencesGroup()
        group.add(row)
        return group

    def _other_row(self, profile, index: int) -> Adw.ActionRow:
        config = self.model.config
        identity = config.git_identity(profile.git_identity_id)
        github = config.github_account(profile.github_account_id)
        parts = [escape(identity.email) if identity else "git sem troca"]
        if github is not None:
            parts.append(escape(github.username))
        if self.model.needs_claude_login(profile):
            parts.append("<span foreground='#D9770B' weight='bold'>⚠ Entrar no Claude</span>")
        row = Adw.ActionRow(title=escape(profile.name), subtitle=" · ".join(parts),
                            activatable=not self.model.switching)
        row.add_prefix(widgets.avatar(profile.icon.value if profile.icon else profile.name, profile.color, 40))
        if index < 9:
            row.add_suffix(widgets.kbd(f"Ctrl+{index + 1}"))
        activate = widgets.button("Ativar", lambda: self.window.on_profile(None, profile.id))
        activate.set_sensitive(not self.model.switching)
        row.add_suffix(activate)
        row.connect("activated", self.window.on_profile, profile.id)
        return row

    def _verification_has_problems(self) -> bool:
        report = self.model.verification
        return report is not None and report.ok_count < report.total

    def _refresh_footer(self) -> None:
        report = self.model.verification
        for tone in ("ok", "warning"):
            self.footer_dot.remove_css_class(tone)
        if report is None:
            self.footer_label.set_label("Verificar")
            return
        ok, total = report.ok_count, report.total
        self.footer_label.set_label(f"Verificar: {ok} de {total} ok")
        self.footer_dot.add_css_class("ok" if ok == total else "warning")

    # MARK: events

    def _claude_only_changed(self, row, _param) -> None:
        if self._updating or self.model.switching:
            return
        index = row.get_selected()
        if 0 <= index < len(self._claude_only_values):
            self.window.switch_claude_only(self._claude_only_values[index])

    # MARK: introspection for tests and screenshots

    def hero_title(self) -> str | None:
        return self.hero_row.get_title() if self.hero_row else None

    def detail_texts(self) -> dict[str, str]:
        import re
        return {key: re.sub(r"<[^>]+>", "", row.get_title()) for key, row in self.detail_rows.items()}

    def claude_only_options(self) -> list[str]:
        if self.claude_only_row is None:
            return []
        model = self.claude_only_row.get_model()
        return [model.get_string(i) for i in range(model.get_n_items())]

