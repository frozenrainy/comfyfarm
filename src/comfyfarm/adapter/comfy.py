"""ComfyUI worker protocol. Every other module sees classified outcomes, never raw HTTP exceptions.

The facts this module relies on are listed (and verified) in docs/design/04-worker-protocol.md.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from ..health import memory

log = logging.getLogger(__name__)

DISPATCH_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=3.0)
# httpx timeouts are per phase: a body trickling out (or an answer trickling in) a few bytes at a time never ends. This
# bounds one POST /prompt as a whole, so "a POST may still be in flight" has an end that other processes can rely on.
DISPATCH_DEADLINE = 15.0
QUERY_TIMEOUT = httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0)

# connect-phase failures: the request never reached the process
CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
# the TCP connection existed: the process is there but did not answer in time / broke mid-way
BUSY_ERRORS = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.ReadError, httpx.WriteError,
               httpx.RemoteProtocolError)


# ---- outcomes -------------------------------------------------------------------------------------------------------
@dataclass
class Accepted:
    number: int | None = None


@dataclass
class Rejected:                      # 4xx: final, identical on every worker (D-07)
    status: int
    error: dict = field(default_factory=dict)
    node_errors: dict = field(default_factory=dict)


@dataclass
class Unreachable:                   # never reached the process → safe to re-queue
    reason: str


@dataclass
class Ambiguous:                     # may or may not have been accepted → ask the worker (D-05)
    reason: str


SubmitOutcome = Accepted | Rejected | Unreachable | Ambiguous


@dataclass
class JobState:
    """What the worker says about one prompt_id. status None = the worker does not know it (404)."""
    status: str | None                        # pending | in_progress | completed | failed | cancelled | None
    raw: dict = field(default_factory=dict)


@dataclass
class NoAnswer:                      # worker did not answer the question (busy or dead) — not evidence of absence
    reason: str


def classify_exception(e: Exception) -> str:
    """'dead' | 'busy' — the health classification of D-11. Unknown exceptions count as busy."""
    if isinstance(e, CONNECT_ERRORS):
        return "dead"
    return "busy"


def _ename(e: Exception) -> str:
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__   # httpx messages are often empty


def _object(r: httpx.Response) -> dict | None:
    """The body as a JSON object, else None. Something other than ComfyUI can answer 200 on a worker's address (a login
    page in front of it, a proxy's error page, `null`): that is no answer, not an answer with nothing in it."""
    try:
        j = r.json()
    except ValueError:                                           # also covers undecodable bytes
        return None
    return j if isinstance(j, dict) else None


