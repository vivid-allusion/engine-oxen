# AGENTS.md — engine-template

Rules for anyone changing the skeleton, and for anyone starting an engine from
it.

## The property to protect

**The template is green out of the box.** Its own tests pass and the shared
conformance suite passes against the built-in `template/*` models. A stranger's
first run must be green before they change anything, so any red afterwards is
their change.

Breaking that property is the one change that is always a bug: it turns the
starting point into a puzzle.

## Rules

1. **The catalogue is data.** Adding a model is a TOML file, never a branch in
   `engine.py`. If a provider needs a third code path, that is a design
   conversation, not a patch.
2. **`engine.py` holds no wire knowledge.** URLs, envelopes and terminal states
   live in `transport.py` and `tasks.py`. `engine.py` is the loop.
3. **Never invent a model, a parameter or a price.** Every catalogue entry
   comes from the provider's own document or page, and `new_engine.py` refuses
   to guess rather than defaulting.
4. **Fail loud.** A success with no file, an undeclared parameter sent, media
   on a model that takes none, a job that never finishes — each is an error
   with a message, never a shrug.
5. **The scope holds.** Four generation categories — `IMG`, `VID`, `LLM`,
   `AUD` — and only the ones a Generator consumes (SCOPE.md). A model is
   carried where it belongs by what it produces; a retired folder is a defect.
6. **A new rule in the shared suite arrives as a version bump**, never as a
   silent change under an existing tag.

## When the skeleton changes

The frozen fixture inside `engine-conformance` is a copy of this package. A
change here that should reach the suite is refreshed there deliberately, in
its own commit, with the mutant list re-run — never by a script that copies
quietly.
