"""Inicia ambos os processos novos, valida 01–36 e inspeciona logs MCP/A2A."""
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


def iniciar(command, env, log, url):
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=log)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('Processo terminou durante startup')
            try:
                urllib.request.urlopen(url, timeout=0.2).close()
                return process
            except urllib.error.HTTPError:
                return process
            except urllib.error.URLError:
                time.sleep(0.05)
        raise RuntimeError('Timeout iniciando processo')
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


def registros(log):
    log.seek(0)
    items = []
    for line in log:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and 'method' in item:
            items.append(item)
    return items


def main():
    mcp_port = int(os.environ.get('MCP_TEST_PORT', '7301'))
    agent_port = int(os.environ.get('AGENTE_TEST_PORT', '7300'))
    mcp_url = f'http://127.0.0.1:{mcp_port}/mcp'
    agent_url = f'http://127.0.0.1:{agent_port}'
    with tempfile.TemporaryFile(mode='w+') as mcp_log, tempfile.TemporaryFile(mode='w+') as agent_log:
        env = {**os.environ, 'REQUEST_STATE_SECRET': secrets.token_hex(32),
               'PORT': str(mcp_port), 'HOST': '127.0.0.1'}
        server = iniciar([sys.executable, 'servidor-mcp/servidor.py'], env, mcp_log, mcp_url)
        try:
            agent = iniciar([sys.executable, '-m', 'agente.main'],
                {**os.environ, 'AGENTE_PORT': str(agent_port), 'AGENTE_HOST': '127.0.0.1',
                 'MCP_URL': mcp_url, 'A2A_PATH': '/a2a'}, agent_log,
                agent_url + '/.well-known/agent-card.json')
            try:
                token = validator.verificar_mcp(mcp_url)
                validator.verificar_a2a(agent_url, agent_url + '/a2a', token)
            finally:
                parar(agent)
        finally:
            parar(server)
        if validator.R.falhou:
            agent_log.seek(0)
            print(agent_log.read(), file=sys.stderr)
            raise AssertionError(f'{validator.R.falhou} checks falharam')
        emitted = [r for r in registros(agent_log) if r.get('protocolo') == 'MCP']
        received = registros(mcp_log)
        assert emitted[0]['method'] == 'tools/list', emitted[0]
        assert len({r['id'] for r in emitted}) == len(emitted), 'IDs MCP repetidos'
        traced = [r for r in emitted if r['traceparent'] == validator.TRACEPARENT]
        assert {'tools/list', 'resources/read', 'tools/call'} <= {r['method'] for r in traced}
        assert all(any(s['id'] == r['id'] and s['method'] == r['method'] and
                       s['traceparent'] == r['traceparent'] for s in received) for r in emitted)
        print('PASS stderr: descoberta antes das tools, IDs distintos e trace propagado ao servidor')
        print(f'PASS traceparent observado: {validator.TRACEPARENT}')
        print('36/36 checks PASS')


if __name__ == '__main__':
    main()
