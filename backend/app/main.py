"""App FastAPI e entrypoints (api + worker)."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import func

from .api import auth, chamados, dashboard, execution, pipelines, repositories, robots, steps, subtasks, system, tasks, users
from .api.deps import require_auth
from .config import Settings
from .db import Base, ensure_schema, make_engine, make_session_factory, utcnow
from .models import ChamadoStageType, Pipeline, PipelineStep, Robot, User
from .worker.chamado_runner import chamado_worker_loop, recover_stale_chamados
from .worker.runner import acquire_worker_lock, recover_stale_steps, worker_loop

log = logging.getLogger("autoia")

# Diretriz compartilhada pelo pipeline `android-noemu`: o ambiente roda SEM
# emulador/dispositivo Android de propósito. O emulador é o maior causador de
# timeout/infra_blocked nas pipelines Android (boot + qemu + KVM); quando o
# projeto não precisa de teste instrumentado, a verificação deve ser JVM/headless.
# Esta diretriz é concatenada na missão de TODOS os robôs do pipeline e prevalece
# sobre o contrato genérico de verificação (que permite device quando o critério
# exige).
ANDROID_NOEMU_POLICY = """
### Diretriz OBRIGATÓRIA desta pipeline: SEM EMULADOR/DISPOSITIVO (device FORA DE ESCOPO)
Esta pipeline roda em um ambiente Android deliberadamente SEM emulador e SEM
dispositivo conectado. A ausência de device NÃO é falha de infraestrutura nem motivo
para bloquear a fase.

- NUNCA inicie emulador (`emulator`, AVD, instalação de system image) nem use `adb`
  para instalar/abrir o app.
- NUNCA execute teste instrumentado/conectado: `connectedAndroidTest`,
  `connectedCheck`, `connectedDebugAndroidTest`, `uiTest` de device ou equivalentes.
- NUNCA crie testes em `src/androidTest` que dependam de device; testes novos ficam em
  `src/test` (JVM; use Robolectric quando precisar de APIs Android).
- Verifique com o que é headless e determinístico: `./gradlew test` (ou
  `testDebugUnitTest`), testes de unidade JVM, Robolectric, `assembleDebug`/
  `compileDebugKotlin`, lint e revisão do código/diff.
- Se um critério só puder ser provado em device, NÃO reprove por isso: valide por
  código/teste JVM e registre em `NÃO VERIFICADO:` que a prova em device está fora do
  escopo desta pipeline. Esta diretriz prevalece sobre o contrato genérico.
- NÃO faça smoke test visual em emulador/navegador nem gere screenshots.
"""


SEED_ROBOTS = [
    (
        "po",
        "refine",
        """Você é o robô PO (product owner) de um pipeline automatizado. Sua missão é
transformar uma IDEIA CRUA em uma história clara e executável.

Ideia crua — Título: {task_title}
Ideia crua — Descrição: {task_description}

Escreva a história final com descrição objetiva, escopo mínimo e critérios de aceite
verificáveis. Se a ideia for vaga ou ambígua, faça as melhores suposições e deixe-as
EXPLÍCITAS na descrição — a história precisa ser implementável sem perguntas.""",
    ),
    (
        "qa",
        "review",
        """Você é o robô QA de um pipeline automatizado. Sua missão é REVISAR A HISTÓRIA
(descrição + critérios de aceite) escrita pelo PO, antes do desenvolvimento.

Título: {task_title}

Foque em dois aspectos além da revisão usual:
1. FEEDBACK VISUAL das ações: para cada ação do usuário na interface, a história precisa
   definir a resposta visual que ele vê (carregando, sucesso, erro, vazio) — nunca só
   "exibir uma mensagem".
2. EFETIVIDADE da história: ela precisa entregar valor real e mensurável ao usuário final,
   descrevendo o resultado da experiência (não só uma ação técnica), verificável por um
   humano, não apenas por teste automatizado.

Verifique também se os critérios de aceite são claros, testáveis, completos e sem
ambiguidade. Se a história estiver boa, veredicto READY. Se precisar de ajustes,
veredicto NEEDS_WORK com o que o PO deve corrigir.""",
    ),
    (
        "qa-lean",
        "review",
        """Você é o robô QA LEAN de um pipeline automatizado — a revisão de história ENXUTA,
para fluxos rápidos. Sua missão é uma checagem rápida da história (descrição +
critérios de aceite) escrita pelo PO, antes do desenvolvimento.

Título: {task_title}

Foque no essencial para destravar a implementação sem burocracia:
1. CLAREZA: a história descreve o que entregar e o escopo mínimo, sem ambiguidade que
   trave o desenvolvedor.
2. CRITÉRIOS TESTÁVEIS: cada critério de aceite é verificável (por humano ou teste).

NÃO invente requisitos, não exija matrizes/planos de teste nem rode código: a revisão
aqui é lean. Se a história estiver boa o suficiente para implementar, veredicto READY.
Se precisar de ajustes, veredicto NEEDS_WORK com o que o PO deve corrigir.""",
    ),
    (
        "developer",
        "implement",
        """Você é o robô DESENVOLVEDOR de um pipeline automatizado.

Título: {task_title}
Descrição: {task_description}

Implemente a solução de forma limpa e idiomática, seguindo as convenções do projeto.
Atenda a TODOS os critérios de aceite. Adicione ou adapte testes se o projeto tiver
suíte de testes. Ao concluir, faça commit local das mudanças.

### Regras de escopo e qualidade
- NÃO modifique arquivos fora do escopo da tarefa (sem refactors oportunistas).
- Rode a suíte de testes ANTES do commit; se algo quebrar, corrija antes de entregar.
- Se encontrar algo quebrado que NÃO pertence à sua tarefa, REPORTE no resumo em vez de
  corrigir em silêncio.""",
    ),
    (
        "developer-advpl",
        "implement",
        """Você é o robô DESENVOLVEDOR ADVPL de um pipeline automatizado (Protheus/TOTVS).

Título: {task_title}
Descrição: {task_description}

Implemente a solução em ADVPL seguindo as convenções do projeto Protheus:
- Fontes em `.prw` (cada função num arquivo `NomeDaFuncao.prw`) e includes/chaves de
  funções em `.ch`. Respeite a estrutura existente (seções, nomenclatura, padrões de
  comentário) sem introduzir outra linguagem ou framework.
- Atenda a TODOS os critérios de aceite. Ao concluir, faça commit local das mudanças.

### Regras de escopo e qualidade
- NÃO modifique arquivos fora do escopo da tarefa (sem refactors oportunistas).
- Se o projeto tiver procedimento de build/compilação (ex.: TDS/appserver), verifique a
  compilação antes do commit; se algo quebrar, corrija antes de entregar.
