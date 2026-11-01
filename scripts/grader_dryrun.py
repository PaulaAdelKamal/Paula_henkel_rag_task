"""Cross-platform twin of scripts/grader_dryrun.sh (used on Windows hosts).

Usage: uv run python -m scripts.grader_dryrun [--repo URL_OR_PATH] [--env-file .env]
                                               [--port 8000] [--ca-bundle PEM] [--keep]

Simulates the grader's exact steps from a clean clone: git clone -> copy .env
-> docker build -t chatbot:1.0 . -> docker run -p PORT:PORT --env-file .env
chatbot:1.0 -> wait until healthy -> the UI is served -> one smoke question
through the real UI inside the container must return Sources with a page.
--ca-bundle is only for hosts behind a TLS-inspecting proxy (see README).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
NAME = "rag-grader-dryrun"
IMAGE = "chatbot:1.0"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("  $", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=str(ROOT), help="git URL or path (default: this repo)")
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--ca-bundle", type=Path, help="dev-only CA bundle (TLS-inspecting proxy)")
    ap.add_argument("--keep", action="store_true", help="leave the container running")
    args = ap.parse_args()
    if not args.env_file.is_file():
        print(f"missing {args.env_file} (copy .env.example and fill it in)")
        return 1
    ca = args.ca_bundle.resolve() if args.ca_bundle else None
    work = Path(tempfile.mkdtemp(prefix="rag-dryrun-"))
    started = time.perf_counter()
    try:
        print(f"[1/5] git clone {args.repo}")
        run(["git", "clone", "--quiet", args.repo, str(work / "repo")])
        repo = work / "repo"

        print(f"[2/5] fill .env from {args.env_file}")
        shutil.copyfile(args.env_file, repo / ".env")

        print(f"[3/5] docker build -t {IMAGE} .")
        secret = ["--secret", f"id=ca_bundle,src={ca}"] if ca else []
        run(["docker", "build", *secret, "-t", IMAGE, "."], cwd=repo)

        print(f"[4/5] docker run -p {args.port}:{args.port} --env-file .env {IMAGE}")
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)
        mount = (
            [
                "-v",
                f"{ca}:/etc/ssl/certs/dev-ca-bundle.pem:ro",
                "-e",
                "SSL_CERT_FILE=/etc/ssl/certs/dev-ca-bundle.pem",
            ]
            if ca
            else []
        )
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                NAME,
                "-p",
                f"{args.port}:{args.port}",
                "--env-file",
                ".env",
                "-e",
                f"APP_PORT={args.port}",
                *mount,
                IMAGE,
            ],
            cwd=repo,
            capture_output=True,
        )

        print(f"[5/5] open http://localhost:{args.port}")
        base = f"http://localhost:{args.port}"
        for _ in range(60):
            try:
                if httpx.get(f"{base}/_stcore/health", timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            print("not healthy after 120 s")
            subprocess.run(["docker", "logs", "--tail", "40", NAME], check=False)
            return 1
        index = httpx.get(base, timeout=10)
        assert index.status_code == 200 and "<title>" in index.text, "UI page not served"
        print(f"  UI is served on {base} (boot {time.perf_counter() - started:.0f} s since start)")

        print("[smoke] one question through the real UI inside the container")
        smoke = (ROOT / "scripts" / "smoke_ui.py").read_bytes()
        result = subprocess.run(
            ["docker", "exec", "-i", NAME, "python", "-"],
            input=smoke,
            capture_output=True,
            check=False,
        )
        print(result.stdout.decode("utf-8", "replace"))
        if result.returncode != 0:
            print(result.stderr.decode("utf-8", "replace")[-2000:])
            return 1
        size = subprocess.run(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Size}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        print(f"image size: {int(size) / 1e6:.0f} MB; total {time.perf_counter() - started:.0f} s")
        print("DRY RUN PASSED")
        return 0
    finally:
        if not args.keep:
            subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
