# Blueprint — Autenticação, Responsáveis por Tarefa e Dashboard do Usuário

## Objetivo

Adicionar identidade ao autoia: **usuários autenticados**, **tarefas com responsável**,
**permissão de atuação** (só o responsável — ou admin do projeto — pode agir nas fases de
uma tarefa com responsável definido), **participação automática em projetos** (ao ser
atribuído a uma tarefa de um projeto, o usuário passa a participar dele) e um
**dashboard pessoal** com destaque visual para as tarefas do usuário.

**IMPORTANTE:** siga os padrões do `AGENTS.md` do repositório (backend tipado com
`Mapped[...]`/`mapped_column`, Pydantic v2, sessões SQLAlchemy por unidade de trabalho,
migração aditiva em `db.py`, mensagens em PT-BR, testes com as fixtures do `conftest.py`,
`npm run build` para atualizar o `frontend/dist`).

---

## Conceitos

- **Usuário** (`users`): identidade com e-mail único e senha (hash com salt).
- **Sessão** (`sessions`): token aleatório guardado em cookie HttpOnly (`autoia_session`).
- **Responsável da tarefa** (`tasks.responsible_id`): quem responde pela tarefa. Quando
  definido, **somente ele** (ou um **admin do projeto**) pode atuar nas fases (decidir,
  retomar, aprovar, instruir, reexecutar). Sem responsável, **qualquer usuário autenticado**
  pode atuar.
- **Participação no projeto** (`repository_users`): vínculo usuário↔repositório com papel
  (`member` | `admin`). **Ser atribuído a uma tarefa de um projeto insere automaticamente**
  o usuário como `member` daquele projeto (se ainda não participa).
- **Admin global** (`users.role = "admin"`): o **primeiro usuário criado** vira admin;
  admins criam/gerenciam os demais usuários e atuam em qualquer tarefa.

---

# Autenticação

## Senhas

- Hash com `hashlib.pbkdf2_hmac("sha256", senha, salt, 200_000)` (stdlib — **sem nova
  dependência**). Armazenar `f"{salt_hex}:{hash_hex}"` em `users.password_hash`.
- Login sempre valida com `hmac.compare_digest`.

## Sessões e cookie

- Token de sessão: `secrets.token_urlsafe(32)`.
- Cookie `autoia_session`: `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` quando o request
  for https (ou `AUTOIA_COOKIE_SECURE=1`). Expira em `AUTOIA_SESSION_DAYS` (default 30).
- `GET /api/auth/me` renova/valida a sessão; `POST /api/auth/logout` apaga a sessão.

## Feature flag (importante para testes)

- `Settings.auth_enabled` (env `AUTOIA_AUTH_ENABLED`, default `"1"`).
- **ON**: todos os routers `/api/*` exigem login (dependência `require_auth`).
- **OFF**: `require_auth` retorna `None` (comportamento atual). O fixture `settings` do
  `conftest.py` usa `auth_enabled=False` — a suíte existente continua verde sem refactor.

## Endpoints de auth (`api/auth.py`, prefix `/api/auth`)

- `POST /register` `{name, email, password}` → **somente quando `users` está vazio**
  (bootstrap). O usuário criado vira `admin`. Seta cookie. Retorna `UserOut`.
- `POST /login` `{email, password}` → seta cookie, retorna `UserOut`.
- `POST /logout` → apaga a sessão/cookie.
- `GET /me` → `UserOut` atual (401 se não logado).

## Endpoints de usuários (`api/users.py`, prefix `/api/users`, **admin**)

- `GET /` → lista usuários.
- `POST /` `{name, email, password, role?}` → cria usuário.
- `PATCH /{user_id}` `{name?, email?, role?, active?, password?}` → edita usuário.

---

# Responsável por tarefa

## Modelo (aditivo)

- `tasks.responsible_id` (FK `users.id`, nullable).
- `task_steps.responsible_id` (FK `users.id`, nullable) — **snapshot** de quem era o
  responsável quando a fase foi reivindicada/executada (o worker grava no `claim_next`).
- `task_steps.finished_by_id` (FK `users.id`, nullable) — quem finalizou/aprovou a fase.

## Atribuição

- `TaskCreate.responsible_id` opcional → default = **criador da task**.
- `PUT /api/tasks/{id}/responsible` `{user_id | null}` → reatribui/desatribui. Permitido a
  **admin global**, **admin do projeto** e ao próprio responsável (self-assign quando vazio).
- Ao definir um responsável (na criação ou via PUT), **upsert** de `RepositoryUser(repo,
  user, role="member")` — participação automática no projeto.

