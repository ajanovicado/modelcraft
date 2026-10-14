"""Qwen 4B context budget: references and old history never displace the current request."""

from .contract_ollama import ContractOllamaModelRuntime


class ProfileOllamaRuntime(ContractOllamaModelRuntime):
    def _messages(
        self,
        system_prompt: str,
        user_prompt: str,
        refs: str,
        history: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        messages = super()._messages(system_prompt, user_prompt, refs, history)
        if not self._fits(messages) and refs:
            # Extra runtime examples must not displace the recent conversation.
            messages[-1]["content"] = user_prompt
        # Drop whole oldest exchanges, never user/schema text or half an answer.
        # The evidence records the exact prompt and whether references fit.
        while len(messages) > 2 and not self._fits(messages):
            messages.pop(1)
            while len(messages) > 2 and messages[1]["role"] == "assistant":
                messages.pop(1)
        return messages

    @classmethod
    def _fits(cls, messages: list[dict[str, str]], max_tokens: int = 512) -> bool:
        return cls._prompt_bytes(messages) + max_tokens + max(128, 64 * len(messages)) <= 4096

    def _repair_messages(
        self,
        messages: list[dict[str, str]],
        output: str,
        error: object,
        max_tokens: int,
    ) -> list[dict[str, str]]:
        base = [dict(message) for message in messages]
        while len(base) > 2:
            base.pop(1)
        feedback = (
            "Correct the validation error; return only the required JSON: " + str(error)[:500]
        )
        excerpt = output[:512]
        while True:
            repair = {"role": "user", "content": feedback}
            if excerpt:
                repair["content"] += "\nPrevious output excerpt (may be incomplete):\n" + excerpt
            result = [*base, repair]
            if self._fits(result, max_tokens):
                return result
            if excerpt:
                excerpt = excerpt[: len(excerpt) // 2]
            elif len(feedback) > 100:
                feedback = feedback[: max(100, len(feedback) // 2)]
            else:
                from .runtime import LocalModelError

                raise LocalModelError("Context budget cannot fit a repair; narrow the request")

    @staticmethod
    def _prompt_bytes(messages: list[dict[str, str]]) -> int:
        return sum(len(m["content"].encode("utf-8")) for m in messages)
