# Claude Usage Tray (Windows)

Um widget de **bandeja do sistema (system tray)** para Windows que monitora o
quanto da sua conta Claude (assinatura Pro/Max) já foi consumido. É o
equivalente Windows do widget de Waybar do projeto
[`ai-usagebar`](https://github.com/akitaonrails/ai-usagebar), que só funciona no Linux.

[![Baixar .exe](https://img.shields.io/badge/Baixar-ClaudeUsageTray.exe-2ea44f?style=for-the-badge&logo=windows)](https://github.com/tedrc/ai-usage/releases/latest/download/ClaudeUsageTray.exe)

> **Download rápido:** clique no botão acima para baixar o executável pronto
> (não precisa de Python). Veja a [página de releases](https://github.com/tedrc/ai-usage/releases) para todas as versões.

---

## O que ele mostra

- **Ícone colorido** na bandeja com o maior percentual de uso entre as janelas:
  - 🟢 **verde** — abaixo de 70% (tranquilo)
  - 🟠 **âmbar** — entre 70% e 90% (atenção)
  - 🔴 **vermelho** — 90% ou mais (quase no limite)
- **Tooltip** (passar o mouse): plano + uso das janelas de 5h e 7 dias.
- **Janela de detalhes** (clique no ícone): explica cada limite com barra de
  progresso, percentual, quando reseta e um conselho automático. Agora também
  mostra **tendência** (↑/↓ %/hora) e **projeção** de quando cada janela
  atinge 100% no ritmo atual.
- **Tokens reais** (menu → *Tokens reais (7 dias)*): consumo de tokens de
  verdade lido dos logs do Claude Code. Mostra **entrada+saída** (o que você
  de fato gastou) separado do **cache** (leituras enormes mas baratas), com
  **quebra por modelo** (Opus/Sonnet/Haiku). Mensagens repetidas no log
  (streaming, turnos de ferramenta, sessões retomadas) são deduplicadas por
  `message.id`. O custo em US$ é só **referência de volume** a preço de API
  público — em planos Pro/Max/Enterprise não há cobrança por token.
- **Notificações**: toast do Windows quando uma janela cruza 70%, 90% ou 100%.
- **Atualizar agora** (menu): força uma atualização imediata sem esperar o
  ciclo de 5 minutos.
- **Histórico**: cada leitura é gravada num banco SQLite local
  (`~/.claude/usage_tray_history.db`) — base para tendência e projeção.

### As janelas de limite

| Janela | O que é |
|---|---|
| **5 horas** | Limite de curto prazo. Reseta 5h após o primeiro uso da janela. |
| **7 dias (todos os modelos)** | Limite semanal geral — costuma ser o que trava o uso por mais tempo. |
| **7 dias (Sonnet)** | Limite semanal específico do modelo Sonnet. |
| **Créditos extras** | Pay-as-you-go: cobrança avulsa quando o plano se esgota (se habilitado). |

### Tokens reais vs. percentual de limite

O endpoint da Anthropic só informa **% de utilização** de cada janela — não
quantos tokens você gastou. Os tokens reais vêm de outra fonte: os logs de
sessão que o Claude Code grava em `~/.claude/projects/*/*.jsonl`. O widget
soma `input`, `output` e `cache` por modelo e estima o custo a preço de API
público (seu plano é fixo; o valor é só referência de "quanto custaria via
API"). Tabela de preços fica em `_PRICING` no `usage_extras.py` — ajuste se
os preços mudarem.

---

## Como rodar

### Opção A — executável (recomendado, não precisa de Python)

Dê dois cliques em **`ClaudeUsageTray.exe`**. O ícone aparece na bandeja
(pode estar escondido na setinha **▲** ao lado do relógio — arraste para fixar).

### Opção B — direto pelo Python

```powershell
pip install pystray pillow requests
python claude_usage_tray.py
```

Use `pythonw claude_usage_tray.py` para rodar sem janela de console.

---

## Iniciar junto com o Windows

Rode uma vez (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File .\install-startup.ps1
```

Isso cria um atalho em `shell:startup`, então o widget sobe sozinho a cada
login. Para remover: apague o atalho `ClaudeUsageTray` da pasta
`shell:startup` (Win+R → `shell:startup`).

---

## Como funciona por dentro

1. **Lê as credenciais** que o Claude Code grava em
   `%USERPROFILE%\.claude\.credentials.json` (o blob OAuth `claudeAiOauth`).
2. **Renova o token** automaticamente se estiver perto de expirar, via
   `POST https://platform.claude.com/v1/oauth/token` (mesmo fluxo do
   `src/anthropic/oauth.rs` deste repo). O `client_id` usado é o ID público
   da CLI do Claude — não é segredo.
3. **Consulta o uso** em
   `GET https://api.anthropic.com/api/oauth/usage` com o header
   `anthropic-beta: oauth-2025-04-20`.
4. **Atualiza o ícone** a cada 5 minutos. Se algo falhar, mostra o erro no
   tooltip em vez de fechar.

### Configuração

Edite as constantes no topo de `claude_usage_tray.py`:

| Constante | Padrão | Função |
|---|---|---|
| `POLL_INTERVAL_SECS` | `300` | Frequência de atualização (segundos). |
| `CREDS_PATH` | `~/.claude/.credentials.json` | Caminho das credenciais (ajuste se usa WSL). |
| `_PRICING` (em `usage_extras.py`) | preços públicos | Tabela US$/1M tokens por modelo, usada no custo estimado. |
| `_THRESHOLDS` (em `usage_extras.py`) | `(70, 90, 100)` | Níveis que disparam notificação toast. |

Depois de editar, **reconstrua o .exe** (veja abaixo).

---

## Reconstruir o executável

```powershell
pip install pyinstaller
python -m PyInstaller --noconsole --onefile --name ClaudeUsageTray `
  --distpath . --workpath build --specpath build claude_usage_tray.py
```

O `.exe` resultante fica nesta pasta. As pastas `build/` são temporárias.

---

## Limitações e avisos

- **Só funciona com conta de assinatura** (Pro/Max via OAuth). Uso por API key
  avulsa não é coberto por esse endpoint.
- O endpoint `/api/oauth/usage` é **não-documentado** e pode mudar de formato
  sem aviso. Se parar de funcionar, capture a resposta nova e ajuste as chaves
  `five_hour` / `seven_day` / `seven_day_sonnet` no código.
- Se você usa o Claude Code **dentro do WSL**, as credenciais ficam no
  sistema de arquivos do WSL — ajuste `CREDS_PATH`.
- O token é renovado e regravado no arquivo de credenciais, mantendo uma única
  fonte de verdade compartilhada com o Claude Code.

---

## Arquivos desta pasta

| Arquivo | Descrição |
|---|---|
| `claude_usage_tray.py` | Código-fonte do widget (tray, janelas, poll loop). |
| `usage_extras.py` | Analytics: histórico, projeção, tokens reais, custo, notificações. |
| `ClaudeUsageTray.exe` | Executável standalone (gerado pelo PyInstaller). |
| `install-startup.ps1` | Cria o atalho de inicialização automática. |
| `README.md` | Esta documentação. |
| `build/` | Artefatos temporários do PyInstaller (pode apagar). |
