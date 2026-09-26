
***

<div align="center">

  # HexSecGPT

  <p>
    <strong>An advanced AI framework, engineered to explore the frontiers of language model interactions.</strong>
  </p>
   
  <h4>
    <a href="https://github.com/hexsecteam/">GitHub</a>
    <span> · </span>
    <a href="https://www.instagram.com/hex.sec/">Instagram</a>
    <span> · </span>
    <a href="https://www.youtube.com/@hex_sec">YouTube</a>
  </h4>
</div>

---

## 🚀 Showcase

Here is a glimpse of the HexSecGPT framework in action.

![HexSecGPT Demo Screenshot](https://github.com/hexsecteam/HexSecGPT/blob/main/img/home.png)



---

## :notebook_with_decorative_cover: Table of Contents

- [About The Project](#star2-about-the-project)
  - [What is this Repository?](#grey_question-what-is-this-repository)
  - [The Real HexSecGPT: Our Private Model](#gem-the-real-HexSecGPT-our-private-model)
- [Features](#dart-features)
- [Getting Started](#electric_plug-getting-started)
  - [Prerequisites: API Key](#key-prerequisites-api-key)
  - [Installation](#gear-installation)
- [Configuration](#wrench-configuration)
- [Usage](#eyes-usage)
- [Contributing](#wave-contributing)
- [License](#warning-license)

---

## :star2: About The Project

HexSecGPT is designed to provide powerful, unrestricted, and seamless AI-driven conversations, pushing the boundaries of what is possible with natural language processing.

### :grey_question: What is this Repository?

This repository contains an open-source framework that demonstrates the *concept* of HexSecGPT. It utilizes external, third-party APIs from providers like **OpenRouter** or **DeepSeek** and combines them with a specialized system prompt. This allows a standard Large Language Model (LLM) to behave in a manner similar to our private HexSecGPT, offering a preview of its capabilities.

**It is important to understand:** This code is a wrapper and a proof-of-concept, not the core, fine-tuned HexSecGPT model itself.

### :gem: The Real HexSecGPT: Our Private Model

While this repository offers a glimpse into HexSecGPT's potential, our flagship offering is a **privately-developed, fine-tuned Large Language Model.**

Why choose our private model?
- **Ground-Up Development:** We've trained our model using advanced techniques similar to the DeepSeek methodology, focusing on pre-training, Supervised Fine-Tuning (SFT), and Reinforcement Learning (RL).
- **Superior Performance:** The private model is significantly more intelligent, coherent, and capable than what can be achieved with a simple system prompt on a public API.
- **Enhanced Security & Privacy:** Offered as a private, managed service to ensure security and prevent misuse.
- **True Unrestricted Power:** Built from the core to handle a wider and more complex range of tasks without the limitations of public models.

#### How to Access the Private Model

Access to our private model is exclusive. To inquire about services and pricing, please contact our team via Telegram.

➡️ **Join our Telegram Channel for more info:** [https://t.me/hexsec_tools](https://t.me/hexsec_tools)

---

## :dart: Features

- **Powerful AI Conversations:** Get intelligent and context-aware answers to your queries.
- **Unrestricted Framework:** A system prompt designed to bypass conventional AI limitations.
- **Easy-to-Use CLI:** A clean and simple command-line interface for smooth interaction.
- **Cross-Platform:** Runs on Windows, Linux, macOS and Termux.

---

## :electric_plug: Getting Started

Follow these steps to get the HexSecGPT framework running on your system.

### :key: Prerequisites: API Key

To use this framework, you **must** obtain an API key from a supported provider. These services offer free tiers that are perfect for getting started.

1.  **Choose a provider:**
    *   **OpenRouter:** Visit [OpenRouter.ai](https://openrouter.ai/keys) to get a free API key. They provide access to a variety of models.
    *   **DeepSeek:** Visit the [DeepSeek Platform](https://platform.deepseek.com/api_keys) for a free API key to use their powerful models.

2.  **Copy your API key.** You will need to paste it into the script when prompted during the first run.

### :gear: Installation

We provide simple, one-command installation scripts for your convenience.

#### **Windows**
1. Download the `install.bat` script from this repository.
2. Double-click the file to run it. It will automatically clone the repository and install all dependencies.

#### **Linux / Termux**
1. Open your terminal.
2. Run the following command. It will download the installer, make it executable, and run it for you.
   ```bash
   bash <(curl -s https://raw.githubusercontent.com/xbustcodex/HexSecGPT/main/install.sh)
   ```

<details>
<summary>Manual Installation (Alternative)</summary>

If you prefer to install manually, follow these steps.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/xbustcodex/HexSecGPT.git
    ```
2.  **Navigate to the directory:**
    ```bash
    cd HexSecGPT
    ```
3.  **Install Python dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
</details>

---

## :wrench: Configuration

You can easily switch between API providers.

1.  Open the `HexSecGPT.py` file in a text editor.
2.  Locate the `API_PROVIDER` variable at the top of the file.
3.  Change the value to either `"openrouter"` or `"deepseek"`.

    ```python
    # HexSecGPT.py

    # Change this value to "deepseek" or "openrouter"
    API_PROVIDER = "openrouter" 
    ```
4. Save the file. The script will now use the selected provider's API.

---

## :lock: Security

### Your API key

The key is stored in a `.HexSec` file in the project directory. This file is
listed in `.gitignore` and **must never be committed**. If you ever share this
folder, a screenshot, or a zip of it — rotate the key at your provider first.

On Linux/macOS you can restrict it further:

```bash
chmod 600 .HexSec
```

### The self-upgrade manager

`upgrademanger.py` is **off by default** and stays disabled until you configure
it. It refuses to install anything it cannot authenticate.

| Protection | Behaviour |
|---|---|
| `signature_key` empty | Downloads are refused entirely |
| `update_server` not `https://` | Refused |
| HMAC-SHA256 mismatch | Package rejected, nothing written |
| Manifest path escaping the project | Rejected before any write |
| Zip/tar traversal, links, symlinks | Rejected |
| Hash mismatch on a file | File not written |
| `monitor` command | Reports updates only, never installs unattended |

To enable it, set both values in `upgrade_config.json`:

```json
{
    "update_server": "https://your-domain.example/upgrades",
    "signature_key": "a-long-random-secret"
}
```

Packages must be served as `<version>.zip` with a detached HMAC-SHA256 signature
at `<version>.zip.sig`, generated with that same key:

```bash
python -c "import hmac,sys;print(hmac.new(sys.argv[2].encode(),open(sys.argv[1],'rb').read(),'sha256').hexdigest())" pkg.zip "$SIGNATURE_KEY" > pkg.zip.sig
```

```bash
python upgrademanger.py     # then: status / upgrade / rollback / monitor
```

Backups are written to `.upgrade_backups/` and exclude `.HexSec`, `.env` and
key material, so your credentials are never duplicated into them.
---
## 📽️ Demo Setup

▶️ YouTube Demo:  
[https://www.youtube.com/watch?v=EM08JC4Mv6c](https://www.youtube.com/watch?v=EM08JC4Mv6c)
## :eyes: Usage

```bash
python3 HexSecGPT.py
```

The first time you run it, you will be prompted to enter your API key. It is saved
to `.HexSec` (gitignored) and reused on later runs.

### Command-line options

| Flag | Effect |
|---|---|
| `--list-models` | List the currently-free OpenRouter models and exit |
| `--model <id>` | Pin a specific model for this run |
| `--provider openrouter\|deepseek` | Override the provider for this run |
| `--upgrade [version]` | Run the upgrade manager before starting |

```bash
python HexSecGPT.py --list-models
python HexSecGPT.py --model nvidia/nemotron-3-ultra-550b-a55b:free
python HexSecGPT.py --provider deepseek
```


## 🔄 Model Compatibility (OpenRouter)

Free models on OpenRouter are retired and rate-limited constantly. HexSecGPT
therefore **resolves a working model at runtime** instead of hard-coding a name:

1. `MODEL_NAME` is set to `auto`. On startup the app queries the OpenRouter
   catalogue and picks a free, chat-capable model.
2. The catalogue is cached to `.model_cache.json` for an hour, so an offline
   start still works from the last known-good list.
3. If a model 404s, is rate-limited, or has no live endpoint mid-chat, the app
   switches to another free model and tells you it did so.
4. If your pinned model has been retired, the pin is discarded rather than
   failing — a dead model name is never fatal.

### Checking the free tier

```bash
python SeeOpenRouterFreeModels.py            # human-readable, with context sizes
python SeeOpenRouterFreeModels.py --json     # machine-readable
python SeeOpenRouterFreeModels.py --refresh  # bypass the cache
```

No API key is required: the public `/models` endpoint is open, so discovery works
before you have configured a key.

### Pinning a model

Resolution order is `--model` → `HEXSEC_MODEL` → `HEXSEC_MODEL` in `.HexSec` →
best available free model.

```bash
# one-off
python HexSecGPT.py --model qwen/qwen3.8-27b:free

# persistent
export HEXSEC_MODEL=qwen/qwen3.8-27b:free
```

To pin a model permanently in code, set `Config.PROVIDERS["openrouter"]["MODEL_NAME"]`
to a concrete id instead of `Config.AUTO_MODEL`.

> ⚠️ Free models are best-effort. They can be disabled, rate-limited or removed
> without notice, and OpenRouter enforces its own content policy server-side
> regardless of the system prompt. Pin a specific model if you need repeatable
> behaviour.

---

## :test: Tests

```bash
python -m unittest test_hexsec -v
```

The suite is offline and deterministic: model resolution, chat-model filtering,
runtime model fallback and every self-upgrade guard (path containment, signature
verification, secret-excluding backups, rollback integrity) are covered.








