# Refatoração da tela de acompanhamento dos desenvolvimentos via Codex

Quero refatorar a experiência da tela de consulta/acompanhamento dos desenvolvimentos realizados automaticamente pelo Codex.

O problema atual é que a tela apresenta informação demais de uma vez, principalmente logs, prompts, respostas completas e detalhes técnicos da execução. Isso dificulta entender rapidamente **o que foi feito**.

Quero transformar essa tela em uma interface de acompanhamento de desenvolvimento com **3 níveis progressivos de detalhe**.

---

# 1. Nível principal — Visão resumida

Este deve ser o nível mais importante e o que o usuário vê primeiro.

O objetivo é responder rapidamente:

> "O que aconteceu com esse desenvolvimento?"

Após a execução do Codex, deve ser feita uma chamada de API para uma LLM responsável exclusivamente por analisar o contexto da execução e gerar um resumo.

A LLM deverá receber, conforme disponível:

- Solicitação original enviada ao Codex;
- Detalhes adicionais fornecidos manualmente pelo usuário;
- Entrada completa enviada ao Codex;
- Saída completa retornada pelo Codex;
- Tarefas propostas pelo Codex;
- Tarefas executadas;
- Resultado de tool calls relevantes;
- Arquivos alterados;
- Resultado de testes;
- Erros, falhas ou limitações;
- Status da execução.

A LLM não deve realizar o desenvolvimento. Ela apenas deve interpretar o que aconteceu e produzir uma representação resumida e objetiva.

## O resumo deve priorizar

### O que foi solicitado

Uma descrição curta da necessidade original.

### O que foi implementado

Descrição concreta das alterações realizadas, evitando frases genéricas.

### Principais mudanças

Lista curta das alterações mais importantes.

### Resultado

Informar se a implementação foi concluída, parcialmente concluída, falhou ou ficou pendente.

### Problemas/observações

Somente quando houver algo relevante.

O resumo deve permitir que alguém compreenda o desenvolvimento sem precisar abrir os logs.

---

# 2. Nível intermediário — Detalhamento do desenvolvimento

Ao expandir o desenvolvimento, o usuário deve conseguir entender **como o trabalho foi realizado**, mas ainda sem ser exposto aos logs brutos.

Neste nível devem aparecer informações como:

## Solicitação

A solicitação original do desenvolvimento.

## Contexto adicional

O conteúdo informado manualmente pelo usuário através do campo de detalhes da implementação.

Esse campo é importante porque permite que o usuário complemente ou corrija o contexto antes ou durante o desenvolvimento.

## Etapas e tarefas

Exibir as tarefas relacionadas ao desenvolvimento.

O Codex pode, através de `tool_call`, sugerir novas tarefas durante o processo.

As tarefas devem possuir estados claros, por exemplo:

- Pendente
- Em execução
- Concluída
- Falhou
- Bloqueada
- Aguardando decisão
- Aguardando dependência
- Cancelada

Também deve ser possível identificar quais tarefas foram originalmente propostas e quais foram sugeridas posteriormente pelo Codex.

Uma sugestão do Codex não deve ser automaticamente considerada uma tarefa concluída.

## Fluxo de execução

Mostrar de maneira visual as etapas pelas quais o desenvolvimento passou.

O usuário deve conseguir identificar em qual etapa o desenvolvimento está atualmente.

Também deve ser possível **voltar o desenvolvimento para uma etapa anterior**, respeitando as regras existentes do fluxo.

Essa ação deve ser claramente identificada como uma alteração do estado do desenvolvimento e não apenas como uma navegação visual.

## Arquivos alterados

Quando disponível, apresentar os arquivos relevantes modificados pelo Codex.

## Resultado dos testes

Apresentar de forma resumida:

- testes executados;
- testes aprovados;
- testes que falharam;
- eventuais mensagens relevantes.

---

# 3. Nível avançado — Execução técnica / auditoria

O terceiro nível deve ser destinado principalmente para investigação, debug e auditoria.

Aqui podem continuar disponíveis todas as informações atualmente existentes, incluindo:

