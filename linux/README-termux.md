# MultiCred no Termux (Android)

O MultiCred troca, numa transação, a identidade do git, a conta ativa do GitHub CLI, a pasta e a
conta do Claude Code (`CLAUDE_CONFIG_DIR`) e as chaves de API dos terminais. No Termux ele é só o
comando `multicred`: as mesmas regras e o mesmo `config.json` do app do macOS e do Linux, sem
janela nem bandeja. Se um passo da troca falha, os anteriores são desfeitos na ordem inversa.

## Instalar

O pacote é gerado num computador e copiado para o celular:

```sh
linux/packaging/build-deb-termux.sh     # gera linux/dist/termux/multicred_<versão>_all.deb
```

No Termux, sem root:

```sh
pkg update
pkg install python git gh util-linux    # util-linux traz o setsid, usado nos logins pelo terminal
apt install ./multicred_<versão>_all.deb
```

O `apt` instala o `python` e o `git` (dependências) e recomenda `gh`, `nodejs-lts` e
`util-linux`; se ele não instalar os recomendados, use o `pkg install` acima. O pacote põe o app
em `$PREFIX/lib/multicred` e o comando em `$PREFIX/bin/multicred`, e não roda nenhum script na
instalação. O Claude Code é opcional; veja [Claude Code no Android](#claude-code-no-android).

## Primeiro uso

```sh
multicred configurar
```

Procura git, gh e Claude Code (com o comando para instalar o que falta), as contas que já existem
e a versão antiga em scripts (`~/Perfis`); sugere perfis para editar; mostra o diff exato do
`~/.bashrc` (e do `~/.zshrc` ou do fish, se escolhidos) e do gitconfig antes de instalar; e ativa o
primeiro perfil. Terminais já abertos adotam o perfil no próximo prompt.

## Comandos

| Comando | Exemplo | O que faz |
|---|---|---|
| `configurar` | `multicred configurar` | Configuração inicial (ou de novo): detecta, sugere perfis, mostra o diff e instala. |
| `status` | `multicred status` | Perfil ativo, desde quando, git, GitHub, pasta e conta do Claude e chaves. |
| `trocar <perfil>` | `multicred trocar Trabalho` ou `multicred trocar 2` | Troca git, GitHub, Claude e chaves para o perfil (nome sem diferenciar maiúsculas, ou o número 1–9). |
| `perfis` | `multicred perfis` | Lista os perfis (✓ no ativo, "⚠︎ Claude precisa entrar" quando falta login); num terminal abre o menu para criar, editar, ativar e apagar perfis (`--menu` força o menu com entrada por script). |
| `contas` | `multicred contas` | Lista Git, GitHub, Claude e Chaves de API; num terminal abre o menu para cadastrar, editar e remover (`--menu` também). |
| `claude [conta]` | `multicred claude Marco` | Troca só a conta Claude; a pasta do perfil fica. Sem argumento lista as contas; `multicred claude pasta` volta para o login da própria pasta. |
| `entrar claude <conta>` | `multicred entrar claude Marco` | Entra de novo numa conta Claude (`claude auth login` no próprio terminal, ou um token colado). |
| `entrar pasta [caminho]` | `multicred entrar pasta ~/.claude-pessoal` | Login da própria pasta do Claude (a do perfil ativo sem caminho). |
| `entrar github [servidor]` | `multicred entrar github` | `gh auth login --web` no terminal (o código e o endereço do GitHub aparecem no terminal). |
| `verificar` | `multicred verificar` | Compara o que o perfil ativo espera com o que git, gh, os shells e os segredos têm agora. |
| `verificar --corrigir` | `multicred verificar --corrigir` | Mostra os comandos de "Corrigir tudo" e pergunta antes de executar. |
| `desinstalar` | `multicred desinstalar` | Mostra o que volta ao original e, se você confirmar, desinstala. |
| `--version` | `multicred --version` | Versão do MultiCred. |

Sem comando, `multicred` mostra a ajuda. Códigos de saída: 0 deu certo (no `verificar`, tudo de
acordo), 1 falhou ou ficou algo para resolver, 2 uso errado, 130 interrompido com Ctrl+C num menu.

Modo de teste: `multicred --test-mode status` (ou qualquer outro comando) usa um HOME de teste em
`$TMPDIR/multicred-sandbox` com perfis de exemplo, não executa `git`, `gh` nem `claude` (só registra)
e guarda segredos só na memória. `--fresh` começa de um HOME de teste vazio.

## O que o MultiCred altera

- `~/.config/multicred/` (0700): `config.json`, `state.json`, os arquivos gerados (o gitconfig do
  perfil, `env.sh`, `env.fish`, os hooks dos shells), `manifest.json` e os backups dos arquivos
  alterados em `backups/`.
- `~/.config/multicred/secrets/` (pasta 0700, arquivos 0600): um arquivo por chave de API ou token do
  Claude, e em `claude-login/` os logins completos do Claude guardados pelo MultiCred. O Android não
  tem chaveiro do sistema; o hook do shell lê esses arquivos com comandos internos do shell, sem
  rodar programa nenhum a cada prompt.
- Um bloco marcado (`# >>> MultiCred >>>` … `# <<< MultiCred <<<`) no fim do `~/.bashrc` (e do
  `~/.zshrc`, se o zsh for escolhido), ou `~/.config/fish/conf.d/multicred.fish` para o fish. O bloco
  só carrega `~/.config/multicred/shell/hook.<shell>`, que exporta as chaves e o `CLAUDE_CONFIG_DIR`.
- No gitconfig global (`~/.gitconfig`), um bloco marcado com
  `[include] path = "~/.config/multicred/gitconfig"`.
- A conta ativa do `gh` (`gh auth switch`); `multicred entrar github` roda `gh auth login --web`.
- Nas pastas do Claude usadas pelos perfis: em `settings.json` só a chave `attribution` (quando a
  conta do GitHub do perfil desliga a co-autoria da Claude); com contas de login completo, a chave
  `claudeAiOauth` de `<pasta>/.credentials.json` (0600) e `oauthAccount` em `.claude.json`.
- `~/.local/state/multicred/`: logs.

## O que ele nunca toca

- Qualquer linha dos seus arquivos fora do bloco do MultiCred.
- O `hosts.yml` e os tokens do `gh` (só via o próprio `gh`); trocar de perfil não desloga ninguém.
- Conversas, projetos, agents e skills das pastas do Claude; em `.credentials.json` o resto
  (inclusive `mcpOAuth`) fica como está.
- Os arquivos do Termux em `$PREFIX` (além do próprio pacote) e qualquer coisa fora do seu HOME.
  Não pede root, não usa `/tmp` (usa `$TMPDIR`) e não procura systemd nem D-Bus, que o Android não tem.
- Tokens nunca aparecem na tela.

## Limitações

- Sem janela, bandeja, notificações ou atalhos: tudo é pelo comando `multicred`.
- Claude Code no Android: a partir da 2.1.113 o pacote do npm procura um binário nativo que não
  existe para Android e para com "native binary not installed" (issue #50270 da Anthropic). O
  `multicred configurar` e o `multicred verificar` detectam isso e mostram o caminho:
  - instalar a última versão que roda, com a atualização automática desligada:
    `pkg install nodejs-lts` e
    `DISABLE_AUTOUPDATER=1 npm install -g @anthropic-ai/claude-code@2.1.112`;
  - manter a atualização desligada: `"env": {"DISABLE_AUTOUPDATER": "1"}` no `settings.json` da
    pasta do Claude (ou `export DISABLE_AUTOUPDATER=1` no `~/.bashrc`) e proteger a pasta do pacote:
    `chmod -R a-w $PREFIX/lib/node_modules/@anthropic-ai/claude-code`;
  - ou usar o Claude Code num Ubuntu dentro do Termux:
    `pkg install proot-distro && proot-distro install ubuntu && proot-distro login ubuntu` (mais
    lento; a troca de pasta e conta do MultiCred não vale lá dentro, e sessões do Claude abertas no
    proot não entram na contagem de sessões).
- A atualização automática só é vista como desligada quando está no `settings.json` ou no
  `.claude.json` da pasta em uso; um `export DISABLE_AUTOUPDATER=1` no shell funciona, mas o
  `verificar` não consegue enxergá-lo.
- Os logins pelo terminal (`entrar claude`, `entrar pasta`, `entrar github` e os dos menus) rodam
  num pseudo-terminal pelo `setsid`; sem ele, instale com `pkg install util-linux`.
- O leitor de segredos do fish (`env.fish`) foi testado só no Linux, não num aparelho.

## Desinstalar

1. `multicred desinstalar`: mostra o diff de cada arquivo (só o bloco do MultiCred sai) e o que
   mais será feito, e pergunta "Desinstalar? [s/N]" (Enter ou "n" cancela: "Nada foi alterado.").
   Remove os blocos, o arquivo do fish, os segredos em `~/.config/multicred/secrets` e
   `~/.config/multicred/` (os backups vão para `~/.config/multicred-backups-<data>`); cada pasta do
   Claude volta para o próprio login e a co-autoria desligada é desfeita. Se o MultiCred criou
   pastas do Claude, pergunta antes se também deve sair delas (`claude auth logout`) e apagá-las.
   As contas continuam logadas no `gh` e no Claude.
2. `apt remove multicred`.
