# YouTube Clipper — Chrome extension

Adds a **Clip** button next to Like/Share on YouTube watch pages. It opens a
panel that reads the player's current position, so start and end can be set
with one click while you watch. Rendering happens on the local Flask server —
nothing is uploaded anywhere.

## 1. Start the server

The extension scans ports `8765, 5000, 8000, 5001, 3000, 8080` and remembers
whichever one answers. `8765` is checked first:

```bash
PORT=8765 python3 app.py
```

It has to stay running while you clip.

## 2. Install the extension

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked** and pick this `extension/` folder

Chrome shows a "Disable developer mode extensions" nag on each restart. To get
rid of it you have to publish to the Chrome Web Store (one-time $5 developer
fee); nothing in the code needs to change for that.

## 3. Use it

1. Open any YouTube video
2. Play to where the clip should start → **Now** next to *Start*
3. Play to the end point → **Now** next to *End*
4. Adjust position/timing, or open **Output & style** for resolution, aspect
   ratio, fit mode and the credit box styling
5. **Create clip** — about 10 s for a short 1080p clip
6. Preview it in the panel, then **Download clip**

Every setting except the timestamps is remembered between videos.

## How it talks to the server

`content.js` never calls the server directly. It messages `background.js`,
which does the fetching under the extension's host permissions — content
scripts are subject to the page's CORS rules and YouTube's would block it.

The server only sends `Access-Control-Allow-Origin` back to `chrome-extension://`
and `moz-extension://` origins, so an ordinary website cannot reach the render
endpoint even though it listens on localhost.

## If the button does not appear

- Check the server responds: `curl http://127.0.0.1:8765/clipper/ping`
- Reload the YouTube tab — the button is re-inserted on navigation, but a
  YouTube layout change can move the row it attaches to
- Non-standard port: the extension also honours a `customBase` value in
  `chrome.storage.local`, e.g. from the extension's service worker console:
  `chrome.storage.local.set({customBase: "http://127.0.0.1:9000"})`

The full-page version of the tool is still available at `/clipper` on the same
server.
