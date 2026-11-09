# secrets/

Everything in this folder except this README is gitignored. Check with `git check-ignore -v secrets/dash_readonly_password`.

## `dash_readonly_password`

The dashboard reads the database as **`dash_readonly`**, a user with `SELECT` on `ROAD_DATA` and `ROAD_SAFETY` only.

- `scripts/setup_access.py` generates the password into this file (mode 600) and creates or updates the user. Running it again reuses the file.
- `scripts/start_dash_server.sh` passes it to Dash Server, which keeps its own copy in its secret store for the profile `road-safety-demo`.
- To change it: delete the file, run `setup_access.py` again, then restart Dash Server with `DASH_SERVER_EXASOL_OVERWRITE=true` once so it takes the new password.

The `sys` password of the database is not stored here: the scripts read it from the Exasol Personal deployment (`~/.exasol/personal/deployments/road-safety-demo/secrets.json`). Never print or paste a password into a chat with an agent.
