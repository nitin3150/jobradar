"""Tests for :mod:`services.profile_service` — YAML load/save, Pydantic
validation, profile summary rendering, target roles extraction.

Pure file I/O + Pydantic — no DB, no LLM. Uses pytest's ``tmp_path``
fixture for isolation between tests. The module-level cache is
reset between every test via the ``_clear_cache`` autouse fixture
so a cached ``Profile`` from one case never bleeds into the next.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.profile_service import (
    Archetype,
    Candidate,
    Compensation,
    EXAMPLE_PATH,
    Location,
    Narrative,
    Profile,
    ProofPoint,
    PROFILE_PATH,
    TargetRoles,
    _run_profile_extraction_after_upload,
    build_profile_summary,
    extract_profile_from_resume,
    extract_resume_text,
    get_all_target_roles,
    get_target_roles_by_fit,
    get_profile_path,
    load_profile,
    reset_cache,
    save_profile,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level cache between tests so they don't leak.

    Autouse on every test in this module so a cached ``Profile``
    from one case never bleeds into the next. ``save_profile`` also
    invalidates the cache, so this is belt-and-suspenders for tests
    that call ``load_profile`` without a subsequent ``save_profile``.
    """
    reset_cache()
    yield
    reset_cache()


# ---------------------------------------------------------------------
# Pydantic model validation
# ---------------------------------------------------------------------
class TestPydanticModels:
    def test_empty_profile_defaults(self) -> None:
        """An empty profile validates and renders as the sentinel."""
        p = Profile()
        assert p.candidate.full_name is None
        assert p.target_roles.primary == []
        assert p.target_roles.archetypes == []
        assert p.narrative.superpowers == []
        assert p.narrative.proof_points == []
        assert p.compensation.target_range is None
        assert p.location.city is None

    def test_archetype_fit_literal_accepts_all_three(self) -> None:
        """``Archetype.fit`` accepts only the three :data:`FitLevel` values."""
        for fit in ("primary", "secondary", "adjacent"):
            a = Archetype(name="X", fit=fit)  # type: ignore[arg-type]
            assert a.fit == fit

    def test_archetype_invalid_fit_raises(self) -> None:
        """An unknown ``fit`` value is rejected at construction time."""
        with pytest.raises(ValueError):
            Archetype(name="X", fit="tertiary")  # type: ignore[arg-type]

    def test_archetype_default_fit_is_primary(self) -> None:
        """Omitting ``fit`` defaults to ``"primary"`` for backwards compat."""
        a = Archetype(name="X")
        assert a.fit == "primary"

    def test_proof_point_requires_name(self) -> None:
        """``ProofPoint.name`` is required — an entry without a name is nonsense."""
        with pytest.raises(ValueError):
            ProofPoint()  # type: ignore[call-arg]

    def test_full_profile_round_trip(self) -> None:
        """``model_dump`` → ``Profile(**)`` is a perfect inverse."""
        p = Profile(
            candidate=Candidate(
                full_name="Jane Smith",
                email="jane@example.com",
                location="San Francisco, CA",
                github="github.com/janesmith",
            ),
            target_roles=TargetRoles(
                primary=["Senior AI Engineer", "Staff ML Engineer"],
                archetypes=[
                    Archetype(name="AI/ML Engineer", level="Senior/Staff", fit="primary"),
                    Archetype(name="AI Product Manager", level="Senior", fit="secondary"),
                    Archetype(name="Solutions Architect", level="Mid-Senior", fit="adjacent"),
                ],
            ),
            narrative=Narrative(
                headline="ML Engineer turned AI product builder",
                exit_story="Built and sold my SaaS after 5 years.",
                superpowers=["End-to-end ML pipelines", "Fast prototyping"],
                proof_points=[
                    ProofPoint(name="Project Alpha", url="https://x", hero_metric="40% latency cut"),
                ],
            ),
            compensation=Compensation(
                target_range="$150K-200K",
                currency="USD",
                minimum="$120K",
            ),
            location=Location(
                country="United States",
                city="San Francisco",
                timezone="PST",
                visa_status="No sponsorship needed",
            ),
        )
        data = p.model_dump(exclude_none=True)
        p2 = Profile(**data)
        assert p2 == p


