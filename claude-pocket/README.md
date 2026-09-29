# Claude Pocket

Follow and steer Claude Code on your Mac from your Pixel — over the internet, no cable.

- **Inbox** — approve or deny tool permissions, answer Claude's multiple-choice questions, and reply when Claude finishes a turn (so it keeps working).
- **Chats** — live view of every Claude Code session on the Mac, and send it messages.
- **Usage** — 5-hour and weekly plan limits with reset times, plus context-window use.
- **Send** — share a screenshot (or any file) from Android's share sheet straight to Claude, or just drop it in `~/Downloads/Claude Pocket` on the Mac.

```
Pixel (PWA) ⇄ HTTPS ⇄ relay (Railway) ⇄ HTTPS ⇄ Mac (Claude Code hooks + status line)
```

Nothing listens on the Mac; both sides only make outgoing requests to the relay, so it works on any network.

## 1. Deploy the relay

The relay is a single zero-dependency Node file (`relay/server.mjs`) that also serves the phone app.

On Railway: New service → GitHub repo → set **Root directory** to `claude-pocket/relay`, then add:

| Variable | Value |
| --- | --- |
| `POCKET_TOKEN` | a long random string — `openssl rand -hex 24` |
| `NTFY_URL` | optional, for push notifications: `https://ntfy.sh/<long-random-topic>` |
| `PUBLIC_URL` | optional, the relay's URL, so tapping a notification opens the app |

Attach a volume at `/data` if you want state and files to survive redeploys. Generate a domain for the service.

Run it anywhere else with `POCKET_TOKEN=… node relay/server.mjs` (Node 18+).

## 2. Install the bridge on the Mac

```sh
node claude-pocket/mac/pocket.mjs install --relay https://your-relay.up.railway.app --token YOUR_TOKEN
```

This adds hooks and a status line to `~/.claude/settings.json` (a backup is written next to it; an existing status line keeps working — it's wrapped, not replaced). Restart running Claude Code sessions. `node pocket.mjs uninstall` removes it again.

Optional — to get plain files (not meant for Claude) onto the Mac automatically, leave this running:

```sh
node claude-pocket/mac/pocket.mjs watch
```

## 3. Install the app on the Pixel

1. Open `https://your-relay.up.railway.app/#token=YOUR_TOKEN` in Chrome (the token is saved and removed from the URL).
2. Chrome menu → **Add to home screen** → **Install**. Installing is what makes *Claude Pocket* show up in the share sheet.
3. For notifications while the app is closed, install the **ntfy** app and subscribe to the same topic as `NTFY_URL`.

## How it behaves

**Away mode off (default):** everything is mirrored to the phone and you get a push for permission prompts, questions and finished turns, but you answer on the Mac as usual.

**Away mode on:** permission prompts, `AskUserQuestion` and "Claude finished" wait for the phone (up to 30 minutes, then fall back to the Mac). Replying to a finished turn makes Claude carry on with your message. Change the wait with `"waitMinutes"` in `~/.claude-pocket/config.json`.

**Screenshots to Claude:** share to Claude Pocket, pick *To Claude*, add a note. The file is downloaded to the Mac and Claude is told the path at its next tool call or prompt — or immediately, if it's waiting for your reply. Claude opens it with its Read tool.

## Security

The token is the only thing protecting the relay — anyone with it can approve commands on your Mac. Keep it long and private, and rotate it (change `POCKET_TOKEN`, re-run `install`, re-open the pairing link) if it leaks. Files are deleted from the relay after 3 days.