class ComfyAdapter:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.http = client or httpx.AsyncClient()

    async def close(self):
        await self.http.aclose()

    async def submit(self, base_url: str, prompt_id: str, prompt: dict, extra_data: dict, client_id: str) -> SubmitOutcome:
        body = {"prompt": prompt, "prompt_id": prompt_id, "client_id": client_id, "extra_data": extra_data}
        try:
            async with asyncio.timeout(DISPATCH_DEADLINE):
                r = await self.http.post(f"{base_url}/prompt", json=body, timeout=DISPATCH_TIMEOUT)
        except CONNECT_ERRORS as e:
            return Unreachable(_ename(e))
        except Exception as e:                                   # read/write timeout, reset after send, deadline, …
            return Ambiguous(_ename(e) if not isinstance(e, TimeoutError) else f"no answer within {DISPATCH_DEADLINE:g} s")
        if r.status_code == 200:
            j = _object(r)
            if j is None:                                        # not ComfyUI's answer: nothing says it was queued
                return Ambiguous("HTTP 200 without a JSON object")
            return Accepted(j.get("number"))
        if 400 <= r.status_code < 500:
            j = _object(r) or {}
            return Rejected(r.status_code, j.get("error") or {}, j.get("node_errors") or {})
        return Ambiguous(f"HTTP {r.status_code}")                # 5xx: the worker may have queued it

    async def job_state(self, base_url: str, prompt_id: str, jobs_api: bool = True) -> JobState | NoAnswer:
        """Authoritative state of one job on the worker (P4). Older ComfyUI without /api/jobs: /queue + /history."""
        if not jobs_api:
            return await self._job_state_legacy(base_url, prompt_id)
        try:
            r = await self.http.get(f"{base_url}/api/jobs/{prompt_id}", timeout=QUERY_TIMEOUT)
        except Exception as e:
            return NoAnswer(_ename(e))
        if r.status_code == 404:                                 # {"error": "Job not found"} — verified on 0.41.0
            return JobState(None)
        if r.status_code != 200:
            return NoAnswer(f"HTTP {r.status_code}")
        j = _object(r)
        if j is None:
            return NoAnswer("HTTP 200 without a JSON object")
        return JobState(j.get("status"), j)

    async def _job_state_legacy(self, base_url: str, prompt_id: str) -> JobState | NoAnswer:
        try:                                                     # a malformed answer anywhere = no answer
            q = (await self.http.get(f"{base_url}/queue", timeout=QUERY_TIMEOUT)).json()
            if any(x[1] == prompt_id for x in q.get("queue_running", [])):
                return JobState("in_progress")
            if any(x[1] == prompt_id for x in q.get("queue_pending", [])):
                return JobState("pending")
            h = (await self.http.get(f"{base_url}/history/{prompt_id}", timeout=QUERY_TIMEOUT)).json().get(prompt_id)
            if not h:
                return JobState(None)
            st = h.get("status", {})
            msgs = [m[0] for m in st.get("messages", [])]
            if "execution_interrupted" in msgs:                  # P8
                return JobState("cancelled", h)
            return JobState("completed" if st.get("completed") else "failed", h)
        except Exception as e:
            return NoAnswer(_ename(e))

    async def supports_jobs_api(self, base_url: str) -> bool | None:
        try:
            r = await self.http.get(f"{base_url}/api/jobs", params={"limit": 1}, timeout=QUERY_TIMEOUT)
            return r.status_code == 200
        except Exception:
            return None

    async def inventory(self, base_url: str) -> dict | None:
        """Node class names and model files per folder (core routes /object_info, /models, /models/{folder}) with
        their hashes — the group consistency check (core/consistency.py). None if anything did not answer."""
        from ..core.consistency import hashes
        try:
            r = await self.http.get(f"{base_url}/object_info", timeout=90)
            if r.status_code != 200:
                return None
            nodes = sorted(r.json())
            folders = (await self.http.get(f"{base_url}/models", timeout=QUERY_TIMEOUT)).json()
            models = {}
            for f in folders:
                lr = await self.http.get(f"{base_url}/models/{f}", timeout=QUERY_TIMEOUT)
                models[f] = sorted(lr.json()) if lr.status_code == 200 else []
        except Exception as e:
            log.debug("inventory of %s failed: %s", base_url, type(e).__name__)
            return None
        nh, mh = hashes(nodes, models)
        return {"at": time.time(), "nodes": nodes, "models": models, "nodes_hash": nh, "models_hash": mh}

    async def worker_info(self, base_url: str) -> dict | None:
        """The comfyfarm_worker extension's GET /comfyfarm/info (allow-list state, ws fix, version); None without
        the extension or without an answer — the two are told apart by the caller (404 vs no answer)."""
        try:
            r = await self.http.get(f"{base_url}/comfyfarm/info", timeout=QUERY_TIMEOUT)
        except Exception:
            return None
        if r.status_code == 404:
            return {"installed": False}
        j = _object(r) if r.status_code == 200 else None
        return {"installed": True, **j} if j is not None else None

    async def vitals(self, base_url: str, extension: dict | None) -> tuple[str | None, dict | None]:
        """(why a worker that answers /queue cannot run jobs, or None; its memory reading, or None). Its prompt executor
        thread can die while the process keeps answering (field, dev.21: a GPU fault; /queue 200, every job waited
        forever). The extension from 0.9 says so on GET /comfyfarm/health, and reports memory there from 0.11; an
        older worker shows a dead GPU context as GET /system_stats → 500 (ComfyUI asks the broken context for free
        memory there), and its RAM there. No answer is not evidence: the probe judges reachability."""
        version = str((extension or {}).get("extension") or "")
        try:
            newer = tuple(int(x) for x in version.split("-")[0].split(".")[:2]) >= (0, 9)
        except ValueError:
            newer = False
        why = mem = None
        try:
            if newer:
                r = await self.http.get(f"{base_url}/comfyfarm/health", timeout=QUERY_TIMEOUT)
                j = _object(r) if r.status_code == 200 else None
                if j is None:
                    return None, None
                if j.get("executor") == "dead":
                    why = "its prompt executor has stopped"
                elif str(j.get("device", "")).startswith("error"):
                    why = f"GPU {j['device']}"[:200]
                mem = memory.from_extension(j.get("memory"))
                if mem is not None or why is not None:
                    return why, mem
            r = await self.http.get(f"{base_url}/system_stats", timeout=QUERY_TIMEOUT)
            if r.status_code >= 500:
                return why or (None if newer else "system_stats answers HTTP 500 (a GPU fault?)"), None
            return why, memory.from_system_stats(_object(r)) if r.status_code == 200 else None
        except Exception:
            return why, mem

    async def cancel(self, base_url: str, prompt_id: str, jobs_api: bool = True) -> bool | None:
        """Cancel wherever the job is (pending or running). True = something was cancelled, False = nothing to
        cancel (finished/unknown), None = no answer. Interrupt is cooperative (P7): the job may run on for a while."""
        if jobs_api:
            try:
                r = await self.http.post(f"{base_url}/api/jobs/{prompt_id}/cancel", timeout=QUERY_TIMEOUT)
                if r.status_code == 200:
                    return bool(r.json().get("cancelled"))
            except Exception as e:
                log.warning("cancel %s on %s: %s", prompt_id, base_url, _ename(e))
                return None
        deleted = await self.delete_pending(base_url, prompt_id)      # older ComfyUI: no cancel API
        interrupted = await self.interrupt(base_url, prompt_id)
        return deleted or interrupted

    async def validate(self, base_url: str, prompt: dict) -> tuple[str, dict]:
        """('ok'|'invalid'|'unavailable', body). 'unavailable' = no answer in 4 s, or no validate endpoint here."""
        try:
            r = await self.http.post(f"{base_url}/comfyfarm/validate", json={"prompt": prompt},
                                     timeout=httpx.Timeout(4.0, connect=2.0))
        except Exception:
            return "unavailable", {}
        if r.status_code == 200:
            return "ok", {}
        if r.status_code == 400 and (j := _object(r)) is not None:
            return "invalid", j
        return "unavailable", {}

    async def history(self, base_url: str, prompt_id: str) -> dict | None:
        try:
            r = await self.http.get(f"{base_url}/history/{prompt_id}", timeout=QUERY_TIMEOUT)
            h = r.json().get(prompt_id) if r.status_code == 200 else None
        except Exception:
            return None
        return h if isinstance(h, dict) else None

    async def interrupt(self, base_url: str, prompt_id: str) -> bool:
        try:
            r = await self.http.post(f"{base_url}/interrupt", json={"prompt_id": prompt_id}, timeout=QUERY_TIMEOUT)
            return r.status_code == 200
        except Exception as e:
            log.warning("interrupt %s on %s failed: %s", prompt_id, base_url, _ename(e))
            return False

    async def delete_pending(self, base_url: str, prompt_id: str) -> bool:
        try:
            r = await self.http.post(f"{base_url}/queue", json={"delete": [prompt_id]}, timeout=QUERY_TIMEOUT)
            return r.status_code == 200
        except Exception:
            return False

    async def probe(self, base_url: str) -> tuple[bool, str | None, str]:
        """Health probe: (ok, failure_kind 'busy'|'dead'|'refused'|None, detail). 'refused': the worker extension's
        allow-list turned the router away — up and answering, but not busy; the detail is the address it saw (0.8)."""
        try:
            r = await self.http.get(f"{base_url}/queue", timeout=QUERY_TIMEOUT)
        except Exception as e:
            return False, classify_exception(e), _ename(e)
        if r.status_code == 200:
            return True, None, "ok"
        if r.status_code == 403:
            try:
                body = r.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and str(body.get("error", "")).startswith("comfyfarm:"):
                return False, "refused", str(body.get("peer") or "")
        return False, "busy", f"HTTP {r.status_code}"

    async def capabilities(self, base_url: str) -> dict | None:
        try:                                                     # a malformed answer = no answer, never a crashed tick
            r = await self.http.get(f"{base_url}/system_stats", timeout=QUERY_TIMEOUT)
            s = r.json()
            system, devices = s.get("system", {}), s.get("devices", [])
            torch = system.get("pytorch_version", "")
            backend = "rocm" if "rocm" in torch else "cuda" if "+cu" in torch else (devices[0].get("type") if devices else "cpu")
            return {"backend": backend, "torch": torch, "comfyui": system.get("comfyui_version"), "os": system.get("os"),
                    "argv": system.get("argv"),
                    "devices": [{"name": d.get("name"), "type": d.get("type"), "vram_total": d.get("vram_total")} for d in devices]}
        except Exception:
            return None
