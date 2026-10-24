# Notes for Claude sessions

Christy owns this project. Claude designs, writes task specs, runs Codex, reviews its output and writes learning cards. Codex writes code and tests. Everything runs locally on Christy's Windows PC (repo at `D:\climate-disclosure-agent`), so every GitHub action uses Christy's own `git` and `gh` login.

## Start of every session
1. Read `specs/03-tasks.md` (status and order) and the latest handoff in Christy's notes: `E:\OneDrive - The University of Sydney (Students)\Obsidian\开发\Climate Disclosure Agent\05-待办与交接.md`.
2. Read `specs/02-design.md` sections relevant to the current batch. `specs/01-requirements.md` only when scope is in question.
3. Do not re-open decided items (D1–D68) unless an experiment result or a bug forces it; record any change in both `specs/02-design.md` and the Chinese notes (`02-项目设计.md`, `04-设计决策卡.md`).
4. Check `git config user.name` / `user.email` give `LiChenyang-GZ` / `104789004+LiChenyang-GZ@users.noreply.github.com`, `gh auth status` is logged in as LiChenyang-GZ, and `codex login status` shows an API key. If not, stop and tell Christy.

## Running a task
1. Write `specs/tasks/TNN.md` from `specs/tasks/_template.md`; commit and push it on `main`.
2. `git switch -c task/TNN main`, then `uv sync`.
3. Run Codex in the background (it can take 5–20 minutes; do not block on it):
   ```
   codex exec -m <model from the spec: gpt-6-luna | gpt-6.1-sol> --sandbox workspace-write -c sandbox_workspace_write.network_access=true -o .codex/summary.md "Read AGENTS.md first and follow it exactly. Implement the task in specs/tasks/TNN.md. Finish with the three-section summary defined in AGENTS.md."
   ```
   For a revision, add to the prompt: "This is a revision. Address every point in .codex/feedback.md; keep the rest of the work."
   If Codex fails to start (sandbox or auth error), stop and report the exact error to Christy; do not switch to `--dangerously-bypass-approvals-and-sandbox`.
4. After Codex finishes:
   - Revert any change to protected paths: `specs/ AGENTS.md CLAUDE.md .github/ .codex/ docs/`.
   - Cost: `uv run python .github/scripts/codex_usage.py <luna|sol>`.
   - Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest -q`.
5. Review. `strict` tier: line by line, check every failure case has a test. `normal` tier: summary, diff, test results.
   Changes needed: write `.codex/feedback.md` (git-ignored) and rerun step 3 as a revision. At most 2 send-backs; after that Claude fixes it directly or splits the task.
6. Commit, push the branch, open the PR: `gh pr create --base main --title "TNN: <title>" --body-file .codex/summary.md`, then `gh pr comment` with the cost line and test results. CI runs on the PR.
7. Merge when CI is green:
   - `normal` tier: Claude runs `gh pr merge --squash --delete-branch`.
   - `strict` tier: do not merge yet. Give Christy a 3-line summary in Chinese (做了什么 / 怎么测的 / 剩下什么风险) and wait for her "可以".
   - After merging: `git switch main && git pull`, delete `.codex/feedback.md`, write the learning card in Christy's notes, add the cost to the spend log (`06-协作流程（Claude × Codex）.md` §7).
8. Independent tasks may run in parallel only in separate git worktrees.
9. Work in batches (`specs/03-tasks.md`); stop after each batch and report: tasks done, OpenAI spend (batch / total / remaining vs $25 coding budget), next batch and estimate, decisions needed, and every "Uncertain" item from the batch's PR summaries translated into Chinese. Strict-tier PRs awaiting Christy's approval are listed first.

## Fixed rules
- Commits are authored as `LiChenyang-GZ <104789004+LiChenyang-GZ@users.noreply.github.com>`. No `Co-Authored-By` lines and no AI attribution in commits, PRs or comments; Claude and Codex must not appear as contributors.
- The OpenAI key stays in Codex's own login (`codex login`) and in GitHub Actions secrets. Never print it, write it to the repo, or put it in a command line.
- Force-push only with Christy's explicit approval.
- Repo files are in English; Christy's notes and learning cards are in Chinese.
- Design changes go to both places (repo specs and Christy's notes).
- `.github/workflows/codex-task.yml` is a manual fallback (cloud Codex) and no longer runs on branch pushes. Use it only if Christy asks.
