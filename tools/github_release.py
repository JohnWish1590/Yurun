"""Create a GitHub release for Yurun and upload the ASCII-named release assets.

Why this script exists: the release step in RELEASE.md used to be described in
prose only. GitHub mangles non-ASCII asset names (a Chinese file name lands as
"default.exe"), so the assets attached here must already carry ASCII names.
Re-running is safe: an existing release for the tag is reused and already
uploaded assets are skipped.

Usage:
    set GH_TOKEN=ghp_xxx
    python tools\\github_release.py v1.4.4 "语润 v1.4.4 — <title>" <body.md> <asset>

Example:
    python tools\\github_release.py v1.4.4 "语润 v1.4.4 — 纠错选区读取修复" ^
        release_body_v1.4.4.md ^
        dist\\Yurun-Setup-v1.4.4.exe

Notes:
    * The token is read from the GH_TOKEN environment variable and never written
      to disk, so this script is safe to keep in the repository.
    * Requires `requests`, which ships with the project's runtime dependencies.
"""

import os
import sys

import requests

API = "https://api.github.com"
REPO = "JohnWish1590/Yurun"


def main() -> int:
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("ERROR: GH_TOKEN is not set")
        return 2

    if len(sys.argv) < 5:
        print(__doc__)
        return 2

    tag, title, body_file = sys.argv[1], sys.argv[2], sys.argv[3]
    assets = sys.argv[4:]

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "yurun-release",
    }

    with open(body_file, "r", encoding="utf-8") as fh:
        body = fh.read()

    payload = {
        "tag_name": tag,
        "name": title,
        "body": body,
        "draft": False,
        "prerelease": False,
    }

    resp = requests.post(f"{API}/repos/{REPO}/releases", headers=headers, json=payload, timeout=60)
    if resp.status_code == 201:
        release = resp.json()
        print(f"CREATED release {release['id']}")
    elif resp.status_code == 422 and "already_exists" in resp.text:
        resp = requests.get(f"{API}/repos/{REPO}/releases/tags/{tag}", headers=headers, timeout=60)
        resp.raise_for_status()
        release = resp.json()
        print(f"REUSED release {release['id']}")
    else:
        print(f"FAILED create: {resp.status_code} {resp.text[:800]}")
        return 1

    upload_url = release["upload_url"].split("{")[0]
    existing = {a["name"] for a in release.get("assets", [])}

    for path in assets:
        name = os.path.basename(path)
        if name in existing:
            print(f"SKIP existing asset {name}")
            continue
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"UPLOADING {name} ({size_mb:.1f} MB) ...")
        with open(path, "rb") as fh:
            up = requests.post(
                f"{upload_url}?name={name}",
                headers={**headers, "Content-Type": "application/octet-stream"},
                data=fh,
                timeout=(30, 900),
            )
        if up.status_code == 201:
            print(f"  OK {up.json()['name']} -> {up.json()['browser_download_url']}")
        else:
            print(f"  FAILED {up.status_code} {up.text[:400]}")
            return 1

    print(f"RELEASE_URL {release['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
