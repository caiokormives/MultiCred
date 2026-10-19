# MultiCred para Linux

App de bandeja para Ubuntu e Pop!_OS que troca, com um clique e numa transação, a identidade do
git, a conta ativa do GitHub CLI, a pasta e a conta do Claude Code (`CLAUDE_CONFIG_DIR`) e as
chaves de API dos terminais. Se um passo falha, os anteriores são desfeitos na ordem inversa; se
nem isso dá certo, o app marca o estado como inconsistente e oferece **Verificar › Corrigir**.

É o mesmo app do macOS (mesmas regras, mesmo formato de `config.json`), escrito em Python com
GTK 4 e libadwaita. O código fica em `linux/`; o app do macOS não muda.

## Requisitos

- Ubuntu 24.04 ou mais novo, ou Pop!_OS 24.04 ou mais novo (libadwaita ≥ 1.5, Python ≥ 3.12).
  O 22.04 não é suportado.
- Área de ícones (bandeja): o painel do COSMIC (Pop!_OS) já mostra; no GNOME (Ubuntu) é preciso a
  extensão AppIndicator, que o Ubuntu traz ativada. Sem bandeja o app ainda funciona pela janela.
- `git`; recomendado o `gh` (GitHub CLI) e o Claude Code (`claude`). O pacote instala o resto:
  PyGObject, GTK 4, libadwaita, libsecret e `secret-tool`.

## Gerar e instalar

```sh
linux/packaging/build-deb.sh                       # gera linux/dist/multicred_<versão>_all.deb
sudo apt install ./linux/dist/multicred_<versão>_all.deb
```

Para gerar o pacote: `dpkg-deb`, `fakeroot`, `dpkg-parsechangelog` (pacote `dpkg-dev`) e o
pycairo (`python3-gi-cairo`). Se `gir1.2-rsvg-2.0` estiver instalado, os ícones PNG saem do SVG;
senão, o mesmo desenho é feito com cairo.

O pacote instala o app em `/usr/lib/multicred`, o comando `/usr/bin/multicred`, o atalho
`/usr/share/applications/app.multicred.MultiCred.desktop`, os ícones em
`/usr/share/icons/hicolor/` e os metadados do AppStream em `/usr/share/metainfo/`. Não roda nenhum
script como root na instalação: tudo que é do seu usuário é feito pelo próprio app, no onboarding.

## Primeira execução

Abra **MultiCred** pelo lançador. O onboarding tem três passos, dentro da janela:

1. **Preparar**: mostra o que foi encontrado (git, gh, Claude Code, com versão ou o comando para
   instalar), as contas que já existem e a versão antiga em scripts (`~/Perfis`), se houver.
2. **Perfis**: perfis sugeridos a partir do que foi encontrado, para editar.
3. **Aplicar**: escolha dos shells (bash, zsh, fish) e o diff exato de cada arquivo antes de
   aplicar. Depois disso, terminais já abertos adotam o perfil no próximo prompt.

Na janela: Ctrl+1…Ctrl+9 ativam os nove primeiros perfis e Ctrl+R abre Verificar.

## Modo de teste

```sh
multicred --test-mode                      # HOME de teste em /tmp/multicred-sandbox, com perfis de exemplo
multicred --test-mode --sandbox ~/teste    # outro HOME de teste
multicred --fresh                          # HOME de teste vazio (novo a cada vez), para ver o onboarding
```

No modo de teste o app usa um HOME de teste, não executa comandos externos (`git`, `gh`,
`systemctl`, `claude`: ficam só registrados ou são simulados), guarda segredos e logins só na
memória e mostra a faixa "MODO DE TESTE". Nada do sistema real muda.

## O que o app altera

- Um bloco marcado (`# >>> MultiCred >>>` … `# <<< MultiCred <<<`) no fim de `~/.bashrc` (e de
  `~/.bash_profile`, só se ele já existir) e de `~/.zshrc`, conforme os shells escolhidos. O
  bloco só carrega `~/.config/multicred/shell/hook.<shell>`.
- `~/.config/fish/conf.d/multicred.fish` (se o fish for escolhido).
- No gitconfig global (`~/.gitconfig`, ou `~/.config/git/config` se só ele existir), um bloco
  marcado com `[include] path = "~/.config/multicred/gitconfig"`.
- A conta ativa do `gh` (`gh auth switch`); "Entrar no GitHub" roda `gh auth login --web`.
- A pasta do Claude para apps gráficos: `systemctl --user set-environment CLAUDE_CONFIG_DIR=…`
  (ou `unset-environment` quando o perfil usa a pasta padrão) e o arquivo
  `~/.config/environment.d/90-multicred.conf`.
- No chaveiro do sistema (Secret Service; no Ubuntu e no Pop!_OS, o GNOME Keyring): itens com service
  `app.multicred.apikey` (valores das chaves de API e dos tokens do Claude) e
  `app.multicred.claude-login` (logins completos do Claude guardados pelo MultiCred).
- Nas pastas do Claude usadas pelos perfis:
  - `settings.json`: só a chave `"attribution": {"commit": "", "pr": ""}`, quando a conta do GitHub
    do perfil desliga a co-autoria da Claude, e só esse valor é removido quando ela é religada;
  - com contas de login completo: a chave `claudeAiOauth` de `<pasta>/.credentials.json` (0600)
    recebe o login da conta, e em `.claude.json` (`~/.claude.json` na pasta padrão) muda só
    `oauthAccount`. Sessões do Claude Code já abertas adotam a conta sem reiniciar. Antes de trocar,
    o login que estava na pasta é guardado de volta para o dono dele.
