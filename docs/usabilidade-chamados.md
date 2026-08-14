# Usabilidade do subsistema de Chamados — avaliação e plano de melhorias

Data: 2026-08-13 · Branch: `feat/chamados` · Escopo: Fase 1 (MVP) do fluxo de atendimento
(`Projeto > Épico > Chamado`), paralelo à pipeline de tasks.

Este documento mapeia **como está hoje** (fluxos e telas), avalia a usabilidade contra
princípios básicos (clareza do estado, feedback, prevenção de erro, consistência,
recuperação) e lista as melhorias — as que **já foram aplicadas** nesta revisão e as
que ficam como **sugestão** (Fase 1.1 / Fase 2).

---

## 1. Mapa atual (como o usuário vive o fluxo)

### 1.1 Fluxo principal

```
Criar chamado (lista)                 POST /api/chamados
   └─ etapa inicial "entrada" ativa   (catálogo: entrada→analise→desenvolvimento→deploy)
        │
        ├─ [Ferramentas da etapa]  →  texto + clique no botão da ferramenta
        │     ex.: "assistente", "escopo"   → worker roda LLM no checkout (read-only)
        │     └─ interações viram mensagens (user / assistant_text / tool_call / system)
        │
        ├─ [Fechar avaliação]        → robô decide: next:<etapa> | resposta | cancelar | concluir
        │     └─ validado contra close_options do catálogo (inválida mantém etapa ativa)
        │
        └─ etapa concluída: chamado  → em_andamento / respondido / cancelado / concluido
```

### 1.2 Telas

| Tela | Rota | Conteúdo |
| --- | --- | --- |
| Lista de chamados | `/:repoId/chamados` | Cards (id, título, status, etapa, custo), filtros (projeto, status), formulário de criação inline |
| Detalhe do chamado | `/:repoId/chamados/:id` | Header (status da vida + etapa atual + custo + worker), histórico de etapas, ferramentas da etapa, chat/transcript, campo de pedido |
| Projetos/Épicos | `/:repoId/projects` | CRUD de projeto, épicos por projeto, resumo (LLM) do projeto, escopo/resumo (LLM) do épico, botões de gerar/regenerar |

### 1.3 Observações de arquitetura relevantes à UX

- `Chamado.workflow_status` (etapa atual) é o status **principal**; `Chamado.status`
  (vida) é secundário. Os dois aparecem no header do detalhe.
- A execução é **assíncrona**: o usuário envia o pedido da ferramenta e o
  `chamado-worker` (processo separado) processa. A UI faz polling de 2 s.
- Falha de ferramenta não é fatal: a etapa volta a `ativa` com `error` e o usuário
  refaz o pedido. **Decisão de fechamento inválida** também mantém a etapa ativa.
- Etapas `aguardando`/`executando` bloqueiam novas ações (uma por chamado por vez).

---

## 2. Avaliação (achados)

### 2.1 Clareza do estado
- ✅ Etapa atual + status de vida + custo + worker visíveis no header; stepper de etapas.
- ⚠️ **Transcrição mistura etapas**: o chat mostra TODAS as mensagens de todas as etapas
  em sequência, mas o título é "Conversa da etapa". O usuário não distingue o que
  pertence à etapa atual do histórico de etapas anteriores → corrigido (agrupar por etapa).
- ⚠️ **Respostas do assistente em texto cru**: sem formatação markdown (código, listas,
  tabelas ficam ilegíveis) → corrigido (reuso do renderizador `lib/markdown.tsx`).
- ⚠️ **"Processando…" genérico**: sem spinner por etapa; ao voltar, sem resumo do que a
  ferramenta fez além do texto final → aceitável para o MVP; sugestão de `tool_done` com
  duração.

### 2.2 Ação → feedback
- ⚠️ **Qual ferramenta vai rodar?** Enter dispara a primeira ferramenta silenciosamente;
  os botões ficam desabilitados sem texto — sem affordance de qual ferramenta está
  "selecionada" → corrigido (seleção explícita com destaque + nome da ferramenta no rodapé).
- ⚠️ **"Fechar avaliação" sem confirmação**: um clique dispara uma avaliação LLM (custo e
  tempo); risco de clique acidental → corrigido (diálogo de confirmação).
- ✅ Falha de ferramenta é visível (erro na etapa) e recuperável (refazer pedido).

### 2.3 Recuperação / situações de trava
- ⚠️ **Ação encaminhada + worker offline = travado**: se o `chamado-worker` cai com uma
  ação em `aguardando`, o usuário não tem como "desfazer"; o chamado fica bloqueado até o
  worker voltar (recuperação de órfãos só roda no startup do worker) → corrigido
  (`POST /{id}/cancel-action` + aviso de "worker offline" na tela).
- ⚠️ **Sem cancelamento manual do chamado**: só é possível encerrar por decisão do robô;
  não há "cancelar chamado" do usuário → corrigido (`POST /{id}/cancel`).
- ⚠️ **Sem exclusão na tela de detalhe**: o endpoint DELETE existe, mas sem botão → corrigido.
- ⚠️ **Worker offline**: a UI mostra o pontinho, mas não diz o que fazer com uma ação presa
  → corrigido (aviso contextual quando há ação pendente e worker offline).

