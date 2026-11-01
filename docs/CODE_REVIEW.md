# Code review — SOLID pass over every section

A whole-repository review, then the refactoring it justified. Nothing here
changes behaviour: the acceptance criterion was that the existing test suite,
`ruff check .` and `mypy medical_stt host benchmarks` stay green throughout,
and they did at every commit.

## How it was reviewed

| Method | What it catches |
|--------|-----------------|
| Read every module, in responsibility order (entry point → state → I/O) | a class doing several jobs, a layer importing "upwards" |
| Function-length and file-length census (AST, not eyeballing) | methods that have quietly become four methods |
| Import-graph check per package | cycles, and facades that exist only because of a cycle |
| Cross-reference of docs against files | documentation that names files that no longer exist |
| The gates themselves, per commit | a refactor that changed behaviour |

The three gates after the work:

```
pytest tests/ -q                1228 passed, 4 skipped
ruff check .                    All checks passed!
mypy medical_stt host benchmarks  Success: no issues found in 80 source files
scripts/scan_secrets.py         clean (working tree)
scripts/measure_capacity.py     RESULT: PASS (all application-side criteria met)
```

## Section-by-section

### `medical_stt/configuration/` + `medical_stt/config.py`

**Found.** One 700-line `config.py` mixed the `Settings` model, YAML I/O,
validation, bundled-data catalogs, first-run bootstrap and DPAPI secret
loading. Every consumer imported it, so every consumer transitively imported
`sounddevice`-adjacent paths it never used.

**Changed.** Split into `errors`, `model`, `validation`, `yaml_io`,
`catalogs`, `bootstrap`, `loader`; `config.py` is now a 115-line facade. The
facade re-exports the *private* helpers tests rely on
(`_load_yaml`, `_valid_client_id`, `_reject_plaintext_secrets`, `os`, …),
because the alternative — deleting them — silently breaks callers.

**Left.** The facade is a deliberate shim over a stable public import path.
Removing it would be a breaking change with no quality gain.

### `medical_stt/session/`

**Found.** `LiveMedicalSTT` had grown into a god object: session state,
provider threads, transcript routing, rendering, injection, watchdog timers
and warnings, all in one class, each reachable from the others.

