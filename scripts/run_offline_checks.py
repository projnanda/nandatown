"""Run repository tests with external networking and live-model smokes disabled.

Loopback remains available for the repository's synthetic Lab/Track HTTP tests.
Even a caught external connection attempt makes the final exit unsuccessful.
"""
import ipaddress
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
attempts = []
real_connect = socket.socket.connect
real_connect_ex = socket.socket.connect_ex
real_getaddrinfo = socket.getaddrinfo


def local(host):
    if host in (None, 'localhost', b'localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check(host):
    if not local(host):
        attempts.append('external network attempt')
        raise AssertionError('External network forbidden during offline checks')


def connect(sock, address):
    check(address[0])
    return real_connect(sock, address)


def connect_ex(sock, address):
    check(address[0])
    return real_connect_ex(sock, address)


def getaddrinfo(host, *args, **kwargs):
    if host in ('', b''):
        # Malformed endpoint tests must get the normal resolution error, without
        # asking the OS resolver to interpret an empty hostname.
        raise socket.gaierror(socket.EAI_NONAME, 'Empty host')
    check(host)
    return real_getaddrinfo(host, *args, **kwargs)


if __name__ == '__main__':
    for name in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY'):
        os.environ.pop(name, None)
    os.environ['NANDATOWN_RUN_LIVE_DEEPSEEK'] = '0'
    os.environ.pop('NANDATOWN_PIGUARD_SNAPSHOT', None)
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    import pytest
    args = sys.argv[1:] or [str(ROOT / 'tests'), '-q']
    selection = 'not manual_live_deepseek_end_to_end and not playwright_reads_clean_local_fixture'
    if '-k' in args:
        index = args.index('-k')
        selection = f'({args[index + 1]}) and ({selection})'
        del args[index:index + 2]
    code = pytest.main([*args, '-k', selection])
    print(f'External network attempts: {len(attempts)}')
    raise SystemExit(1 if attempts else code)