### 2.4 Consistência / convenções
- ✅ Reuso dos padrões visuais existentes (`.card`, `.chat-*`, `.badge`, `.form-field`).
- ⚠️ Geração de conteúdo de Projeto/Épico: o clique faz `setTimeout(load, 1200)` — sem
  indicação de que está gerando nem polling de acompanhamento → corrigido (flag
  `generating` na API + spinner + polling curto).
- ⚠️ Status de Projeto/Épico não editável na UI (criados sempre `aberto` e nunca mudam)
  → corrigido (select de status inline).
- ⚠️ Após criar o chamado, a lista não navegava para o detalhe → corrigido (navega para o
  chamado criado).
- ⚠️ Sem atalho de lista de chamados por projeto/épico a partir de Projetos → corrigido
  (link `?project=<id>` que pré-filtra a lista).

### 2.5 Custo / transparência
- ✅ Custo acumulado vs. orçamento no header.
- ⚠️ Sem breakdown por etapa/mensagem (cada `ChamadoMessage` tem `cost`, mas a UI não
  mostra) → sugestão (Fase 1.1).

---

## 3. Melhorias aplicadas nesta revisão

### Backend
- `POST /api/chamados/{id}/cancel-action` — limpa uma ação pendente (`aguardando`) e
  devolve a etapa a `ativa` (desfazer pedido encaminhado sem executar).
- `POST /api/chamados/{id}/cancel` — cancelamento manual do chamado (fecha a etapa atual
  com decisão `cancelado_manualmente` e status `cancelado`).
- `generating` em `ProjectOut`/`EpicOut` (espelho do `_IN_FLIGHT` do chamado_runner) para
  feedback real de geração de conteúdo.
- Helpers `_project_out`/`_epic_out` centralizando a serialização.

### Frontend
- **ChamadoDetail**:
  - Chat agrupado por etapa (cabeçalho por etapa; transcrição da etapa atual em destaque).
  - Markdown nas respostas do assistente (`lib/markdown.tsx`).
  - Seleção explícita de ferramenta (botão ativo + nome no rodapé); Enter executa a
    selecionada; desabilita só sem texto.
  - Confirmação no "Fechar avaliação".
  - "Cancelar ação pendente" (quando `aguardando`) e aviso de "worker offline" com ação presa.
  - "Cancelar chamado" (manual) e "Excluir" com confirmação.
- **Chamados (lista)**: navega para o chamado criado; filtro por épico; aceita `?project=`
  e `?epic=` da URL (links a partir de Projetos).
- **Projects**: spinner/polling enquanto `generating`; select de status do projeto/épico;
  link "ver chamados" por projeto pré-filtrado.

---

## 4. Sugestões para as próximas fases (não aplicadas)

### Fase 1.1 (polimento)
1. **Custo por etapa/mensagem** na UI (somando `ChamadoMessage.cost` por etapa) + alerta ao
   chegar perto do orçamento.
2. **`tool_done` com duração** (medir `started_at→finished_at` da ação) e mostrar "última
   atividade".
3. **Cancelar/reset de ação `executando`** com garantias: hoje é seguro apenas para
   `aguardando`; para `executando`, o fluxo correto é reiniciar o worker (recupera órfãos).
   Um botão "forçar parada" exigiria canal de stop-file por chamado (padrão das tasks).
4. **Busca por título** na lista de chamados.
5. **Botão "copiar resposta"** no resultado de uma etapa fechada como `resposta`.
6. **Deep-link** da lista para uma etapa específica (ex.: `?stage=<id>`).

### Fase 2 (entrega)
7. **Configuração de catálogo na UI** (hoje o CRUD de `chamado-stage-types` é só API):
   editar `allowed_tools`, `close_options` e `delivery_config` (MR/URL) visualmente.
8. **Estágio de desenvolvimento real**: quando a avaliação decidir `next:desenvolvimento`,
   permitir vincular/executar uma pipeline (ou MR via `delivery_config`) e exibir o
   progresso dentro da etapa.
9. **Assistente com histórico cross-etapa**: contexto do prompt hoje usa só a transcrição da
   etapa atual; considerar incluir resumo das etapas anteriores.
10. **Notificações/notices** de chamados que exigem atenção (aguardando há muito tempo,
    worker offline, orçamento alto) no dashboard global.

---

## 5. Critérios de aceite da usabilidade (checklist)

- [ ] Consigo saber em qual etapa o chamado está e o que posso fazer nela.
- [ ] Consigo distinguir a conversa da etapa atual do histórico.
- [ ] Consigo ver exatamente qual ferramenta será executada antes de enviar.
- [ ] Não disparo uma avaliação de fechamento por acidente (confirmação).
- [ ] Consigo desfazer/cancelar uma ação pendente sem reiniciar o worker.
- [ ] Consigo cancelar ou excluir um chamado manualmente.
- [ ] Se o worker está offline, a tela me diz o que fazer.
- [ ] Geração de resumo/escopo de Projeto/Épico mostra feedback de progresso.
- [ ] Respostas do assistente com markdown são legíveis.
- [ ] Ao criar um chamado, sou levado a ele.
