from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_DIR = BASE_DIR / "cloud_private"
KEY_FILE = BASE_DIR / ".jobfinder_fernet_key"
CV_DIR = BASE_DIR / "cvs"
SENT_FILE = BASE_DIR / "sent_applications.jsonl"
SOURCE_FILE = BASE_DIR / "auto_jobfinder.py"
CV_NAMES = ("cv_ats_en.pdf", "cv_ats_fr.pdf", "cv_normal_en.pdf", "cv_normal_fr.pdf")


def literal_assignment(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
    raise RuntimeError(f"Could not find literal {name} in auto_jobfinder.py")


def read_state(fernet: Fernet) -> dict:
    path = PRIVATE_DIR / "state.enc"
    if not path.exists():
        contents = SENT_FILE.read_text(encoding="utf-8") if SENT_FILE.exists() else ""
        return {"schema_version": 1, "last_message_ids": {}, "sent_applications_jsonl": contents}
    try:
        state = json.loads(fernet.decrypt(path.read_bytes()).decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Existing encrypted state cannot be opened with the local key") from exc
    if not isinstance(state, dict):
        raise RuntimeError("Existing encrypted state has the wrong structure")
    state.setdefault("schema_version", 1)
    state.setdefault("last_message_ids", {})
    # Preserve any local sent history not already represented in cloud state.
    old = str(state.get("sent_applications_jsonl", ""))
    local = SENT_FILE.read_text(encoding="utf-8") if SENT_FILE.exists() else ""
    entries = {}
    for source in (old, local):
        for line in source.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = record.get("key")
            except Exception:
                key = None
            identity = str(key) if key else line
            entries[identity] = line
    state["sent_applications_jsonl"] = "".join(line + "\n" for line in entries.values())
    return state


def main() -> None:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="ascii").strip().encode("ascii")
    else:
        if (PRIVATE_DIR / "state.enc").exists() or any((PRIVATE_DIR / "cvs").glob("*.fernet")):
            raise RuntimeError("Encrypted data already exists but local key is missing; do not generate a new key")
        key = Fernet.generate_key()
        KEY_FILE.write_text(key.decode("ascii"), encoding="ascii")
    fernet = Fernet(key)

    missing = [name for name in CV_NAMES if not (CV_DIR / name).is_file()]
    if missing:
        raise RuntimeError("Missing CV files in cvs/: " + ", ".join(missing))
    for filename in CV_NAMES:
        encrypted = fernet.encrypt((CV_DIR / filename).read_bytes())
        target = PRIVATE_DIR / "cvs" / (filename + ".fernet")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encrypted)

    if not SOURCE_FILE.is_file():
        raise RuntimeError("auto_jobfinder.py is required locally to extract the private candidate profile")
    tree = ast.parse(SOURCE_FILE.read_text(encoding="utf-8"))
    profile_text = literal_assignment(tree, "CANDIDATE")
    gmail_address = literal_assignment(tree, "GMAIL_ADDRESS")
    match = re.search(r"(?m)^Name:\s*\n([^\n]+)", profile_text)
    if not match:
        raise RuntimeError("Could not extract candidate name from CANDIDATE profile")
    private_profile = {
        "candidate_profile": profile_text.strip(),
        "candidate_name": match.group(1).strip(),
        "gmail_address": gmail_address.strip(),
    }
    (PRIVATE_DIR / "profile.enc").write_bytes(
        fernet.encrypt(json.dumps(private_profile, ensure_ascii=False).encode("utf-8"))
    )

    state = read_state(fernet)
    raw_state = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    (PRIVATE_DIR / "state.enc").write_bytes(fernet.encrypt(raw_state))

    # Make sure the local key never enters Git.
    ignore_file = BASE_DIR / ".gitignore"
    line = ".jobfinder_fernet_key"
    existing = ignore_file.read_text(encoding="utf-8") if ignore_file.exists() else ""
    if line not in existing.splitlines():
        with ignore_file.open("a", encoding="utf-8", newline="\n") as file:
            if existing and not existing.endswith("\n"):
                file.write("\n")
            file.write(line + "\n")

    print("Encrypted four CV files into cloud_private/cvs/.")
    print("Encrypted candidate profile into cloud_private/profile.enc.")
    print("Encrypted sent-application history and initialized Telegram cursors in cloud_private/state.enc.")
    print("The encryption key is stored locally in .jobfinder_fernet_key (this file is Git-ignored).")
    print("Copy the key to your clipboard in PowerShell with:")
    print("  Set-Clipboard -Value ((Get-Content .\\.jobfinder_fernet_key -Raw).Trim())")
    print("Add the clipboard value to GitHub Actions Secrets as JOBFINDER_FERNET_KEY.")


if __name__ == "__main__":
    main()