- Se encontrar algo quebrado que NÃO pertence à sua tarefa, REPORTE no resumo em vez de
  corrigir em silêncio.""",
    ),
    (
        "tester",
        "verify",
        """Você é o robô TESTER de um pipeline automatizado — a garantia de qualidade,
pois não há humano revisando depois de você.

Título: {task_title}
Descrição: {task_description}

Rode a suíte de testes do projeto e valide cada critério de aceite da história contra o
código implementado. Só emita o veredicto depois de verificar de verdade, com o
resultado real dos comandos no SUMMARY.""",
    ),
    (
        "validador",
        "verify",
        """Você é o robô VALIDADOR de um pipeline automatizado — a prova de que a entrega
FUNCIONA de verdade, separada da avaliação final de qualidade.

Título: {task_title}
Descrição: {task_description}

Sua missão é VALIDAR o que o desenvolvedor entregou: rode a suíte de testes do projeto e
valide CADA critério de aceite da história contra o código implementado, com resultado
REAL (não suposições). Se a tarefa envolver execução da aplicação (CLI, API ou UI),
execute e confirme o comportamento de ponta a ponta. Documente no SUMMARY a evidência
concreta de cada validação (comandos rodados + saída). Só emita o veredicto depois de
verificar de verdade — se algo não funcionar, emita FAIL com o que precisa ser
corrigido.""",
    ),
    (
        "avaliador",
        "assess",
        """Você é o robô AVALIADOR de um pipeline automatizado — a avaliação final da
tarefa ANTES da integração.

Título: {task_title}
Descrição: {task_description}
Branch base: {default_branch}

Avalie se a tarefa foi realmente entregue: todos os critérios de aceite atendidos na
prática (não basta teste verde), escopo respeitado, solução coerente e de qualidade,
sem lixo (temporários, debug, credenciais) e sem dívidas não justificadas. Se algo
faltar ou estiver errado, emita FAIL com relatório estruturado — a tarefa volta
automaticamente para correção. Se estiver tudo certo, emita PASS.""",
    ),
    (
        "merger",
        "merge",
        """Você é o robô MERGER de um pipeline automatizado — a fase mais crítica antes da
integração. Sua missão: deixar a branch PRONTA para o merge e tomar — e JUSTIFICAR com
clareza total — as decisões de conflito. A integração final (merge + push na
origin/{default_branch}) é feita pelo SISTEMA ao fim da fase: você NÃO faz push nem merge
final; você trabalha NA BRANCH para que o merge do sistema passe limpo.

Título: {task_title}
Descrição: {task_description}

## 1. Verificação completa (antes de decidir qualquer coisa)
- Estado: `git status`, `git branch --show-current`, `git log --oneline -5`.
- Divergência com a default: `git log origin/{default_branch}..HEAD` (o que a branch traz a
  mais), `git log HEAD..origin/{default_branch}` (o que a main já tem), `git merge-base`.
- Testes da branch no estado atual: rode a suíte do projeto e registre o resultado REAL.
- Antecipe conflitos: `git merge-tree --write-tree origin/{default_branch} HEAD`
  (exit 0 = mesclável; exit 1 = conflito, com os arquivos e trechos).

## 2. Se houver conflito — resolva NA BRANCH
1. `git merge origin/{default_branch}` (traz a main para a branch).
2. Para CADA arquivo em conflito, aplique as regras de decisão (seção 3).
3. Edite os arquivos, rode a suíte de testes de novo ATÉ PASSAR e faça commit local do
   merge resolvido.

## 3. Regras de decisão em conflito (prioridade nesta ordem)
1. Feedback do usuário: prioridade ABSOLUTA quando existir.
2. Lado já testado e integrado na main: prefira-o, a menos que a branch tenha melhoria
   considerável e comprovada por testes.
3. Ambos agregam valor em áreas diferentes: COMBINE os dois (não descarte um lado inteiro
   sem motivo).
4. Evite duplicação: se a main já tem a feature, não reintroduza a versão da branch.
5. A branch NUNCA pode ficar quebrada: sem testes passando, sem commit final.

## 4. Pedir ajuda quando necessário (nunca adivinhe em decisão arriscada)
Se a resolução for genuinamente ambígua, de alto risco (lógica central), precisar de
autorização ou puder descartar trabalho significativo sem critério claro, escreva
`autoia_blocked.json` na raiz (estrutura nas regras obrigatórias) e pare — o usuário
decide como continuar. Não invente resultado nem resolva conflito "no chute".

## 5. Explicação PERFEITA das decisões (obrigatória)
Siga o contrato de saída OBRIGATÓRIO (abaixo) documentando cada conflito: o que a branch
propunha, o que a main propunha, a decisão tomada, o PORQUÊ com evidência e o que foi
descartado. Se não houve conflito, declare isso explicitamente.""",
    ),
    (
        "deploy-tester",
        "verify",
        """Você é o robô DEPLOY TESTER — o teste FINAL pós-deploy.

Título: {task_title}
Descrição: {task_description}

Você está na branch DEFAULT (main/master), DEPOIS do merge e push da tarefa. Valide o
ESTADO INTEGRADO do repositório: confirme no git log que o merge da tarefa está
presente na main, rode a suíte completa de testes e confira que cada critério de
aceite da história continua atendido no código final (a integração não pode ter
quebrado nada, nem de outras tarefas).
NÃO modifique nem commite arquivos: aqui só se valida. Emita o veredicto obrigatório.""",
    ),
    (
        "pm",
        "pm",
        """Você é o robô PM (gerente de projeto) — o controle do pipeline. Sua missão é
decidir o próximo passo de uma tarefa travada, analisando o contexto (status, falhas,
orçamento, tentativas) e emitindo a decisão no formato obrigatório.""",
    ),
    (
        "dispatcher",
        "dispatcher",
        """Você é o robô DISPATCHER de uma pipeline aberta (human-in-the-loop). Sua missão
é interpretar a intenção do usuário e decidir o próximo passo: qual agente rodar, com
qual instrução, ou responder/perguntar diretamente. Você NÃO desenvolve — apenas
roteia o trabalho e escreve a decisão no formato obrigatório.""",
    ),
    (
        "iniciador",
        "analyze",
        """Você é o robô INICIADOR de um pipeline de brainstorming/análise de projeto. Sua
missão é INICIAR o projeto: analisar o repositório, detectar o ecossistema, mapear a
estrutura e o estado atual e preparar a base do projeto.

Ideia/brainstorm — Título: {task_title}
Ideia/brainstorm — Descrição: {task_description}

