"""Servidor MCP stateless central-de-salas, sem LLM e com reservas em memoria."""
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool
from mcp import MCPError
from mcp.server.request_state import RequestStateSecurity
from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from pydantic import BaseModel
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
import uvicorn

DADOS = Path(__file__).resolve().parents[1] / 'dados'
FUSO = timezone(timedelta(hours=-3))
CHAVE = '__main__:escolha_de_sala'


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class FerramentaWire(FunctionTool):
    """Mantem a descoberta no formato do contrato, sem metadados extras do framework."""

    def to_mcp_tool(self, **overrides):
        return super().to_mcp_tool(**{'title': None, '_meta': None, **overrides})


class RegistrarRequests:
    """Observa o corpo antes das validacoes do SDK, inclusive requests recusados."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        raw = await Request(scope, receive).body()
        body, params, meta = None, None, None
        try:
            body = json.loads(raw)
            params = body.get('params') or {}
            meta = params.get('_meta') or {}
            log = {'method': body.get('method'), 'id': body.get('id'),
                   'traceparent': meta.get('traceparent')}
        except (ValueError, AttributeError):
            log = {'method': None, 'id': None, 'traceparent': None}
        print(json.dumps(log, ensure_ascii=False), file=sys.stderr, flush=True)
        # O SDK roteia revisoes legadas pelo header antes de comparar o corpo.
        # Validar aqui impede que um header legado contorne o contrato stateless.
        if isinstance(body, dict) and isinstance(params, dict) and isinstance(meta, dict):
            version_key = 'io.modelcontextprotocol/protocolVersion'
            caps_key = 'io.modelcontextprotocol/clientCapabilities'
            error = None
            if version_key not in meta or caps_key not in meta:
                error = (-32602, 'protocolVersion e clientCapabilities obrigatorios em params._meta')
            else:
                headers = Request(scope).headers
                mirrors = {'MCP-Protocol-Version': meta[version_key],
                           'Mcp-Method': body.get('method')}
                if body.get('method') in ('tools/call', 'resources/read'):
                    mirrors['Mcp-Name'] = params.get('name', params.get('uri'))
                if any(headers.get(name) != value for name, value in mirrors.items()):
                    error = (-32020, 'Headers de transporte divergem do corpo')
            if error:
                response = JSONResponse({'jsonrpc': '2.0', 'id': body.get('id'),
                    'error': {'code': error[0], 'message': error[1]}}, status_code=400)
                return await response(scope, receive, send)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': raw, 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)


def criar_app(secret: str | None = None):
    """Cada app carrega os fixtures; apenas as reservas concluidas vivem em memoria."""
    if secret is None:
        secret = os.environ.get('REQUEST_STATE_SECRET', '')
    # O comando documentado usa hexadecimal: validar o material decodificado
    # impede que 32 caracteres hex representem apenas 16 bytes de aleatoriedade.
    try:
        key = bytes.fromhex(secret)
    except ValueError:
        key = secret.encode('utf-8')
    if len(key) < 32:
        raise ValueError('REQUEST_STATE_SECRET deve conter ao menos 32 bytes de aleatoriedade')
    mcp = FastMCP('central-de-salas', version='1.0.0', dereference_schemas=False,
                  tasks=False,
                  request_state_security=RequestStateSecurity(
                      keys=[key], ttl=600, audience='central-de-salas'))
    salas = json.loads((DADOS / 'salas.json').read_text(encoding='utf-8'))
    reservas = json.loads((DADOS / 'reservas.json').read_text(encoding='utf-8'))
    politica = (DADOS / 'politica-de-uso.md').read_text(encoding='utf-8')
    versao = politica.splitlines()[0].removeprefix('versao: ').strip()
    por_id = {sala['id']: sala for sala in salas}
    sequencia = max(int(reserva['id'].removeprefix('res-')) for reserva in reservas)

    def validar(sala, inicio, fim):
        if sala not in por_id:
            raise ToolError(f'Sala inexistente: {sala}')
        try:
            start, end = datetime.fromisoformat(inicio), datetime.fromisoformat(fim)
            if start.utcoffset() is None or end.utcoffset() is None:
                raise ValueError('fuso ausente')
            start, end = start.astimezone(FUSO), end.astimezone(FUSO)
        except ValueError as exc:
            raise ToolError('Data invalida: use ISO 8601 com fuso horario') from exc
        if end <= start:
            raise ToolError('Intervalo invalido: fim deve ser posterior a inicio')
        if (start.date() != end.date() or
                not time(8) <= start.time() <= time(20) or
                not time(8) <= end.time() <= time(20)):
            raise ToolError('Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00')
        if end - start > timedelta(hours=2):
            raise ToolError('Duracao acima do limite: a politica permite no maximo 2 horas')
        return start, end

    def conflitos(sala, start, end):
        return [r for r in reservas if r['sala'] == sala and
                datetime.fromisoformat(r['inicio']) < end and
                datetime.fromisoformat(r['fim']) > start]

    def alternativas(sala, start, end):
        candidates = [s for s in salas if s['capacidade'] >= por_id[sala]['capacidade']
                      and not conflitos(s['id'], start, end)]
        return [s['id'] for s in sorted(candidates, key=lambda s: (s['capacidade'], s['id']))[:3]]

    def criar_reserva(args):
        nonlocal sequencia
        # Os handlers sao async e nao aguardam entre checar e inserir: operacao
        # atomica no event loop do unico processo, inclusive durante retries.
        start, end = validar(args['sala'], args['inicio'], args['fim'])
        if conflitos(args['sala'], start, end):
            raise ToolError('Sala escolhida indisponivel no intervalo')
        sequencia += 1
        reserva = {'id': f'res-{sequencia:04d}', **args}
        reservas.append(reserva)
        return ReservaOut(reserva=reserva['id'], **args, politica=versao)

    def ferramenta(output_model, description):
        def register(fn):
            tool = FerramentaWire.from_function(fn, description=description,
                output_schema=output_model.model_json_schema())
            properties = tool.parameters['properties']
            tool.parameters = {'type': 'object', 'properties': {
                name: {**schema, 'title': name.capitalize()}
                for name, schema in properties.items()}, 'title': fn.__name__ + 'Arguments'}
            if properties:
                tool.parameters['required'] = list(properties)
            mcp.add_tool(tool)
            return fn
        return register

    @ferramenta(ListaDeSalas, description='Lista todas as salas com capacidade e recursos.')
    async def listar_salas() -> ListaDeSalas:
        return ListaDeSalas(salas=salas)

    @ferramenta(Disponibilidade, description='Diz se uma sala esta livre no intervalo, e quais reservas conflitam.')
    async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
        start, end = validar(sala, inicio, fim)
        found = conflitos(sala, start, end)
        return Disponibilidade(sala=sala, livre=not found,
                               conflitos=[ConflitoOut(**r) for r in found])

    @ferramenta(ReservaOut, description='Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.')
    async def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str,
                           ctx: Context) -> ReservaOut | InputRequiredResult:
        args = dict(sala=sala, inicio=inicio, fim=fim, responsavel=responsavel)
        if ctx.request_state is not None:
            # O SDK ja verificou AEAD, expiração, audience, metodo, tool e args.
            state = json.loads(ctx.request_state)
            responses = ctx.input_responses or {}
            if set(responses) != {state['chave']}:
                raise ToolError('Resposta invalida: devolva a chave de inputRequests')
            answer = responses[state['chave']]
            if not isinstance(answer, ElicitResult):
                raise ToolError('Resposta invalida: esperado resultado de elicitation')
            if answer.action in ('decline', 'cancel'):
                return ReservaOut(reservado=False, motivo=(
                    'recusado' if answer.action == 'decline' else 'cancelado'))
            chosen = (answer.content or {}).get('sala')
            if chosen not in state['alternativas']:
                raise ToolError('Sala escolhida fora das alternativas oferecidas')
            return criar_reserva({**state['pedido'], 'sala': chosen})
        if ctx.input_responses is not None:
            raise ToolError('Resposta invalida: requestState obrigatorio no retry')
        start, end = validar(sala, inicio, fim)
        if not conflitos(sala, start, end):
            return criar_reserva(args)
        choices = alternativas(sala, start, end)
        if not choices:
            raise ToolError('Sem alternativas disponiveis no intervalo')
        # Checar a declaracao FORM explicitamente: elicitation generica nao basta.
        meta = ctx.request_context.meta or {}
        caps = meta.get('io.modelcontextprotocol/clientCapabilities', {})
        elicitation = caps.get('elicitation', {})
        if 'form' not in elicitation:
            raise MCPError(code=-32021, message=(
                "Client did not declare the form elicitation capability required by resolver "
                f"'{CHAVE}'"), data={'requiredCapabilities': {'elicitation': {'form': {}}}})
        return InputRequiredResult(input_requests={CHAVE: ElicitRequest(
            params=ElicitRequestFormParams(
                message='A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.',
                requested_schema={'type': 'object', 'properties': {'sala': {
                    'type': 'string', 'title': 'Sala',
                    'description': 'Sala alternativa escolhida', 'enum': choices}},
                    'required': ['sala']}))}, request_state=json.dumps({
                        'chave': CHAVE, 'pedido': args, 'alternativas': choices}))

    @mcp.resource('politica://uso', mime_type='text/markdown')
    async def politica_de_uso() -> str:
        return politica

    return mcp.http_app(path='/mcp', stateless_http=True, json_response=True,
                        middleware=[Middleware(RegistrarRequests)])


if __name__ == '__main__':
    uvicorn.run(criar_app(), host=os.environ.get('HOST', '127.0.0.1'),
                port=int(os.environ.get('PORT', '7301')))
