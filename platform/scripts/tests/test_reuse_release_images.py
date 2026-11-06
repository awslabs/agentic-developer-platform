"""Image reuse must preserve bytes and refuse ambiguity before copying."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location('reuse', Path(__file__).parents[1] / 'reuse-release-images.py')
reuse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reuse)
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
SOURCE = '111111111111'
TARGET = '222222222222'


def identity(env, region, *args):
    assert args == ('sts', 'get-caller-identity')
    if env.get('AWS_PROFILE') == 'source':
        assert 'AWS_ACCESS_KEY_ID' not in env
        return {'Account': SOURCE}
    return {'Account': TARGET}


def test_source_credentials_are_isolated_copy_uses_digest_and_private_receipt():
    with tempfile.TemporaryDirectory() as directory, \
         patch.dict(os.environ, {'AWS_PROFILE': 'target', 'AWS_ACCESS_KEY_ID': 'private'}), \
         patch.object(reuse, 'aws', side_effect=identity), \
         patch.object(reuse, 'image', side_effect=[DIGEST, None, None, DIGEST]), \
         patch.object(reuse, 'run', return_value='password') as run:
        output = Path(directory) / 'receipt.json'
        reuse.reuse('source', TARGET, 'us-east-1', SHA, output, ('adp-gateway',))
        commands = [call.args[0] for call in run.call_args_list]
        copy = next(cmd for cmd in commands if cmd[:2] == ['skopeo', 'copy'])
        assert '--preserve-digests' in copy and '--all' in copy
        assert copy[-2].endswith('/adp-gateway@' + DIGEST)
        assert copy[-1].endswith('/adp-gateway:' + SHA)
        assert all('password' not in cmd for cmd in commands)
        receipt = json.loads(output.read_text())
        assert receipt['status'] == 'complete'
        assert receipt['images']['adp-gateway'] == {'digest': DIGEST, 'status': 'verified'}
        assert output.stat().st_mode & 0o777 == 0o600
        assert not Path(copy[copy.index('--src-authfile') + 1]).exists()


def test_conflicting_later_repository_stops_all_copies():
    with tempfile.TemporaryDirectory() as directory, \
         patch.object(reuse, 'aws', side_effect=identity), \
         patch.object(reuse, 'image', side_effect=[DIGEST, None, DIGEST, 'sha256:' + 'c' * 64]), \
         patch.object(reuse, 'run') as run:
        with pytest.raises(ValueError, match='different bytes'):
            reuse.reuse('source', TARGET, 'us-east-1', SHA, Path(directory) / 'receipt.json',
                        ('adp-gateway', 'adp-agent-runtime'))
        run.assert_not_called()


def test_matching_image_is_verified_without_copy():
    with tempfile.TemporaryDirectory() as directory, patch.object(reuse, 'aws', side_effect=identity), \
         patch.object(reuse, 'image', return_value=DIGEST), patch.object(reuse, 'run', return_value='password') as run:
        reuse.reuse('source', TARGET, 'us-east-1', SHA, Path(directory) / 'receipt.json', ('adp-gateway',))
        assert not any(call.args[0][:2] == ['skopeo', 'copy'] for call in run.call_args_list)


def test_copy_failure_retains_failed_receipt_and_removes_auth():
    auth = []
    def command(cmd, **kwargs):
        if cmd[:2] == ['skopeo', 'login']:
            auth.append(Path(cmd[cmd.index('--authfile') + 1]))
            auth[-1].write_text('private')
        if cmd[:2] == ['skopeo', 'copy']:
            raise RuntimeError('copy failed')
        return 'password'
    with tempfile.TemporaryDirectory() as directory, patch.object(reuse, 'aws', side_effect=identity), \
         patch.object(reuse, 'image', side_effect=[DIGEST, None, None]), patch.object(reuse, 'run', side_effect=command):
        output = Path(directory) / 'receipt.json'
        with pytest.raises(RuntimeError, match='copy failed'):
            reuse.reuse('source', TARGET, 'us-east-1', SHA, output, ('adp-gateway',))
        assert json.loads(output.read_text())['status'] == 'failed'
        assert auth and all(not path.exists() for path in auth)


@pytest.mark.parametrize('failure', ['AccessDeniedException', 'RepositoryNotFound', 'ImageNotFound'])
def test_only_missing_destination_image_is_optional(failure):
    with patch.object(reuse, 'aws', return_value={'images': [], 'failures': [{'failureCode': failure}]}):
        if failure == 'ImageNotFound':
            assert reuse.image({}, 'us-east-1', 'adp-gateway', SHA, optional=True) is None
        else:
            with pytest.raises(ValueError):
                reuse.image({}, 'us-east-1', 'adp-gateway', SHA, optional=True)
        with pytest.raises(ValueError):
            reuse.image({}, 'us-east-1', 'adp-gateway', SHA)


def test_wrong_target_stops_before_registry_access():
    with patch.object(reuse, 'aws', return_value={'Account': SOURCE}), patch.object(reuse, 'image') as image:
        with pytest.raises(ValueError, match='Target account changed'):
            reuse.reuse('source', TARGET, 'us-east-1', SHA, Path('unused'))
        image.assert_not_called()
