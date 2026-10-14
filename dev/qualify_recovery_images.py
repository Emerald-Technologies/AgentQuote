"""Qualify approved local images with committed synthetic fixtures; no network.

Inputs: Git240b64a tests/ad03cd9 budget and three local images. Output: assertions.
Run python dev/qualify_recovery_images.py. No image rebuild, push or live dispatch.
"""
import base64
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {
    "api": ("agentquote-recovery-api:240b64a-20261005-01", "sha256:6f30b783151d035472b88816949b8be8a57ea704e1896bee913232b73a78d050"),
    "author": ("agentquote-recovery-author:240b64a-20261005-01", "sha256:e1abe74b3cc70abf0ba6754cdf4519b0f61c1f55dca4db3f4a908806e5e63aae"),
    "validator": ("agentquote-recovery-validator:240b64a-20261005-01", "sha256:c66dbc07df96739caead7de3fe26f49088a670018b61475d2c67a12d91351006"),
}
SCRIPT = r'''
import ast,base64,sys,types,json
for name,encoded in INPUTS.items():
    tree=ast.parse(base64.b64decode(encoded))
    tree.body=[node for node in tree.body if not(isinstance(node,ast.Import) and any(alias.name=='pytest' for alias in node.names)) and not(isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_'))]
    module=types.ModuleType(name);sys.modules[name]=module;exec(compile(tree,name,'exec'),module.__dict__)
from test_author_recovery import failed_refinement
from bom_processor.author_recovery import reserve_recovery,recovery_projection
from bom_processor.author_budget import begin_execution,complete
from bom_processor.package_writer_gate import package_writer,PackageWriterBusy
from bom_processor.author_recovery_api import RecoveryApi
from types import SimpleNamespace
from unittest.mock import Mock
storage,budget,authority=failed_refinement();before=dict(storage.registry)
claim=reserve_recovery(budget,authority);assert claim['status']=='reserved' and claim['slot']==3
assert all(storage.registry[p]==v for p,v in before.items())
assert reserve_recovery(budget,authority)['reason']=='budget_recovery_consumed'
old=types.ModuleType('bom_processor.old_image_check');old.__package__='bom_processor'
exec(compile(base64.b64decode(OLD),'historical-budget','exec'),old.__dict__)
args=(storage,claim['group_id'],claim['slot'],budget.request_id,claim['reservation_hash'])
assert old.Budget(storage,budget.request_id,clock=budget.clock).inspect()['reason']=='budget_invalid'
prior=dict(storage.registry)
try:old.begin_execution(*args)
except old.BudgetError:pass
else:raise AssertionError('old runtime accepted')
assert storage.registry==prior
budget.mark_dispatch(claim);budget.mark_started(claim,'offline-image-execution')
owner=begin_execution(*args);complete(*args,'completed','replacement-image-candidate',execution_owner=owner)
assert recovery_projection(budget,authority)['outcome']=='completed'
with package_writer(storage,authority['plan_id'],'approval'):
    try:
        with package_writer(storage,authority['plan_id'],'recovery'):raise AssertionError('crossed gate')
    except PackageWriterBusy:pass
store2,budget2,auth2=failed_refinement();claim2=reserve_recovery(budget2,auth2);budget2.mark_dispatch(claim2);budget2.mark_unknown(claim2)
assert budget2.inspect()['reason']=='budget_start_unknown' and reserve_recovery(budget2,auth2)['reason']=='budget_recovery_consumed'
assert not RecoveryApi(None,None).enabled
if ROLE == 'api':
    config=SimpleNamespace(auth_mode='entra',allowed_app_ids=frozenset({'test-app'}),allowed_object_ids=frozenset())
    factory=Mock(side_effect=AssertionError('service must not construct'));api=RecoveryApi(config,factory)
    headers={'X-MS-CLIENT-PRINCIPAL':base64.b64encode(json.dumps({'claims':[{'typ':'appid','val':'test-app'}]}).encode()).decode()}
    assert api.handle({},b'{')[0]==401
    assert api.handle(headers,b'{')[0]==403
    assert api.lookup(headers,b'{')[0]==403
    factory.assert_not_called()
    enabled=RecoveryApi(config,factory,enabled=True)
    assert enabled.handle(headers,b'x'*8193)[0]==413
    assert enabled.lookup(headers,b'x'*8193)[0]==413
    service=Mock();service.lookup.return_value={'status':'hold','reason':'budget_recovery_consumed'}
    query=RecoveryApi(config,lambda:service,enabled=True)
    assert query.lookup(headers,b'{"operator_id":"test-admin","recovery":{}}')[0]==200
    service.lookup.assert_called_once();service.recover.assert_not_called()
print('PASS: '+ROLE+' common recovery contracts'+(' plus API auth/lookup/body bounds' if ROLE=='api' else ' (API-only checks excluded)'))
'''


def main():
    inputs = {name: base64.b64encode(subprocess.check_output(
        ["git", "show", f"240b64a:{path}"], cwd=ROOT)).decode()
        for name, path in [("test_author_budget", "tests/test_author_budget.py"),
                           ("test_author_recovery", "tests/test_author_recovery.py")]}
    old = base64.b64encode(subprocess.check_output(
        ["git", "show", "ad03cd9:bom_processor/author_budget.py"], cwd=ROOT)).decode()
    for role, (image, expected) in IMAGES.items():
        actual = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()
        if actual != expected:
            raise RuntimeError("RETAINED_IMAGE_MISMATCH")
        script = "INPUTS=" + repr(inputs) + "\nOLD=" + repr(old) + "\nROLE=" + repr(role) + "\n" + SCRIPT
        result = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python", expected, "-"],
            input=script, text=True, capture_output=True, timeout=60)
        print(image, result.stdout.strip())
        if result.returncode != 0:
            raise RuntimeError(result.stderr)


if __name__ == "__main__":
    main()