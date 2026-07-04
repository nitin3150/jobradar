def test_optional_api_keys_default_to_none():
    from app.config import Settings

    s = Settings()
    assert s.serper_api_key is None
    assert s.apify_api_key is None


def test_gmail_paths_have_safe_string_defaults():
    from app.config import Settings

    s = Settings()
    # Must be strings, never None — app/gmail/connector.py calls os.path.exists()
    # on these and builds a label query from gmail_label.
    assert s.gmail_token_path == "gmail_token.json"
    assert s.gmail_credentials_path == "gmail_credentials.json"
    assert s.gmail_label == "job-applications"


def test_apply_dry_run_defaults_false():
    from app.config import Settings

    assert Settings().apply_dry_run is False
