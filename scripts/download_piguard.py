"""Download a pinned official snapshot, verify upstream hashes, never load a model."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

REVISION = "dd78b24e330193a22d2293ac66922dd4f982f563"
FILES = ["model.safetensors", "modeling_piguard.py", "config.json", "tokenizer.json",
         "tokenizer_config.json", "special_tokens_map.json", "added_tokens.json", "spm.model", "README.md"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="models/piguard-"+REVISION[:12])
    args = parser.parse_args()
    root = Path(args.out).resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit("Destination is not empty; refusing to overwrite snapshot")
    root.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        f"https://huggingface.co/api/models/leolee99/PIGuard/tree/{REVISION}",
        headers={"User-Agent": "nandatown-piguard-pinned-download"})
    with urllib.request.urlopen(request, timeout=60) as response:
        entries = {row["path"]: row for row in json.load(response)}
    manifest = {"repository": "leolee99/PIGuard", "revision": REVISION,
                "download_only_no_inference": True, "files": {}}
    for name in FILES:
        entry = entries[name]
        size = entry["size"]
        sha = hashlib.sha256()
        blob = hashlib.sha1(f"blob {size}\0".encode())
        path = root/name
        with urllib.request.urlopen(
                f"https://huggingface.co/leolee99/PIGuard/resolve/{REVISION}/{name}?download=true",
                timeout=120) as response, path.open("xb") as stream:
            while True:
                block = response.read(1024*1024)
                if not block:
                    break
                stream.write(block)
                sha.update(block)
                blob.update(block)
        if path.stat().st_size != size:
            raise RuntimeError("File size mismatch: "+name)
        expected = entry.get("lfs", {}).get("oid")
        if expected:
            if sha.hexdigest() != expected.removeprefix("sha256:"):
                raise RuntimeError("LFS SHA256 mismatch: "+name)
        elif blob.hexdigest() != entry["oid"]:
            raise RuntimeError("Git blob identity mismatch: "+name)
        manifest["files"][name] = {"sha256": sha.hexdigest(), "bytes": size,
                                  "upstream_git_blob": entry["oid"], "upstream_lfs_sha256": expected}
        print(f"Verified {name}: {size} bytes", flush=True)
    license_text = Path(__file__).resolve().parents[1] / "tests/fixtures/piguard/LICENSE"
    if license_text.exists():
        (root / "LICENSE").write_bytes(license_text.read_bytes())
    (root/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    print("Snapshot verified. No model loaded; no inference executed.", flush=True)


if __name__ == "__main__":
    main()
