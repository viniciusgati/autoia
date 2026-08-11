# Blueprint — Workspace de Desenvolvimento Orientado por Etapas

## Objetivo

Redesenhar completamente a tela de acompanhamento de uma tarefa de desenvolvimento executada por agentes de IA.

**IMPORTANTE: ignore completamente a UX atual da tela.**

Não quero que a implementação simplesmente reorganize, esconda ou adicione componentes à tela existente.

Quero que a experiência seja pensada **do zero** com base neste documento.

A tela não deve ser tratada como:

- tela de consulta;
- relatório;
- histórico de logs;
- dashboard administrativo;
- viewer de execução.

Ela deve ser tratada como uma **tela de trabalho (workspace)**.

O usuário entra nela para trabalhar junto com o agente durante a execução da tarefa:

- acompanhar o andamento;
- entender cada etapa;
- receber resultados;
- aceitar ou recusar tarefas propostas;
- fornecer instruções;
- tomar decisões;
- destravar etapas;
- pausar a execução;
- continuar a execução;
- voltar para uma etapa anterior;
- executar novamente uma etapa;
- acompanhar o histórico de tudo que aconteceu.

---

# Conceito principal

A entidade principal da tela é a **Tarefa**.

Porém, o conteúdo principal da tela é uma **timeline cronológica de etapas**.

Cada etapa é uma unidade completa de trabalho.

Exemplo:

```text
Tarefa
│
├── PO
├── QA
├── DEV
├── TESTE
└── DEV
```

Uma etapa pode aparecer novamente.

Isso acontece quando o usuário retorna para uma etapa anterior e inicia uma nova execução a partir dela.

**Nunca apagar o histórico anterior.**

Exemplo:

```text
PO
QA
DEV
TESTE → falhou

DEV → nova execução
TESTE → nova execução
```

A timeline deve preservar exatamente essa história.

---

# Estrutura geral da tela

A tela deve possuir três regiões principais:

```text
┌───────────────────────────────────────────────────────────────┐
│ HEADER FIXO                                                   │
│ Informações da tarefa + status + controles + custo            │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│                                                               │
│               TIMELINE DE ETAPAS                              │
│                                                               │
│               conteúdo principal                              │
│                                                               │
│                                                               │
├───────────────────────────────────────────────────────────────┤
│ CHAT / INSTRUÇÃO FIXO                                         │
│ campo para conversar com o agente e controlar a execução     │
└───────────────────────────────────────────────────────────────┘
```

O header deve permanecer fixo.

O campo de interação inferior também deve permanecer fixo.

Somente o conteúdo da timeline deve possuir rolagem.

---

# Header

O topo deve apresentar informações gerais da tarefa.

Exemplo:

```text
← Implementar novo fluxo de pedidos

● Em execução

Custo total: R$ 4,82

[ Pausar ] [ Resumir ]
```

O status deve ser extremamente evidente.

Estados possíveis:

- Não iniciada;
- Em execução;
- Pausada;
- Aguardando decisão;
- Bloqueada;
- Erro;
- Concluída.

Os controles devem mudar conforme o estado.

### Tarefa não iniciada

```text
[ Iniciar ]
[ Resumir ]
```

### Tarefa em execução

```text
[ Pausar ]
[ Resumir ]
```

### Tarefa pausada

```text
[ Continuar ]
[ Resumir ]
```

### Tarefa bloqueada

```text
[ Continuar ]
[ Resumir ]
```

### Tarefa concluída

Permitir continuar/reexecutar conforme as regras existentes.

O custo total da tarefa deve ficar visível no header.

---

# Botão Resumir

O botão **Resumir** deve utilizar uma chamada de API para uma LLM.

A LLM deve analisar o estado atual da tarefa e produzir um resumo.

Esse resumo não deve ser gerado pelo Codex, Claude Code, Kimi, OpenCode ou outro agente executor.

A responsabilidade da LLM de resumo é exclusivamente:

> **Interpretar o trabalho realizado e explicar o resultado de maneira objetiva para o usuário.**

O resumo pode considerar:

- solicitação;
- detalhes fornecidos pelo usuário;
- etapas;
- entradas;
- saídas;
- tarefas;
- tool calls relevantes;
- arquivos alterados;
- diffs;
- testes;
- erros;
- intervenções do usuário.

O resumo deve ser persistido.

Não chamar a LLM toda vez que a tela for aberta.

---

# Timeline

A timeline é o coração da tela.

As etapas devem aparecer:

