# Candidate notes — Task B: Monthly billing run

## 1. What I built

I picked **Task B** mainly because I have more experience with Django.

The three billing endpoints now work end to end, and every invoice is emailed to the company and the freelancer. The frontend is unchanged.

**How it fits the other tasks.**
- **Task A (Approval Inbox)** builds on the same `PATCH /api/timesheets/<id>/`, which now has firmer rules. Only company admins can approve or reject, a rejection needs a reason, and an entry that's already invoiced returns 409. A bulk approve has to treat that 409 as a partial failure, not an error for the whole batch.
- **Task C (webhooks)** can reuse the email delivery pattern. A delivery row is written in the same transaction as the change, sent after commit, and retried by a command. `invoice.created` would also be a natural event to send.

## 2. Key implementation notes

- **Billed once, never changed.** Each timesheet entry is linked one to one to an invoice line, so it can't be billed twice. Lines store what was billed, and issued invoices can't be edited or deleted.
- **Billing period, not work period.** An invoice covers everything approved and not yet billed up to the end of the month, so late approvals roll forward instead of being lost. Only months that have ended can be billed, so an invoice never covers part of a month.
- **Email after commit.** Emails are recorded with the invoice in one transaction (an outbox) and sent only after it commits. A failed send never undoes an invoice. A management command retries outside the web app. Delivery is at-least-once; Celery is the next step.
- **Concurrency.** Runs lock the company and the entries they bill, and that's tested with real concurrent transactions.
- **Fixes billing needed.** Freelancers could approve their own hours, and the API accepted any hours value. Both are now fixed, because billing invoices whatever is approved.

## 3. Workflow

I used **Claude Code**, with a `CLAUDE.md` of working rules (I'll send it separately). The rules were basically: follow the repo's conventions, plan beforehand, agree and discuss design decisions before any code, work in small steps, and I make every commit. Before running anything, I had it check the repo for anything that could compromise my machine. I kept a decision log (about 30 decisions, which I can share).


**Where I steered it.** 
- I pulled Celery out of scope after reading the README's time guidance, but it should be the way to manage async tasks like sending emails. 
- I rejected storing company details as invoice snapshots and pushed for referencing the company record instead. We only store immutable invoice facts and the address the invoice was sent to.
- I pointed out that a monthly invoice doesn't have to include only hours worked in that month.
 As a result, late approved hours can simply be added to the following month's invoice, closing the gap.
- The third review spotted that billing the current month gives you a partial invoice, like a “November” invoice that only covers 1-6 November. So I went with option B: you can only bill months that have already finished

**Reviews.** 
I ran an independent code review four times. Each round found real defects, some of them in earlier fixes. Every one was reproduced with a failing test before it was fixed, and findings that conflicted with my decisions were declined. Most findings were the same rule bypassed by path I hadn't tested, next time I'd list the invariants upfront (e.g. an entry is billed once, an issued invoice never changes, etc.)

**Experience:** 
There are several design assumptions I took that I'd have preferred to have more context for (Business rules), but that's understandable since this is just an exercise. My approach is planning and agreeing on every decision before coding, so the code went quickly and needed little rework. What didn't go well was that I didn't make clear to the AI that documenting problems meant addressing them later based on their priority not to leave them there, so had to push back until I made it a rule, next time I'd make that rule explicit in the beginning and budget the time for the task.

## 4. Next steps

1. **Frontend:** default the Billing page to last month, and show API errors and failed emails.
2. **Celery + Redis** for email delivery, with backoff. The outbox stays as is.
3. **Compliant invoices:** per-issuer numbering, VAT, a PDF, and credit notes.
4. **An overtime pricing policy.**
5. **Split the timesheet fixes into their own PR,** ahead of billing.

## 5. Feedback on the exercise

These are the things that slowed me down or that a future candidate might trip over:

- **Setup steps are missing from the README.** I think a development environment based only on docker compose would have made this project simpler to setup and start working on. The repo instructions leave out some key steps, like having to run `nix develop` (or `direnv allow`), and `task frontend` fails with `vite: command not found` until you run `pnpm install` in `frontend/`. A `deps` task, or running the whole stack in docker compose, would turn all of that into a single command.
- **The webhook receiver isn't in docker-compose.** Task C says it runs there, but compose only defines `db` and `mailpit`. `task receiver` runs `npm install` without a lockfile.
- **The Billing page defaults to the current month.** Billing a month that hasn't ended gives a partial invoice, so the default nudges candidates towards it.
- **The seed data depends on the date it's run.** Its docstring ("March and April are always the two complete prior calendar months") is out of date. Seeded in November, there are no draft or entries, and three contracts that ended months ago are still `active`.
- **There's no linter, formatter or contribution guide,** so I inferred the code and commit style from the existing files and history. A ruff and Prettier config matching the current style would remove the guesswork.
- **The time guidance and the scope pull in different directions.** Task B's questions (idempotency, the transaction boundary around email failure) are good, but answering them properly, with tests, takes well over 2 or 3 hours, saying which parts you'd rather see discussed than built would help.
