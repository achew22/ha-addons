import html, json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fb_reader"))
import fb_reader


def page(nodes, og=True):
    data = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"result": {"data": {"user": {
        "timeline_list_feed_units": {"edges": [{"node": n} for n in nodes]}}}}}}]]]}
    meta = '<meta property="og:title" content="Some Page" />' if og else ""
    return (f"<html><head>{meta}</head><body>"
            f'<script type="application/json" data-sjs>{html.escape(json.dumps(data), quote=False)}</script>'
            "</body></html>")


def node(pid, text, ts):
    return {"post_id": pid, "creation_time": ts,
            "comet_sections": {"content": {"story": {"wwwURL": f"https://www.facebook.com/x/posts/{pid}",
                                                     "message": {"text": text}}}},
            "attachments": [{"styles": {"attachment": {"media": {"photo_image": {"uri": "https://scontent/x.jpg"}}}}}]}


class ExtractTest(unittest.TestCase):
    def test_posts(self):
        now = int(time.time())
        posts = fb_reader.extract_posts(page([node("1", "old", now - 86400 * 3), node("2", "Monday\nA: 5x5", now)]), "x")
        posts.sort(key=lambda p: p["created"], reverse=True)
        p = fb_reader.enrich(posts[0])
        self.assertEqual(p["id"], "2")
        self.assertEqual(p["images"], ["https://scontent/x.jpg"])
        self.assertTrue(p["is_today"])
        self.assertFalse(fb_reader.enrich(posts[1])["is_today"])

    def test_login_wall(self):
        with self.assertRaises(fb_reader.Blocked):
            fb_reader.extract_posts(page([], og=False), "x")


class PushTest(unittest.TestCase):
    def setUp(self):
        self.stored = {}
        def fake(method, path, body=None):
            if method == "GET":
                if not self.stored:
                    raise OSError("404")
                return self.stored
            self.stored = body
            return {}
        fb_reader.ha_request = fake
        os.environ["SUPERVISOR_TOKEN"] = "x"
        self.cfg = {"page": "p", "sensor": "sensor.fb_p", "token": ""}

    def push(self, posts, status, error):
        fb_reader.push_to_ha(self.cfg, posts, status, error, fb_reader.stored_attrs(self.cfg))

    def test_skips_when_today_is_stored(self):
        self.push([self.post("2", "Hyrox", 60)], "ok", None)
        fetched = []
        fb_reader.fetch_page = lambda page: fetched.append(page)
        self.assertFalse(fb_reader.check_page(self.cfg))
        self.assertEqual(fetched, [])

    def test_fetches_when_stored_post_is_old(self):
        self.push([self.post("1", "old", 86400 * 2)], "ok", None)
        fetched = []
        def fake_fetch(page):
            fetched.append(page)
            raise fb_reader.Blocked("login wall")
        fb_reader.fetch_page = fake_fetch
        self.assertTrue(fb_reader.check_page(self.cfg))
        self.assertEqual(fetched, ["p"])
        self.assertEqual(self.stored["attributes"]["status"], "blocked")

    def test_skip_can_be_turned_off(self):
        self.push([self.post("2", "Hyrox", 60)], "ok", None)
        fetched = []
        def fake_fetch(page):
            fetched.append(page)
            raise fb_reader.Blocked("x")
        fb_reader.fetch_page = fake_fetch
        self.assertTrue(fb_reader.check_page(dict(self.cfg, skip_today=False)))
        self.assertEqual(fetched, ["p"])

    def post(self, pid, text, ago):
        return fb_reader.enrich({"id": pid, "text": text, "url": "u", "images": [],
                                 "created": int(time.time()) - ago, "source": "page"})

    def test_older_post_does_not_replace_newer(self):
        self.push([self.post("2", "Hyrox\nrun", 60)], "ok", None)
        self.push([self.post("1", "Monday WOD", 300)], "ok", None)
        self.assertEqual(self.stored["state"], "Hyrox")
        self.assertEqual([p["id"] for p in self.stored["attributes"]["recent"]], ["2", "1"])

    def test_failure_keeps_last_post(self):
        self.push([self.post("2", "Hyrox\nrun", 60)], "ok", None)
        self.push([], "blocked", "login wall")
        self.assertEqual(self.stored["state"], "Hyrox")
        self.assertEqual(self.stored["attributes"]["text"], "Hyrox\nrun")
        self.assertEqual(self.stored["attributes"]["status"], "blocked")

    def test_keeps_five(self):
        for i in range(8):
            self.push([self.post(str(i), f"p{i}", 1000 - i)], "ok", None)
        self.assertEqual([p["id"] for p in self.stored["attributes"]["recent"]], ["7", "6", "5", "4", "3"])


class OptionsTest(unittest.TestCase):
    def test_pages(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"pages": [
                {"page": "https://www.facebook.com/1884FunctionalFitness/"},
                {"page": "NASA", "token": " t ", "sensor": "space"},
                {"page": ""}]}, f)
        fb_reader.OPTIONS_FILE = f.name
        pages = fb_reader.load_options()
        os.unlink(f.name)
        self.assertEqual([p["page"] for p in pages], ["1884FunctionalFitness", "NASA"])
        self.assertEqual(pages[0]["sensor"], "sensor.fb_1884functionalfitness")
        self.assertEqual(pages[1]["sensor"], "sensor.fb_space")
        self.assertEqual(pages[1]["token"], "t")


unittest.main()
