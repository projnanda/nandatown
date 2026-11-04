import json
import re
from concurrent.futures import ThreadPoolExecutor

from .prompts import build_system_prompt


def parse_output(text: str) -> tuple[bool, list[str]]:
    # try JSON first
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return bool(obj.get("injection", False)), list(obj.get("spans", []) or [])
        except Exception:
            pass
    # fallback: legacy "Yes\nInjection: ..." format
    is_inj = text.lower().lstrip().startswith("yes")
    spans = []
    if is_inj and "Injection:" in text:
        spans = [text.split("Injection:", 1)[1].strip()]
    return is_inj, spans


class Detector:
    def __init__(self, cfg, client):
        self.cfg, self.client = cfg, client

    def detect(self, data: str, user_task: str | None = None) -> dict:
        sys = build_system_prompt(user_task, self.cfg.include_definition)
        out = self.client.complete(sys, data)
        is_inj, spans = parse_output(out)
        return {"injection": is_inj, "spans": spans, "raw": out}

    def detect_batch(
        self,
        samples: list[str],
        user_task: str | None = None,
        max_concurrency: int | None = None,
    ) -> list[dict]:
        """Detect over many samples concurrently via a thread pool.

        Order of results matches the order of `samples`. A per-sample failure
        is returned in-place as an error record rather than aborting the batch.
        """
        workers = max_concurrency or self.cfg.max_concurrency
        workers = max(1, min(workers, len(samples) or 1))

        def _one(data: str) -> dict:
            try:
                return self.detect(data, user_task)
            except Exception as e:  # keep the batch alive; surface the error
                return {"injection": False, "spans": [], "raw": "", "error": repr(e)}

        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_one, samples))
