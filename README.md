# 2dehands.be new-listing monitor

Watches one or more 2dehands.be category/search pages and sends you a
Telegram message (title, price, thumbnail, link) as soon as a genuinely new
listing appears - checked roughly every 10 minutes.

## How it works

- `monitor.py` runs **once per check cycle** (it's not a long-running
  process) - a GitHub Actions schedule fires it every 10 minutes.
- For each category in `config.yaml`, it fetches the category page, pulls
  the site's own resolved search parameters out of the embedded JSON, and
  replays them against 2dehands' internal search API
  (`/lrp/api/search`) sorted newest-first. If that ever breaks (endpoint
  changes, gets blocked, etc.) it falls back to whatever the plain page
  itself embedded, so a partial breakage degrades gracefully instead of
  going silent.
- Every listing ID it has already notified you about is stored in
  `data/seen.json`. Since GitHub Actions runners are ephemeral (nothing
  persists between runs on their own), the workflow commits this file back
  to the repo after every run - that's what makes "already seen" tracking
  survive between checks. A run always updates a `last_checked_at`
  timestamp even when nothing else changed, which guarantees a commit every
  cycle - that in turn keeps the repository "active", which matters because
  GitHub auto-disables scheduled workflows after 60 days of *zero* repo
  activity.
- The very first check for a new category seeds the store silently
  (no notification flood for pre-existing listings) - you only get notified
  from the next cycle onward.
- Listings from confirmed business/professional sellers ("TRADER") are
  skipped - only genuinely new listings get one extra request to check
  the seller type, so this doesn't add meaningful load. 2dehands also has
  an "UNKNOWN" seller classification that mixes unclassified private
  sellers with some businesses - those are intentionally still shown,
  since excluding them would also hide real private-seller listings.
- If every category fails to fetch for ~1 hour straight (site down,
  blocked, etc.), you get one "monitoring is down" Telegram alert, and one
  "back up" message when it recovers. Individual transient errors are just
  logged and retried next cycle.

---

## 1. Create your Telegram bot (do this first, ~2 minutes)

1. In Telegram, message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot` and follow the prompts (pick any name/username).
3. BotFather gives you a **bot token** - looks like `123456789:AAExample...`.
   Save it.
4. Send your new bot **any message** (e.g. "hi") so it can see your chat.
5. In a browser, visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   Find `"chat":{"id":123456789, ...}` in the response - that number is your
   **chat ID**. Save it too.

---

## 2. Push this project to a new GitHub repository

This needs to be a **public** repository - GitHub Actions minutes are
unlimited/free only for public repos, and a check every 10 minutes
(~4,300 runs/month) would likely exceed the free private-repo quota
(2,000 min/month). Nothing sensitive lives in the code itself; your
Telegram token/chat ID are stored as encrypted GitHub Secrets (next step),
never committed to the repo.

```powershell
cd C:\Users\jensb\2deHandsBot
git init
git add .
git commit -m "Initial commit: 2dehands monitor"
```

Then on GitHub: create a new **public** repository (no README/license,
since this folder already has one), and push:

```powershell
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

---

## 3. Add your Telegram credentials as GitHub Secrets

In your new repo on GitHub: **Settings -> Secrets and variables -> Actions
-> New repository secret**. Add two secrets:

- `TELEGRAM_BOT_TOKEN` - the bot token from step 1
- `TELEGRAM_CHAT_ID` - the chat ID from step 1

---

## 4. Enable and test the workflow

The workflow file (`.github/workflows/monitor.yml`) is already set up to
run every 10 minutes and on-demand. To confirm it works without waiting for
the schedule:

1. Go to the **Actions** tab in your repo.
2. If prompted, click **"I understand my workflows, go ahead and enable
   them"**.
3. Select the **"2dehands monitor"** workflow, click **"Run workflow"**
   (this is the `workflow_dispatch` trigger), and run it on `main`.
4. Watch the run - expand the `python monitor.py` step. You should see a
   line like `First run for 'Fitnessmaterialen': seeded 63 listings
   silently`.
