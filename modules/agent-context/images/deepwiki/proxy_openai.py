"""Honor the SDK proxy setting when DeepWiki constructs an AdalFlow client."""

import inspect
import os

from adalflow.components.model_client.openai_client import OpenAIClient as AdalFlowOpenAIClient


class OpenAIClient(AdalFlowOpenAIClient):
    def __init__(self, *args, **kwargs):
        # AdalFlow passes its public default explicitly to the SDK, overriding
        # OPENAI_BASE_URL. Preserve explicit caller URLs, including positional
        # arguments, and use the deployment setting only for omitted URLs.
        supplied = inspect.signature(AdalFlowOpenAIClient.__init__).bind_partial(
            self, *args, **kwargs
        )
        proxy = os.environ.get("OPENAI_BASE_URL")
        if proxy and "base_url" not in supplied.arguments:
            kwargs["base_url"] = proxy
        super().__init__(*args, **kwargs)
