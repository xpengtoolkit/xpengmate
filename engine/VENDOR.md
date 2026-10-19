# Vendored code

The modules listed below are copied **verbatim** from the XPENG Home Assistant
integration, [xpengtoolkit/xpengtoolkit](https://github.com/xpengtoolkit/xpengtoolkit)
(`custom_components/xpeng/`). They carry the three things that are expensive to
get right and have already been proven against a live car: the app-tier request
signing and login chain, the mTLS MQTT client with its JSON decode and
partial-frame merging, and the mapping from XPENG's telemetry tree to the JSON
shapes TeslaMate 4.x expects.

Vendored from the **0.6.16 release** (the first release that ships the provider;
the working-tree copy this engine was built from was byte-identical to it).

| File | Upstream | sha256 |
|---|---|---|
| `xpeng_api.py` | `custom_components/xpeng/xpeng_api.py` | see below |
| `xpeng_errors.py` | `custom_components/xpeng/xpeng_errors.py` | |
| `mqtt_client.py` | `custom_components/xpeng/mqtt_client.py` | |
| `vin_descriptor.py` | `custom_components/xpeng/vin_descriptor.py` | |
| `teslamate_mapping.py` | `custom_components/xpeng/teslamate_mapping.py` | `3db249a1…` |
| `teslamate_provider.py` | `custom_components/xpeng/teslamate_provider.py` | `ad6d475f…` |

Only two things differ from upstream:

* `const.py` is **not** vendored — upstream's imports Home Assistant's sensor and
  binary-sensor device classes. `xpeng_engine/const.py` provides the three plain
  values the vendored modules actually import.
* `teslamate_provider.py`'s `async_start_teslamate_provider()` entry point is not
  used. The engine constructs `TeslaMateProvider(source, options)` directly,
  because there is no Home Assistant coordinator to wrap.

Everything else in `xpeng_engine/` (`config.py`, `state.py`, `api_session.py`,
`service.py`) is original to this project.

## Why this is vendored rather than a dependency

Upstream is a Home Assistant integration, not a library: there is no package, no
stable surface, and the code moves quickly. Vendoring pins the revision the
engine was tested against, and keeps the diff to upstream reviewable — refreshing
it is a deliberate copy-and-diff, never an implicit upgrade.

## Refreshing

```bash
SRC=/path/to/xpengtoolkit/custom_components/xpeng
cp "$SRC"/{xpeng_api,xpeng_errors,mqtt_client,vin_descriptor,teslamate_mapping,teslamate_provider}.py engine/xpeng_engine/
python3 - <<'PY'
import ast, pathlib
for p in pathlib.Path('engine/xpeng_engine').glob('*.py'):
    ast.parse(p.read_text())
print('ok')
PY
```

Then run `python -m unittest discover -s engine/tests` and the stack's smoke test
(`scripts/smoke-test.sh`).
