"""Comportamento da ponte A2A/MCP, sem aplicar regras de salas no agente."""
import asyncio
import json
import unittest
from copy import deepcopy

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
    return {'resultType': 'input_required', 'requestState': token, 'inputRequests': {
        'pergunta-servidor': {'method': 'elicitation/create', 'params': {
            'requestedSchema': {'properties': {'sala': {'enum': ['sala-b', 'sala-a']}}}}}}}


def sucesso(sala='sala-b'):
    return {'resultType': 'complete', 'isError': False,
            'structuredContent': {'reservado': True, 'reserva': sala, 'sala': sala}}


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
                                {'resultType': 'complete', 'structuredContent': {'reservado': False}}]
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
        self.host.resultados = [{'resultType': 'complete', 'isError': True,
                                 'content': [{'type': 'text', 'text': 'Sala inexistente: x'}]}]
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