- Prompt completo enviado ao Codex;
- Resposta completa do Codex;
- Tool calls;
- Argumentos das tool calls;
- Retornos das ferramentas;
- Logs;
- Histórico das etapas;
- Alterações de arquivos;
- Erros completos;
- Informações de execução;
- Metadados;
- Tokens/custos, quando disponíveis;
- Demais informações técnicas.

Essas informações não precisam desaparecer.

O objetivo é apenas **tirar esse conteúdo da frente da experiência normal**.

Um usuário deve conseguir entender o desenvolvimento sem abrir esse nível.

---

# Timeline de execução

Adicionar também uma **timeline cronológica da execução do desenvolvimento**.

Essa timeline deve representar o que aconteceu durante a execução do Codex como uma sequência de eventos.

Cada `tool_call` realizada pelo Codex deve ser registrada como um evento na timeline.

Exemplo:

```text
10:32  Desenvolvimento iniciado

10:32  Analisando estrutura do projeto

10:33  Tool Call
       consultar_arquivo
       src/services/OrderService.ts

10:33  Tool Call
       buscar_codigo
       "validação de estoque"

10:34  Arquivo analisado
       OrderService.ts

10:35  Tool Call
       alterar_arquivo
       OrderService.ts

10:36  Tool Call
       executar_testes
       OrderServiceTest

10:37  Testes concluídos
       18 passed

10:38  Desenvolvimento concluído
```

A timeline deve ser **cronológica**, utilizando timestamps reais da execução quando disponíveis.

## Tipos de eventos

A estrutura deve permitir diferentes tipos de eventos, por exemplo:

- Desenvolvimento iniciado;
- Etapa iniciada;
- Etapa concluída;
- Tarefa criada;
- Tarefa sugerida pelo Codex;
- Tarefa iniciada;
- Tarefa concluída;
- `tool_call`;
- Resultado de `tool_call`;
- Arquivo criado;
- Arquivo alterado;
- Arquivo removido;
- Teste iniciado;
- Teste concluído;
- Erro;
- Aviso;
- Intervenção do usuário;
- Alteração de contexto;
- Retorno para etapa anterior;
- Desenvolvimento concluído.

Não é necessário que todos esses eventos existam imediatamente. A arquitetura deve ser preparada para suportá-los.

---

# Tool Calls

Cada `tool_call` deve ser representada como um evento próprio.

Na visualização resumida da timeline, mostrar somente informações essenciais.

Exemplos:

```text
Consultar arquivo
src/services/OrderService.ts
```

ou:

```text
Executar testes
OrderServiceTest
```

ou:

```text
Alterar arquivo
src/services/OrderService.ts
```

O usuário deve poder expandir o evento para visualizar informações adicionais.

## Detalhamento da Tool Call

Ao expandir:

- nome da ferramenta;
- timestamp;
- duração, quando disponível;
- argumentos enviados;
- resultado retornado;
- status;
- erro, se houver.

No nível técnico, disponibilizar também o payload bruto da chamada e o retorno completo.

---

# Timeline + três níveis de detalhe

A timeline deve respeitar a mesma filosofia dos três níveis da aplicação.

## Nível 1

A timeline pode aparecer de forma compacta, mostrando apenas os eventos mais importantes.

Exemplo:

```text
✓ Desenvolvimento iniciado

✓ Analisou estrutura do projeto

✓ Implementou validação de estoque

✓ Executou testes
  18 testes aprovados

✓ Desenvolvimento concluído
```

Não mostrar todas as `tool_calls` individualmente neste nível se isso deixar a interface poluída.

## Nível 2

Ao abrir os detalhes do desenvolvimento, mostrar a timeline completa.

Neste nível devem aparecer todas as `tool_calls` como eventos, permitindo entender a sequência de ações realizadas pelo Codex.

## Nível 3

No nível técnico, cada evento da timeline deve permitir acessar todos os dados disponíveis.

Para uma `tool_call`:

```text
Tool: consultar_arquivo

Timestamp:
10:33:14

Duração:
842ms

Arguments:
{
  ...
}

Result:
{
  ...
}
```

