"""Contrato HTTP e regras de negocio do servidor central-de-salas."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import patch

import httpx2 as httpx


class AppClient:
    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://localhost")
        return await self.client.__aenter__()

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)
        await self.lifespan.__aexit__(*args)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'servidor-mcp'))
from servidor import criar_app

VERSION = '2026-07-28'
CAPS = {'elicitation': {'form': {}}}


def horario(hora, offset='-03:00'):
    return f'2026-11-03T{hora}:00{offset}'


def argumentos(sala='sala-garagem', inicio='14:00', fim='15:00'):
    return dict(sala=sala, inicio=horario(inicio), fim=horario(fim), responsavel='Marty')


class ServidorTest(unittest.TestCase):
    def setUp(self):
        self.secret = secrets.token_hex(32)

    def _callTestMethod(self, method):
        async def run():
            async with AppClient(criar_app(self.secret)) as client:
                self.client = client
                await method()
        asyncio.run(run())

    async def request(self, method, params=None, caps=None, omit=None, headers=None, client=None):
        meta = {'io.modelcontextprotocol/protocolVersion': VERSION,
                'io.modelcontextprotocol/clientCapabilities': CAPS if caps is None else caps,
                'traceparent': '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'}
        if omit:
            meta.pop('io.modelcontextprotocol/' + omit)
        params = {**(params or {}), '_meta': meta}
        transport = {'MCP-Protocol-Version': VERSION, 'Mcp-Method': method,
                     'Accept': 'application/json, text/event-stream'}
        if method in ('tools/call', 'resources/read'):
            transport['Mcp-Name'] = params.get('name', params.get('uri'))
        transport.update(headers or {})
        response = await (client or self.client).post('/mcp', json={
            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, headers=transport)
        self.assertNotIn('mcp-session-id', response.headers)
        return response.status_code, response.json()

    async def call(self, name, args=None, **kwargs):
        return await self.request('tools/call', {'name': name, 'arguments': args or {}}, **kwargs)

    async def pending(self, args=None):
        args = args or argumentos()
        _, body = await self.call('reservar_sala', args)
        result = body['result']
        self.assertEqual(result['resultType'], 'input_required')
        self.assertEqual(len(result['inputRequests']), 1)
        return args, result, next(iter(result['inputRequests']))

    async def retry(self, args, result, key, action='accept', sala='sala-fusca', **kwargs):
        return await self.request('tools/call', {'name': 'reservar_sala', 'arguments': args,
            'requestState': result['requestState'], 'inputResponses': {
                key: {'action': action, **({'content': {'sala': sala}} if action == 'accept' else {})}}}, **kwargs)

    async def test_discovery_and_serialized_content(self):
        _, discovery = await self.request('server/discover')
        self.assertIn('tools', discovery['result']['capabilities'])
        self.assertIn('resources', discovery['result']['capabilities'])
        _, body = await self.request('tools/list')
        expected = json.loads((Path(__file__).resolve().parents[1] /
            'exemplos/wire/01-tools-list.json').read_text())['response']['body']['result']['tools']
        self.assertEqual(body['result']['tools'], expected)
        _, body = await self.call('listar_salas')
        result = body['result']
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
        self.assertEqual(len(result['structuredContent']['salas']), 5)

    async def test_required_meta_on_every_request(self):
        for method, params in [('tools/list', {}), ('tools/call', {'name': 'listar_salas'}),
                               ('resources/read', {'uri': 'politica://uso'})]:
            for field in ('protocolVersion', 'clientCapabilities'):
                with self.subTest(method=method, field=field):
                    status, body = await self.request(method, params, omit=field)
                    self.assertEqual((status, body['error']['code']), (400, -32602))

    async def test_headers_match_body(self):
        for header, value in [('Mcp-Method', 'resources/read'), ('Mcp-Name', 'outra'),
                              ('MCP-Protocol-Version', '2025-11-25')]:
            with self.subTest(header=header):
                _, body = await self.call('listar_salas', headers={header: value})
                self.assertEqual(body['error']['code'], -32020)

    async def test_resource_literal_and_unknown_targets(self):
        _, body = await self.request('resources/read', {'uri': 'politica://uso'})
        resource = body['result']['contents'][0]
        self.assertEqual(resource['mimeType'], 'text/markdown')
        self.assertEqual(resource['text'], (Path(__file__).resolve().parents[1] /
            'dados/politica-de-uso.md').read_text())
        _, body = await self.request('resources/read', {'uri': 'politica://inexistente'})
        self.assertEqual(body['error']['code'], -32602)
        _, body = await self.call('inexistente')
        self.assertTrue(body.get('error', {}).get('code') == -32602 or
                        body.get('result', {}).get('isError'))

    async def test_validation_shared_by_both_tools(self):
        cases = [('sala-delorean', '09:00', '10:00', 'Sala inexistente: sala-delorean'),
            ('sala-aquario', '07:00', '08:00', 'Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00'),
            ('sala-aquario', '19:00', '20:01', 'Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00'),
            ('sala-aquario', '09:00', '12:00', 'Duracao acima do limite: a politica permite no maximo 2 horas'),
            ('sala-aquario', '10:00', '09:00', 'Intervalo invalido: fim deve ser posterior a inicio'),
            ('sala-aquario', '10:00', '10:00', 'Intervalo invalido: fim deve ser posterior a inicio')]
        for tool in ('reservar_sala', 'consultar_disponibilidade'):
            for sala, inicio, fim, message in cases:
                with self.subTest(tool=tool, message=message):
                    args = argumentos(sala, inicio, fim)
                    if tool == 'consultar_disponibilidade':
                        args.pop('responsavel')
                    _, body = await self.call(tool, args)
                    self.assertEqual(body['result']['resultType'], 'complete')
                    self.assertTrue(body['result']['isError'])
                    self.assertIn(message, body['result']['content'][0]['text'])

    async def test_reservation_visible_and_adjacent_interval_free(self):
        args = argumentos('sala-aquario', '08:00', '10:00')
        _, body = await self.call('reservar_sala', args)
        result = body['result']['structuredContent']
        self.assertEqual(result['reserva'], 'res-0003')
        self.assertEqual(result['politica'], '2026-11-01')
        self.assertEqual(set(result), {'reserva', 'reservado', 'sala', 'inicio', 'fim', 'responsavel', 'politica', 'motivo'})
        args.pop('responsavel')
        _, body = await self.call('consultar_disponibilidade', args)
        self.assertFalse(body['result']['structuredContent']['livre'])
        self.assertEqual(body['result']['structuredContent']['conflitos'][0]['id'], 'res-0003')
        args.update(inicio=horario('10:00'), fim=horario('11:00'))
        _, body = await self.call('consultar_disponibilidade', args)
        self.assertTrue(body['result']['structuredContent']['livre'])

    async def test_alternatives_sorted_limited_and_capability_specific(self):
        _, result, key = await self.pending()
        elicitation = result['inputRequests'][key]['params']
        self.assertEqual(elicitation['mode'], 'form')
        self.assertEqual(elicitation['requestedSchema']['properties']['sala']['enum'], ['sala-fusca', 'sala-mirante'])
        for caps in ({}, {'elicitation': {}}, {'elicitation': {'url': {}}}):
            status, body = await self.call('reservar_sala', argumentos(), caps=caps)
            self.assertEqual((status, body['error']['code']), (400, -32021))
            self.assertEqual(body['error']['data']['requiredCapabilities'], CAPS)
        args = argumentos('sala-aquario', '09:00', '10:00')
        await self.call('reservar_sala', args)
        _, result, key = await self.pending(args)
        self.assertEqual(result['inputRequests'][key]['params']['requestedSchema']['properties']['sala']['enum'],
                         ['sala-porao', 'sala-fusca', 'sala-garagem'])

    async def test_restart_retry_and_no_pending_reservation(self):
        args, result, key = await self.pending()
        async with AppClient(criar_app(self.secret)) as restarted:
            _, body = await self.retry(args, result, key, client=restarted)
        self.assertTrue(body['result']['structuredContent']['reservado'])
        self.assertEqual(body['result']['structuredContent']['sala'], 'sala-fusca')
        query = {k: v for k, v in args.items() if k != 'responsavel'}
        query['sala'] = 'sala-fusca'
        _, body = await self.call('consultar_disponibilidade', query)
        self.assertTrue(body['result']['structuredContent']['livre'])

    async def test_tampering_expiry_wrong_tool_and_invalid_answers(self):
        args, result, key = await self.pending()
        tampered = {**result, 'requestState': result['requestState'][:-8] + 'AAAAAAAA'}
        _, body = await self.retry(args, tampered, key)
        self.assertEqual(body['error']['code'], -32602)
        _, body = await self.retry({**args, 'responsavel': 'Biff'}, result, key)
        self.assertEqual(body['error']['code'], -32602)
        with patch('mcp.server.request_state.time.time', return_value=10**12):
            _, body = await self.retry(args, result, key)
        self.assertEqual(body['error']['code'], -32602)
        _, body = await self.request('tools/call', {'name': 'listar_salas', 'arguments': {},
            'requestState': result['requestState'], 'inputResponses': {key: {'action': 'decline'}}})
        self.assertEqual(body['error']['code'], -32602)
        for wrong_key, sala in [(key, 'sala-aquario'), ('outra-chave', 'sala-fusca')]:
            _, body = await self.retry(args, result, wrong_key, sala=sala)
            self.assertTrue('error' in body or body.get('result', {}).get('isError'))

    async def test_decline_cancel_and_recheck_conflict(self):
        args, result, key = await self.pending()
        for action in ('decline', 'cancel'):
            _, body = await self.retry(args, result, key, action=action)
            self.assertFalse(body['result']['isError'])
            self.assertFalse(body['result']['structuredContent']['reservado'])
            self.assertTrue(body['result']['structuredContent']['motivo'])
        await self.call('reservar_sala', {**args, 'sala': 'sala-fusca'})
        _, body = await self.retry(args, result, key)
        self.assertTrue(body['result']['isError'])

    async def test_no_alternatives_does_not_require_capability(self):
        args = argumentos('sala-mirante', '11:00', '12:00')
        await self.call('reservar_sala', args)
        _, body = await self.call('reservar_sala', args, caps={})
        self.assertTrue(body['result']['isError'])
        self.assertIn('Sem alternativas disponiveis no intervalo', body['result']['content'][0]['text'])

    async def test_offset_conversion_and_malformed_datetime(self):
        args = argumentos('sala-aquario', '09:00', '10:00')
        args.update(inicio=horario('11:00', '+00:00'), fim=horario('13:00', '+00:00'))
        _, body = await self.call('reservar_sala', args)
        self.assertTrue(body['result']['structuredContent']['reservado'])
        for value in ('amanha', '2026-11-03T09:00:00'):
            _, body = await self.call('reservar_sala', {**args, 'inicio': value})
            self.assertTrue(body['result']['isError'])

    async def test_logs_include_method_id_traceparent_even_invalid_meta(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            await self.request('tools/list')
            await self.request('tools/list', omit='protocolVersion')
        logs = captured.getvalue()
        self.assertGreaterEqual(logs.count('tools/list'), 2)
        self.assertIn('4bf92f3577b34da6a3ce929d0e0e4736', logs)
        self.assertIn('"id": 1', logs)

    async def test_secret_is_required_and_minimum_length(self):
        for value in ('', 'curto'):
            with self.assertRaises(ValueError):
                criar_app(value)


if __name__ == '__main__':
    unittest.main()
