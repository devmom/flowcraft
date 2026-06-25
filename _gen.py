import os, json, yaml, asyncio, subprocess, threading, time as _time
from datetime import datetime, timezone
from pathlib import Path

SKILLS_DIR = r"D:/work/FlowCraft/core/flowcraft_core/skills"
WS_DIR = r"D:/work/FlowCraft/workspace/skills"

def write_file(filepath, *content_parts):
    content = chr(10).join(content_parts)
    d = os.path.dirname(filepath)
    if d: os.makedirs(d, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content.strip() + chr(10))
    print(f"OK: {os.path.basename(filepath)} ({len(content)} chars)")

print("Build script starting...")