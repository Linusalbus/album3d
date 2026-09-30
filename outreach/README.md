# Outreach

Finds cafés, restaurants, bakeries and salons near home, pulls each
one's logo off its website, and renders a product shot of a 3D-printed QR sign (one standing in a black base, one
lying flat) with
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

For photoreal mockups, install **Blender** (free) from
[blender.org](https://www.blender.org/download/) and drag it into Applications.
The tool finds it automatically and renders with Cycles on the M4's GPU
(a few seconds per business). Without Blender it falls back to a simple
flat renderer.

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
| `python outreach.py followers` | Looks up Instagram follower counts and skips accounts over `max_instagram_followers` |
| `python outreach.py mockups` | Renders `data/mockups/<id>.jpg` in Blender for every business with a logo |
| `python outreach.py page` | Builds `data/index.html` |

`--limit 20` processes only the first 20 businesses (closest first), which is
handy for a first test run. `mockups` skips businesses that already have a
mockup; add `--force` to re-render all of them (e.g. after changing the design).
The very first Blender render on a Mac compiles GPU kernels, which takes a
few minutes once.

## Workflow on the review page

Open the page with `python outreach.py serve` (plain `open data/index.html`
works too, but then the browser only lets the page copy text, not images).

1. Look at the mockup. Skip it if the logo came out wrong.
2. Click **Copy message + image**, then **Open Instagram**.
3. Paste into the DM. Mail apps get text and image in one paste; if a chat
   only takes one of them, paste the text, then click **Copy image** and
   paste again.
4. Set the status to **Sent**. Update it to **Replied** or **Won** later.

Statuses are saved in the browser. **Export CSV** downloads them as a backup.

## Skipping big businesses

The target is small, independent places. `find` flags a place as a chain
(and leaves it out) when any of these hold:

- OpenStreetMap tags it with a brand
- another place nearby has the same name or the same website
- its website is a branch page of a bigger site (`/butikker/…`, `/locations/…`)
- its name matches `chain_names` in `config.json` (add any you spot)

Businesses whose Instagram has more than `max_instagram_followers` (default
1000) are dropped too. Follower counts are fetched without logging in; if
Instagram stops answering, the rest are kept and checked on the next run.

Places without a website are included (`require_website: false`), since many
small shops only have Instagram. Their logo is the Instagram profile picture
(fetched in the `followers` step) or, failing that, their name set as a
wordmark.

## When a logo is wrong or missing

Save the right logo as `data/logos/<id>.png` (the id is in `data/leads.json`),
then run `python outreach.py mockups && python outreach.py page`. Logos you
place by hand are never overwritten unless you pass `--force`.

SVG logos need `pip install cairosvg` (on Mac also `brew install cairo`).
Without it they are skipped and the tool falls back to a PNG icon.

## Customize

Everything is in `config.json`: your name, the message, the caption in the
top corner of the image (`image_caption`), the small disclaimer bottom left
(`image_disclaimer`), `show_dimensions`, radius, business
types and the placeholder shown where the customer's own text goes
(`sign_placeholder`). Placeholders in `message`:
`{name}` and `{my_name}`.

The QR code points to the business on Google Maps, where customers can
leave a review. When someone orders, ask them for their direct review link
(Google Business Profile → "Ask for reviews"), add it to their entry in
`data/leads.json` as `"review_url"`, and re-render.

`data/` holds scraped business info and logos and is git-ignored.

## Printing the sign

`models/` has the real parts as STL (Bambu Studio / Orca) and STEP (Fusion 360):

| Part | Size | Print |
|---|---|---|
| `card` | 100 × 140 × 3 mm | Flat, white PLA. ~35 g |
| `base` | 130 × 40 × 14 mm, 9 mm deep slot | Slot up, black PLA, 15 % infill, no supports. ~30 g |
| `slot_test` | 70 × 22 × 14 mm | Slots with 0.2 / 0.3 / 0.4 mm clearance (numbers engraved) |

Print `slot_test` and one `card` first. The right slot takes the card with a
light push and doesn't wobble. The base uses 0.3 mm; if another slot fits
better, set `SLOT_CLEARANCE` in `models/make_models.py` and run
`pip install cadquery && python models/make_models.py` to regenerate.

Print two cards side by side on the plate — the A1 fits both.
