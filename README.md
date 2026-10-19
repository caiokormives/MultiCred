# MultiCred

App de barra de menus para macOS 14+ que troca, com um clique, a identidade do git, a conta ativa
do GitHub CLI, a conta do Claude Code (`CLAUDE_CONFIG_DIR`) e as chaves de API dos terminais.

## Linux (Ubuntu e Pop!_OS)

Há uma versão para Ubuntu 24.04+ e Pop!_OS 24.04+ (GTK 4 + libadwaita, na bandeja do COSMIC ou
do GNOME), em `linux/`: `linux/packaging/build-deb.sh` gera o `.deb`, instalado com
`sudo apt install ./linux/dist/multicred_<versão>_all.deb`. Instalação, o que o app altera, o
que nunca toca, desinstalação e as diferenças em relação ao macOS: [linux/README.md](linux/README.md).
O restante deste arquivo é sobre o macOS.

## Instalar

1. Baixe o `MultiCred.dmg` mais recente em [Releases](https://github.com/Rafael-Leivas/MultiCred/releases/latest).
2. Abra o `.dmg` e arraste o MultiCred para **Aplicativos**.
3. Abra o app. O onboarding detecta git, gh, Claude Code, contas existentes e a versão antiga em
   `~/Perfis`, mostra o diff de cada arquivo e aplica. Depois, abra um novo Terminal.

Ou, com Homebrew: `brew install --cask Rafael-Leivas/tap/multicred`.

**Confira o download:** `shasum -a 256 -c SHA256SUMS.txt` e
`gh attestation verify MultiCred.dmg --repo Rafael-Leivas/MultiCred`. O código é aberto (MIT), o app não tem
telemetria nem chamadas de rede próprias, e a [auditoria de segurança](docs/SECURITY.md) está no repositório.

Compilar você mesmo: `scripts/release.sh` gera `dist/MultiCred.app` (assinatura ad hoc, hardened runtime).

Desenvolvimento: no Xcode, o esquema **MultiCred** roda em modo de teste (nada no Mac muda),
**MultiCred (login real)** faz só o login do Claude de verdade numa pasta de teste, e
**MultiCred (sistema real)** usa o sistema real.

## O que o app altera

- Um bloco marcado (`# >>> MultiCred >>>`) no fim de `~/.zshrc`, `~/.bash_profile`/`~/.bashrc`
  (se bash estiver ativo) e do gitconfig global (`[include]` do arquivo gerado).
- `~/.config/fish/conf.d/multicred.fish` (se fish estiver ativo).
- `launchctl setenv/unsetenv CLAUDE_CONFIG_DIR` (apps gráficos abertos depois da troca).
- A conta ativa do `gh` (`gh auth switch`).
- Itens `app.multicred.apikey` no Keychain (só os valores das chaves de API).
- `~/Library/Application Support/MultiCred/` (config, estado, arquivos gerados, backups) e
  `~/Library/Logs/MultiCred/`. Item de login (opcional).

## O que o app nunca toca

Tokens do Claude Code e do gh, o conteúdo das pastas `~/.claude*`, o `hosts.yml` do gh (só via
`gh`), e qualquer linha dos seus arquivos fora do bloco do MultiCred.

## Desinstalar

Ajustes › Desinstalar: remove só os blocos, o arquivo do fish, a variável do launchd, as chaves do
Keychain e a pasta do app; os backups vão para `MultiCred-backups-<data>`. As contas continuam
logadas nas próprias ferramentas.

Mais detalhes: `docs/PLAN.md` (decisões) e `docs/SECURITY.md` (auditoria).

## Contribuir e segurança

Veja [CONTRIBUTING.md](CONTRIBUTING.md). Vulnerabilidades: [SECURITY.md](SECURITY.md). Licença: [MIT](LICENSE).