5. Check the repo afterward - a new commit "Update monitor state" should
   have appeared, adding `data/seen.json`. That's expected and correct: no
   Telegram message is sent on this first run. From the next cycle onward,
   genuinely new listings will notify you.

From here it just runs on schedule - no server, no further setup.

---

## 5. Adding or adjusting categories

Edit `config.yaml` in the repo (directly on GitHub, or locally + push):

```yaml
categories:
  - name: "Fitnessmaterialen"
    url: "https://www.2dehands.be/l/sport-en-fitness/fitnessmaterialen/"
  - name: "Loopband"
    url: "https://www.2dehands.be/l/sport-en-fitness/fitnessmaterialen/q/loopband/"
```

To get a category's URL: browse to it on 2dehands.be, apply any
price/keyword/location filters you want using their own filter UI, then
copy the URL from your address bar. Sort order in the URL doesn't matter -
the script always fetches newest-first on its own.

No extra setup is needed - `config.yaml` is re-read on every run. A brand
new category gets the same silent first-run seeding as the initial one, so
adding a category never floods you with notifications about listings that
were already there.

**Important:** give each category a unique `name` - it's used as the key
for tracking which listings you've already seen.

---

## 6. Operating it day to day

- **Logs:** Actions tab -> select a run -> expand the `python monitor.py`
  step.
- **Run one check manually:** Actions tab -> "2dehands monitor" ->
  "Run workflow".
- **Pause monitoring:** Actions tab -> "2dehands monitor" -> "..." menu ->
  "Disable workflow".
- **Resume:** same menu -> "Enable workflow".
- **Reset "seen" state for a category** (e.g. to re-seed after a long
  pause): edit `data/seen.json`, remove the category's `seen` entries (or
  the whole category block to fully reset it, or delete the whole file to
  reset everything), commit. The next run will silently reseed rather than
  notify on everything.

## Troubleshooting

- **No notifications ever, no errors in logs:** check the
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` secrets are set correctly and
  that you've messaged the bot at least once (Telegram bots can't message
  you first otherwise).
- **"All categories failed this cycle" in logs:** transient - it'll retry
  next cycle. If it persists past ~1 hour you'll get a Telegram alert
  automatically.
- **Workflow stopped running with no changes on your end:** GitHub
  auto-disables scheduled workflows after 60 days with zero repository
  activity. This shouldn't happen here since every run commits
  `data/seen.json` - but if you ever paused the workflow for a long time,
  re-enabling it (step 6) is all that's needed.
- **Layout/API changes on 2dehands' side:** the script falls back to
  parsing the embedded page data if the internal search API stops working.
  If both break, check the Actions run logs for the specific error and
  re-check the field names in `monitor.py` (`normalize_item`,
  `extract_next_data`) against the current page source.

---

## Project layout

```
monitor.py                          the script (single check cycle per run)
config.yaml                         categories to monitor - edit freely
requirements.txt                    Python dependencies
data/seen.json                      "already seen" store, committed by the workflow
.github/workflows/monitor.yml       schedule + run + auto-commit
deploy/systemd/                     alternative: self-hosted VM deployment (see below)
```

---

## Appendix: self-hosting on a VM instead

If you'd rather run this on an always-on VM (e.g. Oracle Cloud or Google
Cloud's Always Free tiers) instead of GitHub Actions - for tighter timing
precision, or if you just prefer owning the box - `deploy/systemd/`
contains a systemd service + timer that runs the same script every 8-12
minutes with jitter. That path uses a local `.env` file for secrets
instead of GitHub Secrets, and `data/seen.json` just lives on disk between
runs - no git commits needed, since the process persists on the same
machine. The rest of the script (fetching, notifying, outage alerting) is
identical either way. Telegram bot setup (step 1 above) is unchanged; only
VM creation, SSH access, Python install, and systemd install would differ.
Ask if you want the full VM setup guide re-added - it was cut from this
README to keep it focused on the primary GitHub Actions path.
