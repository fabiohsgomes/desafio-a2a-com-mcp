"""Validacao MCP 01–20 e retry entre processos reais, sem iniciar o agente A2A.

Execute: .venv/bin/python test/verificar_http.py
"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('validador', ROOT / 'validador/validar.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
PORT = int(os.environ.get('PORT', '7301'))
URL = f'http://127.0.0.1:{PORT}/mcp'


def iniciar(env, log):
    process = subprocess.Popen([sys.executable, str(ROOT / 'servidor-mcp/servidor.py')],
                               env=env, stdout=subprocess.DEVNULL, stderr=log)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('Servidor terminou durante startup; confira a porta e o stderr')
            try:
                urllib.request.urlopen(URL, timeout=0.2).close()
                return process
            except urllib.error.HTTPError:
                return process  # Endpoint pronto, GET nao e necessario no transporte stateless.
            except urllib.error.URLError:
                time.sleep(0.05)
        raise RuntimeError('Timeout iniciando servidor')
    except BaseException:
        parar(process)
        raise


def parar(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    env = {**os.environ, 'REQUEST_STATE_SECRET': secrets.token_hex(32), 'PORT': str(PORT),
           'HOST': '127.0.0.1'}
    with tempfile.TemporaryFile(mode='w+') as log:
        process = iniciar(env, log)
        try:
            validator.verificar_mcp(URL)
        finally:
            parar(process)
        if validator.R.falhou:
            raise AssertionError(f'{validator.R.falhou} checks MCP falharam')
        process = iniciar(env, log)
        args = dict(sala='sala-garagem', inicio=validator.h('14:00'),
                    fim=validator.h('15:00'), responsavel='Marty')
        try:
            _, body = validator.reservar(URL, **args)
            pending = body['result']
            key = next(iter(pending['inputRequests']))
        finally:
            parar(process)
        process = iniciar(env, log)
        try:
            _, body = validator.retomar(URL, **args, chave=key,
                resposta={'action': 'accept', 'content': {'sala': 'sala-fusca'}},
                estado=pending['requestState'])
            result = body['result']
            assert result['resultType'] == 'complete' and not result['isError'], body
            assert result['structuredContent']['sala'] == 'sala-fusca', body
            print('PASS retry apos reiniciar o processo com o mesmo segredo')
        finally:
            parar(process)
        log.seek(0)
        received = []
        for line in log:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and 'method' in item:
                received.append(item)
        assert received[0]['method'] == 'tools/list', received[0]
        assert any(r['method'] == 'tools/call' and r['traceparent'] == validator.TRACEPARENT
                   for r in received), 'traceparent ausente'
        assert all('id' in r and 'traceparent' in r for r in received)
        print('PASS stderr com metodo, id, traceparent e tools/list antes de tools/call')
    print('20/20 checks MCP PASS; restart e observabilidade PASS')


if __name__ == '__main__':
    main()
