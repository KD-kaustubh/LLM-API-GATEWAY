from gateway.providers.base import Message, ProviderRequest
from gateway.providers.mock import MockProvider


def test_mock_echoes_last_user_message() -> None:
    request = ProviderRequest(
        messages=(
            Message(role="system", content="Be helpful"),
            Message(role="user", content="First question"),
            Message(role="assistant", content="First answer"),
            Message(role="user", content="Hello"),
        )
    )

    result = MockProvider().generate(request)

    assert result.provider == "mock"
    assert result.model == "mock"
    assert result.content == "Mock response: Hello"


def test_mock_usage_is_deterministic_word_count() -> None:
    request = ProviderRequest(messages=(Message(role="user", content="one two three"),))

    first = MockProvider().generate(request)
    second = MockProvider().generate(request)

    assert first == second
    assert first.usage.input_tokens == 3
    assert first.usage.output_tokens == 5
    assert first.usage.total_tokens == 8
