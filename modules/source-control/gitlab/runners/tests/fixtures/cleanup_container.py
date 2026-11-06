import json,os,subprocess,sys
from pathlib import Path
mode=sys.argv[1]
Path("/fixture-scrub.sh").write_text(SCRUB_SOURCE)
paths=['/root/.aws/credentials','/root/.docker/config.json','/home/ec2-user/.aws/credentials','/home/ec2-user/.docker/config.json','/var/lib/amazon/ssm/identity','/var/log/amazon/ssm/output','/root/.ssh/authorized_keys','/home/ec2-user/.ssh/authorized_keys','/etc/ssh/ssh_host_rsa_key','/etc/machine-id','/var/lib/dbus/machine-id']
for name in paths:
 p=Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic-fixture')
if mode!='no_marker':Path('/run/adp-gitlab-image-builder').touch()
binpath=Path('/fixtures-bin');binpath.mkdir()
(binpath/'systemctl').write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open('/run/fixture-calls','a') as f:f.write(json.dumps(args)+'\\n')
if args==['is-active','--quiet','amazon-ssm-agent.service']:sys.exit(0 if os.environ['MODE']=='active' else 3)
if args==['--no-reload','mask','amazon-ssm-agent.service']:sys.exit(1 if os.environ['MODE']=='mask_fails' else 0)
sys.exit(99)
''')
(binpath/'cloud-init').write_text('''#!/usr/bin/env python3
import json,sys
with open('/run/fixture-calls','a') as f:f.write(json.dumps(['cloud-init',*sys.argv[1:]])+'\\n')
sys.exit(0 if sys.argv[1:]==['clean','--logs','--seed'] else 99)
''')
for p in binpath.iterdir():p.chmod(0o755)
env={**os.environ,'PATH':str(binpath)+':'+os.environ['PATH'],'MODE':mode}
result=subprocess.run(['bash','/fixture-scrub.sh'],env=env,capture_output=True,text=True)
record={'mode':mode,'returncode':result.returncode,'remaining':[p for p in paths if Path(p).exists()],'marker':Path('/var/lib/adp-image-build/sanitized').exists(),'calls':[json.loads(line) for line in Path('/run/fixture-calls').read_text().splitlines()] if Path('/run/fixture-calls').exists() else []}
machine_id = Path('/etc/machine-id')
record['fresh_machine_id_mountpoint'] = (machine_id.is_file() and not machine_id.is_symlink()
    and machine_id.read_bytes() == b'' and machine_id.stat().st_mode & 0o777 == 0o644)
print(json.dumps(record))
