# Central de salas — servidor MCP

Servidor Python 3.13+ com `fastmcp==4.0.5`, protocolo `2026-07-28` e
Streamable HTTP stateless em `http://127.0.0.1:7301/mcp`. Implementa somente o
MCP; o agente A2A deve rodar em outro processo.

## Subir

Na raiz do repositorio:

```sh
uv sync --frozen
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
uv run --frozen python servidor-mcp/servidor.py
```

`PORT` altera a porta (default `7301`); `HOST` altera o endereco de escuta
(default `127.0.0.1`). O segredo precisa representar ao menos 32 bytes de
aleatoriedade. O formato hexadecimal gerado acima e decodificado antes do uso;
valores nao hexadecimais sao tratados como bytes UTF-8. Sem segredo valido o
processo recusa iniciar. Nao salve segredos no repositorio.

Para reiniciar durante uma elicitation, mantenha o **mesmo valor** de
`REQUEST_STATE_SECRET`; nao execute novamente o comando que gera o segredo.

## Contrato e estado

A descoberta `server/discover` declara `tools` e `resources`. As ferramentas sao
`listar_salas`, `consultar_disponibilidade` e `reservar_sala`. O resource
`politica://uso` devolve o Markdown literal. Os schemas publicados descrevem os
mesmos campos e tipos de `exemplos/wire/01-tools-list.json`, com a apresentacao
padrao do FastMCP; os resultados completos incluem JSON em
`structuredContent` e em um bloco de texto, alem de `resultType: complete`.

Cada POST exige os dois campos reservados em `params._meta`: protocolVersion e
clientCapabilities. Os headers `MCP-Protocol-Version`, `Mcp-Method` e, para
chamadas/leitura, `Mcp-Name` devem espelhar o corpo. Cada request e registrado no
stderr como uma linha JSON com `method`, `id` e `traceparent`, inclusive requests
recusados. Nao se usa a API MCP depreciada de logging.

Os fixtures sao lidos sem modificacao ao iniciar. Reservas concluidas vivem em
memoria e comecam em `res-0003`. Sobreposicao usa intervalos semiabertos: uma
reserva pode comecar quando a anterior termina. Datas devem incluir fuso e sao
convertidas para `-03:00` antes da verificacao da janela 08:00–20:00 e do limite
de duas horas.

Conflitos oferecem ate tres salas livres, com capacidade suficiente, ordenadas
por capacidade e id. Sem alternativas, retornam erro de execucao. Havendo
alternativas, o cliente precisa declarar explicitamente `elicitation.form`.
A resposta `input_required` encerra o request: o cliente responde em outro POST,
com `inputResponses` e `requestState` dentro de `params` e a mesma chave recebida
em `inputRequests`.

`RequestStateSecurity` do SDK usa AES-256-GCM, expira em **10 minutos** e vincula o
estado ao servidor, metodo, tool e argumentos originais. O token carrega pedido,
alternativas e chave da pergunta; nenhum pedido pendente e armazenado no
processo. Estados adulterados, expirados ou apresentados com argumentos
divergentes sao recusados com `-32602`. O retry aceito revalida a disponibilidade
antes de inserir. `decline` e `cancel` concluem sem reservar. Reservas concluidas
nao sobrevivem a restart, conforme o contrato; tokens pendentes sobrevivem com
a mesma chave.

## Verificar

Testes de regras e protocolo HTTP em memoria, sem portas ou servicos externos:

```sh
uv run --frozen python -m unittest discover -s test -v
```

Checks 01–20 do validador original e retry entre processos reais:

```sh
uv run --frozen python test/verificar_http.py
```

Esse script inicia o servidor na porta escolhida (default 7301), roda apenas
`verificar_mcp` do validador sem altera-lo, encerra o processo, e verifica um novo
ciclo de conflito/retry com restart real. Tambem inspeciona o stderr para confirmar
`tools/list` antes de `tools/call` e o traceparent recebido. Nao inicie outro
servidor na porta escolhida durante esse teste. A2A 21–36 fica fora deste escopo.

Para usar o validador completo com servicos iniciados manualmente:

```sh
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Reinicie o servidor antes de cada validacao, pois os checks criam reservas.
Sem agente, 21–36 falham como esperado.

## Integracao com o SDK

O transporte, despacho, schemas, resultados, MRTR e protecao do estado usam o
SDK instalado. Um detalhe observado em `mcp/server/streamable_http_manager.py`,
metodo `_handle_request`, exige validacao adicional na entrada HTTP:

```python
# TODO(L49): header-only era-routing for now; body-primary classification
# is a follow-up.
if pv is not None and pv not in HANDSHAKE_PROTOCOL_VERSIONS:
    await handle_modern_request(...)
    return
```

Um header legado `2025-11-25` podia selecionar o caminho legado mesmo com
`2026-07-28` no corpo. O teste fail-first de headers demonstrou essa divergencia.
O middleware compara metadados e headers antes desse roteamento e devolve o
`-32020` exigido pelo contrato; o restante do protocolo continua no SDK.

As tools usam o decorator padrao `@mcp.tool()`. O FastMCP gera os schemas a partir
das anotacoes de tipo dos parametros e do retorno, e usa a docstring como
descricao. Os modelos Pydantic descrevem os dados retornados. O parametro
`ctx: Context` e injetado pelo framework e nao aparece nos argumentos publicos.

Nao ha registro personalizado de tools nem ajuste manual dos schemas. Titulos,
metadados e o uso de referencias ou objetos inline seguem o padrao do FastMCP.
Os testes comparam os campos e tipos do contrato sem exigir a mesma apresentacao
do exemplo wire. O middleware usa `BaseHTTPMiddleware` do Starlette para ler o
corpo sem precisar implementar sua retransmissao ao SDK.

## Validacao realizada

Na refatoracao de 14/10/2026: **20 testes automatizados PASS**, **20/20 checks MCP
PASS**, retry apos reiniciar o processo PASS e inspecao automatizada de stderr
PASS. O novo teste de descoberta falhou primeiro porque o schema personalizado
nao declarava a rejeicao de argumentos desconhecidos; o schema padrao passou.
`dados/`, `validador/`, `exemplos/` e `agente/` permaneceram inalterados.
