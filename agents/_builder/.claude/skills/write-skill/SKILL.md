---
name: write-skill
description: Add or improve a skill in an agent's .claude/skills folder. Use when an agent needs a repeatable procedure it should follow on request.
---

1. Name the procedure with a short lowercase-dash name (`summarise-pr`).
2. Write `.claude/skills/<name>/SKILL.md` with frontmatter:

   ```markdown
   ---
   name: <name>
   description: <what it does>. Use when <the situations or phrases that should trigger it>.
   ---
   ```

3. The body is numbered steps: inputs to read, what to produce, where to save it (`workspace/`), and how to check the result.
4. Put long reference material in a second file next to SKILL.md and mention it in the steps, so the skill stays short.
5. Call `validate_agent`; a missing name or description is reported as a warning and means the SDK may not load the skill.
6. Test with a prompt that should trigger the skill and check the reply shows the steps were followed.
