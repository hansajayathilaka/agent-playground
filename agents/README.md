# Seed agent content

- `templates/<name>/`: folders copied into a new agent by `CreateAgent`. `template.json` describes the template and is not copied. Text files may use `{{agent_name}}` and `{{agent_description}}`.
- Runtime agents live on the data volume (`AGENTS_ROOT`), not here.
- `_builder/`: the Builder system agent. The API copies it to `AGENTS_ROOT/_builder` at startup when that folder is missing (`BUILDER_SEED_DIR`).
