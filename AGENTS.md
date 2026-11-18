# Pitch-Guard

You are developing Pitch-Guard
Pitch Guard is a **Python Background Voice Pitch Tracker.**

A Python-based tool for tracking the pitch of your voice live which will notify you when your pitch goes out of defined range for certein period of time.

# Rules

- Use `uv` for managing packages, virtual environments and dev tools such as typechekers, lint and formatting (etc).
- Use `ty` for type checking after you have finished writing your code to ensure there are no type errors.
- Use `ruff` to lint and typecheck your code in the end.
- Make sure your tools use project's configurations. Prefer having configs in `pyproject.toml`.
- Follow your team's coding style and best practices. Use typings where you can and avoid Any/unknown types.
- Use `pytest` for testing your code. Store tests in `tests/` directory which must internally resembe project's folder structure.
- Test what you've done visually. Review how it looks in the terminal and feel free to use host machine for testing Linux/Fedora.
- App must track logs of live use (errors, debug logs, not raw audio!) for debugging.
- Follow project structure, avoid inventing 2 paths for the same purpose.
- Split code to modules, ensure clear boundaries.
- Document your code and make it easy to understand and maintain. In addition to docstrings and in-code comments maintain --help and `docs` in-project folder documentation.

# Git

- Use semantic commit naming, such as `feat: added`, `chore: bumped .. to ..`, `fix: fixed ...`.
- Commit when a complete fully tested working functionality is developed. Push if remote is connected.
- Keep good `.gitignore` up to date with all runtime and development artifacts, such as logs, temporary files, and build artifacts.
- Don't hesitate to commit developer's changes too, don't question just commit all together with yours.