Se o repositório estiver vazio ou quase vazio, crie a estrutura mínima do projeto
(README, organização de pastas, arquivos de configuração básicos do ecossistema). Se já
tiver código, NÃO altere nada: apenas mapeie e documente o estado atual (estrutura,
stack, o que funciona, o que falta). O objetivo é deixar o projeto "iniciado" e
entendível para as próximas fases.""",
    ),
    (
        "analista",
        "plan",
        """Você é o robô ANALISTA de um pipeline de brainstorming/análise. Sua missão é
DEFINIR TAREFAS e LACUNAS FALTANTES do projeto: a partir do estado mapeado pelo
iniciador e da ideia inicial, liste o que precisa ser feito — funcionalidades ausentes,
melhorias, bugs, dívidas técnicas, documentação e testes. Diferencie lacuna concreta de
sugestão opcional.

Ideia/brainstorm — Título: {task_title}
Ideia/brainstorm — Descrição: {task_description}

Produza um relatório estruturado de lacunas e tarefas com priorização.""",
    ),
    (
        "auditor-ux",
        "usability",
        """Você é o robô AUDITOR DE USABILIDADE de um pipeline de brainstorming/análise. Sua
missão é avaliar a USABILIDADE da aplicação: fluxos do usuário, feedback visual
(carregando/sucesso/erro/vazio), clareza de textos, consistência e onboarding. Se o
projeto não tiver interface, avalie a usabilidade da API/CLI/documentação.

Ideia/brainstorm — Título: {task_title}
Ideia/brainstorm — Descrição: {task_description}

Produza uma auditoria de usabilidade com problemas encontrados e recomendações
priorizadas.""",
    ),
    (
        "propositor",
        "propose",
        """Você é o robô PROPOSITOR de um pipeline de brainstorming/análise — a fase final.
Sua missão é CONSOLIDAR as análises das fases anteriores (estado do projeto, lacunas e
usabilidade) e GERAR PROPOSTAS de tarefas usando a ferramenta "criar tarefas"
(autoia_tasks.json): 1 a N propostas que transformam o brainstorm em trabalho executável.

Ideia/brainstorm — Título: {task_title}
Ideia/brainstorm — Descrição: {task_description}

Cada proposta deve ter título claro, descrição com contexto e critérios, e kind adequado
(feature/bug/issue/chore). Priorize por impacto e entregue VALOR VERTICAL: cada proposta
deve deixar o usuário conseguir fazer algo de ponta a ponta (backend + tela + testes na
mesma entrega, `surface: "fullstack"`), não apenas uma camada técnica. Se existir
`docs/PRODUTO.md`, leia-o, declare `coverage` com os IDs das histórias cobertas e cubra
TODAS — UI "para depois" só vale com a proposta de frontend/fullstack correspondente no
mesmo lote. Inclua a proposta final de aceite ponta a ponta (browser real). IMPORTANTE:
as propostas NÃO são criadas automaticamente como tasks — o sistema as registra como
PROPOSTAS pendentes e o usuário decide aceitar ou rejeitar cada uma. No texto final,
documente as propostas criadas, a matriz de cobertura e a justificativa de cada uma.""",
    ),
    (
        "browser-tester",
        "verify",
        """Você é o robô BROWSER TESTER — teste visual e funcional REAL da aplicação após deploy.

Título: {task_title}
Descrição: {task_description}

Sua missão é validar a aplicação em execução REAL: abra o navegador, interaja com a UI,
verifique visualmente cada critério de aceite que envolva interface. Você é o ÚNICO que
testa com um navegador de verdade — o tester tradicional só roda testes automatizados.

### Ferramentas disponíveis
- **kimi-webbridge (MCP)** — use como ferramenta PRINCIPAL. Controle o navegador real para
  abrir páginas, clicar, preencher formulários, verificar textos e elementos visíveis.
  Tire screenshots de cada tela/fluxo testado.
- **Playwright** — use como FALLBACK quando precisar de automação mais complexa
  (múltiplas abas, testes de API + UI combinados, verificações de rede).

### O que testar
1. Abra a aplicação (se for web, use a URL de desenvolvimento ou staging; se o projeto
   tiver um script de start, rode-o primeiro).
2. Para CADA critério de aceite da história que envolva UI, faça o teste visual:
   - Navegue até a tela relevante
   - Interaja com os elementos (clique, preencha, submeta)
   - Verifique se o comportamento está correto
   - Tire um screenshot para cada passo crítico
3. Teste também o caminho feliz E pelo menos 1 cenário de erro (ex.: formulário vazio,
   credenciais inválidas, campo obrigatório não preenchido).

### Screenshots (OBRIGATÓRIO)
Salve TODOS os screenshots no diretório `autoia_screenshots/step_<id>` na raiz do
checkout (o ID da fase está no autoia_handoff.md). Use nomes descritivos:
`login-sucesso.png`, `dashboard-vazio.png`, `form-erro-validacao.png`, etc.

Documente CADA screenshot no seu SUMMARY com:
- O que foi testado
- Se passou ou falhou
- Qualquer observação relevante

Os screenshots serão exibidos automaticamente na interface do autoia como galeria de
artifacts — são a evidência visual do seu trabalho.

### Veredicto
Após testar tudo, escreva `autoia_verdict.txt`:
- PASS: todos os critérios visuais atendidos, sem regressões
- FAIL: qualquer tela/quebra ou comportamento incorreto, com relatório detalhado

NÃO modifique código — apenas TESTE e REPORTE.""",
    ),
    # ── Pipeline Android SEM emulador ────────────────────────────────────────────
    # Fluxo para projetos Android cuja verificação NÃO pode depender de emulador
    # (boot/qemu = timeout e infra_blocked). Todos os robôs carregam a mesma
    # diretriz (`ANDROID_NOEMU_POLICY`) e validam por JVM/Robolectric/build.
    (
        "android-noemu-po",
        "refine",
        """Você é o robô PO do pipeline ANDROID SEM EMULADOR. Sua missão é transformar
uma IDEIA CRUA em uma história clara, executável e verificável SEM emulador/dispositivo.

Ideia crua — Título: {task_title}
Ideia crua — Descrição: {task_description}

Escreva a história final com descrição objetiva, escopo mínimo e critérios de aceite
verificáveis. Se a ideia for vaga ou ambígua, faça as melhores suposições e deixe-as
EXPLÍCITAS na descrição.

### Critérios verificáveis sem device (obrigatório)
- Nenhum critério pode exigir emulador, dispositivo, `connectedAndroidTest`,
  `connectedCheck` ou instalação do APK. Para comportamento de UI, descreva o
  resultado esperado e critérios que possam ser provados por testes JVM/Robolectric,
  build e inspeção de código.
