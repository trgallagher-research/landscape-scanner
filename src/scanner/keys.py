"""API key handling: load from environment / local .env, never from code.

Security rules this module enforces:

* Keys are read from environment variables first, then from a local ``.env``
  file in the project (or user-chosen) directory.
* Keys are WRITTEN only to that local ``.env`` file — and only after
  confirming the file is covered by ``.gitignore`` — so the key-manager UI
  can save keys without any risk of committing them.
* Key VALUES never appear in logs, reports, or exceptions. Status reporting
  is boolean only ("set" / "not set").
"""

from __future__ import annotations

from pathlib import Path

# The providers the scanner knows about, and the environment variable each
# one reads its key from. "required_for" notes what breaks without it.
KNOWN_PROVIDERS: dict[str, dict[str, str]] = {
    "serper": {"env": "SERPER_API_KEY", "role": "primary web search (Google-backed)"},
    "brave": {"env": "BRAVE_API_KEY", "role": "independent second search index"},
    "anthropic": {"env": "ANTHROPIC_API_KEY", "role": "verification + synthesis model (Claude)"},
    "openrouter": {"env": "OPENROUTER_API_KEY", "role": "economy extraction models (DeepSeek / Kimi)"},
}

# The minimum set without which a live (non-demo) run refuses to start.
# Brave is optional (cross-index agreement degrades, run still honest);
# OpenRouter is optional (economy profile falls back to Claude for everything).
REQUIRED_FOR_LIVE_RUN = ["serper", "anthropic"]


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=value`` .env file into a dict.

    Ignores blank lines and ``#`` comments. Strips optional surrounding
    quotes from values. Does not support multi-line values (keys never
    need them).
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        values[name.strip()] = value
    return values


class ProviderKeys:
    """Holds the resolved API keys for one run.

    Resolution order: real environment variables win, then the local
    ``.env`` file. The class never prints or returns key values in its
    string representation.
    """

    def __init__(self, env_file: Path | None = None, environ: dict[str, str] | None = None):
        """Load keys.

        Parameters
        ----------
        env_file:
            Path to a local ``.env`` file. Defaults to ``.env`` in the
            current working directory.
        environ:
            The environment mapping to read from. Defaults to ``os.environ``;
            injectable for tests so they never touch real keys.
        """
        import os

        self.env_file = env_file if env_file is not None else Path.cwd() / ".env"
        source_env = environ if environ is not None else dict(os.environ)
        file_values = parse_env_file(self.env_file)

        # Resolve each known provider: environment beats file.
        self._values: dict[str, str] = {}
        for provider, info in KNOWN_PROVIDERS.items():
            env_name = info["env"]
            value = source_env.get(env_name) or file_values.get(env_name) or ""
            if value:
                self._values[provider] = value

    def get(self, provider: str) -> str | None:
        """Return the key for a provider, or None if not set."""
        return self._values.get(provider)

    def has(self, provider: str) -> bool:
        """True if a non-empty key is set for this provider."""
        return bool(self._values.get(provider))

    def statuses(self) -> dict[str, bool]:
        """Boolean set/not-set status for every known provider (safe to display)."""
        return {provider: self.has(provider) for provider in KNOWN_PROVIDERS}

    def missing_for_live_run(self) -> list[str]:
        """Names of REQUIRED providers that have no key.

        A live run must refuse to start unless this returns an empty list —
        this is the rule that prevents silent fake-data contamination.
        """
        return [p for p in REQUIRED_FOR_LIVE_RUN if not self.has(p)]

    def save_key(self, provider: str, value: str) -> None:
        """Write or update one key in the local .env file.

        Refuses to write unless the .env file is protected by a .gitignore
        in the same directory tree (so a key can never be staged by
        accident). Preserves other lines in the file.
        """
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(f"Unknown provider: {provider}")
        value = value.strip()
        if not value:
            raise ValueError("Refusing to save an empty key.")
        if not self._env_file_is_gitignored():
            raise RuntimeError(
                f"Refusing to write a key: {self.env_file} is not covered by a "
                f".gitignore containing '.env'. Add it first."
            )

        env_name = KNOWN_PROVIDERS[provider]["env"]
        existing = parse_env_file(self.env_file)
        existing[env_name] = value

        # Rewrite the file from the parsed values. Comments are not
        # preserved (acceptable: this file holds only keys).
        lines = [f"{name}={val}" for name, val in existing.items()]
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._values[provider] = value

    def _env_file_is_gitignored(self) -> bool:
        """Check that some .gitignore at or above the .env location lists '.env'.

        A simple textual check (looks for a line equal to '.env' or '.env*'
        variants), which is what our own .gitignore uses. Conservative: if
        no .gitignore is found, returns False and saving is refused.
        """
        directory = self.env_file.parent
        for candidate_dir in [directory, *directory.parents]:
            gitignore = candidate_dir / ".gitignore"
            if gitignore.is_file():
                patterns = {
                    line.strip()
                    for line in gitignore.read_text(encoding="utf-8").splitlines()
                }
                if {".env", ".env*", "*.env", ".env.*"} & patterns:
                    return True
        return False

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        """Safe representation: provider names and set/not-set only."""
        status = ", ".join(f"{p}={'set' if ok else 'missing'}" for p, ok in self.statuses().items())
        return f"ProviderKeys({status})"