**da mais antiga para a mais nova.**

Cada ocorrência de etapa representa uma execução daquela etapa.

Exemplo:

```text
ETAPA: PO

...

ETAPA: QA

...

ETAPA: DEV

...

ETAPA: TESTE

❌ Falhou

...

ETAPA: DEV

Nova execução

...
```

Não agrupar etapas repetidas.

Não substituir uma execução anterior.

Cada nova execução deve aparecer como uma nova ocorrência cronológica.

---

# Estrutura de uma etapa

Cada etapa deve possuir uma estrutura consistente.

Exemplo:

```text
ETAPA: DEV
────────────────────────────────────────

O que será feito

Implementar a validação de estoque no fluxo
de confirmação do pedido.


O que foi entregue

A validação foi adicionada antes da confirmação
do pedido. Foram alterados 3 arquivos e foram
adicionados testes para estoque insuficiente.


Tarefas propostas

...


Arquivos alterados

...


Atividade do sistema

...
```

---

# "O que será feito"

Toda etapa deve apresentar uma explicação clara do objetivo daquela etapa.

Essa informação deve responder:

> **O que o agente precisa realizar nesta etapa?**

Não mostrar necessariamente o prompt bruto.

Deve existir uma versão resumida e compreensível.

Preferencialmente essa informação deve ser derivada de forma estruturada da entrada da etapa ou resumida pela LLM quando necessário.

---

# "O que foi entregue"

Quando uma etapa for concluída, deve existir uma seção:

## O que foi entregue

Esse conteúdo deve ser **gerado por uma LLM dedicada ao resumo**.

Não utilizar o texto do Codex diretamente como resumo.

Não utilizar Claude/Kimi/OpenCode como gerador desse resumo.

A LLM deve receber o contexto da etapa e produzir uma explicação curta e objetiva do resultado real.

O resumo deve responder:

- O que foi feito?
- Qual foi o resultado?
- Quais foram as mudanças importantes?
- Houve alguma limitação?
- Quais arquivos relevantes foram alterados?
- Houve testes?

Evitar frases genéricas.

Exemplo ruim:

> Desenvolvimento realizado com sucesso.

Exemplo bom:

> A validação de estoque foi adicionada antes da confirmação do pedido. O fluxo de reserva existente foi preservado e foram adicionados testes para os cenários de estoque insuficiente.

---

# Etapa em andamento

Enquanto a etapa estiver executando, ela não deve mostrar imediatamente "O que foi entregue".

Deve mostrar o estado atual da execução.

Exemplo:

```text
ETAPA: DEV
────────────────────────────────────────

O que será feito

Implementar a validação de estoque...


Em andamento

● Analisando OrderService.ts...
```

Essa informação deve ser **atualizada automaticamente em tempo real**, conforme a execução avança.

Exemplo:

```text
● Analisando OrderService.ts...
```

depois:

```text
● Alterando OrderService.ts...
```

depois:

```text
● Executando testes...
```

depois:

```text
● Analisando resultado dos testes...
```

A interface deve sempre mostrar o **último comando/atividade relevante em andamento**.

Isso não deve depender da LLM.

Deve vir do estado real da execução.

Quando a etapa terminar, essa área deixa de ser o foco e o sistema gera:

**O que foi entregue**

através da LLM de resumo.

---

# Etapas com falha ou bloqueio

Esse é um requisito extremamente importante.

Quando uma etapa estiver parada, o motivo deve ficar **o mais próximo possível da área de interação do usuário**.

O usuário nunca deve precisar percorrer a timeline procurando o motivo pelo qual a execução parou.

Exemplo:

```text
ETAPA: TESTE
────────────────────────────────────────

O que será feito

Executar os testes da funcionalidade.


❌ ETAPA PARADA

Os testes foram executados, mas 3 testes falharam.


Motivo da parada

A implementação precisa ser corrigida antes
que a execução possa continuar.


Testes com falha:

• deve bloquear pedido sem estoque
• deve liberar pedido com estoque
• deve manter reserva existente
```

Se for uma decisão:

```text
🟠 ETAPA PARADA — DECISÃO NECESSÁRIA

Existem duas abordagens possíveis para implementar
essa validação:

A — validar no OrderService
B — validar no StockService

O agente não deve continuar sem uma decisão.
```

A informação deve ser clara e imediatamente visível.

Não utilizar apenas:

> Status: Bloqueado

É necessário explicar **por que está bloqueado**.

---

# Campo de interação fixo

