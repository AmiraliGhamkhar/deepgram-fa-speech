# Performance audit reproduction probes

> Archived **pre-fix** probes/output. Some scripts intentionally demonstrate behavior that is now rejected, so they are not expected to complete unchanged after remediation. Use `tests/test_performance_fixes.py` and `tests/test_windows_clipboard_policy.py` for current regression checks; see [PERFORMANCE_FIXES.md](PERFORMANCE_FIXES.md).

These are the exact additional probes used for [the 2026-10-14 audit](PERFORMANCE_AUDIT.md). They are diagnostic examples, not new production modules or permanent regression tests. Run from the repository root after installing both development requirement sets. No real credentials, microphone, native input, or live upstream are needed.

Save each fenced Python block to the indicated ignored `.cache/` path, then run the command shown. The desktop probe replaces the SDK client for its lifecycle test; the API probe replaces the grant client before requests. Both use artificial inputs only.

## Desktop lifecycle and processing

```bash
mkdir -p .cache
# Save the following Python block as .cache/audit_probes.py
PYTHONPATH=. .venv/bin/python .cache/audit_probes.py
```

```python
import json, os, statistics, threading, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import yaml
from medical_stt.app import LiveMedicalSTT, LatencyTracker
from medical_stt.audio.queue import BoundedAudioQueue
from medical_stt.config import Settings, load_correction_rules_raw
from medical_stt.injection.backend import DryRunBackend, FailingBackend
from medical_stt.injection.text_injector import TextInjector
from medical_stt.processing.terminology import TerminologyEngine
from medical_stt.processing.normalize import normalize
from medical_stt.processing.bidi import to_injected, to_display
from medical_stt.stt.base import TranscriptEvent, ErrorCategory, ProviderError
from medical_stt.stt.deepgram_provider import DeepgramProvider
from medical_stt.stt.reconnect import ReconnectPolicy, exponential_backoff_delay, decide

out = {}
p = DeepgramProvider(Settings(), token_provider=lambda: 'fake')
q = BoundedAudioQueue()
for _ in range(10):
    q.put_nowait(b'\0' * 2560)
    p.send_audio(q.get())
    q.task_done()
out['before_connection'] = {'chunks_consumed': q.stats().get_total, 'queue_drops': q.stats().dropped_total, 'connections_opened': p.connections_opened}

entered, release = threading.Event(), threading.Event()
calls = []
class Connection:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def on(self, *args): pass
    def start_listening(self): calls.append('listen_started')
    def send_finalize(self): calls.append('finalize')
    def send_close_stream(self): calls.append('close_stream')
def token():
    entered.set()
    assert release.wait(5)
    return 'fake'
connection = Connection()
client = SimpleNamespace(listen=SimpleNamespace(v1=SimpleNamespace(connect=lambda **kw: connection)))
p = DeepgramProvider(Settings(), token_provider=token)
with patch('deepgram.DeepgramClient', return_value=client):
    t = threading.Thread(target=p.start, args=(lambda e: None, lambda e: None))
    t.start()
    assert entered.wait(5)
    p.stop()
    release.set()
    t.join(5)
    assert not t.is_alive()
out['stop_during_token_fetch'] = calls

try:
    exponential_backoff_delay(ReconnectPolicy(), 1025)
except Exception as exc:
    out['retry_1025'] = type(exc).__name__ + ': ' + str(exc)
error = ProviderError(ErrorCategory.RATE_LIMIT, 'fake')
out['retry_after_10_distinct_delays_50_clients'] = sorted(set(decide(ReconnectPolicy(), error, 1, retry_after=10).delay_seconds for _ in range(50)))

engine = TerminologyEngine()
engine.load(load_correction_rules_raw())
event = TranscriptEvent(text='بیمار امروز ویزیت شد', is_final=True, speech_final=True)
for name, backend, mode in [('failed_injection', FailingBackend(), 'paste'), ('type_mode', DryRunBackend(), 'type')]:
    actions = []
    stt = LiveMedicalSTT.__new__(LiveMedicalSTT)
    stt.latency = LatencyTracker()
    stt.settings = Settings(inject_mode=mode)
    stt.terminology = engine
    stt.overlay = SimpleNamespace(set_done=lambda text: actions.append('done'), set_idle=lambda: actions.append('idle'))
    stt.injector = TextInjector(backend=backend)
    stt._finalize_utterance(event)
    out[name] = {'ui_actions': actions, 'error': stt.injector.last_error, 'paste_count': len(getattr(backend, 'pasted', [])), 'type_count': len(getattr(backend, 'typed', []))}

cases = yaml.safe_load(Path('benchmarks/corpus.yaml').read_text())['cases']
def percentiles(samples):
    s = sorted(samples)
    return {'p50_ms': round(statistics.median(s)*1000,3), 'p95_ms': round(s[min(len(s)-1,int(len(s)*.95))]*1000,3), 'max_ms': round(max(s)*1000,3)}
texts = [c['reference'] for c in cases]
for text in texts:
    to_display(engine.apply(normalize(text)))
stages = {key: [] for key in ['normalize','terminology','injected_bidi','display_bidi','total']}
for _ in range(100):
    for text in texts:
        t0=time.perf_counter(); n=normalize(text); t1=time.perf_counter(); r=engine.apply(n); t2=time.perf_counter(); to_injected(r); t3=time.perf_counter(); to_display(r); t4=time.perf_counter()
        for key, duration in zip(stages, [t1-t0,t2-t1,t3-t2,t4-t3,t4-t0]): stages[key].append(duration)
out['processing'] = {'cases': len(texts), 'iterations_per_case': 100, 'max_input_chars': max(map(len,texts)), 'active_rules': engine.rule_count, 'total_rules': engine.total_rule_count, 'stages': {key:percentiles(v) for key,v in stages.items()}}
loads=[]
for _ in range(10):
    t0=time.perf_counter(); e=TerminologyEngine(); e.load(load_correction_rules_raw()); loads.append(time.perf_counter()-t0)
out['warm_rules_read_and_build'] = percentiles(loads)
for repeats in [1,10,100]:
    text = ('بیمار در آی سی یو فشار خون 120/80 و دوز 5 mg بدون درد دارد. ' * repeats)
    samples=[]
    for _ in range(20):
        t0=time.perf_counter(); to_display(engine.apply(normalize(text))); samples.append(time.perf_counter()-t0)
    out[f'long_text_{len(text)}_chars'] = percentiles(samples)
print(json.dumps(out,ensure_ascii=False,indent=2))
```

