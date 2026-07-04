def test_missing_api_keys_default_to_none():
    from app.config import Settings

    s = Settings()
    # These were referenced in code but never defined -> AttributeError at runtime.
    assert s.serper_api_key is None
    assert s.apify_api_key is None
    assert s.gmail_token_path is None
    assert s.gmail_credentials_path is None
    assert s.gmail_label is None


def test_apply_dry_run_defaults_false():
    from app.config import Settings

    assert Settings().apply_dry_run is False
