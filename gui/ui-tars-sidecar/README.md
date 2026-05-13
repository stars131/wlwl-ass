# UI-TARS Sidecar Placeholder

This folder is intentionally a small optional integration point, not a vendored
copy of `UI-TARS-desktop`.

Default runtime:

- Python `tools/gui_operator.py`
- Existing wlwl-ass `vision_tools.py` API config
- Activity/trajectory logging under `temp/`

Use a Node sidecar only when wlwl-ass needs direct reuse of UI-TARS browser,
remote-operator, or visualizer packages. The Python API server detects an
external sidecar via:

```powershell
$env:UI_TARS_SIDECAR_URL="http://127.0.0.1:19091"
```

Expected future contract:

- `GET /health`
- `POST /browser/run`
- `POST /remote/run`
- `POST /visualizer/report`

Keep the sidecar process isolated and talk to it over localhost HTTP/WebSocket.
