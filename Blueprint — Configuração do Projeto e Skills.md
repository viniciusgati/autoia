# Blueprint — Configuração do Projeto e Skills

## Objetivo

Melhorar a tela de **configuração do projeto** (RepoDashboard) com seções organizadas e
**feedback visual em toda ação**, e adicionar o gerenciamento de **skills por projeto**
(upload básico em `.zip`), com as skills **disponibilizadas aos robôs executores** nas
próximas fases.

**IMPORTANTE:** siga os padrões do `AGENTS.md` (backend tipado, Pydantic v2, migração
aditiva, sessões por unidade de trabalho, PT-BR; frontend TS estrito, CSS puro com
variáveis em `styles.css`, sem biblioteca de UI; testes com fixtures do `conftest.py`;
`npm run build` ao final). Reaproveite componentes existentes: `HelpTip`, `StatusBadge`,
`badge`, `card`, `button.danger`, `modal`, `DiffView`.

---

# A. Tela de Configuração do Projeto (melhorias de interface)

## Layout: seções organizadas

A página do projeto (`RepoDashboard.tsx`) ganha a configuração em **seções** claras
(sub-navegação por tabs OU accordion — escolha o que ficar mais simples e consistente):

1. **Geral** — nome, URL, branch default.
2. **Execução** — pipeline default (select), executor default (kimi/opencode), timeout
   por fase (s), máx. tentativas, máx. decisões PM, gerar resumo automático (bool).
3. **Orçamento** — task_budget, cost_per_interaction, top-up do PM.
4. **Regras e ambiente** — `risky_patterns_extra` (textarea), `db_rule` (textarea),
   permitir tasks automáticas (bool), permitir tasks externas (bool).
5. **Skills** (nova) — upload + lista + gerenciamento.

## Feedback visual (obrigatório em TODAS as ações)

- **Botão Salvar** com 3 estados:
  - `Salvar` (normal, disabled se não há mudança ou form inválido).
  - `Salvando…` (spinner no botão, desabilitado).
  - `✓ Salvo` (verde, some sozinho ~2s) **ou** `✕ Falha ao salvar` (vermelho, com a
    mensagem do erro e sem sumir até o usuário corrigir/fechar).
- **Badge "alterações não salvas"**: aparece quando o form diverge do valor salvo.
- **Validação inline**: campo inválido → borda vermelha + mensagem curta abaixo do campo;
  `Salvar` fica desabilitado enquanto houver erro. Regras: nome não vazio, timeout > 0,
  budget >= 0, máx. tentativas >= 1.
- **HelpTip** em cada campo (componente existente) explicando o efeito da config.
- **Estados vazios**: seção sem dados mostra mensagem clara + CTA (ex.: Skills vazia →
  "Nenhuma skill configurada — envie um .zip com SKILL.md").

---

# B. Skills por projeto

## Conceito

- Uma **skill** = pasta com **`SKILL.md`** (obrigatório, na raiz) + arquivos de apoio
  (ex.: `references/*.md`, scripts). É o formato de skills do mercado (opencode/kimi).
- Skills são **configuração do projeto** (não código): ficam **fora do git do repo**,
  armazenadas em disco do autoia (`data/skills/<repo_id>/<skill_id>/`) com metadados no
  banco.
- O **worker** materializa as skills no checkout **antes de cada execução de fase** para
  os robôs usarem quando o assunto casar.

## Upload (básico)

- **Formato aceito**: `.zip` com `SKILL.md` na raiz (obrigatório). Limites: 5 MB máx.,
  50 arquivos máx., sem `..`/absoluto nos caminhos (rejeitar path traversal).
- **Interação** (drop zone + botão "escolher arquivo"):
  - Durante upload: spinner + "Enviando skill…" (barra de progresso opcional).
  - Sucesso: `✓ Skill "nome" enviada` (verde, a lista atualiza).
  - Erro (mensagem específica, vermelha): `✕ ZIP inválido: falta SKILL.md na raiz` /
    `✕ arquivo muito grande (máx. 5 MB)` / `✕ caminho inválido no zip`.