Isso deve funcionar como uma ferramenta de auditoria/debug.

---

# Eventos como entidade própria

Sempre que possível, não tratar a timeline simplesmente como texto gerado pelo frontend.

Criar uma estrutura de evento persistida ou derivável de forma consistente a partir dos dados de execução.

Um evento deve possuir informações semelhantes a:

```json
{
  "type": "tool_call",
  "timestamp": "...",
  "name": "consultar_arquivo",
  "status": "completed",
  "duration_ms": 842,
  "summary": "Consultou OrderService.ts",
  "input": {},
  "output": {}
}
```

O campo `summary` pode ser gerado deterministicamente pelo sistema para eventos conhecidos.

Não utilizar a LLM para gerar o texto de cada evento individualmente.

A LLM deve continuar sendo utilizada principalmente para o **resumo geral do desenvolvimento**.

---

# Relação entre Timeline e LLM

A timeline também deve fornecer contexto para a geração do resumo.

A LLM poderá receber a sequência de eventos, juntamente com a entrada e saída do Codex, para entender melhor o que realmente aconteceu.

Porém, o sistema deve preservar os eventos originais.

A LLM não deve ser considerada a fonte de verdade da execução.

A fonte de verdade deve ser o histórico real das ações realizadas pelo Codex.

---

# Tarefas bloqueadas e retomada por instrução

Adicionar um mecanismo explícito para tratar tarefas que não conseguem continuar automaticamente.

Uma tarefa pode ficar parada por diversos motivos, por exemplo:

- Guardrail;
- Necessidade de decisão humana;
- Dependência de outra tarefa;
- Informação insuficiente;
- Ambiguidade na solicitação;
- Erro que exige intervenção;
- Falha de uma ferramenta;
- Necessidade de autorização;
- Decisão sobre uma abordagem técnica;
- Requisito que precisa ser esclarecido;
- Qualquer outra situação em que o Codex não consiga continuar com segurança ou autonomia.

Essas situações não devem ser tratadas simplesmente como "erro".

A tarefa deve assumir um estado que represente claramente que ela está **bloqueada aguardando intervenção**.

Por exemplo:

**Bloqueada — aguardando instrução**

---

# Campo de instrução para continuar

Quando uma tarefa estiver bloqueada, exibir um campo de texto específico para o usuário informar **como deseja que a execução continue**.

Exemplo:

```text
A tarefa está bloqueada porque existem duas formas possíveis
de implementar a funcionalidade.

Como deseja continuar?

Utilize a abordagem B. Não altere a estrutura atual do serviço
e mantenha compatibilidade com a API existente.
```

Disponibilizar uma ação:

**Continuar execução**

Essa instrução deve ser adicionada ao contexto da tarefa e enviada ao Codex na retomada.

---

# Retomar exatamente de onde parou

Ao clicar em **Continuar execução**, o sistema deve retomar o desenvolvimento a partir do ponto em que a tarefa foi interrompida.

Não deve criar um novo desenvolvimento independente.

O sistema deve preservar:

- desenvolvimento original;
- tarefa;
- etapa atual;
- contexto acumulado;
- histórico da execução;
- tool calls anteriores;
- arquivos já alterados;
- decisões anteriores;
- motivo do bloqueio;
- instruções adicionais fornecidas pelo usuário.

A nova instrução deve ser tratada como uma **intervenção do usuário no fluxo existente**.

---

# Histórico das intervenções

Cada intervenção deve aparecer na timeline.

Exemplo:

```text
10:32  Desenvolvimento iniciado

10:33  Tarefa: Implementar validação

10:35  Tool Call
       consultar_arquivo
       OrderService.ts

10:37  ⚠ Tarefa bloqueada

       Motivo:
       Guardrail impediu alteração automática.

10:40  👤 Intervenção do usuário

       "Pode alterar o serviço, mas não modifique
        a interface pública da classe."

10:41  ▶ Execução retomada

10:42  Tool Call
       alterar_arquivo
       OrderService.ts

10:44  ✓ Tarefa concluída
```

Isso é importante para que seja possível entender posteriormente **por que o agente tomou determinada decisão**.

