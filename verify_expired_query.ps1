<#
  verify_expired_query.ps1
  验证：已审批且已过期的放行单，pass-list --status EXPIRED 能查到，APPROVED 查不到，
  同时待审批、已使用、已撤销结果不回退。
#>
param(
    [string]$Db = "$PSScriptRoot\_verify_expired.db"
)

$env:FSWITCH_ADMINS = "alice@local,bob@local"

if (Test-Path $Db) { Remove-Item $Db -Force }

$testScript = @"
import json, os, sys, tempfile
os.environ['FSWITCH_ADMINS'] = 'alice@local,bob@local'
sys.path.insert(0, r'$($PSScriptRoot.Replace('\','/'))')
from feature_switch.cli.main import cli

def run(*argv):
    import io, contextlib
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli(list(argv))
    return code, stdout.getvalue()

db = r'$($Db.Replace('\','\\'))'
fj = lambda *a: (lambda c,o: (c, json.loads(o) if o else {}))(*run('--db', db, '--format', 'json', *a))

ok = True
def check(cond, msg):
    global ok
    if not cond:
        ok = False
        print(f'[FAIL] {msg}')
    else:
        print(f'[ OK ] {msg}')

# 1. Create window template
code, data = fj('--as', 'alice@local', 'win-create', '--env', 'prod',
    '--time-range', '09:00:18:00:monday-friday', '--approver', 'bob@local')
check(code == 0, f'Create window template (code={code})')

# 2. Create expired pass (valid_until in past)
code, data = fj('--as', 'charlie@local', 'pass-create', '--env', 'prod',
    '--reason', 'expired-verify', '--valid-from', '2020-01-01T00:00:00',
    '--valid-until', '2020-01-01T23:59:59', '--approver', 'bob@local')
check(code == 0, f'Create expired pass (code={code})')
pass_id = data.get('pass', {}).get('pass_id', '')
check(pass_id != '', f'Got pass_id={pass_id}')

# 3. Submit & approve
code, _ = fj('--as', 'charlie@local', 'pass-submit', '--pass-id', pass_id)
check(code == 0, f'Submit expired pass (code={code})')
code, data = fj('--as', 'bob@local', 'pass-approve', '--pass-id', pass_id)
check(code == 0, f'Approve expired pass (code={code})')
check(data.get('status') == 'APPROVED', f'Status is APPROVED after approve')

# 4. pass-list --status EXPIRED should find it
code, data = fj('pass-list', '--status', 'EXPIRED')
check(code == 0, f'EXPIRED query ok (code={code})')
check(data.get('count') == 1, f'EXPIRED count=1 (actual={data.get("count")})')
if data.get('passes'):
    check(data['passes'][0]['pass_id'] == pass_id, f'EXPIRED pass_id matches')

# 5. pass-list --status APPROVED should NOT find it
code, data = fj('pass-list', '--status', 'APPROVED')
check(code == 0, f'APPROVED query ok (code={code})')
check(data.get('count') == 0, f'APPROVED count=0 (actual={data.get("count")})')

# 6. Create PENDING_APPROVAL pass
code, data = fj('--as', 'charlie@local', 'pass-create', '--env', 'prod',
    '--reason', 'pending-verify', '--valid-from', '2030-01-01T00:00:00',
    '--valid-until', '2030-01-01T23:59:59', '--approver', 'bob@local')
pending_id = data.get('pass', {}).get('pass_id', '')
fj('--as', 'charlie@local', 'pass-submit', '--pass-id', pending_id)

# 7. Create USED pass
code, data = fj('--as', 'charlie@local', 'pass-create', '--env', 'prod',
    '--reason', 'used-verify', '--valid-from', '2025-01-01T00:00:00',
    '--valid-until', '2099-12-31T23:59:59', '--approver', 'bob@local')
used_id = data.get('pass', {}).get('pass_id', '')
fj('--as', 'charlie@local', 'pass-submit', '--pass-id', used_id)
fj('--as', 'bob@local', 'pass-approve', '--pass-id', used_id)
fj('--as', 'charlie@local', 'pass-use', '--pass-id', used_id,
   '--order-id', 'REL-VERIFY-001', '--at', '2025-06-19T10:00:00')

# 8. Create CANCELLED pass
code, data = fj('--as', 'charlie@local', 'pass-create', '--env', 'prod',
    '--reason', 'cancelled-verify', '--valid-from', '2030-06-01T00:00:00',
    '--valid-until', '2030-06-01T23:59:59', '--approver', 'bob@local')
cancel_id = data.get('pass', {}).get('pass_id', '')
fj('--as', 'charlie@local', 'pass-submit', '--pass-id', cancel_id)
fj('--as', 'charlie@local', 'pass-cancel', '--pass-id', cancel_id,
   '--reason', 'not-needed')

# 9. Verify other statuses not regressed
code, data = fj('pass-list', '--status', 'PENDING_APPROVAL')
check(data.get('count') == 1, f'PENDING_APPROVAL count=1 (actual={data.get("count")})')

code, data = fj('pass-list', '--status', 'USED')
check(data.get('count') == 1, f'USED count=1 (actual={data.get("count")})')

code, data = fj('pass-list', '--status', 'CANCELLED')
check(data.get('count') == 1, f'CANCELLED count=1 (actual={data.get("count")})')

print()
if ok:
    print('========================================')
    print('  ALL EXPIRED-QUERY CHECKS PASSED')
    print('========================================')
else:
    print('========================================')
    print('  SOME CHECKS FAILED')
    print('========================================')
    sys.exit(1)
"@

$testScript | python -c "import sys; exec(sys.stdin.read())"

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "PowerShell wrapper: ALL CHECKS PASSED" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "PowerShell wrapper: SOME CHECKS FAILED" -ForegroundColor Red
}

if (Test-Path $Db) { Remove-Item $Db -Force }

exit $LASTEXITCODE
