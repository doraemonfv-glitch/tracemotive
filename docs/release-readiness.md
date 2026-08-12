# v0.1 release readiness

Issue 16 validation is local-only. These commands build and inspect artifacts;
they do not upload, publish, create a release, or push a tag.

The Python distribution and import package are both `tracemotive`, with
version `0.1`. Public package-index publication remains a separate
maintainer-controlled release action; this checklist validates the local
artifacts and installed runtime under the final pre-release identity.

The declared runtime and optional dependencies are:

- core: `fastapi>=0.110,<1`
- `server`: `uvicorn>=0.30,<1`
- `openai-agents`: `openai-agents>=0.17,<0.18`

Build from a clean checkout after installing the standard build frontend:

```text
python -m pip install build
python -m build --sdist --wheel
```

Inspect `dist/` and install the wheel into a new virtual environment from a
temporary working directory, not from the repository root:

```text
python -m venv .venv-check
python -m pip install dist/<wheel-file>.whl
python -c "import tracemotive; print(tracemotive.__file__)"
```

The installed path must be in the temporary environment and core import must
work without the `openai-agents` extra. The optional integration may be tested
separately with:

```text
python -m pip install "dist/<wheel-file>.whl[openai-agents,server]"
```

The Collector smoke uses the existing loopback-only factory command from the
root README. The frontend is not embedded in the Python artifacts; run `npm
ci`, `npm test`, and `npm run build` in `frontend/` using the Node engine range
declared in `frontend/package.json`.
