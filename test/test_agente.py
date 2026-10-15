"""Comportamento da ponte A2A/MCP, sem aplicar regras de salas no agente."""
import asyncio
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock

from mcp import types

from agente.main import CentralDeSalas, RpcError

PEDIDO = 'reservar sala=ocupada inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Maria Silva'
TRACE = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'


def mensagem(texto, task_id=None):
    msg = {'messageId': 'usuario', 'role': 'ROLE_USER', 'parts': [{'text': texto}]}
    if task_id:
        msg['taskId'] = task_id
    return msg


class HostFalso:
    def __init__(self):
        self.calls = []
        self.resultados = []
        self.started = asyncio.Event()
        self.release = None

    async def politica(self, trace):
        return '2026-10-01'

    async def reservar(self, args, trace, input_responses=None, request_state=None):
        self.calls.append(deepcopy((args, trace, input_responses, request_state)))
        self.started.set()
        if self.release:
            await self.release.wait()
        return self.resultados.pop(0)


def pausa(token):
    return types.InputRequiredResult.model_validate({
        'resultType': 'input_required', 'requestState': token, 'inputRequests': {
        'pergunta-servidor': {'method': 'elicitation/create', 'params': {
            'message': 'Escolha uma sala',
            'requestedSchema': {'properties': {'sala': {'enum': ['sala-b', 'sala-a']}}}}}}})


def sucesso(sala='sala-b'):
    return types.CallToolResult(content=[], structured_content={
        'reservado': True, 'reserva': sala, 'sala': sala})


class PonteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = HostFalso()
        self.central = CentralDeSalas(self.host)

    async def test_pausa_invalida_retry_opaco_e_terminal(self):
        self.host.resultados = [pausa('estado-opaco'), sucesso()]
        task = await self.central.enviar(mensagem(PEDIDO), TRACE)
        self.assertEqual(task['status']['state'], 'TASK_STATE_INPUT_REQUIRED')
        self.assertEqual(task['status']['message']['parts'], [{'text': 'alternativas: sala-b, sala-a'}])
        self.assertNotIn('estado-opaco', json.dumps(task))
        invalid = await self.central.enviar(mensagem('escolha=invalida', task['id']), None)
        self.assertEqual(invalid['status'], task['status'])
        self.assertEqual(len(self.host.calls), 1)
        completed = await self.central.enviar(mensagem('escolha=sala-b', task['id']), None)
        args, trace, responses, state = self.host.calls[-1]
        self.assertEqual(args, self.host.calls[0][0])
        self.assertEqual(args['responsavel'], 'Maria Silva')
        self.assertEqual(trace, TRACE)
        self.assertEqual(responses, {'pergunta-servidor': {'action': 'accept', 'content': {'sala': 'sala-b'}}})
        self.assertEqual(state, 'estado-opaco')
        self.assertEqual(completed['status']['state'], 'TASK_STATE_COMPLETED')
        self.assertEqual(json.loads(completed['artifacts'][0]['parts'][0]['text'])['politica'], '2026-10-01')
        with self.assertRaises(RpcError):
            await self.central.enviar(mensagem('escolha=sala-a', task['id']), None)

    async def test_estado_independente_por_task_e_recusa(self):
        self.host.resultados = [pausa('token-a'), pausa('token-b'), sucesso(),
                               types.CallToolResult(content=[], structured_content={'reservado': False})]
        a = await self.central.enviar(mensagem(PEDIDO), None)
        b = await self.central.enviar(mensagem(PEDIDO), None)
        await self.central.enviar(mensagem('escolha=sala-a', a['id']), None)
        canceled = await self.central.enviar(mensagem('escolha=recusar', b['id']), None)
        self.assertEqual(self.host.calls[2][3], 'token-a')
        self.assertEqual(self.host.calls[3][3], 'token-b')
        self.assertEqual(self.host.calls[3][2], {'pergunta-servidor': {'action': 'decline'}})
        self.assertEqual(canceled['status']['state'], 'TASK_STATE_CANCELED')
        self.assertEqual(canceled['artifacts'], [])

    async def test_erro_exato_no_historico(self):
        self.host.resultados = [types.CallToolResult(is_error=True,
            content=[types.TextContent(type='text', text='Sala inexistente: x')])]
        task = await self.central.enviar(mensagem(PEDIDO), None)
        self.assertEqual(task['status']['state'], 'TASK_STATE_FAILED')
        self.assertEqual(task['history'][-1]['parts'], [{'text': 'Sala inexistente: x'}])

    async def test_gettask_observa_working_e_snapshot(self):
        self.host.resultados = [sucesso()]
        self.host.release = asyncio.Event()
        pending = asyncio.create_task(self.central.enviar(mensagem(PEDIDO), None))
        await self.host.started.wait()
        task_id = next(iter(self.central.tasks))
        snapshot = self.central.obter(task_id)
        self.assertEqual(snapshot['status']['state'], 'TASK_STATE_WORKING')
        states = [m['parts'][0]['text'] for m in snapshot['history']]
        self.assertIn('TASK_STATE_SUBMITTED', states)
        self.host.release.set()
        await pending
        self.assertEqual(snapshot['status']['state'], 'TASK_STATE_WORKING')
        self.assertEqual(self.central.obter(task_id)['status']['state'], 'TASK_STATE_COMPLETED')

    async def test_trace_recebido_na_continuacao_sem_trace_inicial(self):
        self.host.resultados = [pausa('token'), sucesso()]
        task = await self.central.enviar(mensagem(PEDIDO), None)
        await self.central.enviar(mensagem('escolha=sala-b', task['id']), TRACE)
        self.assertEqual(self.host.calls[-1][1], TRACE)

    async def test_continuacoes_simultaneas_nao_duplicam_reserva(self):
        self.host.resultados = [pausa('token'), sucesso()]
        task = await self.central.enviar(mensagem(PEDIDO), TRACE)
        self.host.started.clear()
        self.host.release = asyncio.Event()
        continuation = asyncio.create_task(self.central.enviar(
            mensagem('escolha=sala-b', task['id']), None))
        try:
            await asyncio.wait_for(self.host.started.wait(), timeout=1)
            with self.assertRaises(RpcError) as error:
                await self.central.enviar(mensagem('escolha=sala-a', task['id']), None)
            self.assertEqual(error.exception.code, -32002)
            self.assertEqual(len(self.host.calls), 2)
            self.assertEqual(self.central.obter(task['id'])['status']['state'], 'TASK_STATE_WORKING')
        finally:
            self.host.release.set()
            completed = await continuation
        self.assertEqual(completed['status']['state'], 'TASK_STATE_COMPLETED')

    async def test_falha_na_continuacao_nao_expoe_token(self):
        token = 'token-opaco-privado'
        self.host.resultados = [pausa(token)]
        task = await self.central.enviar(mensagem(PEDIDO), TRACE)
        self.host.reservar = AsyncMock(side_effect=ConnectionError(token))
        with self.assertLogs('central-de-salas', level='ERROR') as logs:
            failed = await self.central.enviar(mensagem('escolha=sala-b', task['id']), None)
        self.assertEqual(failed['status']['state'], 'TASK_STATE_FAILED')
        self.assertEqual(failed['status']['message']['parts'], [
            {'text': 'Falha ao comunicar com o servidor MCP'}])
        self.assertNotIn(token, json.dumps(failed))
        self.assertNotIn(token, '\n'.join(logs.output))
        with self.assertRaises(RpcError) as error:
            await self.central.enviar(mensagem('escolha=sala-a', task['id']), None)
        self.assertEqual(error.exception.code, -32002)
        self.assertEqual(self.host.reservar.await_count, 1)

    async def test_falha_na_politica_ou_reserva_inicial(self):
        for method in ('politica', 'reservar'):
            with self.subTest(method=method):
                host = HostFalso()
                setattr(host, method, AsyncMock(side_effect=ConnectionError('payload privado')))
                central = CentralDeSalas(host)
                with self.assertLogs('central-de-salas', level='ERROR') as logs:
                    task = await central.enviar(mensagem(PEDIDO), TRACE)
                self.assertEqual(task['status']['state'], 'TASK_STATE_FAILED')
                self.assertEqual(task['artifacts'], [])
                self.assertNotIn('payload privado', json.dumps(task) + '\n'.join(logs.output))
                if method == 'politica':
                    self.assertEqual(host.calls, [])

    async def test_mensagens_malformadas_nao_chamam_mcp(self):
        self.host.politica = AsyncMock()
        invalid = [None, {}, {**mensagem(PEDIDO), 'role': 'ROLE_AGENT'},
                   {**mensagem(PEDIDO), 'messageId': None},
                   {**mensagem(PEDIDO), 'parts': []},
                   {**mensagem(PEDIDO), 'parts': [{'text': 1}]}]
        for message in invalid:
            with self.subTest(message=message):
                with self.assertRaises(RpcError) as error:
                    await self.central.enviar(message, None)
                self.assertEqual(error.exception.code, -32602)
        self.assertEqual(self.central.tasks, {})
        task = await self.central.enviar(mensagem('reservar sala=ocupada'), None)
        self.assertEqual(task['status']['state'], 'TASK_STATE_FAILED')
        self.assertIn('Pedido invalido:', task['status']['message']['parts'][0]['text'])
        self.host.politica.assert_not_awaited()
        self.assertEqual(self.host.calls, [])

    async def test_validacao_preserva_tipos_e_erros_existentes(self):
        cases = [
            (None, -32602, 'Esperada mensagem ROLE_USER'),
            ({**mensagem(PEDIDO), 'role': None}, -32602, 'Esperada mensagem ROLE_USER'),
            ({**mensagem(PEDIDO), 'messageId': 123}, -32602,
             'Mensagem deve conter messageId e parts de texto'),
            ({**mensagem(PEDIDO), 'parts': [{'text': 123}]}, -32602,
             'Mensagem deve conter messageId e parts de texto'),
            ({**mensagem(PEDIDO), 'parts': [{'text': None}]}, -32602,
             'Mensagem deve conter messageId e parts de texto'),
            ({**mensagem(PEDIDO), 'parts': ({'text': PEDIDO},)}, -32602,
             'Mensagem deve conter messageId e parts de texto'),
        ]
        for task_id in (1, False, [], {}, 'inexistente'):
            cases.append(({**mensagem(PEDIDO), 'taskId': task_id}, -32001, 'Task inexistente'))
        # Erros de mensagem continuam tendo prioridade sobre o identificador da Task.
        cases.append(({**mensagem(PEDIDO), 'taskId': 1, 'messageId': None}, -32602,
                      'Mensagem deve conter messageId e parts de texto'))
        self.host.politica = AsyncMock()
        for message, code, text in cases:
            with self.subTest(message=message):
                with self.assertRaises(RpcError) as error:
                    await self.central.enviar(message, None)
                self.assertEqual((error.exception.code, error.exception.message), (code, text))
        self.assertEqual(self.central.tasks, {})
        self.assertEqual(self.host.calls, [])
        self.host.politica.assert_not_awaited()

    async def test_partes_de_texto_e_campos_extras(self):
        self.host.resultados = [sucesso()]
        message = mensagem(PEDIDO)
        message.update(taskId=None, contextId='ignorado', extra='ignorado')
        first, second = PEDIDO.split(' responsavel=')
        message['parts'] = [{'text': first, 'extra': 'ignorado'},
                            {'text': 'responsavel=' + second}]
        task = await self.central.enviar(message, None)
        self.assertEqual(task['status']['state'], 'TASK_STATE_COMPLETED')
        self.assertEqual(self.host.calls[0][0]['responsavel'], 'Maria Silva')
        user = next(item for item in task['history'] if item['role'] == 'ROLE_USER')
        self.assertEqual(user, {'messageId': 'usuario', 'role': 'ROLE_USER',
                               'parts': [{'text': first}, {'text': 'responsavel=' + second}]})

    async def test_sucesso_com_json_no_bloco_de_texto(self):
        reservation = {'reserva': 'res-1', 'sala': 'sala-b', 'reservado': True}
        self.host.resultados = [types.CallToolResult(content=[
            types.TextContent(type='text', text=json.dumps(reservation))])]
        task = await self.central.enviar(mensagem(PEDIDO), None)
        self.assertEqual(task['status']['state'], 'TASK_STATE_COMPLETED')
        artifact = json.loads(task['artifacts'][0]['parts'][0]['text'])
        self.assertEqual(artifact, {**reservation, 'politica': '2026-10-01'})
