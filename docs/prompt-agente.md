# Tarefa: implementar o agente "Central de Salas" (host MCP + servidor A2A) em Python

## Contexto e premissas
O servidor MCP JÁ ESTÁ PRONTO: um processo separado, em Streamable HTTP, escutando em
`http://localhost:7301/mcp`, expondo as tools `listar_salas`, `consultar_disponibilidade`
e `reservar_sala`, e o resource `politica://uso`. Você NÃO o implementa nem o modifica.

Você implementa o AGENTE, em `agente/`, como um processo separado que:
- por dentro é HOST MCP: fala com o servidor acima por HTTP, como um cliente MCP de
  verdade (é proibido importar a função da tool direto no código do agente);
- por fora é SERVIDOR A2A: publica um Agent Card e atende JSON-RPC 2.0.

Não use LLM em nenhum ponto do caminho de execução: dado o mesmo pedido, o resultado
deve ser sempre o mesmo. Sem ORM, sem banco, sem camada de serviços. As pastas `dados/`,
`validador/` e `exemplos/` são congeladas — não as altere.

Contrato de entrada/saída (fixo, sem linguagem natural):
- Pedido:  `reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>`
- Resposta à pausa: `escolha=<id da sala>` para aceitar, ou `escolha=recusar` para recusar.

Portas/paths (parametrizáveis por variável de ambiente, com estes defaults):
- Agente: porta **7300**, endpoint JSON-RPC em **`/a2a`**, card em
  **`/.well-known/agent-card.json`**.
- Servidor MCP: `http://localhost:7301/mcp`.

## Setup com uv
O projeto já existe na raiz, com `pyproject.toml` e `uv.lock` (Python 3.13+). Trabalhe nele.

1. Sincronize o ambiente do projeto:
   ```
   uv sync
   ```

2. Adicione o SDK oficial do MCP para Python (o cliente usado pelo host). Fixe a versão
   resolvida no `pyproject.toml`/`uv.lock`:
   ```
   uv add mcp
   ```

3. Adicione o servidor HTTP do agente. Se usar ASGI:
   ```
   uv add starlette uvicorn
   ```

4. (Opcional) SDK oficial do A2A: só use se ele produzir exatamente as formas v1.0 exigidas
   neste prompt (ver o Agent Card abaixo). Se divergir, implemente o binding JSON-RPC 2.0
   manualmente — são só dois métodos:
   ```
   uv add a2a-sdk
   ```

5. Rode o agente SEMPRE pelo ambiente do projeto:
   ```
   uv run python -m agente.main
   ```
   (ou `uv run agente/main.py`, conforme a estrutura que você criar).

Não adicione nenhuma dependência de SDK de provedor de LLM.

## Papel 1 — o agente como HOST MCP
- Descubra as tools por `tools/list` ANTES da primeira chamada. NÃO carregue uma lista
  fixa de ferramentas no código.
- Leia o resource `politica://uso` e extraia dele a versão declarada na PRIMEIRA LINHA
  (o valor aparece como `versao: <valor>`). Essa versão vai no artifact da Task.
- Todo request MCP emitido pelo agente deve carregar:
  - `_meta` com `io.modelcontextprotocol/protocolVersion` e
    `io.modelcontextprotocol/clientCapabilities`;
  - a capability de elicitation EM FORM MODE, na forma `{"elicitation": {"form": {}}}`;
  - os headers espelhados do corpo: `MCP-Protocol-Version`, `Mcp-Method` e, em
    `tools/call` e `resources/read`, `Mcp-Name`. Header que não bate com o corpo é
    recusado pelo servidor com `-32020`.
- Propague o trace: quando o cliente A2A enviar o header `traceparent`, use o MESMO
  trace-id no `traceparent` dentro do `_meta` de TODOS os requests MCP daquela Task. O
  span-id pode ser novo; o trace-id não.