- Prefira critérios de unidade, contrato, estado e build (ex.: `./gradlew test`,
  `assembleDebug`) a roteiros manuais em device."""
        + ANDROID_NOEMU_POLICY,
    ),
    (
        "android-noemu-qa",
        "review",
        """Você é o robô QA do pipeline ANDROID SEM EMULADOR. Sua missão é REVISAR A
HISTÓRIA (descrição + critérios de aceite) antes do desenvolvimento.

Título: {task_title}

Além da revisão usual (clareza, completude, feedback visual e efetividade):
- REPROVE (NEEDS_WORK) qualquer critério que dependa de emulador, dispositivo ou
  teste conectado. No SUMMARY, reescreva o critério na versão verificável por testes
  JVM/Robolectric, build e inspeção de código.
- A história não pode exigir screenshots/execução em device como prova de aceite.
Se a história estiver boa e verificável sem device, veredicto READY."""
        + ANDROID_NOEMU_POLICY,
    ),
    (
        "android-noemu-dev",
        "implement",
        """Você é o robô DESENVOLVEDOR do pipeline ANDROID SEM EMULADOR.

Título: {task_title}
Descrição: {task_description}

Implemente a solução de forma limpa e idiomática, seguindo a stack Android já existente
do projeto (Kotlin/Java + Gradle e as bibliotecas atuais). Atenda a TODOS os critérios de
aceite.

### Testes e evidência (obrigatório)
- Escreva/ajuste testes em `src/test` (JVM). Use Robolectric quando precisar de APIs
  Android sem device.
- NÃO crie testes em `src/androidTest` que dependam de device e NÃO rode a suíte
  instrumentada existente.
- Antes do commit, rode `./gradlew test` (ou `testDebugUnitTest`) e pelo menos
  `assembleDebug`/`compileDebugKotlin`; registre comandos e saídas reais no resumo.
- Ao concluir, faça commit local das mudanças.

### Regras de escopo e qualidade
- NÃO modifique arquivos fora do escopo da tarefa (sem refactors oportunistas).
- Se algo quebrado NÃO pertencer à sua tarefa, REPORTE no resumo em vez de corrigir
  em silêncio."""
        + ANDROID_NOEMU_POLICY,
    ),
    (
        "android-noemu-validador",
        "verify",
        """Você é o robô VALIDADOR do pipeline ANDROID SEM EMULADOR — a prova de que a
entrega funciona de verdade, sem depender de device.

Título: {task_title}
Descrição: {task_description}

Valide CADA critério de aceite contra o código implementado:
- Rode a suíte JVM (`./gradlew test` ou `testDebugUnitTest`) e o build
  (`assembleDebug`/`compileDebugKotlin`), com resultado REAL no SUMMARY.
- Valide o restante por revisão do código/diff; quando o comportamento for de UI,
  confira a implementação e os testes JVM/Robolectric que a cobrem.
- NÃO instale/abra o app, NÃO use `adb` e NÃO rode teste conectado/instrumentado.
- Se um critério só puder ser provado em device, registre `NÃO VERIFICADO:` com o que
  faltou e continue — ausência de emulador NÃO é FAIL.
Só emita o veredicto depois de verificar de verdade o que é possível sem device."""
        + ANDROID_NOEMU_POLICY,
    ),
    (
        "android-noemu-avaliador",
        "assess",
        """Você é o robô AVALIADOR do pipeline ANDROID SEM EMULADOR — a avaliação final
da tarefa ANTES da integração.

Título: {task_title}
Descrição: {task_description}
Branch base: {default_branch}

Avalie se a tarefa foi realmente entregue: critérios de aceite atendidos, escopo
respeitado, solução coerente e de qualidade, sem lixo (temporários, debug, credenciais)
e sem dívidas não justificadas. Valide por revisão do diff/código e pelas evidências
JVM/build do handoff. NÃO exija prova em device: se o validador registrou
`NÃO VERIFICADO:` por falta de emulador, isso não é defeito por si só. Só emita FAIL
por defeito real no código ou evidência insuficiente para confiar na entrega."""
        + ANDROID_NOEMU_POLICY,
    ),
    (
        "android-noemu-merger",
        "merge",
        """Você é o robô MERGER do pipeline ANDROID SEM EMULADOR — a fase que deixa a
branch pronta para a integração.

Título: {task_title}
Descrição: {task_description}

## Verificação antes de decidir
- Estado: `git status`, `git branch --show-current`, `git log --oneline -5`.
- Divergência: `git log origin/{default_branch}..HEAD`, `git log HEAD..origin/{default_branch}`
  e `git merge-base`.
- Rode APENAS a verificação JVM/build do projeto (`./gradlew test`, `assembleDebug`).
  NÃO rode `connectedAndroidTest`/`connectedCheck`, NÃO use `adb` e NÃO inicie emulador.
- Antecipe conflitos: `git merge-tree --write-tree origin/{default_branch} HEAD`.

## Se houver conflito — resolva NA BRANCH
1. `git merge origin/{default_branch}`.
2. Aplique as regras de decisão: feedback do usuário > lado já integrado/testado >
   combinar ambos > evitar duplicação.
3. Edite, rode a suíte JVM de novo ATÉ PASSAR e faça commit local do merge resolvido.

A integração final (merge + push) é feita pelo SISTEMA ao fim da fase: você NÃO faz
push nem merge final. Se a resolução for ambígua ou de alto risco, escreva
`autoia_blocked.json` e pare."""
        + ANDROID_NOEMU_POLICY,
    ),
    (
        "android-noemu-deploy-tester",
        "verify",
        """Você é o robô DEPLOY TESTER do pipeline ANDROID SEM EMULADOR — a validação
final pós-merge, na branch DEFAULT integrada.

Título: {task_title}
Descrição: {task_description}

Confirme no git log que o merge da tarefa está presente na default e valide o estado
integrado:
- Rode a suíte JVM (`./gradlew test`/`testDebugUnitTest`) e o build
  (`assembleDebug`/`compileDebugKotlin`); confira que os critérios continuam atendidos.
- NÃO instale/abra o app, NÃO use `adb`, NÃO rode teste conectado e NÃO inicie emulador.
- Registre evidências reais e limitações no SUMMARY. Ausência de device não é FAIL:
  valide por build/testes JVM e revisão do código final.
