"""reliability: хард-таймаут на задачу (_max_time_sec) + network_scan валидация подсети.

Превент от часовых зависаний: задача не может крутиться дольше max_time (smart_chat
с keepalive больше не вешает надолго — wait_for срубает).
"""
from types import SimpleNamespace

from caesar.core.orchestrator import Orchestrator
from caesar.core.queue import TaskComplexity
from caesar.tools.shell_files import NetworkScanTool


def _orch():
    oc = SimpleNamespace(max_time_simple_min=10, max_time_medium_min=60, max_time_complex_min=240)
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = SimpleNamespace(orchestrator=oc)
    return orch


def test_max_time_sec_by_complexity():
    o = _orch()
    assert o._max_time_sec(TaskComplexity.SIMPLE) == 600
    assert o._max_time_sec(TaskComplexity.MEDIUM) == 3600
    assert o._max_time_sec(TaskComplexity.COMPLEX) == 14400


async def test_network_scan_rejects_injection_subnet():
    t = NetworkScanTool()
    r = await t.execute(subnet="10.42.0.0/24; rm -rf /")
    assert not r.success  # regex-валидация отбила, без subprocess


async def test_network_scan_accepts_valid_subnet():
    t = NetworkScanTool()
    # валидная подсеть проходит regex → идёт к subprocess (ip neigh + nmap).
    # На CI без nmap — nmap_sn_error, но success=True (ip neigh/arp отработал).
    r = await t.execute(subnet="10.42.0.0/24", timeout=5)
    assert r.success