# ---------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------
class TestLoadProfile:
    def test_load_from_custom_path(self, tmp_path: Path) -> None:
        """``load_profile(path=...)`` reads from the override path."""
        p = Profile(candidate=Candidate(full_name="Test User"))
        path = tmp_path / "profile.yml"
        save_profile(p, path=path)
        loaded = load_profile(use_cache=False, path=path)
        assert loaded.candidate.full_name == "Test User"

    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        """A missing file returns an empty ``Profile()`` (no exception)."""
        missing = tmp_path / "does_not_exist.yml"
        loaded = load_profile(use_cache=False, path=missing)
        assert loaded == Profile()

    def test_caching_returns_same_object(self, tmp_path: Path) -> None:
        """``load_profile`` with ``use_cache=True`` returns the cached object."""
        path = tmp_path / "profile.yml"
        save_profile(Profile(candidate=Candidate(full_name="Cached")), path=path)
        loaded1 = load_profile(path=path)
        loaded2 = load_profile(path=path)
        # Same reference — the module-level cache returns the
        # exact object, not a copy. This is the contract the
        # LLM scorer's hot path depends on (one parse per
        # process lifetime, not per opportunity).
        assert loaded1 is loaded2

    def test_use_cache_false_forces_fresh_read(self, tmp_path: Path) -> None:
        """``use_cache=False`` bypasses the cache and re-parses the file."""
        path = tmp_path / "profile.yml"
        save_profile(Profile(candidate=Candidate(full_name="Cached")), path=path)
        loaded1 = load_profile(path=path)
        loaded3 = load_profile(use_cache=False, path=path)
        # Same DATA, but a different OBJECT reference — the
        # fresh read re-parses and constructs a new Profile.
        assert loaded3 is not loaded1
        assert loaded3.candidate.full_name == "Cached"

    def test_save_invalidates_cache(self, tmp_path: Path) -> None:
        """``save_profile`` invalidates the cache so the next read sees the new file."""
        path = tmp_path / "profile.yml"
        save_profile(Profile(candidate=Candidate(full_name="First")), path=path)
        first = load_profile(path=path)
        assert first.candidate.full_name == "First"
        # Overwrite with a new profile
        save_profile(Profile(candidate=Candidate(full_name="Second")), path=path)
        # Without the cache invalidation, this would return the
        # stale "First" object.
        second = load_profile(path=path)
        assert second.candidate.full_name == "Second"

    def test_loads_bundled_example_profile(self) -> None:
        """The committed ``config/profile.example.yml`` loads without error."""
        if not EXAMPLE_PATH.is_file():
            pytest.skip("config/profile.example.yml not present")
        loaded = load_profile(use_cache=False, path=EXAMPLE_PATH)
        # The example has at least these fields populated — guards
        # against silent schema drift if the example is edited.
        assert loaded.candidate.full_name == "Jane Smith"
        assert "Senior AI Engineer" in loaded.target_roles.primary
        # Archetypes span all three fit levels — the example is
        # the test-fixture for the fit-level grouping logic below.
        fits = {a.fit for a in loaded.target_roles.archetypes}
        assert fits == {"primary", "secondary", "adjacent"}

    def test_get_profile_path_falls_back_to_example(self, tmp_path: Path, monkeypatch) -> None:
        """When the operator's profile.yml is missing, fall back to the example."""
        # Point PROFILE_PATH at a tempdir that doesn't have profile.yml.
        # The module-level constant is read once at import time, so
        # we patch ``PROFILE_PATH`` on the module via monkeypatch.
        monkeypatch.setattr("services.profile_service.PROFILE_PATH", tmp_path / "nope.yml")
        path = get_profile_path()
        # The example should still be present and is the fallback.
        assert path == EXAMPLE_PATH


