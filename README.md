# A2A com MCP — Central de Salas

Agente A2A determinístico, sem LLM, que coordena reservas por um servidor MCP em outro processo. As regras de negócio ficam no servidor MCP.

## Como rodar

Pré-requisitos: `uv` instalado e Python 3.13 ou superior (o `uv` pode baixar uma versão compatível ao sincronizar o ambiente). Após clonar o repositório, abra três terminais na raiz do clone. Mantenha as portas 7300 e 7301 livres e use os valores padrão das variáveis de configuração.

No terminal 1, instale as dependências travadas, gere um segredo com 32 bytes aleatórios e suba o servidor MCP:

```sh
uv sync --frozen
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
uv run --frozen python servidor-mcp/servidor.py
```

O comando de geração requer `python3` no sistema. O MCP atende em `http://localhost:7301/mcp`. Não salve nem versione o segredo. Para reiniciar o MCP durante uma pausa, mantenha o mesmo `REQUEST_STATE_SECRET`; não gere outro valor.

No terminal 2, suba o agente A2A:

```sh
uv run --frozen python -m agente.main
```

O agente atende em `http://localhost:7300/a2a` e publica seu Agent Card em `http://localhost:7300/.well-known/agent-card.json`.

No terminal 3, com os dois processos prontos, execute o validador:

```sh
uv run --frozen python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Encerre os serviços com `Ctrl+C`. Antes de repetir a validação, reinicie ambos: os checks criam reservas em memória e alteram o resultado de execuções posteriores.

## Onde a ponte acontece

Em [`agente/main.py`](agente/main.py), `HostMCP.reservar` chama `call_tool` com `allow_input_required=True` e devolve os modelos `types.CallToolResult` ou `types.InputRequiredResult` do SDK. `CentralDeSalas.resultado` reconhece a pausa por `isinstance(result, types.InputRequiredResult)` (equivalente a `resultType: "input_required"` no protocolo), valida a pergunta como `ElicitRequest` em modo form, guarda `result.request_state` em `Tarefa.request_state` e chama `estado` diretamente para definir `TASK_STATE_INPUT_REQUIRED` e apresentar as alternativas. Na continuação de `SendMessage`, `CentralDeSalas.enviar` delega a `continuar`, que valida a escolha e monta a resposta à pergunta; `CentralDeSalas.reservar` envia os argumentos originais, `input_responses` com a chave da pergunta e o mesmo `request_state` para `HostMCP.reservar`. O SDK serializa esses campos como `inputResponses` e `requestState` no novo `tools/call`. Em [`servidor-mcp/servidor.py`](servidor-mcp/servidor.py), `reservar_sala`, registrada com `@mcp.tool()`, recebe o estado já validado pelo SDK em `ctx.request_state`, processa a resposta e revalida a disponibilidade em `criar_reserva` antes de inserir a reserva.

## Decisões técnicas

- **Validação e tipos:** `CentralDeSalas.enviar` valida a entrada A2A com os modelos Pydantic `MensagemUsuario` e `ParteTexto` em modo estrito. Os resultados MCP permanecem nos modelos do SDK durante o processamento, com acesso a campos como `input_requests`, `request_state` e `structured_content`; a serialização pública da Task continua separada em `wire`.
- **Proteção do `requestState`:** `criar_app` configura `RequestStateSecurity` do SDK MCP com AES-256-GCM e segredo de pelo menos 32 bytes vindo de `REQUEST_STATE_SECRET`. O SDK autentica e cifra o estado, vinculando-o à audiência `central-de-salas`, ao método, à ferramenta e aos argumentos originais. Tokens adulterados, expirados ou reutilizados com argumentos divergentes são rejeitados com `-32602`. O agente trata o token como opaco: guarda e devolve sem interpretá-lo, mantém-o fora do objeto público `wire` e não o registra nos logs.
- **Validade:** `ttl=600`, isto é, **10 minutos a partir da emissão**. O token contém o pedido, as alternativas e a chave da pergunta; o MCP não mantém pedidos pendentes em memória. Ele pode retomar uma pausa após reiniciar o servidor, dentro desse prazo, desde que o segredo seja preservado. As reservas concluídas ficam em memória e são recarregadas dos fixtures ao reiniciar.
- **Estado das Tasks:** `CentralDeSalas.tasks` é um dicionário em memória no processo do agente, indexado pelo ID da Task. Cada `Tarefa` contém o estado público, histórico, argumentos originais, token privado e um `asyncio.Lock` próprio para serializar continuações. `GetTask` devolve uma cópia do estado público. Não há persistência: reiniciar o agente perde as Tasks, inclusive as pausadas.

## Saída do validador

Última execução em **14/10/2026**, com ambos os processos recém-iniciados e o comando de validação acima: **36 verificações passaram, nenhuma falhou; código de saída 0**. Saída completa:

```text
trace-id desta execucao: a17e575f81e2f5d993120ae7e44e510a
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
