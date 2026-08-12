# Redesign do Card de Etapa — Missão da Execução e Resultado Humano

## Objetivo

Redesenhar o conteúdo visual de cada card de etapa da tarefa.

**Ignore a estrutura e os textos atuais dos cards como referência de UX.**

O problema atual é que, quando uma etapa é executada novamente, a interface continua apresentando essencialmente o objetivo original da etapa.

Isso está errado do ponto de vista de experiência do usuário.

Uma nova execução de uma etapa precisa apresentar claramente **qual é a missão específica daquela execução** e, depois, **o que foi resolvido/entregue nessa execução**.

---

# Problema atual

Imagine este fluxo:

```text
Tarefa X

DEV
→ implementou a funcionalidade

TESTER
→ testou

AVALIADOR
→ avaliou e encontrou um problema:
  "Está faltando feedback visual para o usuário."

DEV
→ nova execução
```

Hoje, ao voltar para DEV, o card continua mostrando algo equivalente a:

> "Exclusão de projeto — Você é o robô DESENVOLVEDOR de um pipeline automatizado..."

Isso não é útil.

Eu já sei que estou na etapa DEV.

Também não preciso saber que o agente é um "robô desenvolvedor".

Muito menos preciso ver instruções internas ou descrições técnicas do agente.

O que eu preciso saber é:

> **Por que o DEV está sendo executado novamente?**

E, neste caso:

> **Ele precisa analisar a devolutiva do avaliador e corrigir o problema de feedback visual apontado.**

Essa é a informação principal do card.

---

# Conceito: cada execução de etapa possui uma MISSÃO

Uma etapa não deve ser entendida apenas como:

```text
DEV
```

Ela deve ser entendida como:

```text
DEV — execução 1
Missão: implementar a funcionalidade solicitada.


DEV — execução 2
Missão: analisar o feedback do avaliador e corrigir
o problema de feedback visual apontado.
```

Portanto:

> **Cada ocorrência de uma etapa na timeline deve possuir sua própria missão/contexto de execução.**

Uma nova execução da mesma etapa não deve simplesmente reutilizar visualmente a descrição da execução anterior.

---

# O card deve responder duas perguntas

A interface precisa responder de forma extremamente clara:

## 1. O que esta execução precisa resolver?

Essa é a **missão da etapa**.

## 2. O que esta execução resolveu?

Esse é o **resultado da etapa**.

Todo o restante é secundário.

---

# Nova estrutura do card

O card deve ser visualmente orientado para isso:

```text
┌──────────────────────────────────────────────────────────────┐
│ FASE 3 · DEVELOPER                         EM ANDAMENTO      │
│ Nova execução · tentativa 2                                  │
│                                                              │
│ MISSÃO                                                        │
│                                                              │
│ Analisar a devolutiva do avaliador e corrigir o problema      │
│ de feedback visual identificado na avaliação anterior.        │
│                                                              │
│                                                              │
│ EM ANDAMENTO                                                  │
│                                                              │
│ Analisando a devolutiva do avaliador...                       │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

Quando terminar:

```text
┌──────────────────────────────────────────────────────────────┐
│ FASE 3 · DEVELOPER                         CONCLUÍDO         │
│ Nova execução · tentativa 2                                  │
│                                                              │
│ MISSÃO                                                        │
│                                                              │
│ Analisar a devolutiva do avaliador e corrigir o problema      │
│ de feedback visual identificado na avaliação anterior.        │
│                                                              │
│ O QUE FOI RESOLVIDO                                           │
│                                                              │
│ O feedback visual solicitado pelo avaliador foi implementado │
│ e a interação agora informa claramente ao usuário o resultado │
│ da exclusão.                                                  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

# A missão deve ser humana

A missão deve ser escrita para uma pessoa que está acompanhando o trabalho.

Não deve ser um prompt técnico.

Não deve ser o prompt enviado ao Codex.

Não deve conter instruções internas do agente.

Não deve conter:

- "Você é um robô...";
- "Você é o desenvolvedor de um pipeline...";
- regras internas do agente;
- instruções de sistema;
- comandos;
- paths;
- comandos bash;
- detalhes de implementação;
- nomes de ferramentas;
- informações de infraestrutura.