- Se a tool devolver erro de execução (`isError: true`), termine a Task em
  `TASK_STATE_FAILED` com a MENSAGEM EXATA da tool visível no histórico da Task.
- NÃO implemente regra de negócio de sala: conflito, política e alternativas são decisão
  do servidor MCP. O agente traduz protocolo, não domínio.

IMPORTANTE (armadilha): não registre um callback de elicitation no cliente MCP. Se você
fizer isso, o cliente responde à pergunta sozinho, o ciclo fecha dentro do agente e a Task
nunca pausa. O agente precisa enxergar o `input_required` CRU devolvido pelo servidor (no
SDK Python, procure por `input_required`/MRTR no resultado do `tools/call`), com
`inputRequests` e `requestState` intactos.

## Papel 2 — o agente como SERVIDOR A2A (v1.0, JSON-RPC 2.0 sobre HTTP)

### Agent Card
`GET /.well-known/agent-card.json` responde 200 com um card v1.0 contendo:
- identidade: `name`, `description`, `provider`, `version`;
- `supportedInterfaces`: lista com um item que tem `url` do endpoint A2A,
  `protocolBinding: "JSONRPC"` e `protocolVersion: "1.0"` (a v1.0 usa esse campo dentro do
  item; não use `preferredTransport`, que é de versão anterior);
- `capabilities`: `streaming: false`, `pushNotifications: false`,
  `extendedAgentCard: false`;
- `defaultInputModes` e `defaultOutputModes`: `["text/plain"]`;
- `skills`: uma skill com `id` igual a `reservar-sala`, com `name`, `description`, `tags`,
  `inputModes`/`outputModes` e um `examples` no formato fixo do pedido.

### Endpoint `/a2a`
Aceita os métodos `SendMessage` e `GetTask` no binding JSON-RPC 2.0. Sem streaming/SSE.
- `SendMessage` SEM `taskId` abre uma Task nova, com `id` e `contextId` próprios.
- A Task passa por `TASK_STATE_SUBMITTED` e `TASK_STATE_WORKING` antes de terminar, e
  `GetTask` reflete o estado corrente a qualquer momento.
- Sucesso produz um artifact de `name: "reserva"`, cujo conteúdo é o JSON da reserva criada
  INCLUINDO o campo `politica` com a versão lida do resource.
- Estado terminal é definitivo: `COMPLETED`, `CANCELED` ou `FAILED` não volta a
  `WORKING`. `SendMessage` referenciando uma Task terminal é RECUSADO com erro.

Formas de mensagem/Task:
- mensagem: `{ "messageId", "role": "ROLE_USER" | "ROLE_AGENT", "parts": [{ "text" }],
  "taskId"?, "contextId"? }`;
- Task: `{ "id", "contextId", "status": { "state", "message" }, "history": [...],
  "artifacts": [...] }`;
- resposta: `result.task`; `GetTask` recebe `params: { "id" }`;
- artifact: `{ "artifactId", "name": "reserva", "parts": [{ "text": "<json>" }] }`.

## A PONTE — o requisito central
Quando o `tools/call` de `reservar_sala` devolver `resultType == "input_required"` (conflito
com elicitation e `requestState`):

1. Coloque a Task em `TASK_STATE_INPUT_REQUIRED`.
2. Devolva, como mensagem de texto da Task, EXATAMENTE a linha
   `alternativas: <ids separados por virgula e espaco>`, na MESMA ordem em que vieram no
   `enum` da elicitation. Sem prefixo, sem saudação, sem pontuação extra — duas pausas
   iguais precisam ser idênticas byte a byte.
3. Guarde o `requestState` recebido associado àquela Task. Ele é OPACO: guarde e ecoe,
   nunca abra, interprete nem reconstrua, mesmo que o conteúdo seja legível. Ele NÃO pode
   aparecer no card, no artifact nem em nenhuma mensagem devolvida ao cliente A2A.
