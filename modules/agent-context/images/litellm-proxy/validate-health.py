"""Verify the upstream embedding health fix that replaces runtime source edits."""
from litellm.proxy.health_check import _update_litellm_params_for_health_check

for mode in ('embedding', 'image_generation', 'audio_transcription', 'moderation'):
    parameters = _update_litellm_params_for_health_check(
        {'mode': mode}, {'model': 'bedrock/amazon.titan-embed-text-v2:0'}
    )
    if 'max_tokens' in parameters:
        raise RuntimeError(f'non-chat health check injects max_tokens: {mode}')
chat = _update_litellm_params_for_health_check(
    {'mode': 'chat', 'health_check_max_tokens': 7},
    {'model': 'bedrock/anthropic.claude-sonnet-4-20250514-v1:0'},
)
if chat.get('max_tokens') != 7:
    raise RuntimeError('chat health check no longer preserves its explicit token bound')
print('Upstream health check supports embedding and bounded chat requests')
