"""Execute na raiz: uv run python -m agente.main."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import os
import re
from uuid import uuid4

import httpx2
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

LOGGER = logging.getLogger('central-de-salas')
PROTOCOL_VERSION = '2026-07-28'
TERMINAIS = {'TASK_STATE_COMPLETED', 'TASK_STATE_CANCELED', 'TASK_STATE_FAILED'}
PEDIDO = re.compile(r'reservar sala=(\S+) inicio=(\S+) fim=(\S+) responsavel=(.+)', re.DOTALL)


def registrar(protocolo, metodo, request_id, trace):
    LOGGER.info(json.dumps({'protocolo': protocolo, 'method': metodo,
                            'id': request_id, 'traceparent': trace}, ensure_ascii=False))


class SessaoForm(ClientSession):
    """Anuncia FORM para MRTR manual, sem instalar callback de elicitation.

    O SDK 2.3.0 vincula o anuncio automatico a callbacks. Esta unica adaptacao
    declara o suporte oferecido pela ponte, mantendo transporte/despacho no SDK.
    """
    def _build_capabilities(self, version):
        return types.ClientCapabilities(elicitation=types.ElicitationCapability(
            form=types.FormElicitationCapability()))


class HostMCP:
    def __init__(self, session):
        self.session = session
        self.tools = None
        self.discovery_lock = asyncio.Lock()

    @staticmethod
    def meta(trace):
        # O SDK espelha versao e nome nos headers e repete capabilities em cada POST.
        meta = {'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
                'io.modelcontextprotocol/clientCapabilities': {'elicitation': {'form': {}}}}
        if trace:
            meta['traceparent'] = trace
        return meta

    async def descobrir(self, trace):
        async with self.discovery_lock:
            if self.tools is not None:
                return
            names = set()
            cursor = None
            while True:
                result = await self.session.list_tools(params=types.PaginatedRequestParams(
                    cursor=cursor, _meta=self.meta(trace)))
                names.update(tool.name for tool in result.tools)
                cursor = result.next_cursor
                if not cursor:
                    break
            if 'reservar_sala' not in names:
                raise RuntimeError('O servidor nao publicou reservar_sala')
            self.tools = names

    async def politica(self, trace):
        await self.descobrir(trace)
        result = await self.session.read_resource('politica://uso', meta=self.meta(trace))
        text = '\n'.join(item.text for item in result.contents if hasattr(item, 'text'))
        first_line = text.splitlines()[0] if text else ''
        match = re.fullmatch(r'versao:\s*(\S.*)', first_line)
        if not match:
            raise RuntimeError('Resource de politica sem versao na primeira linha')
        return match.group(1).strip()

    async def reservar(self, args, trace, input_responses=None, request_state=None):
        await self.descobrir(trace)
        result = await self.session.call_tool(
            'reservar_sala', args, meta=self.meta(trace),
            input_responses=input_responses, request_state=request_state,
            allow_input_required=True)
        return result.model_dump(by_alias=True, mode='json', exclude_none=True)


class RpcError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class Tarefa:
    wire: dict
    trace: str | None
    arguments: dict = field(default_factory=dict)
    politica: str | None = None
    request_state: str | None = None
    input_key: str | None = None
    alternativas: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CentralDeSalas:
    def __init__(self, host):
        self.host = host
        self.tasks: dict[str, Tarefa] = {}

    def obter(self, task_id):
        if not isinstance(task_id, str) or task_id not in self.tasks:
            raise RpcError(-32001, 'Task inexistente')
        return deepcopy(self.tasks[task_id].wire)

    @staticmethod
    def estado(task, state, text):
        message = {'messageId': str(uuid4()), 'role': 'ROLE_AGENT',
                   'parts': [{'text': text}], 'taskId': task.wire['id'],
                   'contextId': task.wire['contextId']}
        task.wire['status'] = {'state': state, 'message': message}
        task.wire['history'].append(message)

    def pausar(self, task):
        self.estado(task, 'TASK_STATE_INPUT_REQUIRED',
                    'alternativas: ' + ', '.join(task.alternativas))

    async def resultado(self, task, result, declined=False):
        if result.get('resultType') == 'input_required':
            requests = result.get('inputRequests') or {}
            if len(requests) != 1 or result.get('requestState') is None:
                raise ValueError('Elicitation inesperada')
            key, question = next(iter(requests.items()))
            choices = question['params']['requestedSchema']['properties']['sala']['enum']
            if not isinstance(choices, list) or not all(isinstance(x, str) for x in choices):
                raise ValueError('Enum invalido')
            task.input_key = key
            task.alternativas = choices
            task.request_state = result['requestState']  # Opaco: somente guardar e ecoar.
            self.pausar(task)
            return
        if result.get('isError'):
            text = '\n'.join(part['text'] for part in result.get('content', []) if 'text' in part)
            self.estado(task, 'TASK_STATE_FAILED', text)
        elif declined:
            self.estado(task, 'TASK_STATE_CANCELED', 'Reserva recusada')
        else:
            data = result.get('structuredContent')
            if data is None:
                data = json.loads(next(p['text'] for p in result['content'] if 'text' in p))
            reservation = {**data, 'politica': task.politica}
            task.wire['artifacts'] = [{'artifactId': str(uuid4()), 'name': 'reserva',
                'parts': [{'text': json.dumps(reservation, ensure_ascii=False)}]}]
            self.estado(task, 'TASK_STATE_COMPLETED', 'Reserva concluida')
        task.request_state = None
        task.input_key = None
        task.alternativas = []

    async def enviar(self, message, trace):
        if not isinstance(message, dict) or message.get('role') != 'ROLE_USER':
            raise RpcError(-32602, 'Esperada mensagem ROLE_USER')
        parts = message.get('parts')
        if (not isinstance(message.get('messageId'), str) or not isinstance(parts, list)
                or not parts or any(not isinstance(p, dict) or not isinstance(p.get('text'), str) for p in parts)):
            raise RpcError(-32602, 'Mensagem deve conter messageId e parts de texto')
        text = ' '.join(p['text'] for p in parts)
        task_id = message.get('taskId')
        user = {'messageId': message['messageId'], 'role': 'ROLE_USER',
                'parts': [{'text': p['text']} for p in parts]}
        if task_id is not None:
            self.obter(task_id)
            task = self.tasks[task_id]
            if task.wire['status']['state'] in TERMINAIS:
                raise RpcError(-32002, 'Task terminal nao aceita continuacao')
            if task.lock.locked() or task.wire['status']['state'] != 'TASK_STATE_INPUT_REQUIRED':
                raise RpcError(-32002, 'Task nao esta aguardando entrada')
            user.update(taskId=task_id, contextId=task.wire['contextId'])
        else:
            task_id = str(uuid4())
            task = Tarefa({'id': task_id, 'contextId': str(uuid4()), 'history': [],
                           'artifacts': [], 'status': {}}, trace)
            self.tasks[task_id] = task
            self.estado(task, 'TASK_STATE_SUBMITTED', 'TASK_STATE_SUBMITTED')
        async with task.lock:
            if task.trace is None and trace:
                task.trace = trace
            task.wire['history'].append(user)
            if task.request_state is not None:
                choice = text.removeprefix('escolha=') if text.startswith('escolha=') else None
                if choice != 'recusar' and choice not in task.alternativas:
                    # Preserve a mensagem da pausa byte a byte, inclusive no status.
                    return self.obter(task_id)
                declined = choice == 'recusar'
                answer = {'action': 'decline'} if declined else {
                    'action': 'accept', 'content': {'sala': choice}}
                self.estado(task, 'TASK_STATE_WORKING', 'TASK_STATE_WORKING')
                try:
                    result = await self.host.reservar(task.arguments, task.trace,
                        input_responses={task.input_key: answer}, request_state=task.request_state)
                    await self.resultado(task, result, declined)
                except Exception as exc:
                    self.falha_host(task, exc)
            else:
                await asyncio.sleep(0)
                self.estado(task, 'TASK_STATE_WORKING', 'TASK_STATE_WORKING')
                match = PEDIDO.fullmatch(text)
                if not match:
                    self.estado(task, 'TASK_STATE_FAILED',
                        'Pedido invalido: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>')
                else:
                    task.arguments = dict(zip(('sala', 'inicio', 'fim', 'responsavel'), match.groups()))
                    try:
                        task.politica = await self.host.politica(task.trace)
                        result = await self.host.reservar(task.arguments, task.trace)
                        await self.resultado(task, result)
                    except Exception as exc:
                        self.falha_host(task, exc)
        return self.obter(task_id)

    def falha_host(self, task, exc):
        # Erros de transporte/protocolo nao expoem o payload ou token opaco.
        LOGGER.error('Falha MCP task=%s tipo=%s', task.wire['id'], type(exc).__name__)
        self.estado(task, 'TASK_STATE_FAILED', 'Falha ao comunicar com o servidor MCP')
        task.request_state = None


async def log_mcp(request):
    if request.method == 'POST':
        body = json.loads(request.content)
        registrar('MCP', body['method'], body.get('id'),
                  (body.get('params', {}).get('_meta') or {}).get('traceparent'))


def criar_app(host=None):
    path = os.environ.get('A2A_PATH', '/a2a')
    port = int(os.environ.get('AGENTE_PORT', '7300'))
    public_url = os.environ.get('AGENTE_URL', f'http://localhost:{port}').rstrip('/')

    @asynccontextmanager
    async def lifespan(app):
        if host is not None:
            app.state.central = CentralDeSalas(host)
            yield
            return
        async with httpx2.AsyncClient(event_hooks={'request': [log_mcp]}, timeout=30) as http:
            async with streamable_http_client(os.environ.get('MCP_URL', 'http://localhost:7301/mcp'),
                                              http_client=http, terminate_on_close=False) as streams:
                async with SessaoForm(*streams, read_timeout_seconds=30) as session:
                    # Versao explicita do contrato stateless; sem initialize/sessao legada.
                    session.adopt(types.DiscoverResult(supported_versions=[PROTOCOL_VERSION],
                        capabilities=types.ServerCapabilities(), ttl_ms=0, cache_scope='public'))
                    app.state.central = CentralDeSalas(HostMCP(session))
                    yield

    async def card(request):
        return JSONResponse({'name': 'Central de Salas',
            'description': 'Reservas de salas por MCP com continuacao A2A.',
            'provider': {'organization': 'Central de Salas', 'url': public_url}, 'version': '1.0.0',
            'supportedInterfaces': [{'url': public_url + path, 'protocolBinding': 'JSONRPC',
                                     'protocolVersion': '1.0'}],
            'capabilities': {'streaming': False, 'pushNotifications': False, 'extendedAgentCard': False},
            'defaultInputModes': ['text/plain'], 'defaultOutputModes': ['text/plain'],
            'skills': [{'id': 'reservar-sala', 'name': 'Reservar sala',
                'description': 'Reserva uma sala e solicita escolha em caso de conflito.',
                'tags': ['salas', 'reservas'], 'inputModes': ['text/plain'], 'outputModes': ['text/plain'],
                'examples': ['reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty']}]})

    async def rpc(request: Request):
        request_id = None
        try:
            try:
                body = await request.json()
            except (ValueError, UnicodeDecodeError):
                raise RpcError(-32700, 'JSON invalido') from None
            if not isinstance(body, dict):
                raise RpcError(-32600, 'Request JSON-RPC invalido')
            request_id = body.get('id')
            registrar('A2A', body.get('method'), request_id, request.headers.get('traceparent'))
            if (body.get('jsonrpc') != '2.0' or 'id' not in body or
                    not isinstance(body.get('method'), str) or
                    isinstance(request_id, (dict, list, bool))):
                raise RpcError(-32600, 'Request JSON-RPC invalido')
            params = body.get('params', {})
            if not isinstance(params, dict):
                raise RpcError(-32602, 'params deve ser objeto')
            central = request.app.state.central
            if body['method'] == 'SendMessage':
                task = await central.enviar(params.get('message'), request.headers.get('traceparent'))
            elif body['method'] == 'GetTask':
                task = central.obter(params.get('id'))
            else:
                raise RpcError(-32601, 'Metodo desconhecido')
            return JSONResponse({'jsonrpc': '2.0', 'id': request_id, 'result': {'task': task}})
        except RpcError as exc:
            return JSONResponse({'jsonrpc': '2.0', 'id': request_id,
                                 'error': {'code': exc.code, 'message': exc.message}})

    return Starlette(routes=[Route('/.well-known/agent-card.json', card),
                             Route(path, rpc, methods=['POST'])], lifespan=lifespan)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    uvicorn.run(criar_app(), host=os.environ.get('AGENTE_HOST', '127.0.0.1'),
                port=int(os.environ.get('AGENTE_PORT', '7300')))
