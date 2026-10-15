"""Execute na raiz: uv run python -m agente.main."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import os
import re
from typing import Literal
from uuid import uuid4

import httpx2
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

LOGGER = logging.getLogger('central-de-salas')
PROTOCOL_VERSION = '2026-07-28'
TERMINAIS = {'TASK_STATE_COMPLETED', 'TASK_STATE_CANCELED', 'TASK_STATE_FAILED'}
PEDIDO = re.compile(
    r'reservar sala=(?P<sala>\S+) inicio=(?P<inicio>\S+) '
    r'fim=(?P<fim>\S+) responsavel=(?P<responsavel>.+)', re.DOTALL)


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
        # O SDK acrescenta versao, capabilities e headers em cada POST.
        return {'traceparent': trace} if trace else {}

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

    async def reservar(self, args, trace, input_responses=None,
                       request_state=None) -> types.CallToolResult | types.InputRequiredResult:
        await self.descobrir(trace)
        return await self.session.call_tool(
            'reservar_sala', args, meta=self.meta(trace),
            input_responses=input_responses, request_state=request_state,
            allow_input_required=True)


class RpcError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


class ParteTexto(BaseModel):
    model_config = ConfigDict(strict=True)

    text: str


class MensagemUsuario(BaseModel):
    model_config = ConfigDict(strict=True)

    role: Literal['ROLE_USER']
    messageId: str
    parts: list[ParteTexto] = Field(min_length=1)
    taskId: str | None = None


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

    def resultado(self, task, result: types.CallToolResult | types.InputRequiredResult,
                  declined=False):
        if isinstance(result, types.InputRequiredResult):
            requests = result.input_requests or {}
            if len(requests) != 1 or result.request_state is None:
                raise ValueError('Elicitation inesperada')
            key, question = next(iter(requests.items()))
            if not isinstance(question, types.ElicitRequest) or not isinstance(
                    question.params, types.ElicitRequestFormParams):
                raise ValueError('Esperada elicitation em modo form')
            choices = question.params.requested_schema['properties']['sala']['enum']
            if not isinstance(choices, list) or not all(isinstance(x, str) for x in choices):
                raise ValueError('Enum invalido')
            task.input_key = key
            task.alternativas = choices
            task.request_state = result.request_state  # Opaco: somente guardar e ecoar.
            self.estado(task, 'TASK_STATE_INPUT_REQUIRED',
                        'alternativas: ' + ', '.join(task.alternativas))
            return
        if result.is_error:
            text = '\n'.join(part.text for part in result.content if isinstance(part, types.TextContent))
            self.estado(task, 'TASK_STATE_FAILED', text)
        elif declined:
            self.estado(task, 'TASK_STATE_CANCELED', 'Reserva recusada')
        else:
            data = result.structured_content
            if data is None:
                data = json.loads(next(p.text for p in result.content if isinstance(p, types.TextContent)))
            reservation = {**data, 'politica': task.politica}
            task.wire['artifacts'] = [{'artifactId': str(uuid4()), 'name': 'reserva',
                'parts': [{'text': json.dumps(reservation, ensure_ascii=False)}]}]
            self.estado(task, 'TASK_STATE_COMPLETED', 'Reserva concluida')
        task.request_state = None
        task.input_key = None
        task.alternativas = []

    async def enviar(self, message, trace):
        try:
            message = MensagemUsuario.model_validate(message)
        except ValidationError as exc:
            fields = [error['loc'] for error in exc.errors(include_input=False)]
            # Preserva a prioridade e as mensagens de erro do contrato A2A.
            if any(loc in ((), ('role',)) for loc in fields):
                raise RpcError(-32602, 'Esperada mensagem ROLE_USER') from None
            if any(loc[0] != 'taskId' for loc in fields):
                raise RpcError(-32602, 'Mensagem deve conter messageId e parts de texto') from None
            raise RpcError(-32001, 'Task inexistente') from None
        text = ' '.join(part.text for part in message.parts)
        task_id = message.taskId
        user = message.model_dump(exclude={'taskId'})
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
                await self.continuar(task, text)
            else:
                await self.iniciar(task, text)
        return self.obter(task_id)

    async def iniciar(self, task, text):
        """Interpreta o pedido inicial; as regras de reserva ficam no MCP."""
        await asyncio.sleep(0)  # Permite que GetTask observe SUBMITTED.
        self.estado(task, 'TASK_STATE_WORKING', 'TASK_STATE_WORKING')
        match = PEDIDO.fullmatch(text)
        if not match:
            self.estado(task, 'TASK_STATE_FAILED',
                'Pedido invalido: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>')
            return
        task.arguments = match.groupdict()
        await self.reservar(task)

    async def continuar(self, task, text):
        """Traduz a escolha em resposta a pergunta recebida do MCP."""
        choice = text.removeprefix('escolha=') if text.startswith('escolha=') else None
        if choice != 'recusar' and choice not in task.alternativas:
            return  # Mantem a mensagem e o status da pausa byte a byte.
        answer = {'action': 'decline'} if choice == 'recusar' else {
            'action': 'accept', 'content': {'sala': choice}}
        self.estado(task, 'TASK_STATE_WORKING', 'TASK_STATE_WORKING')
        await self.reservar(task, answer)

    async def reservar(self, task, answer=None):
        """Executa o pedido ou retry e trata falhas sem expor dados privados."""
        try:
            if answer is None:
                task.politica = await self.host.politica(task.trace)
            result = await self.host.reservar(
                task.arguments, task.trace,
                input_responses={task.input_key: answer} if answer is not None else None,
                request_state=task.request_state)
            self.resultado(task, result, declined=answer is not None and answer['action'] == 'decline')
        except Exception as exc:
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
