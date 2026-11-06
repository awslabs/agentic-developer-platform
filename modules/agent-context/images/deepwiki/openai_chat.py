"""Keep DeepWiki's OpenAI chat consumer on the Chat Completions protocol."""


async def respond_stream(self, prompt: str):
    # AdalFlow's LLM acall now returns Responses API events. DeepWiki's chat
    # consumer expects choices/delta chunks; call that SDK endpoint explicitly.
    # Embeddings and RAG retain their existing AdalFlow methods and parsers.
    if self.client.async_client is None:
        self.client.async_client = self.client.init_async_client()
    response = await self.client.async_client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        **self.model_kwargs,
    )
    try:
        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta is not None:
                content = chunk.choices[0].delta.content
                if content is not None:
                    yield content
    finally:
        await response.close()