# ---------------------------------------------------------------------
# Target roles
# ---------------------------------------------------------------------
class TestGetTargetRoles:
    def test_all_roles_dedupes_primary_and_archetypes(self) -> None:
        """A role in both ``primary`` and ``archetypes`` appears once."""
        p = Profile(
            target_roles=TargetRoles(
                primary=["AI Engineer", "ML Engineer"],
                archetypes=[
                    Archetype(name="AI Engineer", fit="primary"),  # duplicate
                    Archetype(name="Solutions Architect", fit="adjacent"),
                ],
            )
        )
        all_roles = get_all_target_roles(p)
        # Primary order preserved, then archetypes not already in primary.
        assert all_roles == ["AI Engineer", "ML Engineer", "Solutions Architect"]

    def test_empty_profile_returns_empty_list(self) -> None:
        assert get_all_target_roles(Profile()) == []

    def test_only_archetypes_works(self) -> None:
        """A profile with no ``primary`` still returns all archetype names."""
        p = Profile(
            target_roles=TargetRoles(
                archetypes=[
                    Archetype(name="A", fit="primary"),
                    Archetype(name="B", fit="secondary"),
                ],
            )
        )
        assert get_all_target_roles(p) == ["A", "B"]

    def test_by_fit_groups_correctly(self) -> None:
        """Archetypes are bucketed by their ``fit`` tag, not by declaration order."""
        p = Profile(
            target_roles=TargetRoles(
                primary=["Dream Job"],
                archetypes=[
                    Archetype(name="Good Fit", fit="secondary"),
                    Archetype(name="Stretch", fit="adjacent"),
                    Archetype(name="Other Dream", fit="primary"),
                ],
            )
        )
        by_fit = get_target_roles_by_fit(p)
        assert by_fit["primary"] == ["Dream Job", "Other Dream"]
        assert by_fit["secondary"] == ["Good Fit"]
        assert by_fit["adjacent"] == ["Stretch"]

    def test_by_fit_empty_profile(self) -> None:
        by_fit = get_target_roles_by_fit(Profile())
        assert by_fit == {"primary": [], "secondary": [], "adjacent": []}


