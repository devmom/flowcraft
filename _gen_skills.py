
import os, sys, json, yaml, subprocess, asyncio, threading, time as _time
from datetime import datetime, timezone
from pathlib import Path

SKILLS_DIR = r"D:/work/FlowCraft/core/flowcraft_core/skills"
WS_DIR = r"D:/work/FlowCraft/workspace/skills"

def w(path, content):
    d = os.path.dirname(path)
    if d: os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.strip() + "
")
    print(f"OK: {os.path.basename(path)} ({len(content)} chars)")

print("Generator loaded")
