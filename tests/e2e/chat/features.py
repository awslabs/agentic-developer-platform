"""Resolve the deployed Chat flag without changing it or printing credentials."""
import requests

from .helpers import CLOUDFRONT_URL, fetch_test_credentials, get_cognito_tokens


def read_chat_enabled():
    tokens = get_cognito_tokens(fetch_test_credentials())
    response = requests.get(
        CLOUDFRONT_URL.rstrip('/') + '/api/features',
        headers={'Authorization': 'Bearer ' + tokens['access_token']},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f'Features read returned HTTP {response.status_code}')
    enabled = response.json().get('features', {}).get('chat')
    if not isinstance(enabled, bool):
        raise RuntimeError('Live chat flag is missing or not boolean')
    return enabled


if __name__ == '__main__':
    print('on' if read_chat_enabled() else 'off')
