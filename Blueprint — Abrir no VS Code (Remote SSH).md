# Blueprint — Abrir no VS Code (Remote SSH)

## Objetivo

Adicionar no **Workspace** da tarefa um botão **"Abrir no VS Code"** que abre, na
máquina local de quem está acessando, o código da **branch da tarefa** que os robôs
estão alterando. Como o backend roda em **servidor remoto**, a solução é **VS Code
Remote - SSH** (sem helper local, sem tunnel): o VS Code já registra um handler de URL
que abre pasta remota via SSH.

**IMPORTANTE:** siga os padrões do `AGENTS.md` (backend tipado, Pydantic v2, migração
aditiva, sessões por unidade de trabalho, PT-BR; frontend TS estrito, CSS puro com
variáveis em `styles.css`, sem biblioteca de UI; testes com fixtures do `conftest.py`;
`npm run build` ao final). Reaproveite componentes existentes (`HelpTip`, etc.).

---

## A. Mecanismo (decisão)

URL que o navegador abre e o VS Code local interpreta:

```
vscode://vscode-remote/ssh-remote+<host>/<caminho-remoto>
```

- `<host>`: alias do `~/.ssh/config` (ou `user@host`) que o cliente usa para conectar
  no servidor. É uma configuração **client-side** — por isso precisa ser configurável
  no autoia (env/global).
- `<caminho-remoto>`: caminho absoluto **no servidor** do checkout da task
  (`<workspace_dir>/<repo_id>/task_<task_id>`), que já fica na branch `autoia/task-<id>`
  durante o trabalho dos robôs.

Requisito por usuário (setup único): VS Code + extensão **Remote-SSH** + host SSH
configurado (chave recomendada — senha não é salva pelo Remote-SSH).

---

## B. Configuração global (backend)

1. `config.py` — novo campo no `Settings`:
   - `open_ssh_host: str` = `AUTOIA_OPEN_SSH_HOST`, default `""` (vazio = feature
     desligada / botão oculto).
2. Expor/editável na tela **Configuração geral** (`SystemConfig.tsx` + endpoint
   correspondente em `api/system.py`), como as demais configurações globais.
3. Sem `open_ssh_host` configurado → o endpoint `/open` retorna `ssh_host` vazio e a UI
   não mostra o botão (ou mostra desabilitado com dica de setup).

---

## C. Endpoint (read-only, sem mutação git)

`GET /api/tasks/{id}/open` → `{ ssh_host, path, branch, exists }`

- `ssh_host` = `settings.open_ssh_host`.
- `path` = `os.path.abspath(_task_workspace(eff, repo.id, task.id))` (reusa
  `api/tasks.py` já importa `_task_workspace`/`_effective`).
- `branch` = `task.branch`.
- `exists` = `os.path.isdir(os.path.join(path, ".git"))` (checkout já clonado).
- Respeitar auth/escopo como os demais endpoints de task.

Schema novo em `schemas.py` (ex.: `TaskOpenOut`).

---

## D. Frontend (`Workspace.tsx`)

1. Botão/link **"Abrir no VS Code ↗"** no header (`ws-header-top`, junto ao
   "detalhes técnicos ↗").
2. Só renderiza quando `ssh_host` configurado e `exists === true`; senão, desabilitado
   com `HelpTip` explicando o setup único (Remote-SSH + host no `~/.ssh/config`).
3. Ação: `window.open("vscode://vscode-remote/ssh-remote+" + ssh_host + path)`.
4. `api.ts`: novo `getTaskOpen(id)`.

---

## E. Comportamento (escopo decidido)

- Só **abrir a pasta** — o VS Code mostra a branch atual na barra de status.
- **Sem auto-checkout** da branch (não mexer no checkout enquanto a fase pode estar
  `running`; a branch da task já é a corrente durante as fases pré-merge).
- Botão **só no Workspace** (não em TaskDetail/cards).

---

## F. Verificação na implementação

- Confirmar o formato exato da URL remota (`vscode://vscode-remote/ssh-remote+<host>/<path>`),
  usado pelo Remote-SSH; fallback: abrir só o host (sem path) se algo divergir.
- Testar com um host SSH real (ou simular a geração da URL em teste unitário).

---

## G. Testes

- Unit: `GET /api/tasks/{id}/open` retorna `path`/`branch`/`exists` corretos; retorna
  `ssh_host` vazio quando `AUTOIA_OPEN_SSH_HOST` não configurado.
- `test_seed.py`/`test_system_api.py`: campo novo aparece na config global (se aplicável).

---

## H. Caveats / pendências

- `open_ssh_host` é **global** (mesmo alias para todos os usuários). Se cada usuário
  tiver alias SSH diferente, evoluir depois para config **por usuário** (campo no
  `User` ou `RepositoryUser`).
- Alternativa futura: **VS Code Tunnels** (`code tunnel` no servidor → URL vscode.dev),
  se algum usuário não puder usar SSH.
