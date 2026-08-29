#!/usr/bin/env python3
"""Script per generare docs/openapi.json."""
import json
import sys
from pathlib import Path

# Assicuriamoci di essere nella directory giusta
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.api.app import app

docs_dir = project_root / "docs"
docs_dir.mkdir(exist_ok=True)

openapi_schema = app.openapi()
openapi_path = docs_dir / "openapi.json"

with open(openapi_path, "w") as f:
    json.dump(openapi_schema, f, indent=2)

print(f"Generato {openapi_path}")
print(f"Dimensione: {openapi_path.stat().st_size} bytes")
print(f"Endpoint: {len(openapi_schema.get('paths', {}))}")