### Recorded output

```json
{
  "before_connection": {
    "chunks_consumed": 10,
    "queue_drops": 0,
    "connections_opened": 0
  },
  "stop_during_token_fetch": [
    "listen_started"
  ],
  "retry_1025": "OverflowError: int too large to convert to float",
  "retry_after_10_distinct_delays_50_clients": [
    10.0
  ],
  "failed_injection": {
    "ui_actions": [
      "done",
      "idle"
    ],
    "error": "backend reported failure",
    "paste_count": 0,
    "type_count": 0
  },
  "type_mode": {
    "ui_actions": [
      "done",
      "idle"
    ],
    "error": null,
    "paste_count": 1,
    "type_count": 0
  },
  "processing": {
    "cases": 22,
    "iterations_per_case": 100,
    "max_input_chars": 221,
    "active_rules": 727,
    "total_rules": 835,
    "stages": {
      "normalize": {
        "p50_ms": 0.018,
        "p95_ms": 0.05,
        "max_ms": 0.116
      },
      "terminology": {
        "p50_ms": 0.018,
        "p95_ms": 0.046,
        "max_ms": 0.151
      },
      "injected_bidi": {
        "p50_ms": 0.001,
        "p95_ms": 0.002,
        "max_ms": 0.02
      },
      "display_bidi": {
        "p50_ms": 0.06,
        "p95_ms": 0.105,
        "max_ms": 0.384
      },
      "total": {
        "p50_ms": 0.097,
        "p95_ms": 0.155,
        "max_ms": 0.627
      }
    }
  },
  "warm_rules_read_and_build": {
    "p50_ms": 6.206,
    "p95_ms": 7.07,
    "max_ms": 7.07
  },
  "long_text_60_chars": {
    "p50_ms": 0.148,
    "p95_ms": 0.288,
    "max_ms": 0.288
  },
  "long_text_600_chars": {
    "p50_ms": 1.244,
    "p95_ms": 1.33,
    "max_ms": 1.33
  },
  "long_text_6000_chars": {
    "p50_ms": 17.99,
    "p95_ms": 35.292,
    "max_ms": 35.292
  }
}
```