- **Nome e descrição** extraídos do `frontmatter` do `SKILL.md` (`name:`, `description:`);
  fallback: nome da pasta do zip.

## Lista de skills

- Card por skill: **nome**, **descrição** (frontmatter), **nº de arquivos**, **tamanho**.
- Ações: `[Ver]` (modal com preview do `SKILL.md`) e `[Excluir]` (confirmação: "Excluir a
  skill 'X'? Os robôs deixarão de usá-la na próxima fase.").

## Backend

- **Tabela nova** `repository_skills` (criada por `create_all`): id, repository_id FK,
  name, description, file_count, size_bytes, created_at.
- Arquivos em `data/skills/<repo_id>/<skill_id>/` (já coberto pelo gitignore de `data/`).
- Endpoints (requer **admin do projeto** — use o mesmo padrão de permissão da auth,
  `repository_users` com `role="admin"`):
  - `GET /api/repositories/{id}/skills` → lista de skills.
  - `POST /api/repositories/{id}/skills` (multipart `file`) → valida + extrai + grava.
  - `DELETE /api/repositories/{id}/skills/{skill_id}` → remove do disco + banco.
  - `GET /api/repositories/{id}/skills/{skill_id}/file` → conteúdo do `SKILL.md` (preview).
- Descompactação **segura**: `zipfile` com `ZipFile.namelist()` validado (rejeita caminhos
  absolutos/`..`), limites de tamanho/quantidade, e extração em diretório controlado.

## Integração com o executor (worker)

Antes de cada execução de fase (em `execute_step`, após garantir o checkout), se o repo
tiver skills:

1. Copiar `data/skills/<repo_id>/<skill_id>/*` → `<checkout>/.autoia/skills/<skill_name>/`.
2. Se a pasta existir, passar `--skills-dir <checkout>/.autoia/skills` ao **kimi**
   (`kimi_exec.run_kimi`).
3. Para **opencode**: copiar também para `<checkout>/.opencode/skills/` (auto-descoberta).
4. **Fallback p/ qualquer executor**: injetar no prompt uma seção
   `## Skills do projeto disponíveis` com `nome — descrição` de cada skill (o robô sabe
   que existem e lê o SKILL.md quando o assunto casar).

Sem skills configuradas → comportamento atual, sem custo extra.

---

# C. Frontend

- `RepoDashboard.tsx`: reorganizar a config em seções (com o feedback visual acima) e
  incluir a seção Skills.
- Novo componente `ProjectSkills.tsx`: drop zone + lista + modal de preview + confirmação.
- `api.ts`: `listProjectSkills`, `uploadProjectSkill` (FormData), `deleteProjectSkill`,
  `getProjectSkillFile`.
- `types.ts`: `RepositorySkill`.
- `styles.css`: drop zone, estados de botão Salvar (sucesso/erro), toast/badge de
  feedback, cards de skill, `.skills-empty`.

---

# D. Testes

- Upload: zip válido → skill criada + arquivos no disco; zip sem `SKILL.md` → 400 com
  mensagem; zip com `../` no caminho → 400 (rejeitado); zip > limite → 400.
- Lista + preview + exclusão (e 404 após excluir).
- Permissão: não-admin → 403.
- Worker: com skills → checkout ganha `.autoia/skills/` e o prompt inclui a seção;
  sem skills → nada muda (suíte existente verde).
- Suíte completa passando + `npm run build`.

---

# Critérios de aceite

1. Config do projeto organizada em seções, com feedback visual (salvando/sucesso/erro)
   em toda ação e badge de alterações não salvas.
2. Validação inline com mensagens claras; Salvar desabilitado quando inválido.
3. Upload de skill via `.zip` (com `SKILL.md`) com feedback de progresso/sucesso/erro.
4. Lista de skills com nome/descrição/tamanho e ações Ver/Excluir (com confirmação).
5. ZIP sem `SKILL.md`, com path traversal ou acima do tamanho são rejeitados com
   mensagem específica.
6. As skills chegam ao executor (`.autoia/skills/` + `--skills-dir` no kimi e a seção
   no prompt como fallback).
7. Testes passando e `npm run build` ok.
