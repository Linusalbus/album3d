# Outreach

Finds cafés, restaurants, bakeries and salons near home, pulls each
one's logo off its website, and renders a 3D-printed QR table-sign mockup with
that logo on it. A review page then gives you one card per business: the
mockup, a ready-made message, and buttons to copy it and open their Instagram.

Sending is manual on purpose (about 20 seconds per business). Automated bulk
messages break Danish marketing law (markedsføringsloven §10, which covers
businesses too) and get Instagram accounts banned. Keep every message
personal and don't send more than a handful a day.

## Setup (Mac)

```bash
cd outreach
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python outreach.py all           # find -> logos -> mockups -> page
open data/index.html
```

Or one step at a time:

| Command | What it does |
|---|---|
| `python outreach.py find` | Looks up businesses within `radius_m` of the addresses in `config.json` (OpenStreetMap) |
| `python outreach.py logos` | Visits each website and saves the logo, plus any Instagram handle and e-mail it links to |
| `python outreach.py mockups` | Renders `data/mockups/<id>.jpg` for every business with a logo |
| `python outreach.py page` | Builds `data/index.html` |

`--limit 20` processes only the first 20 businesses (closest first), which is
handy for a first test run.

## Workflow on the review page

1. Look at the mockup. Skip it if the logo came out wrong.
2. Click **Save image**, then **Open Instagram**.
3. Click **Copy message**, paste it into a DM, attach the image and send.
4. Set the status to **Sent**. Update it to **Replied** or **Won** later.

Statuses are saved in the browser. **Export CSV** downloads them as a backup.

## When a logo is wrong or missing

Save the right logo as `data/logos/<id>.png` (the id is in `data/leads.json`),
then run `python outreach.py mockups && python outreach.py page`. Logos you
place by hand are never overwritten unless you pass `--force`.

SVG logos need `pip install cairosvg` (on Mac also `brew install cairo`).
Without it they are skipped and the tool falls back to a PNG icon.

## Customize

Everything is in `config.json`: your name, radius, business types, price and
quantity, the text on the sign for each type, and the message template.
Placeholders: `{name}`, `{my_name}`, `{product}`, `{qty}`, `{price}` and
`{qr_use}`.

`data/` holds scraped business info and logos and is git-ignored.
