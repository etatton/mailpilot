# MailPilot 📬

A self-hosted inbox copilot. MailPilot watches a Gmail inbox, drafts replies with
Claude, and queues every draft for **your** review — nothing is ever sent without
your explicit approval. Approved replies go out from your own Gmail address,
threaded into the original conversation.

Everything runs on your computer. Your mail, credentials, and drafts never touch
anyone's server except Google's and Anthropic's.

## Install

Download the latest release for your platform from the **Releases** page:

- **Windows** — `MailPilot.exe`. Windows SmartScreen will warn about an
  unrecognized app the first time: click **More info → Run anyway**. (The app is
  unsigned, not unsafe — the source is this repo.)
- **Mac** — `MailPilot-mac.zip`. Unzip, move `MailPilot.app` to Applications.
  macOS Gatekeeper will block the first launch: go to **System Settings →
  Privacy & Security**, scroll down, and click **Open Anyway** (on older macOS,
  right-click the app → Open works too).

Double-click the app. Your browser opens to a five-minute setup wizard that walks
you through everything:

1. **How MailPilot thinks** — either paste an Anthropic API key
   (create one at [console.anthropic.com](https://console.anthropic.com); a typical
   reply costs about a cent) or use the Claude Code app already installed and
   signed in on your computer with your Claude subscription.
2. **Gmail** — the wizard links you to Google's app-password page and tests the
   connection live before you can continue.
3. **Your voice** — sign-off name and tone notes.
4. **Autostart** — one checkbox and MailPilot starts with your computer and
   restarts itself if it ever crashes.

MailPilot starts in **test mode**: approve one practice draft (it's simulated,
nothing sends), then flip Live sending on in Settings.

## Beyond the basics

- **Voice learning** — paste real emails you've written (Voice dialog), click
  *Analyze my voice*, and every draft follows the distilled, editable profile.
- **Waiting-drafts notifications** — MailPilot emails *you* (and only you — the
  path is hard-wired to your own address) when drafts are queued, at most once
  per cooldown window.
- **Thread memory** — drafts include your recent correspondence with that
  sender, so replies carry context.
- **People rules** — per-sender settings: ★ VIP (always drafted, top of queue,
  instant notification), always-draft (beats bulk filters), auto-skip, plus
  notes that feed the drafting context ("my landlord — keep it formal").
- **Follow-up nudges** — a sent reply with no response after N days (default 3,
  0 = off) gets a short nudge drafted and queued. Like everything else, it
  never sends itself.
- **Multiple inboxes** — add more Gmail accounts in Settings; one queue with
  per-inbox badges, and every reply goes out AS the inbox it arrived in.
- **Time controls** — quiet hours (no notifications overnight), vacation mode
  (mail is collected but nothing drafts until you're back), and per-draft
  snooze (1 day / 3 days / 1 week).
- **Update banner + diagnostics** — the app tells you when a new release is
  out, and Settings can export a secrets-scrubbed diagnostics report.

## Labs (experimental, all off by default — Settings → Labs)

- **Negotiation Copilot** — for emails that are really negotiations, drafts
  three stances (anchor high / meet in the middle / walk away) with rationale;
  optional auto-detect.
- **Rehearsal** — replays mail you already answered: MailPilot's draft
  side-by-side with what you actually sent. Never queued, never sent; one
  click promotes your real reply into your voice samples.
- **Relationship Radar** — a one-off headers-only scan of your last 12 months
  learns who you talk to on a rhythm, then flags people who've gone quiet for
  2+ months and offers a reconnection draft (through the normal queue).
- **Parley** — when both correspondents run MailPilot, the two apps settle a
  meeting time between themselves (max 3 rounds, from your stated
  availability). Every round is a queued draft a human approves; a recipient
  without MailPilot just sees a polite scheduling email.

## What it never does

- It never sends without your click — the send path has a hard series of guards
  and every one of them must pass, ending with your approval.
- It never auto-replies to bulk mail, newsletters, or no-reply senders.
- It never sends to anyone on your ignore list.
- It never stores your real Google password — only a revocable app password, kept
  in your OS credential store (Windows Credential Manager / macOS Keychain).
- It never uploads your mail anywhere except to Anthropic's API to draft the
  reply you asked for.

## Bring your own Claude

Each person runs their own MailPilot with their own credentials — their own
API key, or their own Claude subscription via their own signed-in Claude Code
install. MailPilot performs no Anthropic login itself and must not be offered as
a hosted service on someone else's subscription; that's against Anthropic's
usage policies.

## Running from source (developers)

```sh
python -m venv venv && . venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py                                  # opens the browser UI
python app.py --headless                       # server only (what autostart runs)
python scripts/selftest.py                     # prove the send guards hold
```

Data lives in the per-OS app-data dir (`%APPDATA%\MailPilot`,
`~/Library/Application Support/MailPilot`, or `~/.local/share/mailpilot`):
`config.json`, `mailpilot.db`, `mailpilot.log`. Secrets go to the OS keyring.

### Architecture

| Piece | File | Job |
|---|---|---|
| Entry | `app.py` | windowed launch (browser) or `--headless` for the service |
| Server | `mailpilot/server.py` | FastAPI: wizard + queue UI + JSON API, 127.0.0.1 only |
| Watcher | `mailpilot/poller.py` | IMAP poll, classification, dedup; `\Seen` only after processing |
| Drafter | `mailpilot/drafter.py` | Claude via API key or local Claude Code CLI |
| Sender | `mailpilot/sender.py` | THE send choke-point — six ordered guards, then SMTP |
| Autostart | `mailpilot/autostart.py` | Scheduled Task / LaunchAgent / systemd user unit |

Rules the code keeps: all outbound mail goes through `sender.send_reply()` and
nothing else imports smtplib; datetimes are TEXT ISO-8601 with offset; secrets
are never logged (presence/length only); background failures surface in the UI
error banner, never just a log line.

### Releases

CI (`.github/workflows/build.yml`) runs the guard self-test on every push, and a
tag `v*` builds `MailPilot.exe` (Windows) and `MailPilot-mac.zip` (macOS,
ad-hoc signed) and attaches them to a GitHub Release:

```sh
git tag v0.1.0 && git push origin v0.1.0
```
