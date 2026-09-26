# Repository Guidelines

## Project Overview

HexSecGPT is a single-user terminal application (Rich TUI) that wraps third-party LLM APIs behind a fixed "hacker persona" system prompt. It is a **client, not a model**: it holds no weights and trains nothing. Three backends are supported — OpenRouter's free tier, DeepSeek, and a local Ollama server.

The design problem it solves is that free-tier model names churn constantly. Instead of pinning a name that gets retired, the app resolves a working model at runtime and switches away mid-chat when one dies.

End state for a user: `python HexSecGPT.py` opens a menu; chatting needs a key *unless* the `local` provider is used, which needs none.

## Architecture & Data Flow

Four modules, no package directory, no build step. Import is by flat module name, so the CWD must be the repo root.

```
HexSecGPT.py                    # entry point, UI, brain, app loop
  ├── Config                    # providers, paths, theme
  ├── UI                        # Rich rendering only; no business logic
  ├── HexSecBrain               # OpenAI client + streaming chat
  ├── App                       # orchestration, menu, key entry
  ├── SeeOpenRouterFreeModels   # imported lazily, inside methods
  └── SeeLocalModels            # imported lazily, inside methods
```

Request path:

```
__main__ → check_dependencies(auto_install=True) → sys.exit(main())
main()    → argparse → --list-models?      → SeeOpenRouterFreeModels.main([])
          →            --list-local-models? → SeeLocalModels.main([])
          →            --provider?         → mutates Config (see state warning)
          →            --upgrade?          → run_upgrade() (non-fatal)
          → App(model_override).start()

App.start()  → setup()          # verify provider, build brain
             → _sync_ui()       # mirror state into UI
             → loop: banner() → main_menu() → get_input("MENU")
                      1 → run_chat()  (gated on self._connected)
                      2 → configure_key() → setup()
                      3 → about()
                      5 → switch_provider() → setup()
                      4 → sys.exit(0)

App.run_chat() → HexSecBrain.chat()  # generator; UI.stream_markdown() consumes
                    → resolve_model() # "auto" → a concrete model, once
                    → client.chat.completions.create(stream=True)
                    → on failure: _is_model_failure() → _switch_model() → retry ×3
```

**Model resolution is per-provider.** `HexSecBrain.provider` is captured in `__init__`, and `_catalogue()` returns the matching discovery module. `resolve_model()` and `_switch_model()` both branch on it. A dead local model must move to another *local* model, never reach for the OpenRouter tier.

**The menu renders before any auth.** This is load-bearing: option `[2]` is how a user configures a key, so gating the menu behind a working key would make it unreachable without one. `setup()` returning False is a normal state, not a fatal error.

## Key Directories

There is one flat source tree. No `src/`, no package, no build output.

| Path | Purpose |
|---|---|
| `HexSecGPT.py` | ~748 lines. Everything: config, UI, brain, app loop, CLI |
| `SeeOpenRouterFreeModels.py` | 321 lines. OpenRouter catalogue + free-tier ranking. Library *and* CLI |
| `SeeLocalModels.py` | 180 lines. Ollama discovery. Library *and* CLI |
| `upgrademanger.py` | 1024 lines. Self-upgrade with signature/path/zip-slip guards. Off by default |
| `test_hexsec.py` | 873 lines, 71 tests |
| `img/` | README screenshot assets |

## Development Commands

Run everything from the repo root — flat imports depend on it.

```bash
# Run the app
python HexSecGPT.py

# Full test suite (~75s; the upgrade tests are the slow ones)
python -m unittest test_hexsec

# One class, or one test — the fast loop while iterating
python -m unittest test_hexsec.TestProviderSwitching -v
python -m unittest test_hexsec.TestLocalModelResolution.test_auto_resolves_to_a_local_model -v

# List what each backend currently offers (these hit the network / localhost)
python HexSecGPT.py --list-models
python HexSecGPT.py --list-local-models

# Scripted smoke test: menu with no key, no network
printf '1\n3\n\n4\n' | python HexSecGPT.py
```

No linter or formatter is configured. Match surrounding style by hand; see below.

## Code Conventions & Common Patterns

**Docstrings explain *why*, not *what*.** Existing examples: `"""True if a model is usable at zero cost."""`, and the longer form that records a bug's root cause — the pricing parser documents that OpenRouter returns pricing as *strings* so `== 0` silently never matched. Follow that: when fixing a subtle bug, leave a comment naming the trap.

**Comments carry the invariant, not the mechanics.** E.g. `# Drop the half-built brain: run_chat() checks self.brain, which would otherwise pass on a client that never verified.`

**Type hints are partial and pragmatic.** Present on method signatures where useful (`def get_input(self, label: str = "COMMAND") -> str:`), absent on most internals. Add where it aids clarity; do not retrofit.

**Rich markup is inline in strings.** Panel titles, table cells, and messages embed `[bold green]…[/]`. When adding user-visible text, match the existing color vocabulary: `yellow` for locked/warning, `red` for failure, `green`/`cyan` for success, `magenta` for help.

**Two-module-as-CLI pattern.** Each discovery module exposes `main(argv=None) -> int` returning an exit code, plus library functions. `main()` in `HexSecGPT.py` dispatches to them. When adding a discovery source, mirror this shape.