---

# Motivo do bloqueio

Quando o Codex não puder continuar, deve registrar um motivo estruturado sempre que possível.

Exemplo:

```json
{
  "status": "blocked",
  "reason_type": "decision_required",
  "reason": "Existem duas abordagens possíveis para implementar a integração.",
  "question": "Deve ser utilizada a API existente ou criada uma nova camada de integração?"
}
```

O frontend deve apresentar esse motivo de maneira amigável.

---

# Instrução de retomada

A instrução fornecida pelo usuário deve ser armazenada separadamente do contexto original.

Exemplo:

```text
Solicitação original:
Implementar integração com o serviço X.

Detalhes iniciais:
A integração deve manter compatibilidade com o sistema atual.

Bloqueio:
Existem duas abordagens possíveis.

Intervenção do usuário:
Utilize a abordagem B e mantenha a interface atual.

Execução retomada:
Sim.
```

Isso permite diferenciar claramente:

**O que foi solicitado originalmente**

de

**O que foi decidido posteriormente para permitir a continuidade.**

---

# Não perder o contexto

A retomada não deve simplesmente enviar somente o texto digitado pelo usuário para uma nova chamada do Codex.

O agente precisa receber contexto suficiente para continuar corretamente.

A execução de retomada deve considerar:

1. Solicitação original;
2. Detalhes fornecidos pelo usuário;
3. Tarefas existentes;
4. Etapa atual;
5. Histórico relevante;
6. Motivo do bloqueio;
7. Estado da execução;
8. Alterações já realizadas;
9. Tool calls relevantes;
10. Nova instrução do usuário.

A nova instrução deve funcionar como uma **decisão/intervenção adicional**, e não como uma nova solicitação independente.

---

# Continuação após dependência

Esse mecanismo também deve funcionar quando uma tarefa depende de outra.

Exemplo:

```text
Tarefa A
✓ Concluída

Tarefa B
⚠ Bloqueada

Motivo:
Depende da decisão sobre a API utilizada.

[ Campo para instrução ]

"Utilize a API v2."

[ Continuar ]
```

Após a intervenção, a tarefa B deve continuar sem obrigar o usuário a iniciar novamente todo o desenvolvimento.

---

# Voltar etapa + continuar

Esse mecanismo deve ser integrado à funcionalidade de retorno para etapas anteriores.

O usuário pode:

1. Voltar o desenvolvimento para uma etapa anterior;
2. Adicionar detalhes ou uma nova instrução;
3. Executar novamente a partir daquele ponto;
4. Acompanhar a nova execução na timeline.

O histórico anterior não deve ser apagado.

A nova execução deve aparecer como uma nova ramificação ou continuação identificável do histórico.

Isso é especialmente importante para auditoria.

---

# Campo de detalhes da implementação

Deve existir um campo de texto para que o usuário possa adicionar detalhes à implementação.

Esse campo deve permitir complementar o contexto original, esclarecer requisitos ou orientar a execução.

É importante diferenciar claramente:

**Contexto original**
> O que foi solicitado inicialmente.

**Detalhes adicionados pelo usuário**
> Informações acrescentadas posteriormente para orientar ou corrigir a implementação.

**Informações geradas pelo Codex**
> Sugestões, decisões e tarefas produzidas durante a execução.

Isso evita misturar a intenção original do usuário com informações produzidas pela IA.

---

# Geração do resumo por LLM

O resumo gerado pela LLM deve ser persistido no banco junto ao desenvolvimento.

Não deve ser necessário chamar a LLM novamente sempre que o usuário abrir a tela.

Deve existir uma ação para:

**Regenerar resumo**

Isso será útil caso:

- novas tarefas tenham sido executadas;
- o usuário tenha voltado uma etapa;
- novas informações tenham sido adicionadas;
- a execução tenha sido alterada;
- o modelo utilizado para gerar o resumo tenha mudado.

A falha na geração do resumo não pode impedir o desenvolvimento nem alterar os dados originais do Codex.

O sistema deve continuar funcionando mesmo sem o resumo.

---

