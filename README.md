# AI Pool for Home Assistant

Route AI calls across several providers from a single entity, with rotation to
spread daily quotas and automatic failover when a provider refuses.

[![CI](https://github.com/jmorille/ha-ai-pool/actions/workflows/ci.yml/badge.svg)](https://github.com/jmorille/ha-ai-pool/actions/workflows/ci.yml)
[![Validate](https://github.com/jmorille/ha-ai-pool/actions/workflows/validate.yml/badge.svg)](https://github.com/jmorille/ha-ai-pool/actions/workflows/validate.yml)

A pool publishes **one** entity in the domain it fronts. Your automations call
that entity and know nothing about the routing:

```yaml
actions:
  - action: ai_task.generate_data
    data:
      task_name: Weather announcement
      entity_id: ai_task.morning_pool   # the pool, not a provider
      instructions: "{{ prompt }}"
    response_variable: report
```

Four pool types are supported, each fronting its own domain:

| Pool type      | Publishes            | Delegates through                        |
| -------------- | -------------------- | ---------------------------------------- |
| `ai_task`      | `ai_task.*`          | `ai_task.async_generate_data`            |
| `conversation` | `conversation.*`     | `conversation.async_converse`            |
| `tts`          | `tts.*`              | `tts.async_get_media_source_audio`       |
| `stt`          | `stt.*`              | the member entity's audio stream handler |

## What this actually buys you

**Quota rotation.** Free tiers are commonly metered *per model*, so alternating
between two models on the same API key gives you two daily counters rather than
one. Rotating across three providers multiplies it again.

**Failover that knows why.** A provider that is momentarily overloaded should be
retried in five minutes; one that is out of allowance should sit out until
tomorrow; one with a bad API key should never be called again. These are
different situations and the pool treats them differently.

## The honest limitation, read this first

**No provider reports its remaining quota to Home Assistant.** The pool can only
count *its own* calls and compare them against a limit you type in. It cannot
see what the same API key spent elsewhere — another integration, a script, your
phone.

So the two halves of this integration have very different reliability:

- **Failover on error is exact.** The provider told us it refused.
- **Rotation on quota is an estimate.** It steers traffic away from a member you
  *believe* is spent.

That asymmetry is deliberate in the design: declared limits only influence
*ordering*, and a member believed to be exhausted is still tried as a last
resort. A wrong guess about a quota should never turn into a silent no-op.

## Install

### HACS

Add `https://github.com/jmorille/ha-ai-pool` as a custom repository of type
*Integration*, install **AI Pool**, restart Home Assistant, then add the
integration from *Settings → Devices & Services*.

### Manual

Copy `custom_components/ai_pool` into your Home Assistant `config/custom_components`
directory and restart.

## Configuration

Everything is configured in the UI, in three steps:

1. **Name and type** — which domain the pool fronts. This cannot be changed
   afterwards, because it decides which platform is loaded.
2. **Members and policy** — the member entities, in preference order, plus the
   selection strategy, the cooldown, and the cap on attempts per call.
3. **Daily allowances** — a declared limit and a weight per member. Use `0` when
   you do not know the limit.

Members are always picked from the pool's own domain, and pool entities are
excluded from the picker so a pool can never contain itself.

### Strategies

| Strategy      | Behaviour                                                          |
| ------------- | ------------------------------------------------------------------ |
| `round_robin` | Rotate on every call. This is what spreads load across quotas.     |
| `least_used`  | Whoever has the most allowance left goes first.                    |
| `priority`    | Always try in configured order. Pure failover, no load spreading.  |

`least_used` compares *shares* rather than raw counts, so a large allowance half
spent outranks a small one nearly spent. `weight` biases a member as if it had
proportionally more room.

### Failure handling

| Provider says                                | Classified as | Consequence                       |
| -------------------------------------------- | ------------- | --------------------------------- |
| `429`, `RESOURCE_EXHAUSTED`, quota, billing  | quota         | Out until the local day rolls     |
| `503`, `UNAVAILABLE`, high demand, overload  | capacity      | Cooldown, then eligible again     |
| `500`, `502`, `504`, timeout, connection     | transient     | Next member, no penalty           |
| `401`, `403`, invalid API key                | auth          | Disabled; retrying cannot help    |
| `400`, not supported, response schema        | unsupported   | Next member                       |
| anything else                                | unknown       | Next member, recorded for triage  |

Home Assistant flattens provider errors into a single exception type, so the
status is only recoverable from the message text. That is why classification is
pattern-based, and why `unknown` exists rather than being guessed at.

## Observability

Each member gets a diagnostic sensor counting the calls it served today, with
its status, remaining allowance, cooldown, and last error as attributes. The
config entry's *Download diagnostics* returns the same data plus the routing
policy.

Counters are persisted, so a restart at 18:00 does not hand a spent member a
fresh allowance. They reset on the local calendar day.

## Per-type notes

- **`ai_task`** — attachments are not supported. They arrive already resolved
  and there is no supported way to pass a resolved attachment to another entity,
  so Home Assistant rejects such calls before they reach the pool.
- **`conversation`** — a member answering with an error response counts as a
  failure, otherwise the pool would return "sorry" from the first broken member
  and never reach a working one. Languages are advertised as match-all.
- **`tts`** — languages and options are the *union* across members; a member
  that cannot handle a request raises and the next one is tried.
- **`stt`** — audio is buffered so a second member can be given the same
  recording. Audio *format* capabilities are the *intersection* across members,
  because the pipeline encodes once before any member is chosen. Recordings
  larger than the buffer limit fall back to a single attempt rather than being
  retried truncated.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest tests -q
.venv/bin/ruff check custom_components tests
```

The Home Assistant test harness imports `fcntl` and therefore **only runs on
Linux and macOS**. On Windows the provider-independent tests still run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_errors.py tests/test_strategies.py -q --noconftest
```

CI runs the full suite on Ubuntu against every supported Home Assistant
version, plus `hassfest` and HACS validation. The harness pins one Home
Assistant version per release, so the matrix selects versions by harness
version; `scripts/component_requirements.py` then reads the fronted components'
own dependencies from the installed manifests, which keeps them right across
those versions.

### Supported Home Assistant versions

Tested against **2026.8.3** and **2026.9.0**. 2026.8.3 is the minimum declared
in `hacs.json`; older versions are untested rather than known-broken.

### Releasing

Bump `version` in `custom_components/ai_pool/manifest.json`, then push a matching
tag:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The release workflow refuses to publish when the tag and the manifest disagree,
builds `ai_pool.zip`, and attaches it to a GitHub release.

## License

MIT
