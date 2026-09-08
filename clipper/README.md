# YouTube Clipper

Cut a section out of a YouTube video and burn in an animated channel credit.

    ./clip

That is the whole thing. It builds what it needs on the first run, starts on
http://localhost:5055 and opens the page.

## You need two things first

- **ffmpeg** — does the cutting and encoding.
  macOS: `brew install ffmpeg` · Windows: `winget install ffmpeg`
- **uv** — builds the Python environment so you do not have to.
  `curl -LsSf https://astral.sh/uv/install.sh | sh`

The script checks for both and tells you which one is missing.

## Why it runs on your machine

YouTube refuses downloads from datacentre addresses, so a hosted version does
not work — free or paid. Your own connection is not a datacentre, which is why
this is a thing you run rather than a page you open. Nothing is uploaded and
there is no account.

Made by Linus · https://linusworkshop.com
