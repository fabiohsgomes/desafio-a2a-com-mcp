# Tarefa: implementar o servidor MCP "central-de-salas" (Python + fastmcp)

## Contexto
Você está no repositório forkado do starter `a2a-com-mcp`. As pastas `dados/`,
`validador/` e `exemplos/` são CONGELADAS — é proibido alterá-las. O código do servidor
vai em `servidor-mcp/`. Você implementa APENAS o servidor MCP; o agente A2A é outro
processo e outra entrega.

Stack obrigatória: Python 3.13+ com o SDK oficial do MCP para Python (`fastmcp==4.0.5`,
já travado no `pyproject.toml`), alinhado à revisão `2026-07-28` da spec. Não use LLM em
nenhum ponto do caminho de execução. Sem ORM, sem banco, sem camada de serviços: os
dados vêm dos JSON em `dados/` e as reservas criadas vivem em memória.

Antes de escrever código, leia `exemplos/wire/01` a `06` e `11` — eles são o contrato
exato, capturado de uma execução real. A forma dos requests, das respostas e dos `_meta`
deve bater com esses arquivos.

## Objetivo
Um processo único em **Streamable HTTP**, endpoint único **`/mcp`**, escutando na porta
**7301** (parametrizável por variável de ambiente, com esse default). Declara as
capabilities de `tools` e `resources`.

### 1. Validação de `_meta` por request (stateless)
O servidor NÃO tem sessão. Em TODO request, antes de qualquer processamento:
- Exija `params._meta["io.modelcontextprotocol/protocolVersion"]` e
  `params._meta["io.modelcontextprotocol/clientCapabilities"]`.
- Se faltar qualquer um dos dois → erro JSON-RPC **`-32602`** com **HTTP 400**.
- Nunca infira versão ou capabilities de request anterior nem de conexão aberta.

### 2. Headers espelhados do corpo
Aceite e valide os headers de transporte `MCP-Protocol-Version`, `Mcp-Method` e, em
`tools/call` e `resources/read`, `Mcp-Name`. Header que não bate com o corpo é recusado
com **`-32020`**.

### 3. Três tools (todas com `inputSchema` que seja objeto JSON Schema válido)

**`listar_salas`** — sem parâmetros.
- Retorna `structuredContent` conforme um `outputSchema` declarado no formato do wire 01:
  `{ "salas": [ { "id", "nome", "capacidade", "recursos" } ] }` (dados de `dados/salas.json`).
- Retorna também o mesmo JSON serializado em um bloco `content[].type == "text"`.

**`consultar_disponibilidade`** — args `{ sala, inicio, fim }`.
- Retorna `{ sala, livre, conflitos }`, onde `conflitos` é a lista de reservas em conflito
  (`id`, `inicio`, `fim`, `responsavel`, conforme wire 01).
- Aplica as MESMAS validações de sala e de política que `reservar_sala` e devolve os
  mesmos erros de execução.

**`reservar_sala`** — args `{ sala, inicio, fim, responsavel }`.
- Com `structuredContent` trazendo exatamente as chaves:
  `reserva, reservado, sala, inicio, fim, responsavel, politica, motivo`
  (o wire 04 mostra o caminho feliz; o wire 11 mostra a recusa).

Todas as três devolvem `structuredContent` além do bloco de texto.

### 4. Resource
Expõe o resource **`politica://uso`** devolvendo o conteúdo literal de
`dados/politica-de-uso.md`, com `mimeType: "text/markdown"`.
- `resources/read` de URI inexistente → **`-32602`**. Devolver `contents` vazio é proibido
  pela spec.

### 5. Regras de negócio e mensagens EXATAS
A versão da política é a primeira linha do resource: `versao: 2026-11-01` — esse valor
precisa aparecer no campo `politica` de toda reserva.

Erros de execução, devolvidos como resultado `complete` com `isError: true`:
- Sala ausente de `dados/salas.json` → `Sala inexistente: <id informado>`
- Início ou fim fora da janela 08:00–20:00 (-03:00) →
  `Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00`
- Duração > 2 horas → `Duracao acima do limite: a politica permite no maximo 2 horas`
- `fim <= inicio` → `Intervalo invalido: fim deve ser posterior a inicio`
- Conflito e NENHUMA alternativa possível →
  `Sem alternativas disponiveis no intervalo`

O validador exige o texto exato; prefixos tipo `Error executing tool <nome>:` que o SDK
adicionar são aceitos.

Erros de protocolo (JSON-RPC):
- Tool inexistente → recusada (via `-32602` ou via `isError: true`; os dois são aceitos).
- Sem `_meta` obrigatório → `-32602` + HTTP 400.
- Elicitation sem capability → ver item 6.
- `requestState` adulterado/expirado → `-32602`.

Além disso: **crie a reserva quando o intervalo está livre e dentro da política**.
Persistência em memória; reservas criadas ficam visíveis para consultas seguintes do
mesmo processo e NÃO precisam sobreviver a restart (os `dados/reservas.json` de fixture
contam como estado inicial; a primeira reserva criada deve receber `res-0003` seguindo a
sequência dos fixtures).

### 6. Ciclo MRTR em `reservar_sala` (o núcleo do servidor)
Quando o intervalo pedido conflita com reserva existente:

1. Calcule as alternativas: salas **livres naquele intervalo** com **capacidade ≥** a da
   sala pedida, no máximo 3, ordenadas por **capacidade crescente** e, em empate, por
   **id alfabético**.

