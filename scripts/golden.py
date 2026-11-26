"""Write the Go port's golden files from the Python scan.

`make golden` runs this. For each `testdata/golden/<scenario>/fixture.json`
it replays the fixture through the real Python scan (`repoagent.jobs.scan`)
with GitHub, DeepSeek, Resend and the state blob faked and the clock frozen
at the fixture's `now`, and writes what came out:

- `subject.txt`, `text.txt`, `html.html`: the digest;
- `prompt.json` and `system_prompt.txt`: what triage sent DeepSeek;
- `detail_query.graphql`: the first batched detail query sent to GitHub;
- `state.json`: the state document the run saved.

The Go tests (`internal/scan/golden_test.go`) replay the same fixture and
must produce the same bytes. That is the proof the port changes nothing a
reader of the digest, the state blob or DeepSeek's logs could see.

It goes with the Python at the cutover; the golden files stay.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parent.parent / "testdata" / "golden"


class _FakeBlob:
    """`state._blob()`'s stand-in: a prior document, and a captured upload."""

    def __init__(self, prior: dict[str, Any] | None) -> None:
        self.prior = prior
        self.saved: bytes | None = None

    def download_blob(self) -> Any:
        if self.prior is None:
            from azure.core.exceptions import ResourceNotFoundError

            raise ResourceNotFoundError("no blob")
        data = json.dumps(self.prior).encode()

        class _Stream:
            def readall(self) -> bytes:
                return data

        return _Stream()

    def upload_blob(self, data: bytes, overwrite: bool) -> None:
        self.saved = data


def _freeze(now: datetime) -> None:
    """Every module that reads the clock reads `now` instead."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return now if tz else now.replace(tzinfo=None)

    from repoagent import llm, state
    from repoagent.checks import delivery, hygiene, renovate, security

    for module in (hygiene, renovate, security, delivery, state, llm):
        module.datetime = Frozen  # type: ignore[attr-defined,misc]


def _handler(fx: dict[str, Any], captured: dict[str, Any]) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        body = json.loads(request.content) if request.content else None

        if url.host == "api.github.com":
            if path == "/app/installations":
                return httpx.Response(200, json=[{"id": 42, "account": {"login": "jay-withers"}}])
            if path == "/app/installations/42/access_tokens":
                return httpx.Response(
                    201,
                    json={
                        "token": "ghs_test",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "permissions": fx["permissions"],
                    },
                )
            if path == "/installation/repositories":
                return httpx.Response(
                    200, json={"total_count": len(fx["repos"]), "repositories": fx["repos"]}
                )
            if path.endswith("/code-scanning/alerts"):
                name = path.removeprefix("/repos/").removesuffix("/code-scanning/alerts")
                if name in fx["alerts_missing"] or name not in fx["alerts"]:
                    return httpx.Response(404, json={"message": "no analysis found"})
                return httpx.Response(200, json=fx["alerts"][name])
            if path.endswith("/issues"):
                name = path.removeprefix("/repos/").removesuffix("/issues")
                return httpx.Response(200, json=fx["issues"].get(name, []))
            if path == "/graphql":
                query = body["query"]
                if "query Catalogue" in query:
                    text = fx["catalogue"]
                    obj = None if text is None else {"text": text}
                    return httpx.Response(200, json={"data": {"repository": {"object": obj}}})
                captured.setdefault("detail_query", query)
                if fx.get("detail_error"):
                    return httpx.Response(200, json={"errors": [{"message": fx["detail_error"]}]})
                variables = body["variables"]
                data = {}
                for key, owner in variables.items():
                    if key.startswith("o"):
                        index = key[1:]
                        full = f"{owner}/{variables['n' + index]}"
                        data[f"r{index}"] = fx["details"].get(full)
                return httpx.Response(200, json={"data": data})

        if url.host == "api.deepseek.com":
            ds = fx["deepseek"]
            if path == "/chat/completions":
                captured["system_prompt"] = body["messages"][0]["content"]
                captured["prompt"] = body["messages"][1]["content"]
                return httpx.Response(
                    200,
                    json={
                        "model": ds["model"],
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": json.dumps(ds["reply"])},
                            }
                        ],
                        "usage": ds["usage"],
                    },
                )
            if path == "/user/balance":
                return httpx.Response(200, json=ds["balance"])

        if url.host == "api.resend.com":
            captured["email"] = body
            return httpx.Response(200, json={"id": "re_123"})

        raise AssertionError(f"unexpected request {request.method} {url}")

    return respond


def run(scenario: Path) -> None:
    fx = json.loads((scenario / "fixture.json").read_text())

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    for name in ("DIGEST_EMAIL_TO", "DEEPSEEK_API_KEY", "IMAGE_TAG", "STATE_CONTAINER_URL"):
        os.environ.pop(name, None)
    os.environ.update(
        {
            "GITHUB_APP_ID": "123456",
            "GITHUB_APP_PRIVATE_KEY": pem,
            "RESEND_API_KEY": "re_test",
            "KEY_VAULT_URI": "",
        }
    )
    if fx["email_to"]:
        os.environ["DIGEST_EMAIL_TO"] = fx["email_to"]
    if fx["deepseek"]:
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    if fx["image_tag"]:
        os.environ["IMAGE_TAG"] = fx["image_tag"]

    from repoagent import digest, fetch, state
    from repoagent import settings as settings_module
    from repoagent.github import auth
    from repoagent.jobs import scan

    for cache in (
        settings_module.dotenv,
        settings_module.settings,
        settings_module.secret,
        settings_module.optional_secret,
        settings_module.credential,
    ):
        cache.cache_clear()
    auth.reset_cache()
    fetch.time.sleep = lambda _s: None  # type: ignore[assignment]
    _freeze(datetime.fromisoformat(fx["now"]).astimezone(UTC))

    blob = _FakeBlob(fx["prior_state"])
    state._blob = lambda: blob if fx["prior_state"] is not None else None  # type: ignore[assignment]

    captured: dict[str, Any] = {}
    client = httpx.Client(transport=httpx.MockTransport(_handler(fx, captured)))
    result = scan.run(send_email=fx["send_email"], http=client)

    out = {
        "subject.txt": digest.subject(result),
        "text.txt": digest.render_text(result),
        "html.html": digest.render_html(result),
    }
    if "email" in captured:
        email = captured["email"]
        sent = (email["subject"], email["text"], email["html"])
        if sent != (out["subject.txt"], out["text.txt"], out["html.html"]):
            raise SystemExit(f"{scenario.name}: the email sent is not the digest rendered")
    if "prompt" in captured:
        out["prompt.json"] = captured["prompt"]
        out["system_prompt.txt"] = captured["system_prompt"]
    if "detail_query" in captured:
        out["detail_query.graphql"] = captured["detail_query"]
    if blob.saved is not None:
        out["state.json"] = blob.saved.decode()
    for name, text in out.items():
        (scenario / name).write_text(text)
    print(f"{scenario.name}: wrote {', '.join(sorted(out))}")


def main() -> int:
    # Somewhere with no .env, as the test suite runs.
    os.chdir(tempfile.mkdtemp())
    scenarios = sys.argv[1:] or sorted(p.name for p in ROOT.iterdir() if p.is_dir())
    for name in scenarios:
        run(ROOT / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
