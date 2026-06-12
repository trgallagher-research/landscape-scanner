"""Tests for key handling: resolution order, live-run gating, and the
gitignore safety check on writes.

These tests use injected environments and temp directories ONLY — they
never read or write the developer's real keys.
"""

import pytest

from scanner.keys import KNOWN_PROVIDERS, ProviderKeys, parse_env_file


def make_keys(tmp_path, env=None, env_file_text=None):
    """Helper: build ProviderKeys against a temp .env and injected environ."""
    env_file = tmp_path / ".env"
    if env_file_text is not None:
        env_file.write_text(env_file_text, encoding="utf-8")
    return ProviderKeys(env_file=env_file, environ=env or {})


def test_reads_keys_from_env_file(tmp_path):
    keys = make_keys(tmp_path, env_file_text="SERPER_API_KEY=abc123\n")
    assert keys.has("serper")
    assert not keys.has("brave")


def test_environment_beats_env_file(tmp_path):
    """Real environment variables take priority over the file."""
    keys = make_keys(
        tmp_path,
        env={"SERPER_API_KEY": "from-env"},
        env_file_text="SERPER_API_KEY=from-file\n",
    )
    assert keys.get("serper") == "from-env"


def test_missing_for_live_run_blocks_without_required_keys(tmp_path):
    """No serper/anthropic keys -> a live run must refuse to start."""
    keys = make_keys(tmp_path)
    missing = keys.missing_for_live_run()
    assert "serper" in missing
    assert "anthropic" in missing


def test_live_run_allowed_with_required_keys(tmp_path):
    keys = make_keys(
        tmp_path,
        env={"SERPER_API_KEY": "s", "ANTHROPIC_API_KEY": "a"},
    )
    assert keys.missing_for_live_run() == []


def test_save_key_refuses_without_gitignore(tmp_path):
    """Writing a key without .gitignore protection must fail loudly."""
    keys = make_keys(tmp_path)
    with pytest.raises(RuntimeError, match="gitignore"):
        keys.save_key("serper", "new-key")


def test_save_key_writes_when_gitignored(tmp_path):
    """With .env in .gitignore, saving works and the key resolves."""
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    keys = make_keys(tmp_path)
    keys.save_key("serper", "new-key")
    # Re-load from disk to prove it persisted
    reloaded = ProviderKeys(env_file=tmp_path / ".env", environ={})
    assert reloaded.has("serper")


def test_save_key_preserves_other_keys(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    keys = make_keys(tmp_path, env_file_text="BRAVE_API_KEY=keep-me\n")
    keys.save_key("serper", "new-key")
    values = parse_env_file(tmp_path / ".env")
    assert values["BRAVE_API_KEY"] == "keep-me"
    assert values["SERPER_API_KEY"] == "new-key"


def test_repr_never_leaks_values(tmp_path):
    """The string form shows set/missing status only, never key material."""
    keys = make_keys(tmp_path, env={"SERPER_API_KEY": "supersecretvalue"})
    assert "supersecretvalue" not in repr(keys)


def test_known_providers_have_env_and_role():
    """Schema check on the provider registry itself."""
    for provider, info in KNOWN_PROVIDERS.items():
        assert info["env"].endswith("_API_KEY")
        assert info["role"]