**Changed.** Now one module per responsibility — `dictation` (state and
composition), `runner` (threads and teardown order), `supervisor`
(retry policy), `transcripts` (event routing), `renderer` (text delivery),
`latency`, `recovery`, `warnings`, `watchdog`, `audio_health`, `messages`,
`runtime` (the `SessionHost` protocol that keeps the pieces from importing
each other's concrete classes).

**Left.** `runner.run_once` (189 lines) is still the largest function in the
repository. It is one linear, heavily-commented start→run→teardown sequence
whose *order* is the invariant; splitting it would scatter that order across
four methods without making any of them independently meaningful. It is the
next candidate if the session lifecycle ever needs a second implementation.

### `medical_stt/ui/`

**Found.** `control.py` was 815 lines: layout, a circle-button factory, a
`dict` of per-canvas paint state living outside the widget, window-position
persistence (including an atomic write), every Persian caption inline, and
the Start/Stop state machine. The same sentence existed twice with different
punctuation (`در حال ذخیره…` / `در حال ذخیره...`).

**Changed.**

- `ui/circles.py` — `CircleButton` owns its canvas, item ids and
  hover/press/disabled state; `tk` is injected, which is what lets the tests
  build real widgets headlessly.
- `ui/strings.py` — one definition per caption; the duplicated busy messages
  collapsed into single entries.
- `ui/state.py` — position persistence: tolerant load, atomic save.
- `control.py` keeps the state machine, the Tk threading rules and the
  layout, plus thin adapters (`_circle_button`, `_set_circle_enabled`,
  `_toggle`) so existing callers and tests are untouched.

**Left.** `_build` (114 lines) is a layout function: every line is a widget
and its placement, and reading it top to bottom is how a reviewer checks the
window. It is long but not complex.

### `host/`

**Found.** `core.py` was 1,137 lines and five responsibilities; `app.py` was
721 lines and did composition, middleware, probes, route logic and
environment parsing. Both were also copied standalone to cPanel, so every
module needs the dual `from .x import` / `from x import` form.

**Changed.**

| Module | Responsibility |
|--------|----------------|
| `config.py` | `HostSettings`, env parsing, Deepgram regions/TTLs, key shape |
| `identity.py` | hashed secrets, registry, constant-time authentication |
| `ratelimit.py` | token-bucket and fixed-window limiters |
| `limits.py` | the five admission-control objects, sized in one place |
| `grant.py` | Deepgram token exchange, sync and async |
| `request_guard.py` | HTTPS decisions and TTL clamping |
| `hardening.py` | security headers, plaintext refusal, probes, `/metrics` |
| `session_service.py` | the `/v1/session` use case, step by step |
| `core.py` | facade: the single documented import path |
| `app.py` | composition root (721 → 361 lines) + `main()` helpers |

**Left.** `HostSettings.from_env` (107 lines) reads ~30 variables in one
place on purpose: an operator comparing `host/.env.example` against the code
should see one block, not a chain of helpers. `provision._generate` (95
lines) and the `scripts/` entry points are one-shot operational tools whose
value is being readable end to end.

### `medical_stt/stt/`

**Found.** `DeepgramProvider.start()` (175 lines) mixed token acquisition,
SDK construction, stream-parameter assembly, four nested event handlers and
error classification. The parameter assembly is a pure function of settings.

**Changed.** Extracted `_connect_parameters()`, and pinned it with two new
tests: optional keys are *omitted* when unset (never `None`), and
`tag`/`extra`/`replace`/`keyterm` carry the configured values — including
that a mono session must not ask for two channels.

**Left.** The event handlers stay nested: they close over `on_transcript`,
`on_error`, `stop_event` and the single-terminal-error flag, and that
captured state is exactly what makes "one error per connection" correct.

### `medical_stt/injection/`

**Found.** `type_text`, `paste_text` and `send_backspaces` each repeated the
same try/except/record/`log.warning` block — and had drifted:
`send_backspaces` neither reset `last_error` nor logged.

**Changed.** One `_delegate(name, operation)` enforces the documented
contract ("return False and record `last_error`") and documents once why the
`except Exception` is deliberate: these run on the provider's callback
thread, where an escaping third-party exception would end dictation silently.

### `medical_stt/processing/`

Cohesive and small: normalize, numbers, negation, `fst` (deterministic
rewriter), terminology, confidence, bidi. `pipeline.py` composes them in one
order, so no caller chooses its own stage order. `bidi.py` is still the only
module allowed to emit directional marks, and a test pins that. No change
needed; `fst.apply` (71 lines) is the state machine it is meant to be.

### `medical_stt/security/`, `medical_stt/audio/`, `medical_stt/paths.py`

Small, single-purpose, no upward imports. Verified rather than changed:
secrets only ever reach DPAPI-protected storage, and
`scripts/scan_secrets.py` reports the working tree clean.

### `benchmarks/`

`metrics.py` scores a hypothesis against a reference (WER/CER, numeric
accuracy, term recall) and deliberately duplicates *no* production code: it
calls `processing.numbers`/`normalize` so a benchmark can never grade a
pipeline against a different definition than the one that shipped. Its
docstring defines every metric, which is the right place for those
definitions. No change.

### `scripts/`

Operational tools (`scan_secrets`, `measure_capacity`, `scrub_history`,
`verify_client_host`, `windows_injection_check`). Each is a linear `main()`
meant to be read before it is run against a real deployment; the long ones
are 96–119 lines of straight-line steps. Left as they are, and they remain
gates: `scan_secrets` and `measure_capacity` both pass.

### `tests/`

Structure-inspection tests were **retargeted, not deleted**, whenever a
behaviour moved: `CONFIG_SOURCES` now reads `host/config.py` plus the modules
that own each "refuses to start" check, the `delta-seconds only` and
https-only claims are asserted against `host/grant.py` and `host/config.py`,
and the status-glyph test asserts the catalog that owns the glyph. A test
that keeps passing after its subject moves is the only kind worth keeping.

## Cross-cutting findings

1. **A facade plus `ruff --fix` is a trap.** `ruff check --fix` deletes a
   facade's re-exports as F401s, which broke six tests. Both facades now list
   their re-exports in `__all__`, with a comment saying why the list must not
   be "tidied". Worth knowing before the next cleanup pass.
2. **The dual import layout is now declared, not fought.** `host/` must
   import both as `host.x` and as top-level `x`. `pyproject.toml` names every
   such module in one mypy override, disabling exactly the three codes that
   are spurious under that duality (`no-redef`, `unused-ignore`,
   `attr-defined`) and nothing else.
3. **Documentation was the least reliable section.** The architecture tree
   described an `app.py` that no longer existed and omitted `session/`,
   `configuration/`, `pipeline.py` and the UI helpers; the host file table
   named `core.py` for logic that had moved. Both READMEs, `host/README.md`
   and `docs/CPANEL_HOSTING.md` now name the module that owns each
   behaviour.
4. **Metrics reconciliation is a load-bearing invariant.** Every exit path
   from `/v1/session` now goes through one `SessionAttempt` object, so
   `session_success_total + session_failures_total == session_starts_total`
   cannot drift — including the `CancelledError` paths, which are
   `BaseException`s and were previously invisible.

## Known remaining work, in priority order

| Item | Size | Why it is left |
|------|------|----------------|
| `session/runner.run_once` | 189 lines | one linear lifecycle whose order is the invariant |
| `stt/deepgram_provider.start` | 175 lines | the four handlers' captured state is what makes them correct |
| `ui/control._build` | 114 lines | declarative layout; complexity is in the state machine, which is now separate |
| `host/config.from_env` | 107 lines | one block to audit against `.env.example` |
| `benchmarks/`, `scripts/` entry points | 60–120 lines | linear operational tools |

Line count was never the goal; *who owns each behaviour* was. `ui/control.py`
is a few lines longer than before despite carrying far less responsibility,
because the widget layer it now delegates to is documented where it lives.
