"""Real installed SDK imports in a fresh interpreter, without platform access."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_actual_apify_sdk_and_actor_entrypoints_import_with_required_billing_contract():
    root = Path(__file__).resolve().parents[1]
    # No actor/API/wallet credentials or configuration are inherited by this probe.
    environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
    environment.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    program = r'''
import asyncio
import importlib.metadata as metadata
import inspect
import json
import runpy
import socket

# Windows implements asyncio's local self-pipe using a loopback socket pair.
# Create that plumbing first, then forbid every subsequent network connection.
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

def forbidden(*args, **kwargs):
    raise AssertionError("Runtime import must not initialize an Actor or connect externally")

socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden

from apify import Actor
from apify._actor import _ActorType
from apify._charging import ActorPricingInfo, ChargingManagerImplementation

assert Actor is not None
checks = {
    "charged_push": "charged_event_name" in inspect.signature(_ActorType.push_data).parameters,
    "pricing_fields": {"is_pay_per_event", "per_event_prices"} <= set(ActorPricingInfo.__annotations__),
    "budget_api": callable(ChargingManagerImplementation.calculate_max_event_charge_count_within_limit),
    "pricing_api": callable(ChargingManagerImplementation.get_pricing_info),
}
assert all(checks.values()), checks
_ActorType.init = forbidden
_ActorType.__aenter__ = forbidden
_ActorType.charge = forbidden
_ActorType.push_data = forbidden

actor_module = runpy.run_path("apify/apify_main.py", run_name="runtime_contract_probe")
dispatcher = runpy.run_path("entrypoint.py", run_name="entrypoint_import_probe")
assert callable(actor_module["main"]) and callable(dispatcher["run"])
print(json.dumps({"checks": checks, "actor_initialized": False, "external_network_calls": 0,
    "versions": {name: metadata.version(name) for name in ("apify", "apify-client", "apify-shared", "crawlee", "browserforge", "pydantic")}}))
loop.close()
'''
    result = subprocess.run([sys.executable, "-c", program], cwd=root, env=environment,
                            capture_output=True, text=True, timeout=40, check=False)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert all(evidence["checks"].values())
    assert evidence["actor_initialized"] is False
    assert evidence["external_network_calls"] == 0
