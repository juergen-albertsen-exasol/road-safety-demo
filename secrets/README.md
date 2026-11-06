# secrets/

Only the v5 dashboard needs a secret: an **OpenAI API key**.

1. Create an API key at <https://platform.openai.com/api-keys> and make sure the account has credits (Billing).
2. Put the key in **`secrets/openai_api_key`**: one line, nothing else. Use your editor, or run this in a terminal outside your agent:
   ```bash
   umask 077 && printf '%s' 'sk-…' > secrets/openai_api_key
   ```
   **Never paste a key into a chat with an AI assistant.** A key that lands in a transcript has to be rotated.
3. Make it readable only by you and link it to where the v5 dashboard reads it. Dash Server starts at login and doesn't see shell variables, so the app reads a file:
   ```bash
   chmod 600 secrets/openai_api_key
   ln -sf "$PWD/secrets/openai_api_key" ~/.exasol-starter-kit/credentials/openai_api_key
   ```

## Road safety demo: `dash_readonly_password`

The road safety dashboard reads the database as **`dash_readonly`**, a user with `SELECT` on `ROAD_DATA` and `ROAD_SAFETY` only, in the Exasol Personal deployment `road-safety-demo`.

- `secrets/dash_readonly_password` holds its password: generated, mode 600, never printed.
- Dash Server's profile `road-safety-demo` keeps its own copy in `~/.exasol-starter-kit/dash-server/instance/exasol-secrets/road-safety-demo.json`.
- If you change the password, update both: `ALTER USER dash_readonly IDENTIFIED BY "..."` as `sys`, then the `secret` field of that JSON file.

Everything in this folder except this README and `openai_api_key.example` is gitignored. Check with `git check-ignore -v secrets/openai_api_key`.
