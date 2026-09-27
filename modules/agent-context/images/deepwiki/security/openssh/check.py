"""Benign SSH/Git compatibility using disposable credentials and loopback only."""
import subprocess
import sys
import uuid

client_image, builder_image = sys.argv[1:3]
name = "adp-openssh-fixture-" + uuid.uuid4().hex[:12]
volume = name + "-data"
server_setup = r'''
set -eu
useradd -u 10001 -M -d /fixture/home -s /bin/sh appuser
usermod -p x appuser
getent passwd sshd >/dev/null || useradd -r -M -d /run/sshd -s /usr/sbin/nologin sshd
mkdir -p /run/sshd /usr/lib/openssh /fixture/home/.ssh
chmod 0755 /run/sshd /fixture
chmod 0700 /fixture/home /fixture/home/.ssh
cp /src/openssh/debian/build-deb/sshd-auth /src/openssh/debian/build-deb/sshd-session /usr/lib/openssh/
keygen=/src/openssh/debian/build-deb/ssh-keygen
"$keygen" -q -t ed25519 -N '' -f /fixture/host_ed25519
"$keygen" -q -t ed25519 -N '' -f /fixture/home/.ssh/id_ed25519
cp /fixture/home/.ssh/id_ed25519.pub /fixture/home/.ssh/authorized_keys
printf '[127.0.0.1]:2222 ' > /fixture/home/.ssh/known_hosts
cat /fixture/host_ed25519.pub >> /fixture/home/.ssh/known_hosts
cat > /fixture/client.conf <<'CONF'
Host fixture
 HostName 127.0.0.1
 Port 2222
 User appuser
 IdentityFile /fixture/home/.ssh/id_ed25519
 IdentityAgent none
 IdentitiesOnly yes
 UserKnownHostsFile /fixture/home/.ssh/known_hosts
 StrictHostKeyChecking yes
 BatchMode yes
 ConnectTimeout 5
CONF
cat > /fixture/sshd_config <<'CONF'
Port 2222
ListenAddress 127.0.0.1
HostKey /fixture/host_ed25519
PidFile /fixture/sshd.pid
AuthorizedKeysFile .ssh/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
PermitRootLogin no
UsePAM no
AllowUsers appuser
LogLevel VERBOSE
Subsystem sftp /src/openssh/debian/build-deb/sftp-server
CONF
mkdir /fixture/seed
git -C /fixture/seed init -q -b main
printf 'synthetic SSH transport fixture\n' > /fixture/seed/README.txt
git -C /fixture/seed add README.txt
git -C /fixture/seed -c user.name=Fixture -c user.email=fixture@example.invalid commit -qm initial
git clone -q --bare /fixture/seed /fixture/remote.git
chown -R 10001:10001 /fixture/home /fixture/seed /fixture/remote.git
chmod 0600 /fixture/home/.ssh/authorized_keys
touch /fixture/ready
exec /src/openssh/debian/build-deb/sshd -D -e -f /fixture/sshd_config
'''
client_check = r'''
import hashlib, json, os, subprocess, time
from pathlib import Path
assert os.getuid() == 10001
home = Path('/fixture/home')
env = dict(os.environ, HOME=str(home), GIT_CONFIG_NOSYSTEM='1',
           GIT_CONFIG_GLOBAL='/dev/null', GIT_SSH_COMMAND='/usr/bin/ssh -F /fixture/client.conf')
def run(args, data=None, expect=0):
    r = subprocess.run(args, input=data, capture_output=True, env=env, timeout=45)
    assert r.returncode == expect, (args, r.returncode, r.stderr.decode(errors='replace'))
    return r.stdout
ssh = ['/usr/bin/ssh', '-F', '/fixture/client.conf']
for attempt in range(60):
    r = subprocess.run(ssh + ['fixture', 'true'], capture_output=True, env=env, timeout=8)
    if r.returncode == 0: break
    time.sleep(.25)
else: raise AssertionError(('local server unavailable', r.stderr.decode()))
algorithms = run(['ssh', '-Q', 'key']).decode().splitlines()
assert all(k in algorithms for k in ['ssh-ed25519','ssh-rsa','ecdsa-sha2-nistp256'])
for kind, extra in [('ed25519', []), ('rsa', ['-b','2048','-m','PEM']), ('ecdsa',['-b','256'])]:
    path = home / ('key-' + kind)
    run(['ssh-keygen','-q','-t',kind,*extra,'-N','','-f',str(path)])
    assert run(['ssh-keygen','-y','-f',str(path)]).split()[0].decode() in algorithms
    run(['ssh-keygen','-lf',str(path)+'.pub'])
encrypted = home/'encrypted'
run(['ssh-keygen','-q','-t','ed25519','-N','fixture-passphrase','-f',str(encrypted)])
run(['ssh-keygen','-y','-P','fixture-passphrase','-f',str(encrypted)])
assert subprocess.run(['ssh-keygen','-y','-P','wrong','-f',str(encrypted)],capture_output=True).returncode != 0
invalid=home/'invalid-key'; invalid.write_text('not a key\n'); invalid.chmod(0o600)
assert subprocess.run(['ssh-keygen','-y','-f',str(invalid)],capture_output=True).returncode != 0
payload=b'ordinary rekey fixture\n'*16384
assert run(ssh+['-o','RekeyLimit=64K','fixture','cat'],payload)==payload
run(['git','clone','ssh://fixture/fixture/remote.git',str(home/'clone')])
assert (home/'clone/README.txt').read_text()=='synthetic SSH transport fixture\n'
(home/'clone/second.txt').write_text('local fixture change\n')
run(['git','-C',str(home/'clone'),'add','second.txt'])
run(['git','-C',str(home/'clone'),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','second'])
run(['git','-C',str(home/'clone'),'push','origin','HEAD:main'])
run(['git','-C',str(home/'clone'),'fetch','origin'])
local=home/'transport.txt';local.write_text('benign transfer\n')
run(['scp','-F','/fixture/client.conf',str(local),'fixture:/fixture/home/scp.txt'])
run(['sftp','-F','/fixture/client.conf','-b','-','fixture'], b'get /fixture/home/scp.txt /fixture/home/sftp.txt\n')
assert (home/'sftp.txt').read_bytes()==local.read_bytes()
wrong=home/'wrong_known_hosts';wrong.write_text('[127.0.0.1]:2222 '+(home/'key-ed25519.pub').read_text())
r=subprocess.run(ssh+['-o','UserKnownHostsFile='+str(wrong),'fixture','true'],capture_output=True,env=env,timeout=10)
assert r.returncode != 0 and b'HOST IDENTIFICATION HAS CHANGED' in r.stderr
r=subprocess.run(['ssh','-F','/dev/null','-o','NotARealOption=yes','-G','fixture.invalid'],capture_output=True)
assert r.returncode != 0
print(json.dumps({'uid':os.getuid(),'ssh_sha256':hashlib.sha256(Path('/usr/bin/ssh').read_bytes()).hexdigest(),
 'key_types':['ed25519','RSA PEM','ECDSA'],'encrypted_key_and_invalid_key_refusal':'passed',
 'ordinary_rekey':'passed','git_clone_fetch_push':'passed','scp_sftp_roundtrip':'passed',
 'strict_known_host_refusal':'passed','invalid_config_refusal':'passed','network':'private loopback in network-none namespace',
 'credentials':'new disposable synthetic keys only','live_acceptance':False}))
'''
def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, **kwargs)
try:
    docker('volume', 'create', '--label', 'adp.security.fixture=openssh-critical', volume,
           capture_output=True)
    docker('run', '-d', '--name', name, '--network', 'none', '--mount',
           f'type=volume,src={volume},dst=/fixture', '--entrypoint', '/bin/sh',
           builder_image, '-c', server_setup, capture_output=True)
    result = docker('run', '--rm', '-i', '--network', f'container:{name}', '--read-only',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--tmpfs', '/tmp:rw,nosuid,nodev,mode=1777', '--mount',
                    f'type=volume,src={volume},dst=/fixture', '-e', 'HOME=/fixture/home',
                    '--entrypoint', 'python', client_image, '-',
                    input=client_check, capture_output=True, text=True)
    print(result.stdout, end='')
except subprocess.CalledProcessError as error:
    if error.stdout:
        print(error.stdout, file=sys.stderr)
    if error.stderr:
        print(error.stderr, file=sys.stderr)
    subprocess.run(['docker','logs',name], check=False)
    raise
finally:
    subprocess.run(['docker','rm','-f',name], check=False, capture_output=True)
    subprocess.run(['docker','volume','rm',volume], check=False, capture_output=True)
