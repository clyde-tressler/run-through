"""Pluggable LLM backends.

Every provider exposes complete(prompt, timeout) -> str and raises
ProviderUnavailable when it cannot serve. The "none" provider always raises,
which drops the app into fully-offline canned mode.
"""

import json
import os
import subprocess
import urllib.request


class ProviderUnavailable(Exception):
    pass


class NoneProvider:
    name = "none"

    def probe(self):
        return False

    def complete(self, prompt, timeout=20):
        raise ProviderUnavailable("offline mode")


class ClaudeCLIProvider:
    """Uses a locally-installed, already-authenticated `claude` CLI."""

    name = "claude-cli"

    def __init__(self, model=""):
        self.model = model or "sonnet"

    def probe(self):
        try:
            r = subprocess.run(
                ["claude", "-p", "Reply with exactly: ok", "--model", self.model],
                capture_output=True, text=True, timeout=30,
            )
            return r.returncode == 0 and "ok" in r.stdout.lower()
        except Exception:
            return False

    def complete(self, prompt, timeout=25):
        try:
            r = subprocess.run(
                ["claude", "-p", prompt, "--model", self.model],
                capture_output=True, text=True, timeout=timeout,
            )
        except Exception as e:
            raise ProviderUnavailable(str(e))
        if r.returncode != 0:
            raise ProviderUnavailable("claude CLI exit %s" % r.returncode)
        return r.stdout


class AnthropicAPIProvider:
    """Direct Messages API call; needs ANTHROPIC_API_KEY. No SDK required."""

    name = "anthropic-api"

    def __init__(self, model=""):
        self.model = model or "claude-sonnet-5"
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")

    def probe(self):
        return bool(self.key)

    def complete(self, prompt, timeout=25):
        if not self.key:
            raise ProviderUnavailable("ANTHROPIC_API_KEY not set")
        body = json.dumps({
            "model": self.model,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            raise ProviderUnavailable(str(e))
        return "".join(b.get("text", "") for b in data.get("content", []))


class OpenAIAPIProvider:
    """Chat Completions call; needs OPENAI_API_KEY. No SDK required."""

    name = "openai-api"

    def __init__(self, model=""):
        self.model = model or "gpt-4o"
        self.key = os.environ.get("OPENAI_API_KEY", "")

    def probe(self):
        return bool(self.key)

    def complete(self, prompt, timeout=25):
        if not self.key:
            raise ProviderUnavailable("OPENAI_API_KEY not set")
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            raise ProviderUnavailable(str(e))
        return data["choices"][0]["message"]["content"]


PROVIDERS = {
    "none": NoneProvider,
    "claude-cli": ClaudeCLIProvider,
    "anthropic-api": AnthropicAPIProvider,
    "openai-api": OpenAIAPIProvider,
}


def make_provider(cfg):
    name = (cfg.get("provider") or "none").strip()
    cls = PROVIDERS.get(name)
    if cls is None:
        raise SystemExit("Unknown provider %r — choose one of: %s" % (name, ", ".join(PROVIDERS)))
    if cls is NoneProvider:
        return cls()
    return cls(model=cfg.get("model", ""))