## API saturation and validation

```bash
mkdir -p .cache
# Save the following Python block as .cache/api_audit_probes.py
PYTHONPATH=. .venv/bin/python .cache/api_audit_probes.py
```

```python
import asyncio, json, os, time
os.environ['DEEPGRAM_API_KEY']='fake-for-local-audit'
os.environ['HOST_SHARED_SECRET']='s'*40
os.environ['HOST_ALLOW_HTTP']='1'
import httpx
from host.app import create_app, app as module_app
from host.core import HostSettings

class Grant:
    closed = False
    calls = 0
    async def grant(self,*args):
        self.calls += 1
        await asyncio.sleep(.05)
        return {'access_token':'fake-local-only','expires_in':30}
    async def aclose(self): self.closed=True

async def main():
    result={}
    for count in (50,100,150):
        app=create_app(HostSettings(deepgram_api_key='fake',shared_secret='s'*40,allow_http=True,client_burst=1000,global_burst=2000))
        await app.state.grant_client.aclose()
        app.state.grant_client=Grant()
        transport=httpx.ASGITransport(app=app,raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport,base_url='http://localhost') as client:
            async def one():
                t=time.perf_counter()
                r=await client.post('/v1/session',headers={'Authorization':'Bearer '+'s'*40},json={})
                return r.status_code,time.perf_counter()-t
            outcomes=await asyncio.gather(*(one() for _ in range(count)))
            times=sorted(t*1000 for s,t in outcomes if s==200)
            result[f'burst_{count}']={'statuses':{str(s):sum(a==s for a,b in outcomes) for s,t in outcomes},'success_p95_ms':round(times[int(len(times)*.95)],2),'inflight_at_end':app.state.inflight.in_flight}
            r=await client.post('/v1/session',headers={'Authorization':'Bearer '+'s'*40},content='{"ttl_seconds":1e309}')
            result['nonfinite_ttl_status']=r.status_code
            started=asyncio.Event(); gate=asyncio.Event()
            async def slow_body():
                yield b'{'
                started.set()
                await gate.wait()
                yield b'}'
            task=asyncio.create_task(client.post('/v1/session',headers={'Authorization':'Bearer '+'s'*40},content=slow_body()))
            await started.wait()
            await asyncio.sleep(.25)
            result['slow_body_after_250ms']={'request_pending':not task.done(),'inflight_grants':app.state.inflight.in_flight}
            gate.set(); await task
        await app.state.grant_client.aclose()
    await module_app.state.grant_client.aclose()
    print(json.dumps(result,indent=2))
asyncio.run(main())
```

### Recorded output

```json
{
  "burst_50": {
    "statuses": {
      "200": 50
    },
    "success_p95_ms": 94.44,
    "inflight_at_end": 0
  },
  "nonfinite_ttl_status": 500,
  "slow_body_after_250ms": {
    "request_pending": true,
    "inflight_grants": 0
  },
  "burst_100": {
    "statuses": {
      "200": 100
    },
    "success_p95_ms": 127.94,
    "inflight_at_end": 0
  },
  "burst_150": {
    "statuses": {
      "200": 100,
      "503": 50
    },
    "success_p95_ms": 213.35,
    "inflight_at_end": 0
  }
}
```

## Separate cold rule-load measurement

Run in a fresh process, rather than after the main processing probe has warmed the YAML cache:

```bash
.venv/bin/python - <<'PY'
import time
from medical_stt.config import load_correction_rules_raw
from medical_stt.processing.terminology import TerminologyEngine
t = time.perf_counter()
engine = TerminologyEngine()
engine.load(load_correction_rules_raw())
print(round((time.perf_counter() - t) * 1000, 2), "ms")
PY
```

Observed: **290.43 ms**, one sample, excluding process/module import time. This is neither a cold-disk guarantee nor a full application-startup benchmark.