## Permissão de atuação

Helper `_ensure_can_act(task, user)` (em `api/tasks.py`):
- `user is None` (auth desligada) → permite (comportamento atual).
- `user.role == "admin"` → permite.
- `task.responsible_id` é None → permite (qualquer autenticado).
- senão → permite se `user.id == task.responsible_id` **ou** o usuário é `admin` do projeto
  (`repository_users` com `role="admin"`); caso contrário `403`.

Aplicar a **todas as mutações** de tarefa: `start`, `pause`, `resume`, `cancel`, `review`,
`bounceback`, `steps/{pos}/retry`, `approve-step`, `blocked/continue`, `instruction`,
`pm/decide`, `proposals/{pid}/accept|reject`, `feedback` (set/clear), `PATCH` da task,
`DELETE` da task e `subtasks/{pos}/retry`.

---

# Dashboard pessoal e participação

- `GET /api/me/projects` → repositórios onde o usuário tem `repository_users` (com o papel
  e contagem de tarefas minhas ativas/pendentes).
- `GET /api/me/tasks` → tarefas com `responsible_id == eu`, com nome do projeto.
- `DashboardOut` ganha: `user` (UserOut), `my_tasks` (list de TaskListItem) e `projects`
  (list de projetos com papel). Com auth ON, as métricas/`notices` passam a ser filtradas
  para os projetos do usuário; com auth OFF, mantém o comportamento global atual.
- `GET /api/repositories/{id}/members` + `POST .../members` + `DELETE .../members/{user_id}`
  + `PATCH .../members/{user_id}` (papel) — **admin do projeto** gerencia membros.

---

# Usabilidade

- **Dashboard do usuário** (Home `/`): seção **"Minhas tarefas"** no topo (com destaque) +
  **"Meus projetos"** (cards com contagem de tarefas minhas). Tarefas que **aguardam o
  usuário** (status que requer ação: `needs_review`, `waiting_approval`, `blocked`) aparecem
  primeiro, com selo "aguardando você".
- **Destaque visual**: no `TaskCard`, quando `task.responsible_id == user.id` → borda/glow
  na cor de acento + badge **"sua tarefa"**; sempre mostrar o responsável no card.
- **Workspace/TaskDetail**: header mostra o responsável + controle de atribuição (admin);
  botões de ação ficam desabilitados/ocultos para quem não pode atuar.
- **Topbar**: nome do usuário logado + botão de logout.

---

# Frontend

- `api.ts`: `credentials: "same-origin"` no request; funções `login`, `logout`, `me`,
  `register`, `listUsers`, `createUser`, `updateUser`, `assignResponsible`,
  `getMyProjects`, `getMyTasks`, membros.
- Novo `src/auth.tsx` (contexto `useAuth` com `me`, `login`, `logout`, `refresh`).
- Nova página `pages/Login.tsx`. `App.tsx`: guarda de rota — sem sessão → renderiza Login;
  topbar com usuário/logout.
- `types.ts`: `User`, `RepositoryMember`, `MyProject`, campos novos (`responsible`,
  `responsible_id`, `finished_by` etc.); espelhar `schemas.py`.

---

# Testes

- `conftest`: `settings(auth_enabled=False)` + fixtures de usuário (criar usuário/role).
- `test_auth.py`: register-primeiro-admin, login/me/logout, admin cria usuário, 401 sem
  cookie, senha errada, flag OFF passa direto.
- `test_responsible.py`: atribuição auto-participa no projeto; só responsável/admin atua
  (403 caso contrário); sem responsável qualquer autenticado atua; snapshot
  `responsible_id` por fase.
- `test_dashboard_me.py`: projetos/tarefas do usuário e filtro com auth ON.
- Garantir que a suíte existente continua passando com a flag OFF.

---

# Critérios de aceite

1. Primeiro registro cria um admin; demais usuários são criados por admin.
2. Sem sessão, as rotas `/api/*` retornam 401 (com auth ON).
3. Toda tarefa criada fica com um responsável (default: criador).
4. Atribuir um responsável insere automaticamente a participação no projeto.
5. Com responsável definido, ações nas fases são bloqueadas (403) para quem não é o
   responsável nem admin do projeto.
6. Cada fase registra quem era o responsável (`responsible_id`) no momento da execução.
7. Home é um dashboard pessoal: "Minhas tarefas" em destaque + "Meus projetos".
8. TaskCard destaca visualmente tarefas do usuário logado.
9. Suíte de testes passando; `frontend/dist` atualizado (`npm run build`).