NÃO modifique nem commite arquivos. Emita o veredicto obrigatório."""
        + ANDROID_NOEMU_POLICY,
    ),
]

SEED_PIPELINES = [
    (
        "po-qa-dev-tester-avaliador-deploytest",
        [
            ("po", False),
            ("qa", False),
            ("developer", False),
            ("tester", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),  # pós-merge: roda na default integrada
        ],
    ),
    (
        "po-qa-dev-tester-avaliador-merge",
        ["po", "qa", "developer", "tester", "avaliador", "merger"],
    ),
    (
        # Fluxo ADVPL (Protheus/TOTVS): usa o desenvolvedor específico de ADVPL e o
        # fluxo padrão de revisão/teste/avaliação/integração (sem fase pós-merge).
        "advpl-po-qa-dev-tester-avaliador-merge",
        ["po", "qa", "developer-advpl", "tester", "avaliador", "merger"],
    ),
    (
        "po-qa-dev-tester-avaliador-deploytest-browser",
        [
            ("po", False),
            ("qa", False),
            ("developer", False),
            ("tester", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),   # pós-merge: valida deploy na default integrada
            ("browser-tester", True),  # pós-merge: smoke test visual com navegador real
        ],
    ),
    # Brainstorm/planejamento: inicia o projeto, mapeia lacunas e usabilidade e, no
    # fim, o propositor escreve autoia_tasks.json → propostas aguardando decisão humana.
    (
        "iniciador-analista-ux-propositor",
        [
            ("iniciador", False),
            ("analista", False),
            ("auditor-ux", False),
            ("propositor", False),
        ],
    ),
    # Pipelines deep-v2 (por superfície da tarefa): QA rigoroso (qa) por padrão +
    # variantes -lean (qa-lean), validador (role verify) substitui o tester e o
    # avaliador/assess vira a avaliação final de qualidade. Superfícies:
    # - deep-v2-backend: sem browser (validação pós-merge só com deploy-tester).
    # - deep-v2-frontend / deep-v2-fullstack: com browser-tester pós-merge.
    (
        "deep-v2-backend",
        [
            ("po", False),
            ("qa", False),
            ("developer", False),
            ("validador", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),  # pós-merge: valida deploy na default integrada
        ],
    ),
    (
        "deep-v2-frontend",
        [
            ("po", False),
            ("qa", False),
            ("developer", False),
            ("validador", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),   # pós-merge: valida deploy na default integrada
            ("browser-tester", True),  # pós-merge: smoke test visual com navegador real
        ],
    ),
    (
        "deep-v2-fullstack",
        [
            ("po", False),
            ("qa", False),
            ("developer", False),
            ("validador", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),   # pós-merge: valida deploy na default integrada
            ("browser-tester", True),  # pós-merge: smoke test visual com navegador real
        ],
    ),
    (
        "deep-v2-backend-lean",
        [
            ("po", False),
            ("qa-lean", False),
            ("developer", False),
            ("validador", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),
        ],
    ),
    (
        "deep-v2-frontend-lean",
        [
            ("po", False),
            ("qa-lean", False),
            ("developer", False),
            ("validador", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),
            ("browser-tester", True),
        ],
    ),
    (
        "deep-v2-fullstack-lean",
        [
            ("po", False),
            ("qa-lean", False),
            ("developer", False),
            ("validador", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),
            ("browser-tester", True),
        ],
    ),
    # Android SEM emulador: fluxo próprio (robôs `android-noemu-*`) para projetos
    # cuja verificação é JVM/Robolectric/build. Sem tester/browser-tester/emulador;
    # o deploy-tester pós-merge também roda apenas a suíte JVM e o build.
    (
        "android-noemu",
        [
            ("android-noemu-po", False),
            ("android-noemu-qa", False),
            ("android-noemu-dev", False),
            ("android-noemu-validador", False),
            ("android-noemu-avaliador", False),
            ("android-noemu-merger", False),
            ("android-noemu-deploy-tester", True),
        ],
    ),
]

# Catálogo padrão de TIPOS DE ETAPA do fluxo de chamados (global; configurável por
# repositório via /api/chamado-stage-types). `close_options` limita as transições
# possíveis ao fechar a etapa: `next:<tipo>`, `resposta`, `cancelar`, `concluir`.
SEED_STAGE_TYPES = [
    {
        "name": "entrada",
        "description": "Registro e entendimento inicial do chamado pelo suporte.",
        "is_initial": True,
        "allowed_tools": ["assistente"],
        "close_options": ["next:analise", "next:desenvolvimento", "resposta", "cancelar"],
    },
    {
        "name": "analise",
        "description": "Análise do problema, entendimento da causa e montagem de escopo se necessário.",
        "is_initial": False,
        "allowed_tools": ["assistente", "escopo"],
        "close_options": ["next:desenvolvimento", "resposta", "cancelar"],
    },
    {
        "name": "desenvolvimento",
        "description": "Execução do desenvolvimento (entrega configurável — fase 2).",
        "is_initial": False,
        "allowed_tools": ["assistente", "escopo"],
        "close_options": ["next:deploy", "resposta", "cancelar", "concluir"],
    },
    {
        "name": "deploy",
        "description": "Validação pós-entrega e fechamento do chamado.",
        "is_initial": False,
        "allowed_tools": ["assistente", "resposta"],
        "close_options": ["concluir", "resposta"],
    },
]


def seed(session_factory) -> None:
    with session_factory() as s:
        robots = {
            r.name: r
            for r in s.query(Robot).filter(Robot.repository_id.is_(None)).all()
        }
        for name, role, mission in SEED_ROBOTS:
            robot = robots.get(name)
            if robot is None:
                robot = Robot(name=name, role=role, mission=mission)
                s.add(robot)
            elif robot.role != role or robot.mission != mission:
                # atualiza robôs de seeds antigos (idempotente)
                robot.role = role
                robot.mission = mission
        s.commit()

        for pipeline_name, steps_spec in SEED_PIPELINES:
            pipeline = (
                s.query(Pipeline)
                .filter(
                    Pipeline.name == pipeline_name,
                    Pipeline.repository_id.is_(None),
                )
                .first()
            )
            if pipeline is None:
                pipeline = Pipeline(name=pipeline_name)
                s.add(pipeline)
            existing = {(ps.position, ps.robot_id): ps for ps in pipeline.steps}
            existing_robots = {ps.robot.name: ps for ps in pipeline.steps if ps.robot}
            for position, spec in enumerate(steps_spec):
                robot_name, post_merge = spec if isinstance(spec, tuple) else (spec, False)
                robot = (
                    s.query(Robot)
                    .filter(Robot.name == robot_name, Robot.repository_id.is_(None))
                    .first()
                )
                if not robot:
                    continue
                # Pipeline existente: adiciona apenas fases do seed que não existem
                # (por robô), preservando personalizações e posições já criadas.
                if robot.name in existing_robots:
                    continue
                if (position, robot.id) in existing:
                    continue
                pipeline.steps.append(
                    PipelineStep(
                        position=position,
                        robot_id=robot.id,
                        post_merge=post_merge,
                    )
                )
        s.commit()
        log.info("seed concluído (robôs: %s; pipelines: %s)",
                 ", ".join(r[0] for r in SEED_ROBOTS),
                 ", ".join(p[0] for p in SEED_PIPELINES))
        _seed_stage_types(s)


def _seed_stage_types(s) -> None:
    """Seed idempotente do catálogo global de tipos de etapa do fluxo de chamados."""
    existing = {st.name: st for st in s.query(ChamadoStageType).filter(ChamadoStageType.repository_id.is_(None)).all()}
    for spec in SEED_STAGE_TYPES:
        st = existing.get(spec["name"])
        if st is None:
            s.add(ChamadoStageType(repository_id=None, **spec))
        else:
            # atualiza seeds antigos (idempotente), preservando o id.
            for key, value in spec.items():
                setattr(st, key, value)
    s.commit()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    engine = make_engine(settings.database_url)
    ensure_schema(engine)
    session_factory = make_session_factory(engine)

    app = FastAPI(title="autoia", version="0.1.0")
    app.state.settings = settings
    app.state.Session = session_factory

    seed(session_factory)

    # Auth: register/login/logout/me e gestão de usuários ficam acessíveis sem
    # proteção global (o próprio fluxo de auth valida a sessão quando preciso).
    app.include_router(auth.router)
    app.include_router(users.router)

    # Com `Settings.auth_enabled=True`, TODOS os routers /api/* exigem sessão
    # válida (401 sem cookie); com OFF, `require_auth` retorna None e o
    # comportamento atual é preservado integralmente.
    for r in (
        repositories.router,
        robots.router,
        pipelines.router,
        tasks.router,
        subtasks.router,
        steps.router,
        dashboard.router,
        dashboard.me_router,
        execution.router,
        chamados.projects_router,
        chamados.epics_router,
        chamados.stage_types_router,
        chamados.chamados_router,
        system.router,
    ):
        app.include_router(r, dependencies=[Depends(require_auth)])

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/worker/status")
    def worker_status(_user: User | None = Depends(require_auth)):
        hb = os.path.join(settings.workspace_dir, "worker.heartbeat")
        try:
            mtime = os.path.getmtime(hb)
            age = time.time() - mtime
        except OSError:
            return {"alive": False, "last_heartbeat_sec": None}
        return {"alive": age < 15, "last_heartbeat_sec": round(age, 1)}

    if settings.frontend_dist and os.path.isdir(settings.frontend_dist):
        assets = os.path.join(settings.frontend_dist, "assets")
        if os.path.isdir(assets):
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            """Serve o build do frontend (PWA) com fallback SPA."""
            if full_path.startswith("api/"):
                raise HTTPException(404)
            candidate = os.path.join(settings.frontend_dist, full_path) if full_path else settings.frontend_dist
            if os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(settings.frontend_dist, "index.html"))

    return app


def _cleanup_workspaces(
    session_factory,
    workspace_dir: str,
    days: int,
    dry_run: bool = False,
    log=print,
) -> dict:
    """Remove workspaces de tasks `done`/`failed` concluídas há +`days` dias.

    Reusa a detecção de `storage._workspace_tasks` (`workspace_dir/<repo>/task_<id>`).
    Nunca toca tasks ativas (`queued`/`in_progress`/`open`/`needs_review`/bloqueadas)
    nem concluídas recentes. Retorna contadores para relatório/teste.
    """
    import shutil

    from app.models import TASK_DONE, TASK_FAILED, Task, TaskStep
    from app.storage import _workspace_tasks

    workspace_map = _workspace_tasks(workspace_dir)
    cutoff = utcnow() - timedelta(days=max(1, days))
    removed = 0
    skipped_active = 0
    skipped_recent = 0
    with session_factory() as s:
        for ws, task_id in sorted(workspace_map.items(), key=lambda kv: kv[1]):
            task = s.get(Task, task_id)
            if task is None or task.status not in (TASK_DONE, TASK_FAILED):
                skipped_active += 1
                continue
            # Data da conclusão: última fase concluída (fallback: updated_at).
            last_finished = (
                s.query(func.max(TaskStep.finished_at))
                .filter(TaskStep.task_id == task_id)
                .scalar()
            )
            concluded_at = last_finished or task.updated_at
            if concluded_at is None or concluded_at >= cutoff:
                skipped_recent += 1
                continue
            if dry_run:
                log(f"[dry-run] removeria {ws} (concluída {concluded_at:%Y-%m-%d})")
                removed += 1
                continue
            try:
                shutil.rmtree(ws)
                log(f"removido {ws} (concluída {concluded_at:%Y-%m-%d})")
                removed += 1
            except OSError as exc:
                log(f"erro ao remover {ws}: {exc}")
    return {
        "removed": removed,
        "skipped_active": skipped_active,
        "skipped_recent": skipped_recent,
    }


def run_cleanup() -> None:
    """CLI `autoia-cleanup`: remove workspaces de tasks concluídas há +N dias.

    Libera disco reusando a detecção de workspaces de `storage.py`
    (`workspace_dir/<repo>/task_<id>`): remove apenas diretórios cuja task está
    `done`/`failed` e cuja última atividade (fase concluída) é mais antiga que
    `--days` dias. Tasks ativas (queued/in_progress/open/needs_review/bloqueadas)
    e concluídas recentes NUNCA são tocadas. `--dry-run` apenas lista.

    Ex.: `autoia-cleanup --days 30` remove workspaces de tasks concluídas há
    mais de 30 dias; `autoia-cleanup --dry-run` mostra o que seria removido.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="autoia-cleanup",
        description="Remove workspaces de tasks concluídas há +N dias (libera disco).",
    )
    parser.add_argument("--days", type=int, default=7, help="idade mínima em dias (default 7)")
    parser.add_argument("--dry-run", action="store_true", help="apenas lista, não remove")
    args = parser.parse_args()

    settings = Settings()
    settings.ensure_dirs()
    engine = make_engine(settings.database_url)
    ensure_schema(engine)
    session_factory = make_session_factory(engine)

    if not os.path.isdir(settings.workspace_dir):
        print("nenhum workspace encontrado.")
        return

    result = _cleanup_workspaces(
        session_factory, settings.workspace_dir, args.days, dry_run=args.dry_run
    )
    action = "removeria" if args.dry_run else "removeu"
    print(
        f"autoia-cleanup {action} {result['removed']} workspace(s)"
        f" ({result['skipped_active']} ativa(s)/inexistente(s) preservada(s),"
        f" {result['skipped_recent']} concluída(s) recente(s) preservada(s))."
    )


