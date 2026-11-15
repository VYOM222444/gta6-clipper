# gta6-clipper

Finds trending GTA 6 moments, turns them into vertical Shorts for 5 brand channels, and
learns from the results. Full blueprint: [CLAUDE.md](CLAUDE.md). Rules: [playbook/PLAYBOOK.md](playbook/PLAYBOOK.md).

## Buttons (GitHub app → Actions)

- **Setup sheet** — tests the Google Sheet connection and creates the dashboard tabs.
  Safe to run any time.
- **Scout** — finds trending GTA 6 videos → `Candidates` tab. Runs every 6 hours by itself.
- **Tracker** — checks views of tracked videos → `Tracker` tab. Runs every 6 hours
  (30 min after Scout) and saves `data/snapshots.csv`.
- **Tests** — runs automatically on every pull request.