O usuário deve enxergar **o objetivo do trabalho**, não o prompt usado para executar o agente.

---

# Exemplo correto

Em vez de:

> Exclusão de projeto — Você é o robô DESENVOLVEDOR de um pipeline automatizado...

Mostrar:

> **Analisar a devolutiva do avaliador e corrigir o problema de feedback visual identificado na exclusão do projeto.**

Muito mais importante:

> **A missão precisa refletir o motivo pelo qual esta execução existe.**

---

# De onde vem a missão?

A missão deve ser construída a partir do contexto que originou aquela execução.

Uma nova execução pode ser causada por:

- primeira execução da etapa;
- retorno de uma etapa posterior;
- feedback de um avaliador;
- falha de testes;
- decisão do usuário;
- instrução manual do usuário;
- tarefa proposta anteriormente;
- dependência que foi resolvida;
- correção de um problema específico.

O sistema deve preservar esse contexto.

---

# Exemplo completo

Imagine:

```text
DEV
```

Primeira execução:

```text
MISSÃO

Implementar a funcionalidade de exclusão de projetos
conforme os requisitos da tarefa.
```

Resultado:

```text
O QUE FOI RESOLVIDO

A exclusão de projetos foi implementada e o fluxo
principal foi concluído.
```

Depois:

```text
TESTER

✓ Testes executados
✓ Fluxo principal funcionando
```

Depois:

```text
AVALIADOR

Problema encontrado:

A exclusão funciona, mas não existe feedback visual
suficiente para informar o usuário sobre o resultado
da operação.
```

Então ocorre:

```text
DEV — nova execução
```

O card dessa nova execução deve mostrar:

```text
MISSÃO

Analisar a devolutiva do avaliador e corrigir o problema
de feedback visual identificado na exclusão do projeto.
```

E não:

```text
MISSÃO

Implementar exclusão de projetos.
```

---

# O resultado também precisa ser específico da execução

Ao finalizar essa nova execução, não mostrar simplesmente:

> "Desenvolvimento concluído."

Mostrar algo como:

> **O feedback visual apontado pelo avaliador foi implementado. A exclusão agora informa claramente ao usuário o resultado da operação.**

O resultado deve responder:

> **O que mudou por causa desta execução?**

---

# A LLM de resumo

A aplicação deve utilizar uma LLM dedicada para gerar os textos humanos exibidos no card.

Essa LLM deve receber como contexto:

- objetivo original da tarefa;
- etapa atual;
- histórico das etapas anteriores relevantes;
- motivo que originou a nova execução;
- feedback do avaliador, quando existir;
- instrução fornecida pelo usuário, quando existir;
- entrada enviada ao agente;
- saída produzida pelo agente;
- resultado de testes;
- arquivos alterados;
- eventos relevantes.

A LLM deve produzir pelo menos:

```text
mission
result
```

### mission

Explica:

> **Por que esta execução está acontecendo e o que ela precisa resolver?**

### result

Explica:

> **O que esta execução efetivamente resolveu ou entregou?**

---

# Importante: a missão não deve ser simplesmente resumir o prompt

Não fazer:

```text
prompt original
    ↓
LLM
    ↓
resumo do prompt
```

A missão precisa considerar o **contexto da execução**.

Especialmente em reexecuções.

Exemplo:

```text
DEV original
        ↓
TESTER
        ↓
AVALIADOR
        ↓
feedback
        ↓
usuário escolheu voltar para DEV
        ↓
nova instrução
        ↓
nova execução DEV
```

A missão da nova execução deve incorporar esse contexto.

---

# Instrução manual do usuário

Quando o usuário voltar para uma etapa e escrever:

> "Analise a devolutiva do avaliador e corrija."

essa instrução não deve simplesmente aparecer como um comentário isolado.

Ela deve fazer parte do contexto que define a missão da nova execução.

O card deve transformar isso em uma missão compreensível:

> **Analisar a devolutiva do avaliador e corrigir o problema de feedback visual identificado.**

---

# Reexecução precisa ser visualmente diferente da primeira execução

A interface deve deixar claro que se trata de uma nova tentativa.