def run_api() -> None:
    import uvicorn

    settings = Settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


def _pending_sandbox_images(settings: Settings, session_factory) -> list[str]:
    """Imagens de sandbox dos repositórios configurados que estão faltando.

    Usado no startup do worker: em vez de descobrir a imagem faltando na cara
    de uma fase (`infra_blocked`), confere na subida — uma limpeza de disco pode
    ter apagado as imagens locais do Docker.
    """
    from app.models import Repository
    from app.worker import runner as runner_mod

    images: list[str] = []
    with session_factory() as s:
        repos = s.query(Repository).all()
    for repo in repos:
        try:
            cfg = runner_mod._sandbox_config(settings, repo)
        except Exception:  # pragma: no cover — repo com config exótica
            continue
        if cfg.enabled and cfg.image:
            images.append(cfg.image)
    return sorted(set(images))


def _preflight_sandbox_images(settings: Settings, session_factory, logger) -> None:
    """No startup: garante (reconstruindo se preciso) as imagens do sandbox.

    Roda em thread daemon para não atrasar o loop; usa o MESMO lock por imagem
    do `sandbox.ensure_image`, então workers concorrentes não duplicam o build.
    """
    if not getattr(settings, "sandbox_build_image", True):
        return
    from app.worker import sandbox as sandbox_mod

    try:
        missing = [
            image
            for image in _pending_sandbox_images(settings, session_factory)
            if not sandbox_mod.docker_image_available(image)
        ]
    except Exception:
        logger.exception("startup: falha ao conferir imagens do sandbox")
        return
    for image in missing:
        logger.warning("startup: imagem do sandbox ausente — construindo %s", image)
        try:
            ok = sandbox_mod.ensure_image(
                image,
                log_dir=settings.log_dir,
                lock_dir=settings.workspace_dir,
                timeout_s=settings.sandbox_build_timeout,
                enabled=True,
            )
        except Exception:
            logger.exception("startup: falha ao construir a imagem %s", image)
            continue
        logger.info(
            "startup: imagem do sandbox %s %s", image, "pronta" if ok else "FALHOU"
        )


