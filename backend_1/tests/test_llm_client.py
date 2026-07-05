# backend/tests/test_llm_client.py
from unittest.mock import MagicMock, patch
from app.llm.client import llm_complete


def test_llm_complete_passes_model_from_settings():
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "hello"

    with patch("app.llm.client.completion", return_value=mock_response) as mock_complete:
        result = llm_complete(messages=[{"role": "user", "content": "hi"}])

    mock_complete.assert_called_once()
    call_kwargs = mock_complete.call_args.kwargs
    assert "model" in call_kwargs
    assert result == "hello"


def test_llm_complete_returns_string():
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "test output"

    with patch("app.llm.client.completion", return_value=mock_response):
        result = llm_complete(messages=[{"role": "user", "content": "test"}])

    assert isinstance(result, str)
    assert result == "test output"
