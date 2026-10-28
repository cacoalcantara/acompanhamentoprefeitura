"""_pid_vivo: deteccao de processo vivo sem sinal (no Windows os.kill(pid, 0) e CTRL_C_EVENT)."""
import os
import subprocess
import sys

from robowin.execucao.ponte_gateway import _pid_vivo


def test_proprio_processo_esta_vivo():
    assert _pid_vivo(os.getpid())


def test_processo_encerrado_esta_morto():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not _pid_vivo(p.pid)


def test_pid_invalido():
    assert not _pid_vivo(0)
    assert not _pid_vivo(-1)


def test_filho_vivo_e_nao_e_interrompido():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _pid_vivo(p.pid)
        assert p.poll() is None  # continua rodando (nenhum Ctrl+C/kill enviado)
    finally:
        p.kill()
        p.wait()