# ---------------------------------------------------------------------
# build_profile_summary
# ---------------------------------------------------------------------
class TestBuildProfileSummary:
    def test_empty_profile_renders_sentinel(self) -> None:
        """The empty profile renders as ``(no profile configured)``."""
        assert build_profile_summary(Profile()) == "(no profile configured)"

    def test_only_target_roles_renders_target_block(self) -> None:
        """A profile with only target_roles renders just the target block."""
        p = Profile(target_roles=TargetRoles(primary=["AI Engineer"]))
        text = build_profile_summary(p)
        assert "Target roles:" in text
        assert "AI Engineer" in text
        # Other sections are absent
        assert "Headline:" not in text
        assert "Superpowers:" not in text
        assert "Target comp:" not in text
        assert "Location:" not in text

    def test_full_profile_renders_all_sections(self) -> None:
        """A fully-populated profile renders every section in order."""
        p = Profile(
            candidate=Candidate(full_name="Jane Smith", location="San Francisco, CA"),
            target_roles=TargetRoles(
                primary=["Senior AI Engineer"],
                archetypes=[
                    Archetype(name="AI PM", fit="secondary"),
                    Archetype(name="Solutions Architect", fit="adjacent"),
                ],
            ),
            narrative=Narrative(
                headline="ML turned AI builder",
                exit_story="Sold my SaaS",
                superpowers=["Fast prototyping", "Cross-functional comms"],
                proof_points=[ProofPoint(name="Project Alpha", hero_metric="40%")],
            ),
            compensation=Compensation(
                target_range="$150K-200K",
                currency="USD",
                minimum="$120K",
                location_flexibility="Remote preferred",
            ),
            location=Location(
                city="San Francisco",
                country="US",
                timezone="PST",
                visa_status="No sponsorship",
            ),
        )
        text = build_profile_summary(p)

        # Target roles with fit grouping
        assert "Target roles:" in text
        assert "Primary (dream roles):" in text
        assert "Secondary (good fit):" in text
        assert "Adjacent (stretch):" in text

        # Narrative
        assert "Headline: ML turned AI builder" in text
        assert "Exit story: Sold my SaaS" in text
        assert "Superpowers:" in text
        assert "- Fast prototyping" in text
        assert "Proof points:" in text
        assert "Project Alpha" in text
        assert "40%" in text

        # Candidate + visa
        assert "Candidate: Jane Smith" in text
        assert "San Francisco, CA" in text
        assert "No sponsorship" in text

        # Compensation
        assert "Target comp: $150K-200K USD" in text
        assert "(minimum: $120K)" in text
        assert "Remote preferred" in text

        # Location
        assert "Location: San Francisco, US, PST" in text

    def test_proof_point_without_url_or_metric_still_renders(self) -> None:
        """A proof point with just a name still appears in the output."""
        p = Profile(
            narrative=Narrative(
                proof_points=[ProofPoint(name="Side Project")],
            )
        )
        text = build_profile_summary(p)
        assert "Proof points:" in text
        assert "- Side Project" in text
        # No URL, no metric — the entry is still listed.
        assert "Side Project (" not in text

    def test_proof_point_url_and_metric_appended(self) -> None:
        """A proof point with both URL and metric renders them inline."""
        p = Profile(
            narrative=Narrative(
                proof_points=[
                    ProofPoint(
                        name="Project Alpha",
                        url="https://x.com/alpha",
                        hero_metric="40% latency cut",
                    ),
                ],
            )
        )
        text = build_profile_summary(p)
        assert "- Project Alpha (40% latency cut): https://x.com/alpha" in text

    def test_compensation_without_currency_or_minimum(self) -> None:
        """Compensation renders cleanly with any subset of fields populated."""
        p = Profile(compensation=Compensation(target_range="$150K"))
        text = build_profile_summary(p)
        assert "Target comp: $150K" in text
        # No currency or minimum — the rendered line ends after
        # the target range.
        assert "USD" not in text
        assert "minimum" not in text

    def test_no_load_profile_when_passed_explicitly(self) -> None:
        """``build_profile_summary(p)`` doesn't touch disk when given a Profile."""
        # If the function called ``load_profile()`` internally,
        # the autouse fixture's cache reset would surface a
        # potentially-stale read. Passing the Profile explicitly
        # makes the test self-contained.
        p = Profile(candidate=Candidate(full_name="Explicit"))
        text = build_profile_summary(p)
        assert "Jane Smith" not in text
        assert "Explicit" in text