**Error handling is deliberately best-effort in discovery, and strict in security.** Discovery swallows failures and returns `None`/empty so the caller reports it. The upgrade manager refuses rather than degrades. Match the right one: never let a network hiccup kill the app; never let an unverifiable upgrade proceed.

**`resolve_free_model` / `resolve_local_model` return `None` rather than raising** when nothing is available. The caller must handle `None` and say so — silently proceeding pins the app to a model that does not exist.

### State that persists and is not scoped

This is the largest hazard when editing. `Config` is a mutable class, reassigned at runtime:

- `Config.API_PROVIDER = chosen` (`switch_provider`) and `= args.provider` (`main`)
- `Config.PROVIDERS[name]["MODEL_NAME"] = ...` — mutated in both places
- `load_dotenv()` writes into `os.environ`, which persists for the whole process
- `App._connected`, `App._no_model`, `App.brain`, `HexSecBrain._resolved`

`switch_provider()` resets the target provider's `MODEL_NAME` to `AUTO_MODEL` because a model pinned for a different backend is meaningless. Preserve that reset when touching provider switching.

In tests, any of this must be saved and restored via `self.addCleanup(...)` — see `TestProviderSwitching.setUp`.

## Important Files

| File | Why it matters |
|---|---|
| `HexSecGPT.py` | Entry point and the only place UI, config, and orchestration meet. Verified starts: `Config` `:81`, `UI` `:143`, `HexSecBrain` `:255`, `App` `:446`, `main()` `:684` |
| `SeeLocalModels.py` | Reuses `EXCLUDED_FRAGMENTS` and `ConfigEnvPath` from the OpenRouter module — edit those there, not here |
| `SeeOpenRouterFreeModels.py` | Owns `MODELS_URL`, the on-disk cache, and the free-tier ranking used by `_score` |
| `requirements.txt` | Pinned ranges. `requests`/`packaging` are upgrade-manager-only, not needed to chat |
| `upgrade_config.json` | Self-upgrade is **off**: `update_server: ""` and `signature_key: ""`. Do not fill these in without explicit instruction |
| `version.json` | `2.0.0`; read by the upgrade manager and `exclude_patterns`-scanned backups |
| `.gitignore` | `.HexSec`, `.env*`, `*.key|*.pem|*.pfx`, `.model_cache.json`, `.upgrade_backups/` |

**Never commit `.HexSec`.** It holds the API key. The upgrade manager's `exclude_patterns` also excludes it from backups — keep that in sync if you add a new secret file.

## Runtime & Tooling Preferences

- **Python 3.12** is what this was developed against. No version pin exists in the repo; stay on 3.x stdlib-compatible code.
- **Stdlib `unittest` only.** No pytest, no tox, no CI config.
- **`pip`**, invoked as `pip3` on POSIX and `python -m pip` on Windows — mirror whichever form the surrounding installer uses.
- Importing `HexSecGPT` must stay **side-effect free**. `check_dependencies(auto_install=False)` is the guard; only `__main__` passes `True`. This is what makes the module importable for tests.
- Shells here are Windows-based: **bash process substitution (`diff <(a) <(b))` does not work** — use temp files or plain `git diff`.
- Canonical remote is `https://github.com/xbustcodex/HexSecGPT.git`. `hexsecteam` appears in marketing copy (`HTTP-Referer`, the About panel, README org/screenshot links) and must not be repointed; functional install/clone URLs use `xbustcodex`.

## Testing & QA

```bash
python -m unittest test_hexsec
# Expect: Ran 71 tests ... OK
```

Framework: stdlib `unittest`. Entry: `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` at the top of the file, then `unittest.main(verbosity=2)`.

| Class | Covers |
|---|---|
| `TestPricingDetection` | The `== 0` string-pricing bug |
| `TestChatSuitability` | Excluding audio-only, safety, router models |
| `TestFreeModelListing` | Catalogue, cache staleness, network failure |
| `TestModelResolutionInApp` | `auto` must never survive as a live model |
| `TestModelFailureDetection` | Retriable model errors vs fatal auth errors |
| `TestChatFallback` | Mid-chat model switching |
| `TestUpgradeGuards` | Traversal, zip-slip, signatures, secret-excluding backups |
| `TestCLI` | Flag dispatch |
| `TestStartupReachesMenuWithoutKey` | Menu reachable with no key; empty-catalogue reporting |
| `TestLocalProviderConfig` / `TestLocalModelResolution` / `TestLocalModelFiltering` / `TestProviderSwitching` | The `local` provider end to end |

**Helpers to reuse, not reinvent:** `write_file`, `write_json`, `stub_catalogue(monkey_target, payload, raise_exc)` which swaps `fetch_models` for a fixture catalogue, and the `FAKE_CATALOGUE` constant. `TestStartupReachesMenuWithoutKey._run_menu` drives `App.start()` with scripted input and records which branch ran.

**Test what a user observes.** Prefer asserting that a branch was taken, that a message was shown, or that a returned model differs from `AUTO_MODEL`. Do not assert on source text, do not re-pin incidental wording, and do not add a test that merely restates the implementation — those are removed rather than maintained.

**Verify the new path, not just the suite.** After changing chat, provider switching, or resolution, run the scripted smoke test (`printf '1\n3\n\n4\n' | python HexSecGPT.py`) and confirm the failure is *explained on screen* rather than surfacing as a bare error in a stream panel. Silence is the bug this codebase keeps hitting.

No coverage tool is configured. When adding a module, add tests for it in the same change.
