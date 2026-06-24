"""File Batch Rename Skill Script — deterministic file renaming."""

import json
import os
import re
import sys
from pathlib import Path

def main():
    try:
        raw = sys.stdin.read()
        params = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid input JSON: {e}"}))
        return

    directory = params.get("directory", ".")
    pattern = params.get("pattern", "*")
    operation = params.get("operation", "preview")
    value = params.get("value", "")
    preview = params.get("preview", True)

    dir_path = Path(directory)
    if not dir_path.exists():
        print(json.dumps({"status": "error", "message": f"Directory not found: {directory}"}))
        return

    # Match files by glob pattern
    import glob as _glob
    files = sorted(_glob.glob(str(dir_path / pattern)))

    if not files:
        print(json.dumps({"status": "success", "message": "No files matched", "changes": []}))
        return

    changes = []
    for i, filepath in enumerate(files, 1):
        p = Path(filepath)
        old_name = p.name
        stem, ext = os.path.splitext(p.name)

        if operation == "prefix":
            new_name = f"{value}{old_name}"
        elif operation == "suffix":
            new_name = f"{stem}{value}{ext}"
        elif operation == "replace":
            new_name = re.sub(value, "", old_name) if value else old_name
        elif operation == "sequential":
            pad = int(params.get("pad", 3))
            new_name = f"{value}{str(i).zfill(pad)}{ext}"
        elif operation == "lowercase":
            new_name = old_name.lower()
        elif operation == "uppercase":
            new_name = old_name.upper()
        else:
            new_name = old_name

        new_path = str(p.parent / new_name)
        changes.append({"old": filepath, "new": new_path})

        if not preview:
            try:
                os.rename(filepath, new_path)
            except OSError as e:
                print(json.dumps({"status": "error", "message": f"Rename failed: {e}"}))
                return

    result = {
        "status": "success",
        "operation": operation,
        "preview": preview,
        "file_count": len(files),
        "changes": changes,
        "summary": f"{'[PREVIEW] ' if preview else ''}Would rename {len(files)} files" if preview else f"Renamed {len(files)} files",
    }
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
