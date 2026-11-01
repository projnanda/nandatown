import re


def fuzzy_remove_one(data: str, span: str, length_guard: float) -> str:
    if not span:
        return data
    if span in data:                       # exact first
        return data.replace(span, "", 1)
    words = re.findall(r"\S+", span)
    if not words:
        return data
    pattern = ".*?".join(re.escape(w) for w in words)   # arbitrary chars between words
    m = re.search(pattern, data, flags=re.DOTALL)
    if not m:
        return data
    if len(m.group(0)) > length_guard * len(span) + 50:  # too risky to delete
        return data
    return data[:m.start()] + data[m.end():]


def sanitize(data, det, cfg):
    if not det["injection"]:
        return {"clean": data, "flagged": False, "spans": []}
    if cfg.on_detect == "block":
        return {"clean": None, "flagged": True, "spans": det["spans"]}   # halt pipeline
    if cfg.on_detect == "flag":
        return {"clean": data, "flagged": True, "spans": det["spans"]}
    out = data
    for s in det["spans"]:
        out = fuzzy_remove_one(out, s, cfg.removal_length_guard)
    return {"clean": out, "flagged": True, "spans": det["spans"]}
