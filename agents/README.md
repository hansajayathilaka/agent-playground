# Seed agent content

- `templates/<name>/`: folders copied into a new agent by `CreateAgent`. `template.json` describes the template and is not copied. Text files may use `{{agent_name}}` and `{{agent_description}}`.
- Runtime agents live on the data volume (`AGENTS_ROOT`), not here.