4. No `SendMessage` de continuação, que traz `taskId` e o texto `escolha=<valor>`:
   - repita o `tools/call` original com um **id de JSON-RPC DIFERENTE** do request inicial;
   - leve `inputResponses` com a MESMA chave que veio em `inputRequests`, e o
     `requestState` ecoado sem modificação;
   - `escolha=<id>` vira `action: "accept"` com `content: { "sala": <id> }`; conclusão
     normal leva a Task a `TASK_STATE_COMPLETED` com o artifact `reserva`;
   - `escolha=recusar` vira `action: "decline"` e a Task termina em
     `TASK_STATE_CANCELED`;
   - `escolha` fora do enum (ou qualquer valor inesperado) MANTÉM a Task em
     `TASK_STATE_INPUT_REQUIRED` e repete a lista de alternativas.
5. O estado pausado é POR TASK: dois pedidos em conflito, pausados ao mesmo tempo, concluem
   cada um com a sua própria reserva, sem trocar de `requestState`.

O agente NÃO decide alternativas nem valida política: se o servidor MCP responder
`input_required`, o agente apenas pausa e repassa; se responder erro de execução, o agente
falha a Task; se responder sucesso, o agente conclui.

## Observabilidade
- Registre no `stderr` do agente os requests A2A recebidos (método, id, `traceparent`
  quando vier) e os requests MCP emitidos, para depuração.
- O log do servidor MCP deve mostrar, na ordem: o `tools/list` de descoberta do agente e,
  depois, os `tools/call`; e o `traceparent` com o mesmo trace-id que o cliente A2A enviou.
  Um par inicial/retry do mesmo `tools/call` precisa ter ids de JSON-RPC diferentes.

## Restrições não negociáveis
- Dois processos separados; o agente fala com o servidor por HTTP.
- Nada de sessão/estado implícito entre requests: não infira versão, capabilities ou
  contexto de request anterior nem de conexão aberta. Manter o objeto cliente vivo entre
  chamadas é permitido (estado de processo ≠ estado de protocolo).
- Sem LLM no caminho de execução.
- Fora de escopo: autenticação, streaming/SSE/push, refinamento de Task/`referenceTaskIds`,
  card autenticado/JWS, prompts, subscriptions, containers, e telemetria além da
  propagação do `traceparent` e do log.

## Como verificar
1. Suba o servidor MCP em um terminal (deixe o `stderr` visível) e o agente em outro:
   ```
   # terminal 1
   export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
   uv run python -m servidor_mcp.main      # conforme o comando de subida do servidor

   # terminal 2
   uv run python -m agente.main
   ```
2. Confira o card:
   ```
   curl -sS http://localhost:7300/.well-known/agent-card.json
   ```
3. Ciclo manual com curl, usando os corpos de `exemplos/wire/08` a `10`:
   - `SendMessage` de uma sala ocupada → Task em `TASK_STATE_INPUT_REQUIRED` com a linha
     de alternativas;
   - `GetTask` da Task pausada;
   - `SendMessage` de continuação com `escolha=<id>` → `TASK_STATE_COMPLETED` com o
     artifact `reserva`;
   - repetir o pedido e responder `escolha=recusar` → `TASK_STATE_CANCELED`;
   - `SendMessage` de sala inexistente → `TASK_STATE_FAILED` com a mensagem exata no
     histórico.
4. Rode o validador de conformidade com os dois processos recém-iniciados e confira as
   verificações que dependem do agente:
   ```
   python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
   ```
   Reinicie ambos antes de cada execução: a validação cria reservas e muda o estado.

## Entregável
- `agente/` com a implementação (host MCP + servidor A2A + a ponte) e o comando de subida
  documentado.
- Propagação do `traceparent` visível no `stderr` do servidor MCP.
- Trecho de README explicando como subir o agente, onde o `input_required` do MCP vira
  `TASK_STATE_INPUT_REQUIRED` e onde o `requestState` volta ao servidor.
