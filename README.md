# Goodreads friend alert

Every 5 minutes, GitHub checks a friend's public Goodreads pages and emails you ("Goodreads <Name> Update") when something new shows up.

## What it can and can't see
Goodreads' RSS feeds are gone (they return 404) and shelf lists need a login, so the script reads the two pages that are public without signing in:

* **Status updates page:** every progress update ("page 171 of 320") with the status text.
* **Profile page:** currently-reading books (with authors) and shelf counts.

From those it reports progress updates, books started, books finished or marked did-not-finish, and how many books were added to to-read. It can't name to-read additions or see ratings and review text, because those are only visible when signed in.

## What stays private
This repo is public (so GitHub Actions is free), so nothing identifying is stored in it:

* **Who to watch and where to send email** are repository secrets. Locally they go in `.env`, which git ignores (copy `.env.example`).
* **What's already been emailed** (`state.json`) is kept in the GitHub Actions cache, not committed.
* **Run logs** are public on a public repo, so the script only logs counts there ("Sending an email with 2 update(s)"), never titles or status text. Secret values are also masked as `***` in logs.

## One-time setup

### 1. Get a Resend API key (free)
1. Sign up at resend.com with the email address you want alerts sent to.
2. Go to **API Keys → Create API Key**, name it "Goodreads alert", choose **Sending access**, and copy the key (it starts with `re_` and is only shown once).

Alerts come from `onboarding@resend.dev`. Without your own domain, Resend only delivers to the address you signed up with, which is all this needs. The free plan allows 100 emails a day.

(Gmail works too: set `GMAIL_ADDRESS` and a 16-letter `GMAIL_APP_PASSWORD` instead of `RESEND_API_KEY`. App passwords require 2-Step Verification on the Google account.)

### 2. Add your secrets
In the repo on GitHub, go to **Settings → Secrets and variables → Actions → New repository secret**. Add each as its own secret:

| Name | Value |
|---|---|
| `FRIEND_NAME` | her first name, as used in the subject line |
| `PROFILE_URL` | her profile link, `https://www.goodreads.com/user/show/<id>-<name>` |
| `RESEND_API_KEY` | the `re_...` key |
| `ALERT_TO` | the address you signed up to Resend with |

Workflow permissions don't need changing: the workflow file asks for the access it needs.

### 3. Test it
Go to **Actions → Goodreads check → Run workflow**, tick **Dry run**, and run it. The log should say how many status updates it read and "Would send an email with 1 update(s)". Nothing is sent or saved.

### 4. Start it
Run the workflow again without **Dry run**. This first real run records everything already there and sends no email. After that it checks every 5 minutes on its own.

## Good to know
* **Timing:** GitHub often starts scheduled runs late, usually by a few minutes and sometimes by 15 to 30 at busy times. Expect alerts within roughly 5 to 20 minutes of a post.
* **If the check stops working** (Goodreads blocks GitHub's servers, the page layout changes, or the profile goes private), you'll get one email with the subject "Goodreads <Name> Update: check failing" after about 30 minutes of failures. Run history is under the **Actions** tab.
* **If the saved state is lost** (for example the cache is cleared), the next run acts like a first run: it records what's there and sends nothing, so you won't get a flood of old updates.
* **Pausing:** **Actions → Goodreads check → ··· → Disable workflow**.
* **Inactive repos:** GitHub pauses schedules in public repos after 60 days with no commits, so the workflow commits today's date to `heartbeat.txt` about every 20 days.
* **Running locally:** copy `.env.example` to `.env`, fill in `FRIEND_NAME` and `PROFILE_URL`, then run `python check.py --dry-run`. Local runs print the full email.
* **Test email:** with the email settings in `.env` too, `python check.py --test-email` sends you a preview of her newest update, with "(test)" in the subject. It saves nothing.