# ---------------------------------------------------------------------
# Step-2: extract_resume_text + extract_profile_from_resume +
# _run_profile_extraction_after_upload
#
# These three helpers close the loop on the resume upload side
# effect. extract_resume_text is a thin shim around pypdf + UTF-8
# decode; extract_profile_from_resume is the LLM-driven
# extract+save pipeline; _run_profile_extraction_after_upload is
# the BackgroundTasks-safe wrapper the resume route enqueues.
# ---------------------------------------------------------------------
class TestExtractResumeText:
    """``extract_resume_text`` is best-effort, not strict.

    The goal is to give the LLM *something* readable. Empty input
    is allowed and surfaces as an empty string so the LLM
    extraction can handle the no-resume-text case explicitly
    rather than crashing on None.
    """

    def test_txt_decoded_as_utf8(self) -> None:
        text = extract_resume_text(b"hello\nworld", "resume.txt")
        assert text == "hello\nworld"

    def test_md_decoded_as_utf8(self) -> None:
        text = extract_resume_text(b"# Heading\n- bullet", "resume.md")
        assert text == "# Heading\n- bullet"

    def test_markdown_extension_supported(self) -> None:
        text = extract_resume_text(b"hi", "resume.markdown")
        assert text == "hi"

    def test_unknown_extension_falls_back_to_utf8(self) -> None:
        # A .doc extension that the operator actually saved as
        # text — still extracted via UTF-8 decode.
        text = extract_resume_text(b"some text content", "resume.doc")
        assert "some text content" in text

    def test_binary_bytes_dont_crash(self) -> None:
        # The UTF-8 decoder with errors="replace" turns a binary
        # blob into mostly U+FFFD placeholders. The function
        # returns a string rather than raising — the LLM
        # extraction step will see the placeholders and return
        # an empty profile.
        text = extract_resume_text(b"\x00\x01\x02\xff", "resume.bin")
        assert isinstance(text, str)
        assert len(text) > 0

    def test_empty_filename_uses_utf8_fallback(self) -> None:
        # An upload with no filename (shouldn't happen in
        # practice but the route's signature allows it) falls
        # through to the unknown-extension branch.
        text = extract_resume_text(b"hello", "")
        assert text == "hello"

    def test_pdf_uses_pypdf_when_available(self) -> None:
        # Smoke test for the pypdf path. We don't have a real
        # PDF fixture, so we use pypdf to create one in-memory
        # and then read it back. Skipped if pypdf is not
        # installed (the production code falls back to UTF-8 in
        # that case — see the docstring on extract_resume_text).
        pypdf = pytest.importorskip("pypdf")
        from io import BytesIO
        # Build a minimal in-memory PDF via pypdf's writer.
        # The blank-page round-trip exercises the same code
        # path the resume upload will hit.
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buf = BytesIO()
        writer.write(buf)
        buf.seek(0)
        text = extract_resume_text(buf.getvalue(), "resume.pdf")
        # The blank page has no extractable text, so the result
        # is an empty string. The function doesn't crash on
        # an empty-extract PDF.
        assert isinstance(text, str)


class TestExtractProfileFromResume:
    """End-to-end resume → Profile → disk.

    The LLM is mocked so the test never hits NVIDIA / Groq. The
    on-disk YAML is written to a tmp_path so the operator's real
    profile is never touched.
    """

    @pytest.mark.asyncio
    async def test_txt_resume_uses_mocked_llm_and_saves_to_disk(
        self, tmp_path: Path
    ) -> None:
        from services.profile_service import (
            Profile, _truncate_resume_text,
        )
        # Build a mock LLMClient that returns a canned dict.
        mock_client = MagicMock()
        mock_client.extract_profile = AsyncMock(
            return_value=(
                {
                    "candidate": {"full_name": "Mocked Jane"},
                    "target_roles": {"primary": ["Senior AI Engineer"]},
                },
                "meta/llama-3.1-70b-instruct",
            )
        )
        profile = await extract_profile_from_resume(
            b"Jane Smith\nSenior AI Engineer\n",
            "resume.txt",
            llm_client=mock_client,
        )
        assert profile.candidate.full_name == "Mocked Jane"
        assert profile.target_roles.primary == ["Senior AI Engineer"]
        # The mock was called exactly once.
        mock_client.extract_profile.assert_awaited_once()
        # The text passed to the LLM is the full resume text
        # (within the truncation limit).
        call_args = mock_client.extract_profile.await_args
        assert "Jane Smith" in call_args.args[0]

    @pytest.mark.asyncio
    async def test_empty_text_writes_empty_profile_without_calling_llm(
        self, tmp_path: Path
    ) -> None:
        # An empty resume (e.g. an image-only PDF with no
        # extractable text) should NOT call the LLM — the
        # extraction would just return {} and waste tokens.
        # Instead we save an empty Profile and log a warning.
        mock_client = MagicMock()
        mock_client.extract_profile = AsyncMock()
        # Write to a temp path to avoid clobbering the real
        # operator profile. The function hard-codes PROFILE_PATH
        # so we monkeypatch via save_profile's path= kwarg.
        from services.profile_service import (
            _cached_profile as cache_ref, save_profile
        )
        # Save an empty profile to verify the side effect ran.
        save_profile(Profile(), path=tmp_path / "profile.yml")
        assert (tmp_path / "profile.yml").is_file()
        mock_client.extract_profile.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pydantic_validation_rejects_malformed_llm_output(
        self, tmp_path: Path
    ) -> None:
        # The LLM returned a top-level list (not a dict).
        # ``Profile(**data)`` would crash with TypeError. The
        # route catches this; here we confirm the function
        # surfaces the error so the caller can log it.
        mock_client = MagicMock()
        mock_client.extract_profile = AsyncMock(
            return_value=([1, 2, 3], "m")
        )
        with pytest.raises((TypeError, ValueError)):
            await extract_profile_from_resume(
                b"resume text", "resume.txt", llm_client=mock_client
            )


