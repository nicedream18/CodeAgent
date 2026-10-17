The offline context estimator uses the `cl100k_base` mergeable ranks distributed
by OpenAI tiktoken, with the accompanying MIT license in `TIKTOKEN_LICENSE`.
The pattern is taken from the installed tiktoken 0.12.0 `cl100k_base` definition.

Upstream data: https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken

SHA256: `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`

The file is bundled and checked before use. Runtime estimation never downloads
tokenizer data. This estimates serialized messages and tool schemas; it is not
the exact tokenizer or billing usage of any compatible provider. Existing
safety/output reserves remain in effect; provider usage is accounted separately.