- `~/.config/multicred/` (0700): configuração, estado, arquivos gerados (gitconfig, `env.sh`,
  `env.fish`, hooks), `manifest.json` e backups dos arquivos alterados em `backups/`.
- `~/.local/state/multicred/`: logs.
- `~/.config/autostart/app.multicred.MultiCred.desktop`, se você ligar **Ajustes › Abrir ao
  iniciar a sessão** (só com o app instalado pelo pacote). Ele abre o app só na bandeja e reaplica
  o perfil ativo, porque o `systemctl --user` esquece a variável a cada reinício.

As chaves de API e os tokens nunca vão para arquivo: o `env.sh` só tem o nome da variável e o
lê com `secret-tool lookup` quando o perfil muda.

## O que o app nunca toca

- Qualquer linha dos seus arquivos fora do bloco do MultiCred.
- O `hosts.yml` e os tokens do `gh` (só via o próprio `gh`); trocar de perfil não desloga ninguém.
- Conversas, projetos, agents e skills das pastas do Claude; em `.credentials.json` o resto
  (inclusive os logins de MCP, `mcpOAuth`) fica como está, e nos JSON só mudam as chaves acima.
- Arquivos de outros usuários e do sistema (`/etc`, `/usr`): o app roda como você, nunca como root.
- No modo de teste, nada fora do HOME de teste.

O app também nunca mostra tokens na tela.

## Desinstalar

1. **Ajustes › Desinstalar o MultiCred…**: a confirmação lista tudo antes. Remove só os blocos
   marcados (um bloco sem o marcador de fim fica como está e o app avisa para remover à mão), o
   arquivo do fish, o `environment.d`, a entrada de autostart, a variável do `systemctl --user`
   (se foi o MultiCred que a definiu) e os segredos do app no chaveiro. Cada pasta do Claude volta
   para o próprio login, a co-autoria desligada é desfeita e os logins guardados são apagados.
   Opcional: também sair (`claude auth logout`) e apagar as pastas do Claude que o MultiCred criou.
   Os backups vão para `~/.config/multicred-backups-<data>` e `~/.config/multicred/` é apagada.
   As contas continuam logadas no `gh` e no Claude. O app fecha no fim.
2. `sudo apt remove multicred`.

## Diferenças em relação ao macOS

| No macOS | No Linux |
|---|---|
| HUD flutuante depois da troca | Notificação da área de trabalho (o Wayland não permite janela flutuante posicionada). "Notificação curta" some em uns 2 s; com sessões do Claude Code abertas, a notificação fica. |
| `launchctl setenv`: apps gráficos abertos depois da troca já veem a pasta do Claude | `systemctl --user` vale para o que o systemd/D-Bus abrir a partir dali; para os apps do lançador e do dock, a pasta vale a partir do próximo login (`environment.d`). Terminais trocam no próximo prompt, como no Mac. |
| Ícone na barra de menus sempre disponível | Bandeja via StatusNotifierItem: nativa no COSMIC; no GNOME puro precisa da extensão AppIndicator. Sem ela, a janela abre com um aviso e fechá-la encerra o app. |
| Chaveiro do macOS (Keychain) | Secret Service (libsecret). O login de cada pasta do Claude é o arquivo `<pasta>/.credentials.json`. |
| `~/Library/Application Support/MultiCred/` e `~/Library/Logs/MultiCred/` | `~/.config/multicred/` e `~/.local/state/multicred/` |
| Item de login (`SMAppService`) | Autostart XDG em `~/.config/autostart/` |
| `.bash_profile` + `.bashrc` | `.bashrc` (o `.profile` do Ubuntu já o carrega); `.bash_profile` só se já existir |
| Atalhos ⌘1…⌘9 e ⌘R | Ctrl+1…Ctrl+9 e Ctrl+R, só com a janela em foco: o Wayland não permite atalho global |
| Cabeçalho do menu em SwiftUI | Menu da bandeja com o cabeçalho em linhas de texto e perfis com ícone colorido |

## Desenvolvimento

```sh
cd linux
python3 -m multicred --test-mode                  # roda do código-fonte, sem instalar
tests/run-ui-tests.sh                             # suíte inteira num Xvfb e num D-Bus privados
python3 -m unittest discover -s tests -t .        # sem tela: os testes de janela/bandeja/chaveiro pulam
python3 -m unittest tests.test_packaging -v       # gera o .deb num diretório temporário e o confere
```

- Testes de janela, D-Bus e chaveiro só rodam pelo `tests/run-ui-tests.sh` (variável
  `MULTICRED_PRIVATE_BUS=1`); sem ele o GTK cairia na sua área de trabalho real. Com
  `MULTICRED_SCREENS_DIR=<pasta>` os testes de interface salvam prints. `MULTICRED_LINTIAN=<comando>`
  faz o teste do pacote rodar o lintian por esse comando, se ele não estiver no PATH.
- Os testes usam HOMEs temporários e implementações falsas; nunca o sistema real.
- Regras de arquitetura (conferidas por `tests/test_architecture.py`): `multicred.core` nunca
  importa GTK/Adw/Gdk/Gsk; só `core/real_env.py` lê `os.environ`, `pwd`, `Path.home()` e
  `expanduser`; só `core/runner.py` e `core/pty.py` usam `subprocess`, sem `shell=True`. Tudo que
  depende da máquina vem do `SystemEnvironment` injetado, e processos filhos recebem um ambiente
  explícito, nunca o do app.
- Textos da interface em português do Brasil; identificadores e comentários em inglês.

Mais detalhes: `docs/PLAN.md` (decisões) e `docs/SECURITY.md` (auditoria), na raiz do repositório.
