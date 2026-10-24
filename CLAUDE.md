# Notes for Claude sessions

Christy owns this project. Claude designs, writes task specs, reviews Codex's PRs, and writes learning cards. Codex (via GitHub Actions) writes code and tests.

## Start of every session
1. Read `specs/03-tasks.md` (status and order) and the latest handoff in Christy's notes (`开发/Climate Disclosure Agent/05-待办与交接.md`, reached through the linked computer).
2. Read `specs/02-design.md` sections relevant to the current batch. `specs/01-requirements.md` only when scope is in question.
3. Do not re-open decided items (D1–D68) unless an experiment result or a bug forces it; record any change in both `specs/02-design.md` and the Chinese notes (`02-项目设计.md`, `04-设计决策卡.md`).

## Running a task
1. Write `specs/tasks/TNN.md` from `specs/tasks/_template.md` and commit it on `main`.
2. Create and push branch `task/TNN` from `main`. This triggers `.github/workflows/codex-task.yml`.
3. Wait for the PR. Read the PR body, the bot comment (tests + token usage + cost) and the CI checks.
4. Review with Claude Code's built-in code review. `strict` tier: line by line, check every failure case has a test. `normal` tier: summary, diff, CI.
5. Changes needed: write `.codex/feedback.md` on the branch and push (re-triggers Codex). At most 2 send-backs; after that Claude fixes it directly or splits the task.
6. Approved:
   - `normal` tier: Claude squash-merges.
   - `strict` tier: do not merge yet. Post a 3-line summary in Chinese to Christy (做了什么 / 怎么测的 / 剩下什么风险) and wait for her "可以" before squash-merging.
   - After merging: delete `.codex/feedback.md`, write the learning card in Christy's notes, add the cost to the spend log.
7. Work in batches (`specs/03-tasks.md`); stop after each batch and report: tasks done, OpenAI spend (batch / total / remaining vs $25 coding budget), next batch and estimate, decisions needed, and every "Uncertain" item from the batch's PR summaries translated into Chinese. Strict-tier PRs awaiting Christy's approval are listed first.

## Fixed rules
- Commits are authored as `LiChenyang-GZ <104789004+LiChenyang-GZ@users.noreply.github.com>`. No `Co-Authored-By` lines; Claude and Codex must not appear as contributors.
- `OPENAI_API_KEY` lives only in GitHub Actions secrets and is passed only to the Codex step.
- Force-push only with Christy's explicit approval.
- Repo files are in English; Christy's notes and learning cards are in Chinese.
- Design changes go to both places (repo specs and Christy's notes).