def _start_sandbox_image_preflight(settings: Settings, session_factory, logger) -> None:
    """Confere/reconstrói as imagens do sandbox em thread daemon (não bloqueia)."""
    thread = threading.Thread(
        target=_preflight_sandbox_images,
        args=(settings, session_factory, logger),
        daemon=True,
        name="sandbox-image-preflight",
    )
    thread.start()


def _worker_setup(settings: Settings, *, recover: bool, logger) -> object:
    """Engine/session + (opcional) recuperação de órfãos + proxy egress.

    Retorna a session_factory. `recover` só é True para o PRIMEIRO processo do
    grupo (o pai): se cada processo recuperasse no startup, um worker mais lento
    resetaria para `pending` uma fase que outro acabou de reclamar → a mesma fase
    roda 2× em paralelo (bug real observado com `--workers 3`).
    """
    engine = make_engine(settings.database_url)
    ensure_schema(engine)  # não depende da API ter subido antes
    session_factory = make_session_factory(engine)
    if recover:
        recovered = recover_stale_steps(session_factory)
        if recovered:
            logger.info("worker recuperou %s step(s) running órfão(s) para re-execução", recovered)

    from app.worker import sandbox as sandbox_mod

    if sandbox_mod.normalize_mode(settings.sandbox) == sandbox_mod.SANDBOX_FULL:
        port = sandbox_mod.ensure_egress_proxy(settings.sandbox_proxy_port, settings.whitelisted_hosts)
        logger.info("sandbox full: proxy de egress allowlist na porta %s", port)
    return engine, session_factory


def _worker_process(settings: Settings, logger) -> None:
    """Loop do worker num processo. Cria engine/sessão PRÓPRIOS (após o fork — o
    pool de conexões do pai não é fork-safe). Sem lock/recover: o grupo já garantiu."""
    _engine, session_factory = _worker_setup(settings, recover=False, logger=logger)
    worker_loop(settings, session_factory, settings.workspace_dir)


def _install_worker_shutdown_handler(logger) -> None:
    """Handler de shutdown do worker (single, filho multi-worker e chamado-worker):
    mata os executores ativos (kimi/opencode, inclusive o contêiner do sandbox) e
    sai IMEDIATAMENTE com `os._exit(0)` — sem passar pelo loop do runner, que
    registraria a execução morta como FALHA da fase ("kimi saiu com código -9").

    Com isso, um desligamento INTENCIONAL deixa o step `running` (nada é gravado);
    o próximo startup o recupera como `pending` via `recover_stale_steps`. Processos
    que escapam do grupo do executor (emuladores, gradle daemons, servidores fake)
    são varridos pelo comando `autoia-stop`.
    """
    import signal

    from app.worker import exec_common

    def _on_shutdown(signum, _frame):
        logger.info("worker recebeu sinal %s — encerrando subprocessos", signum)
        exec_common.kill_all_procs()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT, _on_shutdown)


def _worker_main(settings: Settings) -> None:
    """Worker de instância única (`--workers 1`): lock exclusivo + recover + loop."""
    logger = logging.getLogger("autoia.worker")
    lock = acquire_worker_lock(os.path.join(settings.workspace_dir, "worker.lock"))
    if lock is None:
        try:
            pid = open(os.path.join(settings.workspace_dir, "worker.lock"), encoding="utf-8").read().strip()
        except OSError:
            pid = "?"
        print(
            f"worker já está rodando (PID {pid}); não é possível iniciar outro.",
            file=sys.stderr,
        )
        sys.exit(1)

    _install_worker_shutdown_handler(logger)
    _engine, session_factory = _worker_setup(settings, recover=True, logger=logger)
    # Subida do app: já confere as imagens do sandbox (reconstrói se faltarem).
    _start_sandbox_image_preflight(settings, session_factory, logger)
    worker_loop(settings, session_factory, settings.workspace_dir)


def run_chamado_worker() -> None:
    """Worker do fluxo de CHAMADOS (processo separado, lock próprio)."""
    logger = logging.getLogger("autoia.chamado")
    settings = Settings()
    settings.ensure_dirs()
    lock = acquire_worker_lock(os.path.join(settings.workspace_dir, "chamado-worker.lock"))
    if lock is None:
        try:
            pid = open(os.path.join(settings.workspace_dir, "chamado-worker.lock"), encoding="utf-8").read().strip()
        except OSError:
            pid = "?"
        print(
            f"chamado-worker já está rodando (PID {pid}); não é possível iniciar outro.",
            file=sys.stderr,
        )
        sys.exit(1)

    _install_worker_shutdown_handler(logger)
    engine = make_engine(settings.database_url)
    ensure_schema(engine)
    session_factory = make_session_factory(engine)
    recovered = recover_stale_chamados(session_factory)
    if recovered:
        logger.info("chamado-worker recuperou %s etapa(s) órfã(s)", recovered)
    chamado_worker_loop(settings, session_factory, settings.workspace_dir)


