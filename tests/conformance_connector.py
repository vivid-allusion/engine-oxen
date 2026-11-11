"""The four answers the conformance suite needs from Oxen.ai.

Images are catalogued synchronously and video through the queue; the suite
exercises the inline path here, and the queue is covered by this engine's own
tests. The fake speaks Oxen's envelopes, not a generic one.
"""

from __future__ import annotations

from engine_conformance import Behaviours, Call, Connector, Provider


class OxenConnector(Connector):
    package = "engine_oxen"

    entries = {"text_to_image": "IMG-Models/black-forest-labs-flux-2-klein-4b.toml"}

    provider_modules = ()
    supports_polling = False

    def install(self, monkeypatch, behaviours: Behaviours) -> Provider:
        provider = Provider()
        state = {"submit": list(behaviours.submit_errors)}

        class FakeClient:
            def request_json(self, method, path, payload=None, *, headers=None, params=None):
                if method == "GET":
                    provider.calls.append(Call(kind="poll", path=path))
                    if behaviours.poll_error is not None:
                        raise behaviours.poll_error
                    statuses = behaviours.poll_statuses
                    index = min(state.get("polls", 0), len(statuses) - 1)
                    state["polls"] = state.get("polls", 0) + 1
                    record = {"generation_id": "gen-1", "status": statuses[index]}
                    if statuses[index] == "succeeded" and behaviours.result_urls:
                        record["result_url"] = behaviours.result_urls[0]
                    return record

                provider.calls.append(Call(kind="submit", payload=payload, path=path))
                if behaviours.submit_error is not None:
                    raise behaviours.submit_error
                if state["submit"]:
                    raise state["submit"].pop(0)
                if "images" in path:
                    return {"images": [{"url": url} for url in behaviours.result_urls]}
                return {"generations": [{"generation_id": "gen-1", "status": "queued"}]}

            def request_form(self, method, path, fields, *, files=None):
                return self.request_json(method, path, fields)

            def request_bytes(self, method, path):
                return behaviours.result_bytes

            def download(self, url):
                provider.downloads.append(url)
                provider.calls.append(Call(kind="download", url=url))
                if behaviours.download_error is not None:
                    raise behaviours.download_error
                return behaviours.result_bytes

        fake = FakeClient()
        monkeypatch.setattr(self.engine_class(), "_client", lambda _self: fake)
        return provider