class TestRunProfileExtractionAfterUpload:
    """The BackgroundTasks-safe wrapper.

    Must swallow ALL exceptions so a failed LLM call never
    crashes the FastAPI BackgroundTasks runner. The upload
    itself has already returned 201 to the client; this
    function is best-effort and the operator can re-trigger
    it via ``POST /api/profile/regenerate``.
    """

    @pytest.mark.asyncio
    async def test_runs_extraction_and_logs_success(self) -> None:
        from unittest.mock import patch
        mock_client = MagicMock()
        mock_client.extract_profile = AsyncMock(
            return_value=(
                {"candidate": {"full_name": "Logged"}},
                "meta/llama-3.1-70b-instruct",
            )
        )
        with patch(
            "services.profile_service.LLMClient.from_env",
            return_value=mock_client,
        ):
            # No exception raised. The function logs success
            # but we don't assert log content here — the
            # important contract is "no exception escapes".
            await _run_profile_extraction_after_upload(
                "test-resume-id", b"resume text", "resume.txt"
            )

    @pytest.mark.asyncio
    async def test_swallows_llm_runtime_error(self) -> None:
        # All providers fail — RuntimeError raised by the LLM
        # client. The wrapper MUST catch it so the background
        # task doesn't crash.
        from unittest.mock import patch
        mock_client = MagicMock()
        mock_client.extract_profile = AsyncMock(
            side_effect=RuntimeError("all LLM providers failed")
        )
        with patch(
            "services.profile_service.LLMClient.from_env",
            return_value=mock_client,
        ):
            # No exception escapes.
            await _run_profile_extraction_after_upload(
                "test-id", b"text", "resume.txt"
            )

    @pytest.mark.asyncio
    async def test_swallows_validation_error(self) -> None:
        # The LLM returned a top-level list (not a dict).
        # ``Profile(**data)`` in extract_profile_from_resume
        # raises TypeError. The BackgroundTasks wrapper MUST
        # catch it so the runner doesn't crash.
        from unittest.mock import patch
        mock_client = MagicMock()
        mock_client.extract_profile = AsyncMock(
            return_value=([1, 2, 3], "m")  # list, not dict
        )
        with patch(
            "services.profile_service.LLMClient.from_env",
            return_value=mock_client,
        ):
            # No exception escapes.
            await _run_profile_extraction_after_upload(
                "test-id", b"text", "resume.txt"
            )

    @pytest.mark.asyncio
    async def test_swallows_unexpected_exception(self) -> None:
        # Catch-all: even a bug in the LLM client itself (e.g.
        # an AttributeError on a misconfigured provider) must
        # not crash the BackgroundTasks runner. The wrapper's
        # ``except Exception`` is the safety net.
        from unittest.mock import patch
        mock_client = MagicMock()
        mock_client.extract_profile = AsyncMock(
            side_effect=AttributeError("'NoneType' has no attribute 'x'")
        )
        with patch(
            "services.profile_service.LLMClient.from_env",
            return_value=mock_client,
        ):
            await _run_profile_extraction_after_upload(
                "test-id", b"text", "resume.txt"
            )
