# fb-reader

Reads the newest post from each Facebook page in `pages` and publishes it as:

- A sensor per page, `sensor.fb_<page>` (lowercased, non-alphanumerics become `_`),
  or `sensor.fb_<sensor>` if you set `sensor`. State is the post's first line. Attributes:
  `text`, `url`, `published`, `is_today`, `age_hours`, `images`, `id`, `source`
  (`page` or `graph`), plus `status` (`ok`, `blocked`, `error`), `error` and `checked`.
- An Atom feed per page at `http://<app host>:8884/feed/<page>`, and JSON at `/status`.

## Options

- `poll_minutes`: how often to check every page (5 to 1440, default 30). Pages in one
  round are spaced 20 seconds apart.
- `pages`: list of pages. Each has `page` (the name in the facebook.com URL, or the full
  URL) and optionally `token` and `sensor`.

## How it reads a page

Without a token it loads the public page logged out, like a browser, and takes the
newest post from the JSON Facebook embeds in it (the same method as RSS-Bridge's
FacebookBridge). Facebook sends logged-out requests from datacenter IPs to its login
page, so this works only from a home connection, if at all. A login wall shows up as
`status: blocked`.

With `token` set it uses the Graph API instead. That needs a Page access token with
`pages_read_engagement` for a page the token's user has a role on. Reading a page you
have no role on needs Page Public Content Access, which takes Meta app review.

## Storage

Nothing is written to disk. The newest post per page is held in memory and pushed to
Home Assistant, whose recorder keeps the sensor history. The sensors are not registered
entities, so they disappear on a Home Assistant restart until the next check.