2. Se **não houver nenhuma** alternativa: NÃO há elicitation — devolva o erro de execução
   `Sem alternativas disponiveis no intervalo` (item 5).

3. Se houver alternativa(s): devolva `result` com `resultType: "input_required"` e:
   - `inputRequests`: mapa com **uma única entrada**; a chave é atribuída por você e o
     cliente devolve a MESMA chave no retry. O valor é um request `elicitation/create` em
     **`mode: "form"`**, com `message` e `requestedSchema` PLANO contendo a propriedade
     `sala` do tipo string restrita às alternativas (com ≥2 alternativas use `enum`; com 1,
     `const` também é aceito).
   - `requestState`: opaco, **protegido por integridade** (HMAC ou AEAD). Carrega tudo o
     que o servidor precisa para reconstruir o pedido original, incluindo expiração entre
     **5 e 30 minutos**. Assinar é obrigatório; cifrar é opcional.
   - O servidor **não guarda nada em memória** entre o `input_required` e o retry. Um retry
     apresentado **depois de reiniciar o processo** precisa funcionar.

   A chave de integridade vem da variável de ambiente `REQUEST_STATE_SECRET`, com no
   mínimo 32 bytes de aleatoriedade. NUNCA hardcode o segredo. Usuários geram com:
   `python3 -c "import secrets; print(secrets.token_hex(32))"`.

4. Checagem de capability ANTES de emitir a elicitation: se o cliente não declarou
   `clientCapabilities.elicitation.form` (form mode, não elicitation genérica), responda
   **`-32021`** com `data.requiredCapabilities` listando o que faltou e **HTTP 400**
   (formato exato no wire 06).

5. No retry, o servidor lê `inputResponses` e `requestState` **de dentro de `params`**,
   valida a integridade do `requestState` (adulterado/expirado → `-32602`), reconstrói o
   pedido original a partir do próprio `requestState` e conclui. Os argumentos reenviados
   pelo cliente NÃO são confiáveis: se divergirem do que foi selado, ou rejeite o estado,
   ou use os valores selados (ambos aceitos; nunca aplique o valor adulterado).
   - `action: "accept"` → cria a reserva na sala escolhida e devolve `resultType: "complete"`.
   - `action: "decline"` ou `"cancel"` → conclui SEM reservar, `isError` ausente/false, com
     `structuredContent.reservado: false` e `motivo` preenchido (ver wire 11).

Importante: NÃO implemente isso como callback/pergunta síncrona. O transporte stateless
não tem canal de volta — o servidor termina a resposta pedindo informação, e o cliente
volta com um request novo.

### 7. Observabilidade
Registre cada request recebido no **stderr** (não use a API de logging depreciada) com, no
mínimo: o método, o id e o valor de `traceparent` quando ele vier no `_meta`. Esse stderr é
o instrumento que o avaliador inspeciona (ele procura o `tools/list` antes do primeiro
`tools/call` e o mesmo trace-id propagado desde o cliente A2A).

## Restrições não negociáveis
- Dois processos separados; o agente fala com este servidor por HTTP.
- Nenhuma regra de negócio de sala pode migrar para o agente.
- Nada de sessão/estado implícito entre requests.
- Sem LLM no caminho de execução.
- Não altere `dados/`, `validador/` nem `exemplos/`.
- Se encontrar limitação real do SDK que impeça algum item, documente no README com o
  trecho da evidência em vez de reescrever o protocolo.

## Como verificar (somente o servidor MCP, por enquanto)
O agente ainda não existe, então não valide as verificações de A2A agora.

1. Gere e exporte o segredo e suba o servidor na 7301:
   ```
   export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
   ```

2. Inspeção manual: abra o MCP Inspector em `http://localhost:7301/mcp`, confirme que o
   `_meta` de cada request aparece e que a resposta traz `resultType`. Sem Inspector, use
   curl com os headers espelhados:
   ```
   curl -sS http://localhost:7301/mcp \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -H 'MCP-Protocol-Version: 2026-07-28' \
     -H 'Mcp-Method: tools/list' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{"elicitation":{"form":{}}}}}}'
   ```
   Reproduza o ciclo MRTR com os corpos de `exemplos/wire/03` (conflito) e `04` (retry),
   e a recusa com o `11`.

3. Validador de conformidade, olhando APENAS as verificações 01 a 20 (as que falam HTTP
   direto com o MCP):
   ```
   python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
   ```
   As verificações 21 a 36 dependem do agente A2A e vão falhar enquanto ele não existir —
   isso é esperado neste momento. O alvo aqui é 01 a 20 todas PASS. Reinicie o servidor MCP
   antes de cada execução, pois a validação cria reservas e muda o estado dos fixtures.

4. Confira os dois itens que o validador deixa para inspeção:
   - reinicie o processo do servidor MCP entre o `input_required` e o retry: a reserva
     precisa concluir mesmo assim (o estado vive no `requestState`, não no processo);
   - observe o `stderr`: cada request deve aparecer com método, id e traceparent.

## Entregável
- `servidor-mcp/` com a implementação e o comando de subida documentado.
- Logs de `stderr` suficientes para inspeção manual (método, id, traceparent).
- Testes automatizados em `test/` (na raiz), cobrindo as regras de negócio e o protocolo,
  seguindo o ciclo fail-first descrito no `AGENTS.md`.
- Trecho de README explicando como subir o servidor e gerar `REQUEST_STATE_SECRET`.