Na parte inferior da tela deve existir permanentemente um campo de texto para o usuário enviar instruções ao agente.

Esse campo é parte fundamental do workspace.

Ele não deve parecer um simples campo de comentário.

É o canal de trabalho entre usuário e agente.

Exemplo:

```text
┌───────────────────────────────────────────────────────────────┐
│ Escreva uma instrução para o agente...                       │
│                                                               │
│                                                               │
└───────────────────────────────────────────────────────────────┘

Continuar a partir de: [ Etapa atual ▼ ]

                                           [ Enviar ]
```

O usuário deve poder:

- fornecer novas informações;
- corrigir uma decisão;
- alterar uma abordagem;
- pedir uma alteração;
- destravar uma etapa;
- solicitar uma nova execução;
- voltar para uma etapa anterior.

---

# Retomar a partir de uma etapa

O usuário deve poder escolher a etapa a partir da qual deseja continuar.

Exemplo:

```text
Continuar a partir de:

[ DEV ▼ ]
```

Isso deve permitir:

```text
PO
QA
DEV
TESTE
```

O usuário pode selecionar uma etapa anterior e fornecer uma nova instrução.

Exemplo:

```text
Continuar a partir de:
[ DEV ]

Instrução:

"Não utilize a abordagem atual.
Faça a validação no StockService."

[ Continuar execução ]
```

Isso deve criar uma **nova execução da etapa selecionada**.

O histórico anterior deve permanecer intacto.

---

# Tarefas propostas pelo agente

Durante uma etapa, o agente pode utilizar `tool_call` para sugerir tarefas.

Essas tarefas devem aparecer diretamente dentro da etapa correspondente.

Exemplo:

```text
Tarefas propostas

┌──────────────────────────────────────────────────────────────┐
│ Tarefa proposta                                               │
│                                                               │
│ Criar testes para pedidos sem estoque                        │
│                                                               │
│ [ Aceitar ]                              [ Recusar ]          │
└──────────────────────────────────────────────────────────────┘
```

Outra:

```text
┌──────────────────────────────────────────────────────────────┐
│ Tarefa proposta                                               │
│                                                               │
│ Atualizar documentação da API                                 │
│                                                               │
│ [ Aceitar ]                              [ Recusar ]          │
└──────────────────────────────────────────────────────────────┘
```

É importante diferenciar:

> **Tarefa proposta ≠ tarefa executada**

Quando o usuário aceitar:

```text
✓ Tarefa aceita

Criar testes para pedidos sem estoque
```

Quando recusar:

```text
✕ Tarefa recusada

Atualizar documentação da API
```

Essa decisão deve fazer parte do histórico da etapa.

---

# Tool calls

Existem dois tipos de tool calls e eles devem ser tratados de maneira diferente.

## Tool calls que geram interação

São chamadas que produzem algo que o usuário precisa visualizar ou decidir.

Exemplos:

- sugerir tarefa;
- solicitar aprovação;
- solicitar decisão;
- solicitar informação;
- propor ação.

Essas devem aparecer como elementos de trabalho dentro da etapa.

Exemplo:

```text
Tarefa proposta: Criar testes

[ Aceitar ] [ Recusar ]
```

---

## Tool calls internas do sistema

São chamadas realizadas pelo agente ao próprio sistema/workflow.

Exemplos:

- atualizar status;
- criar etapa;
- criar tarefa;
- registrar informação;
- alterar estado;
- registrar evento.

Essas podem aparecer como atividades discretas dentro da etapa.

Exemplo:

```text
Atividade do sistema

🔧 Criou tarefa "Criar testes"
🔧 Atualizou status da etapa
🔧 Registrou nova informação
```

Ao expandir, permitir visualizar os dados técnicos da chamada.

---

# Não mostrar as iterações internas dos agentes

Não quero que a interface normal mostre:

- Codex → Claude;
- Claude → Kimi;
- Kimi → OpenCode;
- thinking interno;
- mensagens internas;
- prompts internos;
- respostas intermediárias;
- iterações internas dos agentes.

Essas informações podem continuar existindo tecnicamente para auditoria, mas não devem fazer parte da experiência principal.

O usuário quer saber:

> **O que foi feito.**

Não:

> **Como os agentes internos conversaram entre si.**

---

# Arquivos alterados

Dentro da etapa, quando houver alterações, mostrar os arquivos relevantes.

Mostrar inicialmente no máximo 10.

Exemplo:

```text
Arquivos alterados

OrderService.ts
StockService.ts
OrderService.test.ts
InventoryRepository.ts

[ Ver todos os 27 arquivos ]
```

Ao expandir, mostrar todos.

Também deve existir acesso ao diff real.

---

# Diff

O diff real deve vir da fonte de verdade existente no sistema, como Git ou mecanismo equivalente.

A LLM pode explicar o diff no resumo, mas nunca deve ser considerada a fonte de verdade da alteração.

Deve ser possível abrir:

**Ver diff**

e visualizar exatamente o que foi alterado.

---

# Resultado de testes

Quando uma etapa envolver testes, apresentar claramente o resultado.

Exemplo:

```text
TESTE

✓ 42 testes passaram
✕ 3 testes falharam

Resultado: Falhou
```

Se a etapa falhar, o motivo deve aparecer próximo da indicação de falha.

---

# Histórico e reexecução

O histórico é imutável.

Nunca apagar uma etapa anterior porque ela foi reexecutada.

Exemplo:

```text
PO
QA
DEV
TESTE
  ❌ Falhou

DEV
  ↻ Nova execução
  ✓ Concluído

TESTE
  ↻ Nova execução
  ✓ Concluído
```

A interface deve deixar claro quando uma etapa é uma nova execução.

---

# Resumo geral da tarefa

Além dos resumos individuais das etapas, pode existir um resumo geral da tarefa.

Esse resumo deve ser gerado pela LLM dedicada a resumo e pode ser atualizado através do botão:

**Resumir**

Ele deve considerar todo o histórico relevante da tarefa.

A estrutura pode ser algo como:

```json
{
  "summary": "Resumo geral da tarefa",
  "current_state": "Implementação em andamento",
  "completed": [],
  "pending": [],
  "issues": [],
  "next_action": "..."
}
```

A estrutura exata pode ser adaptada à arquitetura existente.

---

# Custo

O custo total da tarefa deve ficar sempre visível no header.

Exemplo:

```text
Custo total: R$ 4,82
```

O sistema pode possuir detalhamento técnico do custo em outro nível, mas a experiência principal deve mostrar somente o total.

---

# Princípios de UX

A tela deve responder imediatamente às seguintes perguntas:

### 1. Onde estou?

Qual é o status da tarefa e qual etapa está sendo executada.

### 2. O que está acontecendo?

Mostrar a atividade atual da etapa em tempo real.

### 3. O que já aconteceu?

Mostrar as etapas anteriores em ordem cronológica.

### 4. O que foi entregue?

Mostrar o resumo gerado pela LLM.

### 5. Preciso fazer alguma coisa?

Se houver bloqueio, decisão ou tarefa proposta, isso deve ficar extremamente evidente.

### 6. Como continuo?

O campo inferior deve permitir fornecer instruções e escolher a etapa de retomada.

---

# Regra de ouro

A experiência deve seguir esta hierarquia:

```text
TAREFA
   │
   ├── ETAPA
   │    ├── O que será feito
   │    ├── Em andamento / última atividade
   │    ├── O que foi entregue
   │    ├── Tarefas propostas
   │    ├── Tool calls do sistema
   │    ├── Arquivos alterados
   │    ├── Diff
   │    └── Bloqueios / decisões
   │
   ├── ETAPA
   │    └── ...
   │
   └── ETAPA
        └── ...
```

A tela deve ser simples na primeira leitura, mas permitir profundidade quando o usuário quiser investigar.

**Resumo → etapa → detalhes técnicos.**

---

# Diretriz final de implementação

Antes de implementar:

1. Analise a arquitetura atual;
2. Entenda como as tarefas são armazenadas;
3. Entenda como as etapas são armazenadas;
4. Entenda como o Codex é executado;
5. Identifique como `tool_call` é tratada atualmente;
6. Identifique como os arquivos/diffs são obtidos;
7. Identifique como custos são registrados;
8. Identifique como é possível pausar/continuar a execução;
9. Identifique como retornar para uma etapa anterior;
10. Identifique como as informações podem ser persistidas para formar a timeline.

**Não altere a UX atual incrementalmente.**

Primeiro pense na nova experiência como um workspace completo e apresente uma proposta de arquitetura para suportá-la.

Depois implemente.

Reaproveite APIs, modelos e mecanismos existentes quando fizer sentido, mas **não deixe a implementação atual da tela limitar o novo design**.

O resultado final deve parecer uma ferramenta de trabalho para desenvolvimento assistido por agentes, e não uma tela de logs.