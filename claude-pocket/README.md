# Claude Pocket

Follow and steer Claude Code on your Mac from your Pixel — over the internet, no cable.

- **Inbox** — approve or deny tool permissions, answer Claude's multiple-choice questions, and reply when Claude finishes a turn (so it keeps working).
- **Chats** — live view of every Claude Code session on the Mac, and send it messages.
- **Usage** — 5-hour and weekly plan limits with reset times, plus context-window use.
- **Messages** — an iMessage-style inbox Claude can text you in from anywhere (a routine tracking a parcel, a finished render, a build on the PC). One thread per topic, tracking links show as previews.
- **Ideas** — a pinned thread where you jot down ideas; each is saved as a numbered idea Claude can list and pick up later.
- **Send** — share a screenshot (or any file) from Android's share sheet straight to Claude, or just drop it in `~/Downloads/Claude Pocket` on the Mac.

```
Pixel (PWA) ⇄ HTTPS ⇄ relay (Railway) ⇄ HTTPS ⇄ Mac (Claude Code hooks + status line)
```

Nothing listens on the Mac; both sides only make outgoing requests to the relay, so it works on any network.

## Quick setup: everything on your Mac (free)

Needs [Node.js](https://nodejs.org) 18+ and [Tailscale](https://tailscale.com/download/mac) (signed in). Then, from this folder:

```sh
bash mac/setup-mac.sh
```

It runs the relay on the Mac (starts at login), publishes it on a fixed https address with Tailscale Funnel, connects Claude Code (hooks, status line, MCP server, `~/.claude/CLAUDE.md` block) and opens a pairing page with a QR code for the phone. Run it again any time; `bash mac/setup-mac.sh --uninstall` removes it. The app is reachable while the Mac is awake.

Prefer a cloud host that is always on? Follow steps 1–3 below instead.

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

## Let Claude text you (Messages + Ideas)

The relay is also an MCP server. Add it as a custom connector — in claude.ai (Settings → Connectors → Add custom connector) or in Claude Code (`claude mcp add --transport http pocket "https://your-relay.up.railway.app/mcp?token=YOUR_TOKEN"`) — with the URL:

```
https://your-relay.up.railway.app/mcp?token=YOUR_TOKEN
```

Claude then has these tools:

| Tool | What it does |
| --- | --- |
| `send_message` | Text your phone in a named thread, optionally with a link preview (e.g. a tracking page) |
| `read_replies` | Read what you answered from the phone |
| `list_ideas` / `update_idea` / `save_idea` | Read your Ideas list, mark one as doing/done with a reply, or save one for you |
| `list_threads` | Overview of all threads |

Example routine prompt: *"Check the tracking for my Index01 order and, if the status changed, send_message to thread 'Index01 shipment' with the new status and the tracking link."*

Without MCP, anything can post with the token:

```sh
curl -X POST https://your-relay.up.railway.app/api/messages \
  -H "Authorization: Bearer YOUR_TOKEN" -H "Content-Type: application/json" \
  -d '{"thread":"Index01 shipment","text":"Out for delivery","url":"https://…","urlTitle":"Track & Trace"}'

node claude-pocket/mac/pocket.mjs message "Render finished" --thread "Mac"
```

## How it behaves

**Away mode off (default):** everything is mirrored to the phone and you get a push for permission prompts, questions and finished turns, but you answer on the Mac as usual.

**Away mode on:** permission prompts, `AskUserQuestion` and "Claude finished" wait for the phone (up to 30 minutes, then fall back to the Mac). Replying to a finished turn makes Claude carry on with your message. Change the wait with `"waitMinutes"` in `~/.claude-pocket/config.json`.

**Screenshots to Claude:** share to Claude Pocket, pick *To Claude*, add a note. The file is downloaded to the Mac and Claude is told the path at its next tool call or prompt — or immediately, if it's waiting for your reply. Claude opens it with its Read tool.

## Security

The token is the only thing protecting the relay — anyone with it can approve commands on your Mac. Keep it long and private, and rotate it (change `POCKET_TOKEN`, re-run `install`, re-open the pairing link) if it leaks. Files are deleted from the relay after 3 days.