Exemplo:

```text
FASE 3 · DEVELOPER

↻ Nova execução · tentativa 2
```

Porém, a informação mais importante não é "tentativa 2".

É:

```text
MISSÃO

Analisar a devolutiva do avaliador e corrigir
o problema de feedback visual.
```

A tentativa é contexto secundário.

---

# Hierarquia visual

A prioridade do conteúdo deve ser:

```text
1. Estado da etapa
2. Missão desta execução
3. Resultado / o que foi resolvido
4. Motivo de parada, se houver
5. Tarefas propostas / decisões
6. Informações complementares
7. Detalhes técnicos
```

Os detalhes técnicos **não devem competir visualmente com a missão**.

---

# Informações técnicas

Não remover os detalhes técnicos existentes.

Eles continuam sendo importantes para investigação e auditoria.

Porém, devem ficar em uma camada secundária, por exemplo:

```text
▸ Detalhes técnicos (34 eventos)
```

ou:

```text
▸ Atividade do sistema
```

Ao expandir, o usuário pode acessar:

- comandos;
- tool calls;
- logs;
- arquivos;
- diff;
- eventos;
- dados técnicos.

Mas isso não deve ocupar o espaço principal do card.

---

# Etapa em andamento

Quando estiver executando, o card deve priorizar:

```text
MISSÃO

Analisar a devolutiva do avaliador e corrigir
o problema de feedback visual.


EM ANDAMENTO

● Analisando a devolutiva...
```

A atividade atual deve ser atualizada automaticamente conforme o agente progride.

Não mostrar comandos técnicos como informação principal.

Em vez de:

```text
bash: cd /home/... && python...
```

preferir:

> **Executando os testes da alteração...**

ou:

> **Verificando o comportamento do feedback visual...**

A informação técnica continua disponível nos detalhes.

---

# Etapa parada

Se a etapa parar, o motivo deve aparecer imediatamente depois da missão/atividade.

Exemplo:

```text
FASE 3 · DEVELOPER

🟠 AGUARDANDO DECISÃO


MISSÃO

Corrigir o problema de feedback visual apontado
pelo avaliador.


MOTIVO DA PARADA

A implementação encontrou duas abordagens possíveis
e precisa de uma decisão antes de continuar.


[ Continuar com instrução... ]
```

O usuário deve entender imediatamente:

**"O que ele deveria fazer?"**

e:

**"Por que ele parou?"**

---

# Etapa concluída

Quando concluída:

```text
FASE 3 · DEVELOPER

✓ CONCLUÍDO
↻ Nova execução · tentativa 2


MISSÃO

Analisar a devolutiva do avaliador e corrigir
o problema de feedback visual.


O QUE FOI RESOLVIDO

O feedback visual solicitado pelo avaliador foi
implementado e o resultado da exclusão agora é
informado claramente ao usuário.
```

Esse é o conteúdo principal do card.

---

# O que NÃO fazer

Não transformar o card em um dump de informações.

Evitar como conteúdo principal:

```text
Você é um robô DESENVOLVEDOR...
pipeline automatizado...
bash...
python...
npm...
tool_call...
34 eventos...
```

Evitar também repetir o objetivo original da tarefa em todas as reexecuções.

Não fazer a segunda execução do DEV parecer idêntica à primeira.

Não mostrar a saída bruta do Codex como se fosse uma explicação para o usuário.

Não utilizar linguagem excessivamente técnica quando uma explicação humana for possível.

---

# Critério de sucesso

Ao olhar rapidamente para uma etapa, o usuário deve conseguir responder em poucos segundos:

> **Qual é a missão desta execução?**

> **Por que ela está acontecendo agora?**

> **O que ela resolveu?**

> **Se estiver parada, por que parou?**

Todo o restante deve ser secundário.

---

# Resultado esperado

Transformar cada card de etapa em uma representação **humana, contextual e orientada à missão daquela execução**.

O card não deve contar simplesmente:

> "O que o agente recebeu."

Ele deve contar:

> **"Por que esta etapa está sendo executada agora, o que ela precisa resolver e qual foi o resultado."**

Essa deve ser a principal informação visual da timeline.