# Estrutura recomendada para o retorno da LLM

Prefiro que a LLM retorne dados estruturados, e não apenas um texto livre.

Por exemplo:

```json
{
  "summary": "Resumo objetivo do desenvolvimento.",
  "request": "Descrição resumida do que foi solicitado.",
  "implementation": "O que foi efetivamente implementado.",
  "changes": [
    "Alteração importante 1",
    "Alteração importante 2"
  ],
  "result": "completed",
  "issues": [
    "Problema ou limitação relevante"
  ],
  "files": [
    "arquivo relevante"
  ],
  "tasks_summary": "Resumo das tarefas executadas e pendentes"
}
```

Os campos podem ser adaptados à estrutura existente do sistema.

O importante é que o frontend não precise interpretar texto livre para montar a interface.

---

# Objetivo final da UX

A experiência deve seguir aproximadamente esta hierarquia:

## Nível 1 — O que aconteceu?

**Resumo do que foi feito**

> Implementada validação de estoque no processo de pedido, incluindo bloqueio da confirmação quando a quantidade solicitada excede o estoque disponível.

**Resultado:** Concluído

**Principais alterações**
- Validação adicionada antes da confirmação.
- Tratamento de erro ajustado.
- Testes executados com sucesso.

**Tarefas:** 4 concluídas · 1 pendente

**Etapa atual:** Implementação

[ Ver detalhes ]

---

## Nível 2 — Como o trabalho foi realizado?

Ao clicar em "Ver detalhes", mostrar:

- solicitação;
- contexto adicional;
- tarefas;
- sugestões do Codex;
- etapas do desenvolvimento;
- timeline completa;
- arquivos alterados;
- testes;
- resultado;
- possibilidade de adicionar mais detalhes;
- possibilidade de retornar a uma etapa anterior;
- possibilidade de intervir em tarefas bloqueadas.

---

## Nível 3 — O que exatamente aconteceu tecnicamente?

Permitir acessar:

- prompt completo;
- resposta completa;
- tool calls;
- argumentos;
- retornos;
- logs;
- outputs;
- erros;
- histórico completo da execução;
- payloads técnicos.

---

# Diretriz principal

Não quero simplesmente esconder informações existentes.

Quero **reorganizar a informação de acordo com a importância para o usuário**.

A regra deve ser:

**Nível 1 → O que aconteceu?**

**Nível 2 → Como o trabalho foi realizado e qual é o estado atual?**

**Nível 3 → O que exatamente aconteceu tecnicamente?**

O nível 1 deve ser extremamente fácil de compreender e deve ser a experiência padrão.

O nível 2 deve ser utilizado quando o usuário quiser acompanhar ou interferir no desenvolvimento.

O nível 3 deve existir para auditoria, debugging e investigação.

---

# Princípio geral do fluxo

O sistema deve permitir um ciclo contínuo:

**Codex executa → encontra um problema/decisão → bloqueia → explica por quê → usuário fornece instrução → instrução entra na timeline → Codex continua do ponto em que parou.**

Uma tarefa bloqueada não significa necessariamente que o desenvolvimento falhou.

Significa:

> **"O agente não pode continuar sozinho neste momento."**

A interface deve permitir que o usuário forneça a informação necessária e diga explicitamente **como continuar**, após o que o sistema deve retomar a execução no mesmo contexto.

---

# Implementação

Antes de implementar, analise a estrutura atual do projeto, incluindo:

- modelo de dados;
- fluxo de execução do Codex;
- armazenamento da entrada e saída;
- implementação atual da tela;
- sistema de tarefas;
- `tool_calls`;
- etapas do workflow;
- mecanismo existente para voltar etapas;
- forma como intervenções do usuário são armazenadas;
- mecanismos de execução/retomada existentes.

Reaproveite a arquitetura existente sempre que possível e evite alterações desnecessárias.

Primeiro apresente uma proposta de arquitetura e as alterações necessárias. Depois implemente a solução.

Não faça alterações destrutivas nos dados históricos existentes.

A implementação deve manter compatibilidade com os desenvolvimentos já registrados sempre que possível.