def run_chat_worker() -> None:
    """Worker do modo human-in-the-loop de tasks (processo separado, lock próprio)."""
    from .worker.chat_runner import chat_worker_loop, recover_stale_chats

    logger = logging.getLogger("autoia.chat")
    settings = Settings()
    settings.ensure_dirs()
    lock = acquire_worker_lock(os.path.join(settings.workspace_dir, "chat-worker.lock"))
    if lock is None:
        try:
            pid = open(os.path.join(settings.workspace_dir, "chat-worker.lock"), encoding="utf-8").read().strip()
        except OSError:
            pid = "?"
        print(
            f"chat-worker já está rodando (PID {pid}); não é possível iniciar outro.",
            file=sys.stderr,
        )
        sys.exit(1)

    _install_worker_shutdown_handler(logger)
    engine = make_engine(settings.database_url)
    ensure_schema(engine)
    session_factory = make_session_factory(engine)
    recovered = recover_stale_chats(session_factory)
    if recovered:
        logger.info("chat-worker recuperou %s ação(ões) órfã(s)", recovered)
    chat_worker_loop(settings, session_factory, settings.workspace_dir)


def _supervise_worker_children(
    children: list[int],
    spawn,
    logger,
    is_stopping,
    *,
    poll_s: float = 1.0,
    waitpid=os.waitpid,
) -> list[int]:
    """Pai supervisor: mantém o grupo de workers com N processos vivos.

    Filho que sai sozinho (crash, `os._exit` de uma exceção que escapou do loop)
    é LOGADO e substituído — sem isso a capacidade cai em silêncio (com 2 de 3
    workers mortos a fila andava com um terço do parque e as tasks pareciam
    "travadas sem worker livre") e o filho morto virava zumbi para sempre.

    `children` é atualizado IN-PLACE: o handler de sinal do pai varre a mesma
    lista. Filho que morre logo após nascer aumenta a espera (anti fork bomb).
    """
    fast_deaths = 0
    while not is_stopping():
        delay = poll_s if fast_deaths == 0 else min(30.0, poll_s * (2**fast_deaths))
        if delay > 0:
            time.sleep(delay)
        if is_stopping():
            break
        alive: list[int] = []
        deaths = 0
        for pid in list(children):
            try:
                wpid, status = waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue  # já colhido por outro caminho
            if wpid == 0:
                alive.append(pid)
                continue
            deaths += 1
            logger.error("worker filho %s saiu (status %s) — repondo o processo", pid, status)
            try:
                alive.append(spawn())
            except Exception:
                logger.exception("não foi possível repor o worker filho %s", pid)
        children[:] = alive
        fast_deaths = deaths if deaths else 0
        if not alive and not deaths:
            # Grupo vazio sem morte nova (todos os spawns falharam) — espera mais
            # para não virar fork bomb e tenta de novo.
            fast_deaths = 2
            logger.error("grupo de workers vazio — repondo no próximo ciclo")
    return children


def run_worker() -> None:
    import argparse
    import signal

    from app.worker import exec_common

    parser = argparse.ArgumentParser(description="autoia worker")
    parser.add_argument("--workers", type=int, default=1, help="número de processos de worker")
    args = parser.parse_known_args()[0]  # ignora args desconhecidos (uvicorn pode injetar)

    # O pipeline executa UMA fase por task por vez — garantido pelo claim atômico
    # (`claim_next`). Com `--workers N`, N PROCESSOS rodam fases de tasks diferentes
    # em paralelo. Threads no mesmo processo não são usadas (contornam o lock e
    # corrompem o estado). O grupo multi-worker é UM dono: o pai segura o lock
    # EXCLUSIVO (um segundo `autoia-worker` é recusado), recupera órfãos UMA vez
    # antes do fork, e os filhos só rodam o loop (sem lock/recover — evita que a
    # recuperação de um filho resetasse uma fase recém-reclamada por outro).
    workers = max(1, args.workers or 1)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    logger = logging.getLogger("autoia.worker")
    settings = Settings()
    settings.ensure_dirs()

    if workers == 1:
        _worker_main(settings)
        return

    lock_path = os.path.join(settings.workspace_dir, "worker.lock")
    lock = acquire_worker_lock(lock_path)
    if lock is None:
        try:
            pid = open(lock_path, encoding="utf-8").read().strip()
        except OSError:
            pid = "?"
        print(
            f"worker já está rodando (PID {pid}); não é possível iniciar outro.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Recuperação de órfãos UMA única vez, no pai, ANTES de qualquer filho claimar.
    parent_engine, _sf = _worker_setup(settings, recover=True, logger=logger)
    parent_engine.dispose()  # fecha o pool antes do fork (os filhos criam o próprio)

    logger.info("iniciando %s processos de worker (parallelismo por task)", workers)

    def _spawn_worker() -> int:
        pid = os.fork()
        if pid == 0:  # filho: loop puro, sem lock/recover (engine próprio)
            try:
                # Handler de shutdown ANTES de qualquer execução: SIGTERM mata os
                # executores do filho e sai SEM gravar falha fake na fase.
                _install_worker_shutdown_handler(logging.getLogger("autoia.worker"))
                _worker_process(settings, logger)
            finally:
                os._exit(0)
        return pid

    children: list[int] = [_spawn_worker() for _ in range(workers)]
    # Subida do grupo: confere/reconstrói as imagens do sandbox em background
    # (após o fork — nunca paralelizar thread de build com `os.fork`).
    _start_sandbox_image_preflight(settings, _sf, logger)

    def _on_signal(signum, _frame):
        logger.info("pai recebeu sinal %s — encaminhando para os workers", signum)
        for pid in children:
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass
        # Deadline: cada filho mata seus executores e sai com _exit(0); se algum
        # travar, SIGKILL nele — nunca deixar o pai pendurado nem kimi órfão.
        deadline = time.monotonic() + 20
        remaining = list(children)
        while remaining and time.monotonic() < deadline:
            for pid in list(remaining):
                wpid, _status = os.waitpid(pid, os.WNOHANG)
                if wpid:
                    remaining.remove(pid)
            if not remaining:
                break
            time.sleep(0.2)
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        exec_common.kill_all_procs()  # belt-and-braces (filhos já deveriam ter matado)
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    # Supervisão em vez de `waitpid` bloqueante no primeiro filho: quem morre é
    # reposto (e colhido — sem zumbi) enquanto o pai estiver de pé.
    _supervise_worker_children(children, _spawn_worker, logger, lambda: False)


if __name__ == "__main__":
    run_api()
