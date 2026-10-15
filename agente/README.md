# Central de Salas — agente A2A e host MCP

Agente determinístico em Python 3.13+, sem LLM. Expõe o binding JSON-RPC
A2A v1.0 e usa o SDK oficial MCP 2.3.0 por Streamable HTTP para conversar
com o servidor em outro processo. Nenhuma regra de disponibilidade ou política
é implementada pelo agente.

## Subir os dois processos

Na raiz do repositório, sincronize o ambiente:

```sh
uv sync --frozen
```

Terminal 1, servidor MCP existente:

```sh
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
uv run --frozen python servidor-mcp/servidor.py
```

Terminal 2, agente:

```sh
uv run --frozen python -m agente.main
```

Card: `http://localhost:7300/.well-known/agent-card.json`.
JSON-RPC: `http://localhost:7300/a2a`, métodos `SendMessage` e `GetTask`.

| Variável do agente | Default | Uso |
| --- | --- | --- |
| `AGENTE_PORT` | `7300` | Porta HTTP |
| `AGENTE_HOST` | `127.0.0.1` | Endereço de escuta |
| `A2A_PATH` | `/a2a` | Caminho JSON-RPC |
| `AGENTE_URL` | `http://localhost:<AGENTE_PORT>` | URL base publicada no card |
| `MCP_URL` | `http://localhost:7301/mcp` | Endpoint do servidor MCP |

## Pedido e continuação

Envie um POST para `/a2a` com este corpo (os exemplos congelados em
`exemplos/wire/08` a `10` também mostram os formatos):

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "pedido-1",
      "role": "ROLE_USER",
      "parts": [{"text": "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"}]
    }
  }
}
```

A resposta contém `result.task`. Para consultar, envie `GetTask` com
`params: {"id": "<id retornado>"}`. Para continuar uma pausa, envie outra
mensagem `SendMessage`, agora com `taskId` dentro de `message` e texto
`escolha=<id oferecido>` ou `escolha=recusar`.

## Onde acontece a ponte

Em `agente/main.py`, `HostMCP.descobrir` executa `tools/list` antes da primeira
chamada e guarda os nomes descobertos. `HostMCP.politica` lê `politica://uso`
para cada Task e extrai a versão da primeira linha. `SessaoForm` anuncia
`elicitation.form` sem registrar callback: o SDK 2.3.0 condiciona seu anúncio
automático à presença de um callback, então a subclasse adapta somente esse
anúncio. A versão MCP explícita do contrato é `2026-07-28`. O agente informa
apenas o trace nos metadados de cada chamada; o SDK acrescenta versão,
capabilities e headers espelhados em cada POST, sem handshake legado.

`CentralDeSalas.enviar` valida a mensagem, localiza ou cria a Task e encaminha
o texto para um de dois métodos:

- `iniciar` interpreta o pedido com grupos nomeados na expressão regular.
- `continuar` valida a escolha e prepara a resposta à pergunta do MCP.

`MensagemUsuario` e `ParteTexto` usam Pydantic em modo estrito para declarar
os campos obrigatórios e os tipos aceitos, incluindo uma lista não vazia de
partes de texto. Os erros de validação são traduzidos para os mesmos códigos
e mensagens A2A existentes; campos extras continuam sendo ignorados.
O conteúdo público da mensagem é serializado a partir do modelo validado.
`Tarefa` continua como `dataclass`, pois guarda estado interno e um lock.

Os métodos `iniciar` e `continuar` usam `CentralDeSalas.reservar`, que lê a política no pedido inicial,
executa a chamada MCP e trata as falhas de comunicação em um único lugar.

`HostMCP.reservar` usa `call_tool(..., allow_input_required=True)` e mantém os
objetos tipados `CallToolResult` e `InputRequiredResult` do SDK, sem convertê-los
em dicionários. Em
`CentralDeSalas.resultado`, um método síncrono que atualiza a Task, o retorno
bruto `input_required` vira `TASK_STATE_INPUT_REQUIRED`, com `alternativas: ...`
na ordem do enum.
O `requestState` é guardado sem interpretação em `Tarefa.request_state`,
fora do objeto público da Task. A chave da pergunta também fica nessa Task.

Na continuação, a rotina comum de reserva repete os argumentos
originais e envia `input_responses` com a mesma chave, mais `request_state`
inalterado. O SDK gera um novo ID JSON-RPC. Escolha inválida mantém a pausa;
recusa conclui em `CANCELED`; erro de execução conserva o texto exato da tool
no histórico e termina em `FAILED`. Sucesso produz o artifact `reserva` com
`politica`. Tasks terminais recusam novas mensagens.

Cada Task tem seu próprio estado e lock. `GetTask` devolve uma cópia e pode
observar `SUBMITTED`/`WORKING` enquanto o MCP responde. Tasks e histórico vivem
em memória: reiniciar o agente perde essas Tasks.

O primeiro `traceparent` recebido na Task é preservado nas chamadas MCP,
inclusive na retomada. Se o pedido inicial não tinha trace, a continuação pode
fornecê-lo. Logs JSON no stderr registram método, ID e trace, sem registrar o
token. Erros de transporte/protocolo falham a Task com mensagem genérica para
não expor payload interno; não há retry automático que duplique reservas.

## Verificação

Testes automatizados, incluindo a ponte e os testes preexistentes do servidor.
Os cenários do agente cobrem também continuações simultâneas sem duplicar a
reserva, falhas de comunicação sem expor o token e mensagens malformadas sem
consultar o MCP. Os testes usam os mesmos tipos de resultado do SDK e verificam
a preservação dos erros, múltiplas partes de texto, campos extras e a conclusão
com JSON no bloco de texto quando não há `structuredContent`:

```sh
uv run --frozen python -m unittest discover -s test -v
```

Validação com ambos os processos iniciados pelo próprio script, encerrados ao
final, e inspeção automatizada do stderr:

```sh
uv run --frozen python test/verificar_agente_http.py
```

Deixe as portas 7300 e 7301 livres para esse script. `AGENTE_TEST_PORT` e
`MCP_TEST_PORT` permitem outras portas. O script usa o validador original sem
modificá-lo e verifica descoberta antes de calls, IDs únicos e trace chegando
ao servidor.

Para serviços iniciados manualmente, reinicie ambos antes de cada execução:

```sh
uv run --frozen python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Validação da refatoração em 14/10/2026: **27 testes automatizados PASS**
(incluindo **12 testes do agente**) e **36/36 checks PASS**, com descoberta,
IDs e trace confirmados nos logs dos dois processos. O servidor aceitou os
requests após a simplificação dos metadados, mantendo a validação obrigatória
de versão, capabilities e headers. Exemplo observado anteriormente no stderr MCP:

```json
{"method":"tools/call","traceparent":"00-1dd494554058476db6df05766f04b092-e6539e5161ffd36a-01"}
```

Nesta refatoração do agente, `servidor-mcp/`, `dados/`, `validador/` e
`exemplos/` não foram alterados.
