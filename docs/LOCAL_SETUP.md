# Local Setup (macOS + VS Code + Claude Code)

## 1. Get this branch

```bash
cd ~/path/to/ollama_campaign_agent      # or: git clone https://github.com/dhruv138/ollama_campaign_agent.git
git fetch origin claude_code
git checkout claude_code
git pull origin claude_code
```

## 2. Python dependency

```bash
python3 --version                # needs 3.10+
python3 -m pip install pyyaml
```

## 3. Ollama

```bash
open -a Ollama                   # or: ollama serve
ollama pull qwen3:1.7b
curl -s http://localhost:11434/api/tags   # should list qwen3:1.7b
```

## 4. Vault paths

- `config.yaml` → `vault_path` must point to your real vault
  (currently `/Users/dhruvmukul/Icemoor_Obsidian_Vault`).
- `config.e2e.yaml` → `vault_path` points to a disposable copy used for testing.
  Create or refresh it with:

```bash
rm -rf ~/Icemoor_Obsidian_Vault_E2E
cp -R ~/Icemoor_Obsidian_Vault ~/Icemoor_Obsidian_Vault_E2E
```

## 5. Check that everything works

```bash
python3 campaign_agent_regression.py                       # no Ollama; expect 0 failed
python3 campaign_agent_regression.py --full                # uses Ollama; several minutes
python3 campaign_agent.py "Sessions/Session_37.md" --config config.e2e.yaml --dry-run
```

## 6. Claude Code in VS Code

1. VS Code 1.94+. Press `Cmd+Shift+X`, search **Claude Code** (publisher Anthropic), click **Install**.
2. Open the `ollama_campaign_agent` folder in VS Code (File → Open Folder).
3. Click the Spark icon (top right of an open file, or in the left Activity Bar) and **Sign in**
   with your Claude.ai account.
4. To continue the cloud conversation: in the Claude Code panel click **Session history** → **Web** tab →
   pick the session. (You must be signed in with your Claude.ai subscription.) It downloads a
   copy; later changes don't sync back to the web.
5. Optional: to run `claude` in the integrated terminal you also need the standalone CLI:
   see https://code.claude.com/docs/en/setup.

Claude Code reads `CLAUDE.md` in this repo automatically, so a fresh local session already
knows the project commands and safety rules.
