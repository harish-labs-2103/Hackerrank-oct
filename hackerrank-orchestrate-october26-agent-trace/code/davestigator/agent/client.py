"""Model clients. The engine depends only on the ModelClient protocol.

OllamaClient talks to a local Ollama server over HTTP with structured (JSON-schema) output,
temperature 0 and a fixed seed, and caches every response on disk keyed by
(prompt version, model digest, options, input). A cached run is byte-for-byte reproducible.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx


class ModelUnavailable(Exception):
    pass


@dataclass
class CallRecord:
    task: str
    model: str
    cached: bool
    input_tokens: int
    output_tokens: int
    latency_s: float
    verdict: str = ""  # filled by the guard: accepted / rejected:<reason>


@dataclass
class UsageMeter:
    calls: list[CallRecord] = field(default_factory=list)

    def summary(self) -> dict:
        out: dict = {}
        for c in self.calls:
            m = out.setdefault((c.model, c.task), {"calls": 0, "cached": 0, "live": 0, "input_tokens": 0,
                                                    "output_tokens": 0, "latency_s": 0.0, "accepted": 0, "rejected": 0})
            m["calls"] += 1
            m["cached" if c.cached else "live"] += 1
            m["input_tokens"] += c.input_tokens
            m["output_tokens"] += c.output_tokens
            m["latency_s"] += c.latency_s
            if c.verdict.startswith("accepted"):
                m["accepted"] += 1
            elif c.verdict.startswith("rejected"):
                m["rejected"] += 1
        return out


class ModelClient(Protocol):
    model: str
    meter: UsageMeter

    def generate_json(self, *, task: str, prompt_version: str, system: str, user: str, schema: dict,
                      images: list[Path] | None = None) -> tuple[dict | None, CallRecord]: ...


class OllamaClient:
    def __init__(self, model: str = "gemma3:4b", host: str | None = None, cache_dir: Path | None = None,
                 options: dict | None = None, timeout_s: float = 300.0, max_retries: int = 1,
                 offline: bool = False):
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.cache_dir = Path(cache_dir or "cache/llm")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.options = {"temperature": 0, "top_k": 1, "seed": 42, "num_ctx": 8192, "num_predict": 768,
                        **(options or {})}
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.offline = offline  # cache-only mode: never call the server
        self.meter = UsageMeter()
        self.digest = self._model_digest() if not offline else self._cached_digest()

    # -------------------------------------------------------------- #
    def _model_digest(self) -> str:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ModelUnavailable(f"Ollama not reachable at {self.host}: {e}") from e
        for m in r.json().get("models", []):
            if m.get("name") == self.model or m.get("model") == self.model:
                digest = m.get("digest", "")
                (self.cache_dir / "model_digest.txt").write_text(f"{self.model} {digest}\n")
                return digest
        raise ModelUnavailable(f"model {self.model} is not pulled (run: ollama pull {self.model})")

    def _cached_digest(self) -> str:
        p = self.cache_dir / "model_digest.txt"
        if p.is_file():
            name, _, digest = p.read_text().strip().partition(" ")
            if name == self.model:
                return digest
        raise ModelUnavailable("offline mode requested but no cached model digest found")

    def _key(self, prompt_version: str, system: str, user: str, schema: dict, images: list[Path]) -> str:
        h = hashlib.sha256()
        for part in (prompt_version, self.model, self.digest, json.dumps(self.options, sort_keys=True), system, user,
                     json.dumps(schema, sort_keys=True)):
            h.update(part.encode())
            h.update(b"\x00")
        for img in images:
            h.update(hashlib.sha256(img.read_bytes()).digest())
        return h.hexdigest()

    # -------------------------------------------------------------- #
    def generate_json(self, *, task, prompt_version, system, user, schema, images=None):
        images = list(images or [])
        key = self._key(prompt_version, system, user, schema, images)
        cpath = self.cache_dir / f"{key}.json"
        if cpath.is_file():
            data = json.loads(cpath.read_text())
            rec = CallRecord(task, self.model, True, data.get("input_tokens", 0), data.get("output_tokens", 0), 0.0)
            self.meter.calls.append(rec)
            return data.get("parsed"), rec
        if self.offline:
            raise ModelUnavailable("cache miss in offline mode")
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": self.options,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if images:
            payload["messages"][1]["images"] = [base64.b64encode(p.read_bytes()).decode() for p in images]
        parsed, data, err = None, {}, ""
        t0 = time.monotonic()
        for _attempt in range(self.max_retries + 1):
            try:
                r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout_s)
                r.raise_for_status()
                data = r.json()
                parsed = json.loads(data.get("message", {}).get("content", ""))
                break
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                err = str(e)
                parsed = None
        latency = time.monotonic() - t0
        rec = CallRecord(task, self.model, False, int(data.get("prompt_eval_count") or 0),
                         int(data.get("eval_count") or 0), latency, "" if parsed is not None else f"rejected:transport:{err[:80]}")
        self.meter.calls.append(rec)
        if parsed is not None:
            cpath.write_text(json.dumps({"parsed": parsed, "input_tokens": rec.input_tokens,
                                         "output_tokens": rec.output_tokens, "latency_s": round(latency, 2),
                                         "task": task, "prompt_version": prompt_version}, sort_keys=True))
        return parsed, rec


class FakeModelClient:
    """Scripted client for tests: returns queued outputs, records calls like the real one."""

    def __init__(self, outputs: list[dict | None], model: str = "fake-model"):
        self.outputs = list(outputs)
        self.model = model
        self.meter = UsageMeter()

    def generate_json(self, *, task, prompt_version, system, user, schema, images=None):
        out = self.outputs.pop(0) if self.outputs else None
        rec = CallRecord(task, self.model, False, len(user) // 4, 20, 0.0)
        self.meter.calls.append(rec)
        return out, rec
